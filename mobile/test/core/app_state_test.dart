import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/models.dart';
import 'package:autoforward_mobile/core/storage.dart';

void main() {
  group('AppState state transitions', () {
    late MemoryTokenStore tokenStore;
    late MemoryPreferenceStore prefStore;
    late AppEnv env;

    setUp(() {
      tokenStore = MemoryTokenStore();
      prefStore = MemoryPreferenceStore();
      env = const AppEnv(
        apiBaseUrl: 'https://atf.example.com/',
        apiBaseUrlDev: 'http://10.0.2.2:8080/',
        minSupportedApiVersion: '1.0.0',
        documentationUrl: 'https://example.com/docs',
        repositoryUrl: 'https://example.com/repo',
      );
    });

    test('restores stored valid session', () async {
      await tokenStore.write(
        StoredCredentials(
          serverUrl: 'https://custom.atf.com/',
          token: 'stored-valid-token',
          userId: 99,
          username: 'bob',
          isAdmin: true,
          expiresAtIso:
              DateTime.now().add(const Duration(days: 2)).toIso8601String(),
        ),
      );

      final state = AppState(
        env: env,
        tokenStore: tokenStore,
        preferenceStore: prefStore,
      );

      await state.restore();

      expect(state.isAuthenticated, isTrue);
      expect(state.userId, 99);
      expect(state.username, 'bob');
      expect(state.isAdmin, isTrue);
      expect(state.serverUrl, 'https://custom.atf.com/');
    });

    test('ignores expired credentials on restore', () async {
      await tokenStore.write(
        StoredCredentials(
          serverUrl: 'https://custom.atf.com/',
          token: 'stored-expired-token',
          userId: 99,
          username: 'bob',
          isAdmin: false,
          expiresAtIso:
              DateTime.now()
                  .subtract(const Duration(minutes: 5))
                  .toIso8601String(),
        ),
      );

      final state = AppState(
        env: env,
        tokenStore: tokenStore,
        preferenceStore: prefStore,
      );

      await state.restore();

      expect(state.isAuthenticated, isFalse);
      expect(state.userId, 0);
      expect(state.username, isEmpty);
    });

    test('sets and persists theme and locale', () async {
      final state = AppState(
        env: env,
        tokenStore: tokenStore,
        preferenceStore: prefStore,
      );
      await state.restore();

      state.setLocale(const Locale('fa'));
      expect(state.locale.languageCode, 'fa');
      expect(await prefStore.getString(PrefKeys.language), 'fa');

      state.setThemeMode(ThemeMode.dark);
      expect(state.themeMode, ThemeMode.dark);
      expect(await prefStore.getString(PrefKeys.themeMode), 'dark');
    });

    test('logout clears session and stored tokens', () async {
      final state = AppState(
        env: env,
        tokenStore: tokenStore,
        preferenceStore: prefStore,
      );
      await state.restore();

      final session = AuthSession(
        token: 'fresh-token',
        userId: 5,
        username: 'charlie',
        isAdmin: false,
        serverUrl: state.serverUrl,
        expiresAt: DateTime.now().add(const Duration(days: 1)),
      );

      await state.onLoginSuccess(session);
      expect(state.isAuthenticated, isTrue);

      await state.logout();
      expect(state.isAuthenticated, isFalse);
      expect(state.userId, 0);
      final stored = await tokenStore.read();
      expect(stored.token, isEmpty);
    });
  });
}
