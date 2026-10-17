/// The mobile half of per-bot defaults: what the sheet shows, and what saving
/// means once a bot can supply a value.
///
/// Two failures are possible here and both are silent:
///
/// * showing the task's *stored* value for a setting it inherits, so the user
///   sees `false` while the forwarder runs with the bot's `true`; and
/// * seeding the sheet from the stored values, so an inherited value the user
///   never touched comes back as a "change" and gets pinned — ending exactly the
///   inheritance the bot default exists to provide.
///
/// The sheet therefore opens on the effective view and diffs against it, and
/// "use default" is what hands a value back — never writing the default value.
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

/// Three settings, one per source: inherited from the bot, owned by the task
/// (chosen, and equal to the registry default), and untouched.
const _schemaBody = '''
{
  "version": 5,
  "groups": ["cleanup"],
  "settings": [
    {"key":"remove_emojis","kind":"bool","group":"cleanup","label":"Remove emoji",
     "help":"Strip emoji before sending.","default":false,"inheritable":true},
    {"key":"remove_links","kind":"bool","group":"cleanup","label":"Remove links",
     "help":"Strip links before sending.","default":false,"inheritable":true},
    {"key":"clean_usernames","kind":"bool","group":"cleanup","label":"Clean usernames",
     "help":"Strip @mentions.","default":false,"inheritable":true}
  ]
}
''';

/// What the API answers for the task: it *stores* two values and inherits one.
/// `remove_emojis` is the bot's `true` although the task stores `false`;
/// `remove_links` is the task's own choice and happens to equal the registry
/// default; `clean_usernames` is nobody's.
String _settingsBody({bool afterReset = false}) => afterReset
    ? '''
{
  "settings": {"remove_emojis": false, "remove_links": false, "clean_usernames": false},
  "effective": {"remove_emojis": true, "remove_links": false, "clean_usernames": false},
  "sources": {
    "remove_emojis": {"value": true, "source": "bot", "default": false,
                      "inheritable": true, "bot_default": true, "bot_label": "@alice_bot"},
    "remove_links": {"value": false, "source": "default", "default": false,
                     "inheritable": true},
    "clean_usernames": {"value": false, "source": "default", "default": false,
                        "inheritable": true}
  }
}
'''
    : '''
{
  "settings": {"remove_emojis": false, "remove_links": false, "clean_usernames": false},
  "effective": {"remove_emojis": true, "remove_links": false, "clean_usernames": false},
  "sources": {
    "remove_emojis": {"value": true, "source": "bot", "default": false,
                      "inheritable": true, "bot_default": true, "bot_label": "@alice_bot"},
    "remove_links": {"value": false, "source": "task", "default": false,
                     "inheritable": true, "pinned": true},
    "clean_usernames": {"value": false, "source": "default", "default": false,
                        "inheritable": true}
  }
}
''';

/// Records every PATCH so the test can assert what the sheet actually sent.
class _Calls {
  final List<Object?> bodies = <Object?>[];
  bool reset = false;

  /// The one body sent, as a map: the client sends structured JSON, not text.
  Map<String, dynamic> get only => bodies.single as Map<String, dynamic>;
}

HttpTransport _transport(_Calls calls) =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async {
      final headersJson = const {'content-type': 'application/json'};
      if (url.path.endsWith('/api/rules/settings/schema')) {
        return http.Response(_schemaBody, 200, headers: headersJson);
      }
      if (url.path.endsWith('/api/bot-tokens')) {
        return http.Response(
          '{"tokens":[],"total":0}',
          200,
          headers: headersJson,
        );
      }
      if (url.path.endsWith('/settings') && method == 'PATCH') {
        calls.bodies.add(body);
        if (body is Map && body['reset'] != null) calls.reset = true;
        return http.Response(
          '{"success":true,"message":"ok","settings":{}}',
          200,
          headers: headersJson,
        );
      }
      if (url.path.endsWith('/settings')) {
        return http.Response(
          _settingsBody(afterReset: calls.reset),
          200,
          headers: headersJson,
        );
      }
      return http.Response('{}', 200);
    };

AppState _state(_Calls calls) => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: _transport(calls),
);

Future<void> _pumpSheet(WidgetTester tester, _Calls calls) async {
  tester.view.physicalSize = const Size(1000, 3000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
      value: _state(calls),
      child: const MaterialApp(
        locale: Locale('en'),
        localizationsDelegates: <LocalizationsDelegate<dynamic>>[
          AppMessages.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppMessages.supportedLocales,
        home: Scaffold(body: RuleSettingsSheet(ruleId: 'r-1')),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

/// The switch rendered for a setting label. Found *through* the label so the
/// assertion cannot quietly pass against a different row.
SwitchListTile _switchFor(WidgetTester tester, String label) =>
    tester.widget<SwitchListTile>(
      find.ancestor(
        of: find.text(label),
        matching: find.byType(SwitchListTile),
      ),
    );

void main() {
  testWidgets(
    'an inherited setting is shown as the bot has it, not as stored',
    (tester) async {
      final calls = _Calls();
      await _pumpSheet(tester, calls);

      // The task stores `false`; the bot says `true`; the forwarder will strip
      // emoji, so the switch must read as on.
      expect(_switchFor(tester, 'Remove emoji').value, isTrue);
      // ... and it says where that came from.
      expect(find.text('from @alice_bot'), findsWidgets);
    },
  );

  testWidgets('a value the task owns is shown as its own', (tester) async {
    final calls = _Calls();
    await _pumpSheet(tester, calls);

    expect(_switchFor(tester, 'Remove links').value, isFalse);
    expect(find.text('set here'), findsWidgets);
  });

  testWidgets('a setting on the registry default offers nothing to hand back', (
    tester,
  ) async {
    final calls = _Calls();
    await _pumpSheet(tester, calls);

    // One setting per source: the inherited one is named, the owned one is
    // marked, and only the owned one can be handed back — the untouched third
    // adds no badge and no button.
    expect(find.text('from @alice_bot'), findsOneWidget);
    expect(find.text('set here'), findsOneWidget);
    expect(find.byIcon(Icons.settings_backup_restore), findsOneWidget);
    expect(_switchFor(tester, 'Clean usernames').value, isFalse);
  });

  testWidgets('"use default" hands the value back instead of writing it', (
    tester,
  ) async {
    final calls = _Calls();
    await _pumpSheet(tester, calls);

    await tester.tap(find.byIcon(Icons.settings_backup_restore).first);
    await tester.pumpAndSettle();

    expect(calls.bodies, hasLength(1), reason: 'exactly one write');
    expect(calls.only['reset'], <String>['remove_links']);
    // The point of the verb: nothing is written as a value.
    expect(calls.only['settings'], isEmpty);
    expect(calls.only['replace_all'], isFalse);
    expect(find.text('Handed back to the default'), findsOneWidget);
    // And the screen reloads: `remove_links` now inherits the default, so it is
    // no longer the task's own and offers nothing to hand back.
    expect(find.text('set here'), findsNothing);
  });

  testWidgets('nothing is sent when the user changes nothing', (tester) async {
    final calls = _Calls();
    await _pumpSheet(tester, calls);

    // Inheriting a value is not a change: it must never be posted back, or the
    // task would be pinned to it and stop following its bot.
    expect(calls.bodies, isEmpty);
  });
}
