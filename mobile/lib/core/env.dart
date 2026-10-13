/// Build-time + runtime environment configuration.
///
/// The default API base URL is shipped as an asset (`assets/cfg/env.json`) so a
/// fork can override it without touching Dart code, and every user can still
/// point the app at their own self-hosted server from Settings → Server.
/// No server-wide API keys, bot tokens or master secrets live here or anywhere
/// else in the client: every request is authenticated with the caller's own
/// JWT, obtained from `/api/auth/login`.
library;

import 'dart:convert';

import 'package:flutter/services.dart' show rootBundle;

/// Environment descriptor loaded from `assets/cfg/env.json`.
class AppEnv {
  const AppEnv({
    required this.apiBaseUrl,
    required this.apiBaseUrlDev,
    required this.minSupportedApiVersion,
    required this.documentationUrl,
    required this.repositoryUrl,
    this.appVersion = '',
  });

  /// Production server. Forks override this in `assets/cfg/env.json`.
  final String apiBaseUrl;

  /// Used when the app is built in debug mode against an Android emulator
  /// (10.0.2.2 is the emulator's host loopback). Never used in release builds.
  final String apiBaseUrlDev;

  /// Oldest Control API release the client understands. The app refuses to
  /// operate against an older server instead of silently misbehaving.
  final String minSupportedApiVersion;

  final String documentationUrl;
  final String repositoryUrl;

  /// Product version shown in Settings → About. Kept here (not hardcoded in
  /// UI) so forks bump it next to their endpoint config. Empty string means
  /// "unknown" and the UI hides the row.
  final String appVersion;

  factory AppEnv.fromJson(Map<String, dynamic> json) => AppEnv(
    apiBaseUrl: json['apiBaseUrl'] as String,
    apiBaseUrlDev: json['apiBaseUrlDev'] as String,
    minSupportedApiVersion: json['minSupportedApiVersion'] as String,
    documentationUrl: json['documentationUrl'] as String,
    repositoryUrl: json['repositoryUrl'] as String,
    appVersion: json['appVersion'] as String? ?? '',
  );

  static Future<AppEnv> load({bool debug = false}) async {
    final raw = await rootBundle.loadString('assets/cfg/env.json');
    final env = AppEnv.fromJson(jsonDecode(raw) as Map<String, dynamic>);
    return env;
  }

  /// The base URL used by a release build. In debug builds the emulator
  /// loopback alias is used so a local docker-compose stack is reachable.
  String get defaultBaseUrl {
    if (kDebugMode) {
      return apiBaseUrlDev;
    }
    return apiBaseUrl;
  }
}

/// Mirrors `bool.fromEnvironment('dart.vm.product')` and stays false in the
/// Dart VM unit tests so they always exercise the production default.
const kDebugMode = !bool.fromEnvironment('dart.vm.product');

/// Normalises and validates a user-entered server URL.
///
/// Requirements enforced for non-development hosts:
///   * must be an absolute http(s) URL
///   * a non-loopback host must use https
///   * no query string, no fragment, no userinfo
class ServerUrl {
  ServerUrl._(this.normalized);

  /// Returns a [ServerUrl] whose canonical form ends in a single '/'.
  static ServerUrl? tryParse(String raw) {
    var value = raw.trim();
    if (value.isEmpty) return null;
    if (!value.startsWith('http://') && !value.startsWith('https://')) {
      return null;
    }
    final uri = Uri.tryParse(value);
    if (uri == null || !uri.hasScheme || uri.host.isEmpty) return null;
    if (uri.hasQuery || uri.hasFragment || uri.userInfo.isNotEmpty) return null;

    final isLoopback =
        uri.host == 'localhost' ||
        uri.host == '127.0.0.1' ||
        uri.host == '10.0.2.2' ||
        uri.host == '[::1]';
    if (!isLoopback && uri.scheme != 'https') return null;

    // Single trailing slash keeps path concatenation trivial and predictable.
    var path = uri.path;
    while (path.endsWith('/')) {
      path = path.substring(0, path.length - 1);
    }
    final rebuilt = uri.replace(path: '$path/');
    return ServerUrl._(rebuilt.toString());
  }

  static String? validate(String raw, {bool allowInsecureLoopback = true}) {
    final parsed = tryParse(raw);
    if (parsed == null) {
      return 'server_url_invalid';
    }
    final uri = Uri.parse(parsed.normalized);
    final isLoopback =
        uri.host == 'localhost' ||
        uri.host == '127.0.0.1' ||
        uri.host == '10.0.2.2' ||
        uri.host == '[::1]';
    if (!allowInsecureLoopback && isLoopback && uri.scheme != 'https') {
      return 'server_url_https_required';
    }
    if (!isLoopback && uri.scheme != 'https') {
      return 'server_url_https_required';
    }
    return null;
  }

  final String normalized;

  String join(String path) {
    final clean = path.startsWith('/') ? path.substring(1) : path;
    return '$normalized$clean';
  }

  @override
  String toString() => normalized;
}

/// True when [entry] is an IPv4/IPv6 loopback literal (test helper).
bool isLoopbackHost(String entry) {
  final host = entry.replaceAll('[', '').replaceAll(']', '');
  return host == 'localhost' ||
      host == '127.0.0.1' ||
      host == '::1' ||
      host == '10.0.2.2' ||
      (host.startsWith('127.') && host.split('.').length == 4);
}
