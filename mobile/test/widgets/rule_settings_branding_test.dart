/// The per-task settings sheet renders every group the server's registry
/// describes — including the `branding` group that carries the trigger policy
/// for the A → C → B hop.
///
/// Two things are asserted against a canned schema:
///
///   * a new server-side group renders as a translated header (the identifier
///     `branding` must never reach the screen), and
///   * the group's switches come from the schema alone, so shipping the
///     registry entries server-side is enough to expose them.
library;

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/rules/rule_settings_sheet.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

/// The registry slice the branding hop adds, as `GET /api/rules/settings/schema`
/// returns it once the server ships the group.
const _schemaBody = '''
{
  "version": 3,
  "groups": ["general", "branding"],
  "settings": [
    {"key":"debug_mode","kind":"bool","group":"general","label":"Debug Mode",
     "help":"Verbose per-message logging.","default":false},
    {"key":"branding_triggers_enabled","kind":"bool","group":"branding",
     "label":"Only hop for configured triggers","help":"Off: every message hops.",
     "default":false},
    {"key":"branding_origin_titles","kind":"list","group":"branding",
     "label":"Forward-origin channels","help":"Channel names.","default":[]},
    {"key":"branding_keywords","kind":"list","group":"branding",
     "label":"Keywords","help":"Words in the text.","default":[]}
  ]
}
''';

HttpTransport _transport() =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async {
      if (url.path.endsWith('/api/rules/settings/schema')) {
        return http.Response(
          _schemaBody,
          200,
          headers: const {'content-type': 'application/json'},
        );
      }
      if (url.path.endsWith('/settings')) {
        return http.Response(
          '{"settings":{"debug_mode":false,"branding_triggers_enabled":false,'
          '"branding_origin_titles":[],"branding_keywords":[]}}',
          200,
          headers: const {'content-type': 'application/json'},
        );
      }
      return http.Response('{}', 200);
    };

AppState _state() => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: _transport(),
);

Future<void> _pumpSheet(WidgetTester tester, Locale locale) async {
  tester.view.physicalSize = const Size(1000, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
      value: _state(),
      child: MaterialApp(
        locale: locale,
        localizationsDelegates: const [
          AppMessages.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppMessages.supportedLocales,
        home: const Scaffold(body: RuleSettingsSheet(ruleId: 'r-1')),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('the branding group renders translated, with its switches', (
    tester,
  ) async {
    await _pumpSheet(tester, const Locale('en'));

    expect(find.text('Branding hop (A → C → B)'), findsOneWidget);
    // Never the raw registry identifier, and never the title-cased fallback.
    expect(find.text('branding'), findsNothing);
    expect(find.text('BRANDING'), findsNothing);

    // The switches are schema-driven: nothing in the app names these keys.
    for (final label in const [
      'Only hop for configured triggers',
      'Forward-origin channels',
      'Keywords',
    ]) {
      expect(find.text(label), findsOneWidget, reason: 'missing $label');
    }
    expect(find.byType(SwitchListTile), findsWidgets);
  });

  testWidgets('the group header follows the chosen locale', (tester) async {
    await _pumpSheet(tester, const Locale('fa'));

    expect(find.text('پرش برندینگ (A → C → B)'), findsOneWidget);
    expect(find.text('Branding hop (A → C → B)'), findsNothing);
    // The neighbouring group still title-cases: only authored groups translate.
    expect(find.text('GENERAL'), findsOneWidget);
  });
}
