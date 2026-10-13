/// Typed HTTP client for the AutoTelegramForward Control API.
///
/// Contract source of truth: `api/main.go` route registrations and
/// `api/internal/handlers/*.go` response bodies. This client adds no
/// behaviour of its own — it is a thin serialisation + error-translation
/// layer so that feature code stays in Dart and stays testable.
///
/// Security invariants the client is built around:
///   * The Bearer [REDACTED] is the caller's own JWT. `owner_user_id` is never sent
///     in a request body; the server derives identity from the token.
///   * A 401/403 is surfaced as [ErrorKind.unauthorized]/[forbidden] and
///     discards the stored token, so a stale credential cannot be reused.
///   * Only idempotent reads are retried, and only with bounded backoff.
///     Mutations are single-shot so a duplicate submission is never created.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'env.dart';
import 'models.dart';
import 'storage.dart';

/// Thrown by [ApiClient] when a call fails. Always carries a stable [kind].
class ApiException implements Exception {
  const ApiException(this.error);

  final AppError error;

  @override
  String toString() => 'ApiException: $error';
}

/// Pluggable transport so unit tests never touch the network.
typedef HttpTransport =
    Future<http.Response> Function(
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    });

Future<http.Response> _ioTransport(
  Uri url, {
  required String method,
  Map<String, String>? headers,
  Object? body,
  required Duration timeout,
}) async {
  throw UnsupportedError('io transport must be injected');
}

/// Real transport backed by an [http.Client]. Used by production code and
/// by integration tests that point at a live server.
HttpTransport ioTransport(http.Client client) {
  return (
    Uri url, {
    required String method,
    Map<String, String>? headers,
    Object? body,
    required Duration timeout,
  }) async {
    final request = http.Request(method, url);
    if (headers != null) {
      request.headers.addAll(headers);
    }
    if (body != null) {
      request.body = body is String ? body : jsonEncode(body);
      request.headers['content-type'] = 'application/json; charset=utf-8';
    }
    final streamed = await client.send(request).timeout(timeout);
    return http.Response.fromStream(streamed);
  };
}

/// Helper for tests and production: builds the default [http.Client]-backed
/// transport.
HttpTransport defaultTransport(http.Client client) => ioTransport(client);

/// Compares two dotted version strings (`major.minor.patch`, each segment
/// Compares strict SemVer strings. A leading v is accepted for tag-style
/// versions; build metadata is ignored and prerelease identifiers follow
/// SemVer precedence. Invalid strings throw FormatException so compatibility
/// checks fail closed instead of coercing malformed components to zero.
int compareApiVersions(String a, String b) {
  final pattern = RegExp(
    r'^[vV]?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$',
  );
  ({List<int> core, List<String> pre}) parse(String raw) {
    final match = pattern.firstMatch(raw.trim());
    if (match == null) throw FormatException('Invalid semantic version', raw);
    final core = <int>[
      int.parse(match.group(1)!),
      int.parse(match.group(2)!),
      int.parse(match.group(3)!),
    ];
    final pre = match.group(4)?.split('.') ?? <String>[];
    if (pre.any((part) => RegExp(r'^0[0-9]+$').hasMatch(part))) {
      throw FormatException('Numeric prerelease identifiers cannot have leading zeros', raw);
    }
    return (core: core, pre: pre);
  }
  final left = parse(a);
  final right = parse(b);
  for (var i = 0; i < 3; i++) {
    final compared = left.core[i].compareTo(right.core[i]);
    if (compared != 0) return compared;
  }
  if (left.pre.isEmpty && right.pre.isEmpty) return 0;
  if (left.pre.isEmpty) return 1;
  if (right.pre.isEmpty) return -1;
  final width = left.pre.length < right.pre.length ? left.pre.length : right.pre.length;
  for (var i = 0; i < width; i++) {
    final l = left.pre[i], r = right.pre[i];
    if (l == r) continue;
    final ln = int.tryParse(l), rn = int.tryParse(r);
    if (ln != null && rn != null) return ln.compareTo(rn);
    if (ln != null) return -1;
    if (rn != null) return 1;
    return l.compareTo(r);
  }
  return left.pre.length.compareTo(right.pre.length);
}

/// Outcome of [ApiClient.probeServer]: whether the server answered its
/// public `/api/version` endpoint, what it reported, and whether that
/// release is new enough for this client.
@immutable
class ProbeResult {
  const ProbeResult({
    required this.ok,
    this.productVersion = '',
    this.apiVersion = '',
    required this.reachable,
    required this.compatible,
    this.errorKind,
    this.errorMessage = '',
  });

  /// The server answered `/api/version` with a usable `api_version`.
  final bool ok;

  /// `product_version` reported by the server (empty when unknown).
  final String productVersion;

  /// `api_version` reported by the server (empty when unknown).
  final String apiVersion;

  /// True when an HTTP response arrived at all (even an error status).
  /// False means the host was unreachable, timed out, or TLS failed.
  final bool reachable;

  /// True when [ok] and `api_version >= minSupportedApiVersion`.
  final bool compatible;

  /// Actionable [ErrorKind] when [!ok]; null on success.
  final String? errorKind;

  /// Machine-readable detail (e.g. `request_timeout`, `server_tls_error`).
  final String errorMessage;

  /// Builds the unreachable case (DNS failure, timeout, TLS, bad URL…).
  factory ProbeResult.unreachable(String kind, String message) => ProbeResult(
    ok: false,
    reachable: false,
    compatible: false,
    errorKind: kind,
    errorMessage: message,
  );

  @override
  String toString() =>
      'ProbeResult(ok=$ok, reachable=$reachable, compatible=$compatible, '
      'api=$apiVersion, product=$productVersion, error=$errorKind)';
}

class ApiClient {
  ApiClient({
    required this.baseUrl,
    required SecureTokenStore tokenStore,
    this.transport = _ioTransport,
    this.timeout = const Duration(seconds: 15),
    this.maxRetries = 2,
  }) : _tokens = tokenStore;

  /// Canonical server base URL, always ending in '/'.
  final String baseUrl;
  final SecureTokenStore _tokens;
  final HttpTransport transport;
  final Duration timeout;
  final int maxRetries;

  String? _bearer;

  /// Attaches an already-known token (session restore).
  void setBearer(String token) => _bearer = token;

  bool get hasBearer => _bearer != null && _bearer!.isNotEmpty;

  // ------------------------------------------------------------------ //
  // Unauthenticated server probe (`GET /api/version`, public endpoint)
  // ------------------------------------------------------------------ //

  /// Probes a server's public `/api/version` endpoint without credentials.
  ///
  /// Fully unauthenticated: no `Authorization` header is attached and the
  /// stored credentials are never read, written, or cleared. Compatibility
  /// requires the server API to satisfy [minSupportedApiVersion] and the
  /// running client to satisfy the server's min_supported_client policy.
  ///
  /// Never throws: network, timeout, TLS, malformed-JSON, and unexpected
  /// HTTP statuses map to a [ProbeResult] carrying an actionable
  /// [ErrorKind].
  static Future<ProbeResult> probeServer(
    String baseUrl, {
    required String minSupportedApiVersion,
    String clientVersion = '1.0.0',
    HttpTransport? transport,
    Duration timeout = const Duration(seconds: 8),
  }) async {
    final parsed = ServerUrl.tryParse(baseUrl);
    if (parsed == null) {
      return ProbeResult.unreachable(
        ErrorKind.validation,
        'server_url_invalid',
      );
    }
    final uri = Uri.parse('${parsed.normalized}api/version');
    HttpTransport effective = transport ?? _ioTransport;
    http.Client? owned;
    if (transport == null) {
      owned = http.Client();
      effective = ioTransport(owned);
    }
    try {
      // Belt and braces: the transport applies [timeout] itself, and the
      // outer guard covers transports that ignore it.
      final response = await effective(
        uri,
        method: 'GET',
        headers: const {'accept': 'application/json'},
        timeout: timeout,
      ).timeout(timeout);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        final int status = response.statusCode;
        final String kind =
            status == 404
                ? ErrorKind.notFound
                : status == 429
                ? ErrorKind.rateLimited
                : status >= 500
                ? ErrorKind.server
                : ErrorKind.unknown;
        return ProbeResult(
          ok: false,
          reachable: true,
          compatible: false,
          errorKind: kind,
          errorMessage: 'probe_http_$status',
        );
      }
      final Map<String, dynamic> json;
      try {
        final decoded = jsonDecode(response.body);
        if (decoded is! Map<String, dynamic>) {
          return const ProbeResult(
            ok: false,
            reachable: true,
            compatible: false,
            errorKind: ErrorKind.server,
            errorMessage: 'probe_invalid_json',
          );
        }
        json = decoded;
      } on FormatException {
        return const ProbeResult(
          ok: false,
          reachable: true,
          compatible: false,
          errorKind: ErrorKind.server,
          errorMessage: 'probe_invalid_json',
        );
      }
      final apiVersion =
          (json['api_version'] ?? json['apiVersion'] ?? '').toString().trim();
      final productVersion =
          (json['product_version'] ?? json['productVersion'] ?? '')
              .toString()
              .trim();
      final minSupportedClient =
          (json['min_supported_client'] ?? json['minSupportedClient'] ?? '').toString().trim();
      if (apiVersion.isEmpty || minSupportedClient.isEmpty) {
        return ProbeResult(
          ok: false,
          productVersion: productVersion,
          apiVersion: apiVersion,
          reachable: true,
          compatible: false,
          errorKind: ErrorKind.server,
          errorMessage: apiVersion.isEmpty ? 'probe_missing_api_version' : 'probe_missing_min_supported_client',
        );
      }
      final compatible =
          compareApiVersions(apiVersion, minSupportedApiVersion) >= 0 &&
          compareApiVersions(clientVersion, minSupportedClient) >= 0;
      if (!compatible) {
        return ProbeResult(
          ok: false,
          productVersion: productVersion,
          apiVersion: apiVersion,
          reachable: true,
          compatible: false,
          errorKind: ErrorKind.incompatibleServer,
          errorMessage: 'probe_incompatible_server',
        );
      }
      return ProbeResult(
        ok: true,
        productVersion: productVersion,
        apiVersion: apiVersion,
        reachable: true,
        compatible: true,
      );
    } on TimeoutException {
      return ProbeResult.unreachable(ErrorKind.timeout, 'request_timeout');
    } on http.ClientException catch (exc) {
      return ProbeResult.unreachable(
        ErrorKind.network,
        _probeNetworkMessage(exc.message),
      );
    } on FormatException {
      return const ProbeResult(
        ok: false,
        reachable: true,
        compatible: false,
        errorKind: ErrorKind.server,
        errorMessage: 'probe_invalid_json',
      );
    } catch (exc) {
      // SocketException, TlsException, ArgumentError… never throw raw.
      return ProbeResult.unreachable(
        ErrorKind.network,
        _probeNetworkMessage('$exc'),
      );
    } finally {
      owned?.close();
    }
  }

  /// Maps a transport failure detail to an actionable message code,
  /// distinguishing TLS/certificate failures from plain unreachable hosts.
  static String _probeNetworkMessage(String detail) {
    final lower = detail.toLowerCase();
    if (lower.contains('tls') ||
        lower.contains('ssl') ||
        lower.contains('certificate') ||
        lower.contains('handshake')) {
      return 'server_tls_error';
    }
    return detail.isNotEmpty ? detail : 'probe_unreachable';
  }

  // ------------------------------------------------------------------ //
  // Core plumbing
  // ------------------------------------------------------------------ //

  Uri _uri(String path, [Map<String, dynamic>? query]) {
    final clean = path.startsWith('/') ? path.substring(1) : path;
    final base =
        baseUrl.endsWith('/')
            ? baseUrl.substring(0, baseUrl.length - 1)
            : baseUrl;
    return Uri.parse(
      '$base/$clean',
    ).replace(queryParameters: query?.map((k, v) => MapEntry(k, '$v')));
  }

  Map<String, String> get _authHeaders {
    final headers = <String, String>{'accept': 'application/json'};
    final token = _bearer;
    if (token != null && token.isNotEmpty) {
      headers['authorization'] = 'Bearer $token';
    }
    return headers;
  }

  AppError _translate(http.Response response, {required bool retryable}) {
    final status = response.statusCode;
    String serverMessage = '';
    try {
      final decoded = jsonDecode(response.body);
      if (decoded is Map<String, dynamic>) {
        final candidate =
            decoded['error'] ?? decoded['message'] ?? decoded['detail'];
        if (candidate is String && candidate.isNotEmpty) {
          serverMessage = candidate;
        }
      }
    } on FormatException {
      serverMessage = '';
    }

    String kind;
    switch (status) {
      case 401:
        kind = ErrorKind.unauthorized;
        break;
      case 403:
        kind = ErrorKind.forbidden;
        break;
      case 404:
        kind = ErrorKind.notFound;
        break;
      case 409:
        kind = ErrorKind.conflict;
        break;
      case 429:
        kind = ErrorKind.rateLimited;
        break;
      case 400:
        kind = ErrorKind.validation;
        break;
      default:
        kind = status >= 500 ? ErrorKind.server : ErrorKind.unknown;
    }

    return AppError(
      kind: kind,
      message: serverMessage.isNotEmpty ? serverMessage : 'HTTP $status',
      statusCode: status,
      retryable: retryable && (status >= 500 || status == 429),
    );
  }

  Future<http.Response> _sendWithRetry({
    required String method,
    required String path,
    Map<String, dynamic>? query,
    Object? body,
    required bool retryable,
  }) async {
    var attempt = 0;
    while (true) {
      attempt++;
      try {
        final response = await transport(
          _uri(path, query),
          method: method,
          headers: _authHeaders,
          body: body,
          timeout: timeout,
        );
        if (response.statusCode >= 200 && response.statusCode < 300) {
          return response;
        }
        final error = _translate(response, retryable: retryable);
        if (error.retryable && attempt <= maxRetries) {
          await Future<void>.delayed(
            Duration(milliseconds: 50 * (1 << (attempt - 1))),
          );
          continue;
        }
        if (error.kind == ErrorKind.unauthorized) {
          _bearer = null;
          await _tokens.clear();
        }
        throw ApiException(error);
      } on ApiException {
        rethrow;
      } on TimeoutException {
        if (attempt > maxRetries || !retryable) {
          throw const ApiException(
            AppError(
              kind: ErrorKind.timeout,
              message: 'request_timeout',
              retryable: true,
            ),
          );
        }
        await Future<void>.delayed(
          Duration(milliseconds: 50 * (1 << (attempt - 1))),
        );
      } on http.ClientException catch (exc) {
        if (attempt > maxRetries || !retryable) {
          throw ApiException(
            AppError(
              kind: ErrorKind.network,
              message: exc.message,
              retryable: true,
            ),
          );
        }
        await Future<void>.delayed(
          Duration(milliseconds: 50 * (1 << (attempt - 1))),
        );
      }
    }
  }

  Future<T> _request<T>({
    required String method,
    required String path,
    Map<String, dynamic>? query,
    Object? body,
    required T Function(Map<String, dynamic> json) fromJson,
    required bool retryable,
  }) async {
    final response = await _sendWithRetry(
      method: method,
      path: path,
      query: query,
      body: body,
      retryable: retryable,
    );
    final decoded =
        response.body.isEmpty
            ? <String, dynamic>{}
            : jsonDecode(response.body) as Map<String, dynamic>;
    return fromJson(decoded);
  }

  Future<List<T>> _requestList<T>({
    required String method,
    required String path,
    Map<String, dynamic>? query,
    required T Function(Map<String, dynamic> json) fromJson,
    required String listKey,
  }) async {
    final response = await _sendWithRetry(
      method: method,
      path: path,
      query: query,
      retryable: true,
    );
    final decoded = jsonDecode(response.body);
    if (decoded is List) {
      return decoded
          .map((e) => fromJson(e as Map<String, dynamic>))
          .toList(growable: false);
    }
    if (decoded is Map<String, dynamic>) {
      final raw = decoded[listKey];
      if (raw is List) {
        return raw
            .map((e) => fromJson(e as Map<String, dynamic>))
            .toList(growable: false);
      }
    }
    return const [];
  }

  // ------------------------------------------------------------------ //
  // Public auth surface (`api/main.go`: /api/auth/*, unauthenticated)
  // ------------------------------------------------------------------ //

  Future<AuthSession> login({
    required String username,
    required String password,
  }) async {
    final response = await transport(
      _uri('/api/auth/login'),
      method: 'POST',
      headers: {'content-type': 'application/json; charset=utf-8'},
      body: jsonEncode({'username': username, 'password': password}),
      timeout: timeout,
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw ApiException(_translate(response, retryable: false));
    }
    final json = jsonDecode(response.body) as Map<String, dynamic>;
    final jwt = json['token'] as String? ?? '';
    if (jwt.isEmpty) {
      throw const ApiException(
        AppError(kind: ErrorKind.unauthorized, message: 'login_no_token'),
      );
    }
    final session = AuthSession(
      token: jwt,
      userId: (json['user_id'] as num?)?.toInt() ?? 0,
      username: json['username'] as String? ?? username,
      isAdmin: json['is_admin'] as bool? ?? false,
      serverUrl: baseUrl,
      expiresAt: DateTime.now().add(const Duration(days: 7)),
    );
    _bearer = jwt;
    await _tokens.write(
      StoredCredentials(
        serverUrl: baseUrl,
        token: jwt,
        userId: session.userId,
        username: session.username,
        isAdmin: session.isAdmin,
        expiresAtIso: session.expiresAt.toIso8601String(),
      ),
    );
    return session;
  }

  Future<bool> register({
    required String username,
    required String password,
  }) async {
    final response = await transport(
      _uri('/api/auth/register'),
      method: 'POST',
      headers: {'content-type': 'application/json; charset=utf-8'},
      body: jsonEncode({'username': username, 'password': password}),
      timeout: timeout,
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw ApiException(_translate(response, retryable: false));
    }
    return true;
  }

  Future<Map<String, dynamic>> me() async {
    return _request(
      method: 'GET',
      path: '/api/auth/me',
      fromJson: (json) => json,
      retryable: true,
    );
  }

  Future<void> changePassword({
    required int userId,
    required String oldPassword,
    required String newPassword,
  }) async {
    await _request(
      method: 'PATCH',
      path: '/api/auth/change-password',
      body: {
        'user_id': userId,
        'old_password': oldPassword,
        'new_password': newPassword,
      },
      fromJson: (json) => json,
      retryable: false,
    );
  }

  // ------------------------------------------------------------------ //
  // Authenticated dashboard surface (`/api/*` in api/main.go)
  // ------------------------------------------------------------------ //

  Future<SystemStats> stats() => _request(
    method: 'GET',
    path: '/api/stats',
    fromJson: SystemStats.fromJson,
    retryable: true,
  );

  Future<List<ServiceHealth>> servicesHealth() => _requestList(
    method: 'GET',
    path: '/api/v1/health/services',
    fromJson: ServiceHealth.fromJson,
    listKey: 'services',
  );

  Future<List<ForwardRule>> listRules() => _requestList(
    method: 'GET',
    path: '/api/rules',
    fromJson: ForwardRule.fromJson,
    listKey: 'rules',
  );

  Future<ForwardRule> createRule(ForwardRule rule) => _request(
    method: 'POST',
    path: '/api/rules',
    body: rule.toCreateJson(),
    fromJson: ForwardRule.fromJson,
    retryable: false,
  );

  Future<ForwardRule> updateRule(String id, ForwardRule rule) => _request(
    method: 'PUT',
    path: '/api/rules/$id',
    body: rule.toUpdateJson(),
    fromJson: ForwardRule.fromJson,
    retryable: false,
  );

  Future<void> deleteRule(String id) => _request(
    method: 'DELETE',
    path: '/api/rules/$id',
    fromJson: (json) => json,
    retryable: false,
  );

  Future<void> toggleRule(String id) => _request(
    method: 'POST',
    path: '/api/rules/$id/toggle',
    fromJson: (json) => json,
    retryable: false,
  );

  Future<void> pauseRule(String id) => _request(
    method: 'POST',
    path: '/api/rules/$id/pause',
    fromJson: (json) => json,
    retryable: false,
  );

  Future<void> resumeRule(String id) => _request(
    method: 'POST',
    path: '/api/rules/$id/resume',
    fromJson: (json) => json,
    retryable: false,
  );

  Future<List<TelegramSession>> listSessions() => _requestList(
    method: 'GET',
    path: '/api/sessions',
    fromJson: TelegramSession.fromJson,
    listKey: 'sessions',
  );

  Future<void> terminateSession(String id) => _request(
    method: 'DELETE',
    path: '/api/sessions/$id',
    fromJson: (json) => json,
    retryable: false,
  );

  Future<List<FilterRule>> listFilters() => _requestList(
    method: 'GET',
    path: '/api/filters',
    fromJson: FilterRule.fromJson,
    listKey: 'filters',
  );

  Future<FilterRule> createFilter(FilterRule filter) => _request(
    method: 'POST',
    path: '/api/filters',
    body: filter.toJson(),
    fromJson: FilterRule.fromJson,
    retryable: false,
  );

  Future<FilterRule> updateFilter(String id, FilterRule filter) => _request(
    method: 'PUT',
    path: '/api/filters/$id',
    body: filter.toJson(),
    fromJson: FilterRule.fromJson,
    retryable: false,
  );

  Future<void> deleteFilter(String id) => _request(
    method: 'DELETE',
    path: '/api/filters/$id',
    fromJson: (json) => json,
    retryable: false,
  );

  Future<List<AiConfig>> listAiConfigs() => _requestList(
    method: 'GET',
    path: '/api/ai-configs',
    fromJson: AiConfig.fromJson,
    listKey: 'configs',
  );

  Future<AiConfig> createAiConfig(AiConfig config) => _request(
    method: 'POST',
    path: '/api/ai-configs',
    body: config.toJson(),
    fromJson: AiConfig.fromJson,
    retryable: false,
  );

  Future<AiConfig> updateAiConfig(String id, AiConfig config) => _request(
    method: 'PUT',
    path: '/api/ai-configs/$id',
    body: config.toJson(),
    fromJson: AiConfig.fromJson,
    retryable: false,
  );

  Future<void> deleteAiConfig(String id) => _request(
    method: 'DELETE',
    path: '/api/ai-configs/$id',
    fromJson: (json) => json,
    retryable: false,
  );

  Future<List<LogEntry>> queryLogs({int limit = 40, String level = ''}) =>
      _requestList(
        method: 'GET',
        path: '/api/logs',
        query: {'limit': limit, if (level.isNotEmpty) 'level': level},
        fromJson: LogEntry.fromJson,
        listKey: 'logs',
      );

  // ------------------------------------------------------------------ //
  // Telegram login lifecycle (`/api/telegram-login/*`, new in this PR)
  // ------------------------------------------------------------------ //

  /// Starts an interactive MTProto login. The challenge id and the next step
  /// are server-generated; the client only ever echoes them back.
  Future<LoginChallenge> startTelegramLogin({
    required int userId,
    required String phoneNumber,
  }) => _request(
    method: 'POST',
    path: '/api/telegram-login/start',
    body: {'phone_number': phoneNumber},
    fromJson: LoginChallenge.fromJson,
    retryable: false,
  );

  Future<LoginChallenge> submitTelegramCode({
    required String challengeId,
    required String code,
  }) => _request(
    method: 'POST',
    path: '/api/telegram-login/$challengeId/code',
    body: {'code': code},
    fromJson: LoginChallenge.fromJson,
    retryable: false,
  );

  Future<LoginChallenge> submitTelegramPassword({
    required String challengeId,
    required String password,
  }) => _request(
    method: 'POST',
    path: '/api/telegram-login/$challengeId/password',
    body: {'password': password},
    fromJson: LoginChallenge.fromJson,
    retryable: false,
  );

  Future<void> cancelTelegramLogin(String challengeId) => _request(
    method: 'POST',
    path: '/api/telegram-login/$challengeId/cancel',
    fromJson: (json) => json,
    retryable: false,
  );

  Future<void> logout() async {
    _bearer = null;
    await _tokens.clear();
  }
}

/// Server-side login challenge. The id is opaque to the client and bound to
/// the authenticated principal server-side — it cannot be transferred.
@immutable
class LoginChallenge {
  const LoginChallenge({
    required this.id,
    required this.state,
    required this.message,
    this.sessionId = '',
    this.retryAfterSeconds = 0,
    this.attemptsRemaining = 0,
    this.phoneNumber = '',
  });

  final String id;

  /// One of [LoginChallengeState].
  final String state;
  final String message;
  final String sessionId;
  final int retryAfterSeconds;
  final int attemptsRemaining;
  final String phoneNumber;

  factory LoginChallenge.fromJson(Map<String, dynamic> json) => LoginChallenge(
    id: json['challenge_id'] as String? ?? json['id'] as String? ?? '',
    state: json['state'] as String? ?? LoginChallengeState.pending,
    message: json['message'] as String? ?? '',
    sessionId: json['session_id'] as String? ?? '',
    retryAfterSeconds:
        (json['retry_after_seconds'] as num?)?.toInt() ??
        (json['retry_after'] as num?)?.toInt() ??
        0,
    attemptsRemaining: (json['attempts_remaining'] as num?)?.toInt() ?? 0,
    phoneNumber: json['phone_number'] as String? ?? '',
  );
}

/// Mirrors the states the Python `LoginFlowManager` returns, surfaced through
/// the new REST contract.
class LoginChallengeState {
  const LoginChallengeState._();

  /// Server accepted the phone and sent a code.
  static const String awaitingCode = 'awaiting_code';

  /// Telegram requires two-step verification.
  static const String awaitingPassword = 'awaiting_password';

  /// Session linked and activated.
  static const String completed = 'completed';

  /// Terminal failure; see [LoginChallenge.message].
  static const String failed = 'failed';

  /// Challenge expired; the user must start a new one.
  static const String expired = 'expired';

  /// Rate limited; retry after [LoginChallenge.retryAfterSeconds].
  static const String rateLimited = 'rate_limited';

  /// In-flight before the first response.
  static const String pending = 'pending';

  /// Cancelled by the user or superseded by a newer challenge.
  static const String cancelled = 'cancelled';

  static bool isTerminal(String state) =>
      state == completed ||
      state == failed ||
      state == expired ||
      state == cancelled;
}

/// Parses the message keys the Python flow returns into a localisable code.
String loginMessageKey(String message) {
  if (message.startsWith('flood_wait:')) return 'login_flood_wait';
  if (message.startsWith('cooldown:')) return 'login_cooldown';
  switch (message) {
    case 'invalid_phone':
      return 'login_invalid_phone';
    case 'invalid_code':
      return 'login_invalid_code';
    case 'code_expired':
      return 'login_code_expired';
    case 'invalid_password':
      return 'login_invalid_password';
    case 'too_many_attempts':
      return 'login_too_many_attempts';
    case 'auth_expired':
      return 'login_auth_expired';
    case 'no_pending_login':
      return 'login_no_pending';
    case 'send_code':
      return 'login_code_sent';
    case 'send_password':
      return 'login_password_needed';
    default:
      return 'login_error';
  }
}

/// Persian/Arabic digits → ASCII, then +E.164-ish normalisation. Mirrors
/// `LoginFlowManager.normalize_phone` in the Python core so the app and the
/// bot accept identical input.
String normalizePhone(String raw) {
  final ascii = raw.replaceAllMapped(RegExp('[۰-۹٠-٩]'), (m) {
    final code = m.group(0)!.codeUnitAt(0);
    // Persian (U+06F0..) and Arabic-Indic (U+0660..) both map to 0..9.
    return String.fromCharCode(
      code <= 0x0669 ? code - 0x0660 + 0x30 : code - 0x06F0 + 0x30,
    );
  });
  var digits = ascii.replaceAll(RegExp(r'[^\d+]'), '');
  if (digits.startsWith('00')) digits = '+${digits.substring(2)}';
  if (digits.startsWith('09') && digits.length >= 10) {
    digits = '+98${digits.substring(1)}';
  } else if (digits.startsWith('98')) {
    digits = '+$digits';
  } else if (digits.isNotEmpty && !digits.startsWith('+')) {
    digits = '+$digits';
  }
  return digits;
}

bool isValidPhone(String raw) =>
    RegExp(r'^\+\d{7,16}$').hasMatch(normalizePhone(raw));

bool isValidVerificationCode(String raw) =>
    RegExp(r'^\d{4,8}$').hasMatch(raw.replaceAll(RegExp(r'\D'), ''));
