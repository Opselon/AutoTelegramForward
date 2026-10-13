import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/storage.dart';

void main() {
  group('SecureTokenStore in-memory', () {
    test('starts empty and supports write / read / clear', () async {
      final store = MemoryTokenStore();
      final initial = await store.read();
      expect(initial.token, isEmpty);
      expect(initial.isValid, isFalse);

      final creds = StoredCredentials(
        serverUrl: 'https://atf.example.com/',
        token: 'test-jwt-token-123',
        userId: 10,
        username: 'alice',
        isAdmin: true,
        expiresAtIso:
            DateTime.now().add(const Duration(days: 7)).toIso8601String(),
      );

      await store.write(creds);
      final readBack = await store.read();
      expect(readBack.token, 'test-jwt-token-123');
      expect(readBack.userId, 10);
      expect(readBack.isAdmin, isTrue);
      expect(readBack.isValid, isTrue);

      await store.clear();
      final afterClear = await store.read();
      expect(afterClear.token, isEmpty);
    });

    test('expired credentials are identified as invalid', () {
      final expired = StoredCredentials(
        serverUrl: 'https://atf.example.com/',
        token: 'expired-token',
        userId: 10,
        username: 'alice',
        isAdmin: false,
        expiresAtIso:
            DateTime.now().subtract(const Duration(days: 1)).toIso8601String(),
      );
      expect(expired.isValid, isFalse);
    });
  });

  group('PreferenceStore in-memory', () {
    test('saves and retrieves strings and booleans', () async {
      final prefs = MemoryPreferenceStore();
      expect(await prefs.getString(PrefKeys.language), isEmpty);

      await prefs.setString(PrefKeys.language, 'fa');
      expect(await prefs.getString(PrefKeys.language), 'fa');

      await prefs.setString(PrefKeys.themeMode, 'dark');
      expect(await prefs.getString(PrefKeys.themeMode), 'dark');
    });
  });
}
