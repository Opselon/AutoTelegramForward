/// The auto-reply shell: five surfaces behind one screen.
///
/// This is a wiring test, not a layout test. It proves the shell builds with a
/// server that answers nothing useful (every list empty, settings at defaults)
/// and that each tab renders without throwing — the state every operator sees
/// on a fresh account.
library;

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/auto_reply/auto_reply_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

AppState _emptyState() => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride:
      (
        Uri url, {
        required String method,
        Map<String, String>? headers,
        Object? body,
        required Duration timeout,
      }) async => http.Response(
        '{}',
        200,
        headers: const {'content-type': 'application/json'},
      ),
);

void main() {
  final t = AppMessages(const Locale('en'));

  testWidgets('the shell renders five tabs and each one builds', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1100, 2600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(
      ChangeNotifierProvider<AppState>.value(
        value: _emptyState(),
        child: const MaterialApp(
          locale: Locale('en'),
          localizationsDelegates: [
            AppMessages.delegate,
            GlobalMaterialLocalizations.delegate,
            GlobalWidgetsLocalizations.delegate,
            GlobalCupertinoLocalizations.delegate,
          ],
          supportedLocales: AppMessages.supportedLocales,
          home: AutoReplyScreen(),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text(t.get('ar_title')), findsOneWidget);
    for (final key in const [
      'ar_tab_settings',
      'ar_tab_prompts',
      'ar_tab_targets',
      'ar_tab_rules',
      'ar_tab_activity',
    ]) {
      expect(find.text(t.get(key)), findsOneWidget, reason: key);
    }

    // Walk the tabs: each view loads its own endpoint on first build, so a
    // crash here is a broken tab rather than a styling nit.
    for (final key in const [
      'ar_tab_prompts',
      'ar_tab_targets',
      'ar_tab_rules',
      'ar_tab_activity',
    ]) {
      await tester.tap(find.text(t.get(key)));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull, reason: key);
    }

    // The activity tab measures from /api/auto-reply/stats, so its first card
    // exists even on an empty account.
    expect(find.text(t.get('ar_stat_sent_today')), findsOneWidget);
  });
}
