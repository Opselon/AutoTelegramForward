/// Production implementations of the storage abstractions in `core/storage.dart`.
///
/// `flutter_secure_storage` on Android routes through the Android Keystore,
/// on iOS through the Keychain (kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly).
/// Nothing else in the app touches these plugins, so the whole Dart-level
/// contract stays testable in the plain VM.
library;

import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'storage.dart';

const String _kCredentialsKey = 'atf.credentials.v1';

/// Stores the JWT in the platform secure enclave. The token is the only
/// long-lived secret the app holds; user preferences are non-secret and go
/// to `shared_preferences`.
class SecureStorageProd implements SecureTokenStore {
  SecureStorageProd({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
              iOptions: IOSOptions(
                accessibility: KeychainAccessibility.first_unlock_this_device,
              ),
            );

  final FlutterSecureStorage _storage;

  @override
  Future<StoredCredentials> read() async {
    final raw = await _storage.read(key: _kCredentialsKey);
    if (raw == null || raw.isEmpty) {
      return StoredCredentials.empty;
    }
    try {
      final decoded = jsonDecode(raw);
      if (decoded is Map<String, dynamic>) {
        return StoredCredentials.fromJson(decoded);
      }
    } on FormatException {
      await _storage.delete(key: _kCredentialsKey);
    }
    return StoredCredentials.empty;
  }

  @override
  Future<void> write(StoredCredentials credentials) async {
    await _storage.write(
      key: _kCredentialsKey,
      value: jsonEncode(credentials.toJson()),
    );
  }

  @override
  Future<void> clear() async {
    await _storage.delete(key: _kCredentialsKey);
  }
}

/// Non-secret preferences: language, theme mode, last server URL.
class PreferenceStoreProd implements PreferenceStore {
  PreferenceStoreProd();

  SharedPreferences? _prefs;

  Future<SharedPreferences> get _instance async =>
      _prefs ??= await SharedPreferences.getInstance();

  @override
  Future<String> getString(String key, {String defaultValue = ''}) async {
    final prefs = await _instance;
    return prefs.getString(key) ?? defaultValue;
  }

  @override
  Future<void> setString(String key, String value) async {
    final prefs = await _instance;
    await prefs.setString(key, value);
  }

  @override
  Future<bool> getBool(String key, {bool defaultValue = false}) async {
    final prefs = await _instance;
    return prefs.getBool(key) ?? defaultValue;
  }

  @override
  Future<void> setBool(String key, bool value) async {
    final prefs = await _instance;
    await prefs.setBool(key, value);
  }
}
