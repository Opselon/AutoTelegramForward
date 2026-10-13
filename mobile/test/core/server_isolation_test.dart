import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/models.dart';
import 'package:autoforward_mobile/core/storage.dart';

const _minApi = '1.0.0';

AppEnv _testEnv() => const AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: _minApi,
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

HttpTransport _versionTransport(
  Map<String, http.Response> byOrigin, {
  List<Uri>? seen,
  Map<String, String?>? authSeen,
}) {
  return (
    Uri url, {
    required String method,
    Map<String, String>? headers,
    Object? body,
    required Duration timeout,
  }) async {
    seen?.add(url);
    authSeen?[url.origin] = headers?['authorization'];
    final response = byOrigin[url.origin];
    if (response != null) return response;
    return http.Response('not found', 404);
  };
}

http.Response _versionBody({
  String productVersion = '1.5.0',
  String apiVersion = '1.5.0',
  String minSupportedClient = '1.0.0',
}) => http.Response(
  jsonEncode({
    'product_version': productVersion,
    'api_version': apiVersion,
    'min_supported_client': minSupportedClient,
    'commit_sha': 'abc123',
    'features': <String>[],
  }),
  200,
);

void main() {
  group('compareApiVersions', () {
    test('orders major/minor/patch numerically', () {
      expect(compareApiVersions('1.4.0', '1.4.0'), 0);
      expect(compareApiVersions('1.5.0', '1.4.0'), greaterThan(0));
      expect(compareApiVersions('1.3.9', '1.4.0'), lessThan(0));
      expect(compareApiVersions('2.0.0', '1.99.99'), greaterThan(0));
      expect(compareApiVersions('1.4.10', '1.4.9'), greaterThan(0));
    });

    test('accepts v prefix and applies SemVer prerelease ordering', () {
      expect(compareApiVersions('v1.4.0', '1.4.0'), 0);
      expect(compareApiVersions('1.4.0-rc.1', '1.4.0'), lessThan(0));
      expect(compareApiVersions('1.4.0-rc.10', '1.4.0-rc.2'), greaterThan(0));
      expect(compareApiVersions('1.4.0+build.2', '1.4.0+build.1'), 0);
    });

    test(
      'rejects malformed SemVer instead of coercing invalid segments to zero',
      () {
        expect(
          () => compareApiVersions('1.invalid.5', '1.0.0'),
          throwsFormatException,
        );
        expect(
          () => compareApiVersions('1.4', '1.4.0'),
          throwsFormatException,
        );
        expect(
          () => compareApiVersions('01.4.0', '1.4.0'),
          throwsFormatException,
        );
      },
    );
  });

  group('probeServer', () {
    test('reports ok/compatible for a current server', () async {
      final result = await ApiClient.probeServer(
        'https://atf.example.com/',
        minSupportedApiVersion: _minApi,
        transport: _versionTransport({
          'https://atf.example.com': _versionBody(),
        }),
      );
      expect(result.ok, isTrue);
      expect(result.reachable, isTrue);
      expect(result.compatible, isTrue);
      expect(result.apiVersion, '1.5.0');
      expect(result.productVersion, '1.5.0');
      expect(result.errorKind, isNull);
    });

    test(
      'enforces server min_supported_client against this app version',
      () async {
        final result = await ApiClient.probeServer(
          'https://new-server.example.com/',
          minSupportedApiVersion: _minApi,
          clientVersion: '1.5.0',
          transport: _versionTransport({
            'https://new-server.example.com': _versionBody(
              minSupportedClient: '2.0.0',
            ),
          }),
        );
        expect(result.ok, isFalse);
        expect(result.reachable, isTrue);
        expect(result.compatible, isFalse);
        expect(result.errorKind, ErrorKind.incompatibleServer);
      },
    );

    test('flags an outdated server as incompatible without throwing', () async {
      final result = await ApiClient.probeServer(
        'https://old.example.com/',
        minSupportedApiVersion: _minApi,
        transport: _versionTransport({
          'https://old.example.com': _versionBody(
            productVersion: '0.9.0',
            apiVersion: '0.9.0',
          ),
        }),
      );
      expect(result.ok, isFalse);
      expect(result.reachable, isTrue);
      expect(result.compatible, isFalse);
      expect(result.errorKind, ErrorKind.incompatibleServer);
    });

    test(
      'sends no Authorization header and reads no stored credentials',
      () async {
        final seen = <Uri>[];
        final authSeen = <String, String?>{};
        final store = MemoryTokenStore(
          StoredCredentials(
            serverUrl: 'https://atf.example.com/',
            token: 'super-secret-jwt',
            userId: 7,
            username: 'alice',
            isAdmin: false,
            expiresAtIso:
                DateTime.now().add(const Duration(days: 1)).toIso8601String(),
          ),
        );
        final result = await ApiClient.probeServer(
          'https://atf.example.com/',
          minSupportedApiVersion: _minApi,
          transport: _versionTransport(
            {'https://atf.example.com': _versionBody()},
            seen: seen,
            authSeen: authSeen,
          ),
        );
        expect(result.ok, isTrue);
        expect(seen, hasLength(1));
        expect(seen.single.path, '/api/version');
        expect(authSeen['https://atf.example.com'], isNull);
        // The stored token is untouched: the probe never reads the store.
        expect((await store.read()).token, 'super-secret-jwt');
      },
    );

    test('maps timeout to ErrorKind.timeout', () async {
      final result = await ApiClient.probeServer(
        'https://slow.example.com/',
        minSupportedApiVersion: _minApi,
        transport: (
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async {
          throw TimeoutException('timed out');
        },
        timeout: const Duration(milliseconds: 50),
      );
      expect(result.ok, isFalse);
      expect(result.reachable, isFalse);
      expect(result.errorKind, ErrorKind.timeout);
    });

    test('maps connection failure to ErrorKind.network', () async {
      final result = await ApiClient.probeServer(
        'https://down.example.com/',
        minSupportedApiVersion: _minApi,
        transport: (
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async {
          throw http.ClientException('Connection refused', url);
        },
      );
      expect(result.ok, isFalse);
      expect(result.reachable, isFalse);
      expect(result.errorKind, ErrorKind.network);
    });

    test('maps TLS failure to network with a tls message', () async {
      final result = await ApiClient.probeServer(
        'https://badcert.example.com/',
        minSupportedApiVersion: _minApi,
        transport: (
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async {
          throw http.ClientException(
            'TLS handshake failure: certificate expired',
            url,
          );
        },
      );
      expect(result.ok, isFalse);
      expect(result.reachable, isFalse);
      expect(result.errorKind, ErrorKind.network);
      expect(result.errorMessage, 'server_tls_error');
    });

    test('maps malformed JSON to a server error, never throws', () async {
      final result = await ApiClient.probeServer(
        'https://weird.example.com/',
        minSupportedApiVersion: _minApi,
        transport: _versionTransport({
          'https://weird.example.com': http.Response('<html>nope</html>', 200),
        }),
      );
      expect(result.ok, isFalse);
      expect(result.reachable, isTrue);
      expect(result.errorKind, ErrorKind.server);
    });

    test(
      'maps non-JSON body and missing api_version to server errors',
      () async {
        final listBody = await ApiClient.probeServer(
          'https://list.example.com/',
          minSupportedApiVersion: _minApi,
          transport: _versionTransport({
            'https://list.example.com': http.Response('[1,2,3]', 200),
          }),
        );
        expect(listBody.ok, isFalse);
        expect(listBody.errorKind, ErrorKind.server);

        final missing = await ApiClient.probeServer(
          'https://noversion.example.com/',
          minSupportedApiVersion: _minApi,
          transport: _versionTransport({
            'https://noversion.example.com': http.Response(
              jsonEncode({'product_version': '1.5.0'}),
              200,
            ),
          }),
        );
        expect(missing.ok, isFalse);
        expect(missing.errorKind, ErrorKind.server);
      },
    );
  });

  group('server isolation', () {
    test('switching server clears stored credentials and logs out', () async {
      final tokens = MemoryTokenStore();
      final prefs = MemoryPreferenceStore();
      final state = AppState(
        env: _testEnv(),
        tokenStore: tokens,
        preferenceStore: prefs,
      );
      await state.restore();

      await tokens.write(
        StoredCredentials(
          serverUrl: 'https://a.example.com/',
          token: 'token-for-a',
          userId: 1,
          username: 'alice',
          isAdmin: false,
          expiresAtIso:
              DateTime.now().add(const Duration(days: 1)).toIso8601String(),
        ),
      );
      await state.onLoginSuccess(
        AuthSession(
          token: 'token-for-a',
          userId: 1,
          username: 'alice',
          isAdmin: false,
          serverUrl: 'https://a.example.com/',
          expiresAt: DateTime.now().add(const Duration(days: 1)),
        ),
      );
      expect(state.isAuthenticated, isTrue);

      await state.setServerUrl('https://b.example.com/');

      expect(state.isAuthenticated, isFalse);
      expect(state.authToken, isNull);
      expect(state.serverUrl, 'https://b.example.com/');
      expect((await tokens.read()).token, isEmpty);
      expect(
        await prefs.getString(PrefKeys.lastServerUrl),
        'https://b.example.com/',
      );
    });

    test('logout clears credentials', () async {
      final tokens = MemoryTokenStore(
        StoredCredentials(
          serverUrl: 'https://a.example.com/',
          token: 'token-for-a',
          userId: 1,
          username: 'alice',
          isAdmin: false,
          expiresAtIso:
              DateTime.now().add(const Duration(days: 1)).toIso8601String(),
        ),
      );
      final state = AppState(
        env: _testEnv(),
        tokenStore: tokens,
        preferenceStore: MemoryPreferenceStore(),
      );
      await state.restore();
      expect(state.isAuthenticated, isTrue);

      await state.logout();

      expect(state.isAuthenticated, isFalse);
      expect((await tokens.read()).token, isEmpty);
    });

    test('persisted server URL is restored on restart', () async {
      final tokens = MemoryTokenStore();
      final prefs = MemoryPreferenceStore();
      final first = AppState(
        env: _testEnv(),
        tokenStore: tokens,
        preferenceStore: prefs,
      );
      await first.restore();
      await first.setServerUrl('https://selfhosted.example.com/');

      final restarted = AppState(
        env: _testEnv(),
        tokenStore: tokens,
        preferenceStore: prefs,
      );
      await restarted.restore();

      expect(restarted.serverUrl, 'https://selfhosted.example.com/');
      expect(restarted.isAuthenticated, isFalse);
    });

    test(
      'stored credentials for server A are never sent to server B',
      () async {
        const serverA = 'https://a.example.com/';
        const serverB = 'https://b.example.com/';
        final seen = <Uri>[];
        final authSeen = <String, String?>{};

        // Credentials minted by A sit in the store while we talk to B.
        final tokens = MemoryTokenStore(
          StoredCredentials(
            serverUrl: serverA,
            token: 'token-for-a',
            userId: 1,
            username: 'alice',
            isAdmin: false,
            expiresAtIso:
                DateTime.now().add(const Duration(days: 1)).toIso8601String(),
          ),
        );

        // Probe of B goes to B's origin only and carries no Authorization.
        await ApiClient.probeServer(
          serverB,
          minSupportedApiVersion: _minApi,
          transport: (
            Uri url, {
            required String method,
            Map<String, String>? headers,
            Object? body,
            required Duration timeout,
          }) async {
            seen.add(url);
            authSeen[url.origin] = headers?['authorization'];
            return _versionBody();
          },
        );
        expect(seen.single.origin, 'https://b.example.com');
        expect(authSeen['https://b.example.com'], isNull);

        // Login against B posts to B's origin only and overwrites the store
        // entry with B's server URL (A's token is never replayed to B).
        final loginSeen = <Uri>[];
        final clientB = ApiClient(
          baseUrl: serverB,
          tokenStore: tokens,
          transport: (
            Uri url, {
            required String method,
            Map<String, String>? headers,
            Object? body,
            required Duration timeout,
          }) async {
            loginSeen.add(url);
            return http.Response(
              jsonEncode({
                'token': 'token-for-b',
                'user_id': 2,
                'username': 'bob',
                'is_admin': false,
              }),
              200,
            );
          },
        );
        await clientB.login(username: 'bob', password: 'secret');
        expect(loginSeen.single.origin, 'https://b.example.com');
        expect((await tokens.read()).serverUrl, serverB);
      },
    );

    test(
      'expired token handling clears the store and expires the session',
      () async {
        final tokens = MemoryTokenStore(
          StoredCredentials(
            serverUrl: 'https://a.example.com/',
            token: 'stale-token',
            userId: 1,
            username: 'alice',
            isAdmin: false,
            expiresAtIso:
                DateTime.now().add(const Duration(days: 1)).toIso8601String(),
          ),
        );
        final state = AppState(
          env: _testEnv(),
          tokenStore: tokens,
          preferenceStore: MemoryPreferenceStore(),
        );
        await state.restore();
        expect(state.isAuthenticated, isTrue);

        final client = ApiClient(
          baseUrl: state.serverUrl,
          tokenStore: tokens,
          transport: (
            Uri url, {
            required String method,
            Map<String, String>? headers,
            Object? body,
            required Duration timeout,
          }) async {
            expect(headers?['authorization'], 'Bearer stale-token');
            return http.Response(jsonEncode({'error': 'token_expired'}), 401);
          },
        );
        client.setBearer('stale-token');

        await expectLater(
          client.listRules(),
          throwsA(
            isA<ApiException>().having(
              (e) => e.error.kind,
              'kind',
              ErrorKind.unauthorized,
            ),
          ),
        );
        // 401 evicts the bearer and clears the persisted credentials.
        expect(client.hasBearer, isFalse);
        expect((await tokens.read()).token, isEmpty);

        state.markExpired();
        expect(state.isAuthenticated, isFalse);
        expect(state.status, SessionStatus.expired);
      },
    );
  });
}
