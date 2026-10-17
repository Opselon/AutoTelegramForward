/// Credential and preference persistence.
///
/// Tokens are stored through [SecureTokenStore], whose production
/// implementation uses `flutter_secure_storage` (Android Keystore /
/// iOS Keychain). Tests inject an in-memory implementation, so the
/// state machines never depend on a platform plugin.
library;

import 'json_cast.dart';

import 'package:flutter/foundation.dart';

@immutable
class StoredCredentials {
  const StoredCredentials({
    required this.serverUrl,
    required this.token,
    required this.userId,
    required this.username,
    required this.isAdmin,
    required this.expiresAtIso,
    this.password = '',
  });

  final String serverUrl;
  final String token;
  final int userId;
  final String username;
  final bool isAdmin;
  final String expiresAtIso;
  final String password;

  bool get isValid {
    if (token.isEmpty || serverUrl.isEmpty) return false;
    final expiry = DateTime.tryParse(expiresAtIso);
    return expiry != null && DateTime.now().isBefore(expiry);
  }

  Map<String, dynamic> toJson() => {
    'server_url': serverUrl,
    'token': token,
    'user_id': userId,
    'username': username,
    'password': password,
    'is_admin': isAdmin,
    'expires_at': expiresAtIso,
  };

  factory StoredCredentials.fromJson(Map<String, dynamic> json) =>
      StoredCredentials(
        serverUrl: json['server_url'] as String? ?? '',
        token: json['token'] as String? ?? '',
        userId: jsonInt(json['user_id']),
        username: json['username'] as String? ?? '',
        password: json['password'] as String? ?? '',
        isAdmin: json['is_admin'] as bool? ?? false,
        expiresAtIso: json['expires_at'] as String? ?? '',
      );

  static const empty = StoredCredentials(
    serverUrl: '',
    token: '',
    userId: 0,
    username: '',
    password: '',
    isAdmin: false,
    expiresAtIso: '',
  );

  @override
  String toString() =>
      'StoredCredentials(server=$serverUrl, user=$userId, valid=$isValid)';
}

/// Abstraction over the platform secure store. The only contract the app
/// relies on is: [read] returns the last persisted credentials or `empty`,
/// and [clear] removes them.
abstract class SecureTokenStore {
  Future<StoredCredentials> read();

  Future<void> write(StoredCredentials credentials);

  Future<void> clear();
}

/// Pure in-memory implementation used by unit + widget tests.
class MemoryTokenStore implements SecureTokenStore {
  MemoryTokenStore([StoredCredentials? initial])
    : _current = initial ?? StoredCredentials.empty;

  StoredCredentials _current;

  @override
  Future<StoredCredentials> read() async => _current;

  @override
  Future<void> write(StoredCredentials credentials) async {
    _current = credentials;
  }

  @override
  Future<void> clear() async {
    _current = StoredCredentials.empty;
  }

  StoredCredentials get current => _current;
}

/// User preferences that are NOT secret: language, theme, last server URL.
/// Backed by `shared_preferences` in production, an in-memory map in tests.
abstract class PreferenceStore {
  Future<String> getString(String key, {String defaultValue = ''});

  Future<void> setString(String key, String value);

  Future<bool> getBool(String key, {bool defaultValue = false});

  Future<void> setBool(String key, bool value);
}

class MemoryPreferenceStore implements PreferenceStore {
  final Map<String, String> _strings = {};
  final Map<String, bool> _bools = {};

  @override
  Future<String> getString(String key, {String defaultValue = ''}) async =>
      _strings[key] ?? defaultValue;

  @override
  Future<void> setString(String key, String value) async {
    _strings[key] = value;
  }

  @override
  Future<bool> getBool(String key, {bool defaultValue = false}) async =>
      _bools[key] ?? defaultValue;

  @override
  Future<void> setBool(String key, bool value) async {
    _bools[key] = value;
  }
}

/// Keys used by [PreferenceStore].
class PrefKeys {
  const PrefKeys._();

  static const String language = 'atf.pref.language';
  static const String themeMode = 'atf.pref.themeMode';
  static const String lastServerUrl = 'atf.pref.lastServerUrl';
}
