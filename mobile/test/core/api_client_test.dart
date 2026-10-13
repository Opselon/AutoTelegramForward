import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/models.dart';
import 'package:autoforward_mobile/core/storage.dart';

void main() {
  group('Phone normalization', () {
    test('converts Persian digits and strips formatting', () {
      expect(normalizePhone('۰۹۱۲۳۴۵۶۷۸۹'), '+989123456789');
      expect(normalizePhone('  +۹۸ (۹۱۲) ۳۴۵-۶۷۸۹  '), '+989123456789');
    });

    test('converts Arabic-Indic digits', () {
      expect(normalizePhone('٠٩١٢٣٤٥٦٧٨٩'), '+989123456789');
    });

    test('prepends + for international formatting if leading 00', () {
      expect(normalizePhone('00989121234567'), '+989121234567');
    });
  });

  group('ApiClient requests and error handling', () {
    late MemoryTokenStore tokenStore;

    setUp(() {
      tokenStore = MemoryTokenStore();
    });

    test('attaches Authorization header when set', () async {
      Uri? requestedUri;
      Map<String, String>? capturedHeaders;

      final client = ApiClient(
        baseUrl: 'https://atf.test/',
        tokenStore: tokenStore,
        transport: (url, {headers, body, required method, required timeout}) async {
          requestedUri = url;
          capturedHeaders = headers;
          return http.Response(
            jsonEncode({'rules': [], 'total_sessions': 1, 'active_sessions': 1}),
            200,
          );
        },
      );

      client.setBearer('jwt-mock-bearer-token');
      final stats = await client.stats();

      expect(requestedUri?.path, '/api/stats');
      expect(capturedHeaders?['authorization'], 'Bearer jwt-mock-bearer-token');
      expect(stats.totalSessions, 1);
    });

    test('throws unauthorized AppError on 401 response', () async {
      final client = ApiClient(
        baseUrl: 'https://atf.test/',
        tokenStore: tokenStore,
        transport: (url, {Map<String, String>? headers, Object? body, required String method, required Duration timeout}) async {
          return http.Response(jsonEncode({'error': 'token_expired'}), 401);
        },
      );

      expect(
        () => client.listRules(),
        throwsA(isA<ApiException>().having(
          (e) => e.error.kind,
          'kind',
          ErrorKind.unauthorized,
        )),
      );
    });

    test('throws conflict AppError on 409 response', () async {
      final client = ApiClient(
        baseUrl: 'https://atf.test/',
        tokenStore: tokenStore,
        transport: (url, {Map<String, String>? headers, Object? body, required String method, required Duration timeout}) async {
          return http.Response(
            jsonEncode({'error': 'rule modified by another session'}),
            409,
          );
        },
      );

      expect(
        () => client.toggleRule('r-1'),
        throwsA(isA<ApiException>().having(
          (e) => e.error.kind,
          'kind',
          ErrorKind.conflict,
        )),
      );
    });

    test('retries on 503 Service Unavailable for idempotent GET', () async {
      int attempts = 0;
      final client = ApiClient(
        baseUrl: 'https://atf.test/',
        tokenStore: tokenStore,
        maxRetries: 2,
        transport: (url, {Map<String, String>? headers, Object? body, required String method, required Duration timeout}) async {
          attempts++;
          if (attempts < 2) {
            return http.Response('temporarily unavailable', 503);
          }
          return http.Response(jsonEncode({'rules': []}), 200);
        },
      );

      final rules = await client.listRules();
      expect(attempts, 2);
      expect(rules, isEmpty);
    });
  });
}
