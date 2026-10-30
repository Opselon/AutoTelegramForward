/// The change-password flow against `PATCH /api/auth/change-password`.
///
/// Two properties are pinned:
///   * the client validates before it calls (mismatched confirmation must
///     never reach the server), and
///   * a successful change ends in an explicit sign-out — the stored
///     credentials hold the old password, so staying "signed in" would mean
///     the next cold start silently re-logs-in with a password that is no
///     longer correct.
library;

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/models.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/settings/settings_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

const _json = {'content-type': 'application/json'};

typedef _Sent = ({String method, String path, Object? body});

HttpTransport _transport(List<_Sent> log, {bool ok = true}) =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async {
      log.add((method: method, path: url.path, body: body));
      if (method == 'PATCH' && url.path.endsWith('/api/auth/change-password')) {
        if (!ok) {
          return http.Response(
            '{"error":"current password is incorrect"}',
            400,
            headers: _json,
          );
        }
      }
      return http.Response('{}', 200, headers: _json);
    };

Future<AppState> _state(HttpTransport transport) async {
  final s = AppState(
    env: _env,
    tokenStore: MemoryTokenStore(),
    preferenceStore: MemoryPreferenceStore(),
    transportOverride: transport,
  );
  await s.onLoginSuccess(
    AuthSession(
      token: 'test-token',
      userId: 42,
      username: 'ops',
      isAdmin: false,
      serverUrl: _env.apiBaseUrlDev,
      expiresAt: DateTime.now().add(const Duration(days: 1)),
    ),
    password: 'old-secret-1',
  );
  return s;
}

Widget _host(AppState state) {
  return ChangeNotifierProvider<AppState>.value(
    value: state,
    child: MaterialApp(
      theme: ThemeData(useMaterial3: true),
      locale: const Locale('en'),
      localizationsDelegates: const [
        AppMessages.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppMessages.supportedLocales,
      home: const SettingsScreen(),
    ),
  );
}

Finder _key(String key) => find.byKey(ValueKey<String>(key));

Future<void> _openDialog(WidgetTester tester) async {
  await tester.tap(
    find.text(AppMessages(const Locale('en')).get('settings_change_password')),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('a successful change hits the endpoint and ends signed out', (
    tester,
  ) async {
    final log = <_Sent>[];
    final state = await _state(_transport(log));
    tester.view.physicalSize = const Size(400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(_host(state));
    final t = AppMessages(const Locale('en'));

    await _openDialog(tester);
    expect(_key('change_password_dialog'), findsOneWidget);

    await tester.enterText(_key('cp_old_field'), 'old-secret-1');
    await tester.enterText(_key('cp_new_field'), 'new-secret-8');
    await tester.enterText(_key('cp_confirm_field'), 'new-secret-8');
    await tester.tap(_key('cp_submit'));
    await tester.pumpAndSettle();

    final sent = log.where((s) => s.method == 'PATCH').toList();
    expect(sent, hasLength(1));
    expect(sent.single.path, endsWith('/api/auth/change-password'));
    expect(
      sent.single.body,
      containsPair('new_password', 'new-secret-8'),
      reason: 'the body must carry the field names the Go handler binds',
    );

    // The dialog ends on the explicit re-auth step, not on a silent close.
    expect(find.text(t.get('settings_password_changed')), findsOneWidget);
    await tester.tap(find.text(t.get('settings_sign_in_again')));
    await tester.pumpAndSettle();

    expect(state.isAuthenticated, isFalse);
  });

  testWidgets('a mismatched confirmation never reaches the server', (
    tester,
  ) async {
    final log = <_Sent>[];
    tester.view.physicalSize = const Size(400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(_host(await _state(_transport(log))));
    final t = AppMessages(const Locale('en'));

    await _openDialog(tester);
    await tester.enterText(_key('cp_old_field'), 'old-secret-1');
    await tester.enterText(_key('cp_new_field'), 'new-secret-8');
    await tester.enterText(_key('cp_confirm_field'), 'different-9');
    await tester.tap(_key('cp_submit'));
    await tester.pump();

    expect(log.where((s) => s.method == 'PATCH'), isEmpty);
    expect(find.text(t.get('settings_password_mismatch')), findsOneWidget);
  });

  testWidgets('a short new password is rejected client-side', (tester) async {
    final log = <_Sent>[];
    tester.view.physicalSize = const Size(400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(_host(await _state(_transport(log))));
    final t = AppMessages(const Locale('en'));

    await _openDialog(tester);
    await tester.enterText(_key('cp_old_field'), 'old-secret-1');
    await tester.enterText(_key('cp_new_field'), 'short');
    await tester.enterText(_key('cp_confirm_field'), 'short');
    await tester.tap(_key('cp_submit'));
    await tester.pump();

    expect(log.where((s) => s.method == 'PATCH'), isEmpty);
    expect(find.text(t.get('settings_password_too_short')), findsOneWidget);
  });

  testWidgets('a server refusal surfaces as a message, not a success', (
    tester,
  ) async {
    final log = <_Sent>[];
    tester.view.physicalSize = const Size(400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(_host(await _state(_transport(log, ok: false))));
    final t = AppMessages(const Locale('en'));

    await _openDialog(tester);
    await tester.enterText(_key('cp_old_field'), 'wrong-pass');
    await tester.enterText(_key('cp_new_field'), 'new-secret-8');
    await tester.enterText(_key('cp_confirm_field'), 'new-secret-8');
    await tester.tap(_key('cp_submit'));
    await tester.pumpAndSettle();

    expect(log.where((s) => s.method == 'PATCH'), hasLength(1));
    // The refusal text comes straight from the server's error field.
    expect(find.text('current password is incorrect'), findsOneWidget);
    expect(find.text(t.get('settings_password_changed')), findsNothing);
  });
}
