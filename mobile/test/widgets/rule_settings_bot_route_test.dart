/// The mobile half of the source → BOT → target route.
///
/// The route is a per-task setting, but `bot_token_id` is a server-side id the
/// user never sees. This test pins the two things that make the field usable:
/// it lists the account's attached bots *by name*, and it offers an inline way
/// to attach one without leaving the task.
///
/// The refusal paths (a token Telegram rejects, a bot a task still routes
/// through) are the server's and are asserted in the API/servicer suites; what
/// is asserted here is that the client renders whatever the server describes,
/// including the warning when a task is on the bot route with no bot attached.
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

const _schemaBody = '''
{
  "version": 4,
  "groups": ["general", "route"],
  "settings": [
    {"key":"debug_mode","kind":"bool","group":"general","label":"Debug Mode",
     "help":"Verbose per-message logging.","default":false},
    {"key":"delivery_transport","kind":"str","group":"route",
     "label":"Delivery route","help":"Send as the account, or as an attached bot.",
     "default":"USER","choices":["USER","BOT"]},
    {"key":"bot_token_id","kind":"str","group":"route",
     "label":"Bot","help":"The bot this task delivers as.","default":""}
  ]
}
''';

/// The account's bots, as `GET /api/bot-tokens` returns them: ids and resolved
/// usernames only — the credential itself is write-only.
const _botsBody = '''
{"tokens":[{"id":"tok-a","label":"@alice_bot","is_active":true}],"total":1}
''';

const _emptyBotsBody = '{"tokens":[],"total":0}';

HttpTransport _transport({String bots = _botsBody, String transport = 'BOT'}) =>
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
        return http.Response(bots, 200, headers: headersJson);
      }
      if (url.path.endsWith('/settings')) {
        return http.Response(
          '{"settings":{"debug_mode":false,"delivery_transport":"$transport",'
          '"bot_token_id":"tok-a"}}',
          200,
          headers: headersJson,
        );
      }
      return http.Response('{}', 200);
    };

AppState _state({String bots = _botsBody, String transport = 'BOT'}) =>
    AppState(
      env: _env,
      tokenStore: MemoryTokenStore(),
      preferenceStore: MemoryPreferenceStore(),
      transportOverride: _transport(bots: bots, transport: transport),
    );

Future<void> _pumpSheet(
  WidgetTester tester,
  Locale locale, {
  String bots = _botsBody,
  String transport = 'BOT',
}) async {
  tester.view.physicalSize = const Size(1000, 3000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
      value: _state(bots: bots, transport: transport),
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
  testWidgets(
    'the route group is labelled and the bot field lists the bot by name',
    (tester) async {
      await _pumpSheet(tester, const Locale('en'));

      expect(find.text('Delivery route'), findsWidgets);
      // Never the raw registry identifiers.
      expect(find.text('route'), findsNothing);
      expect(find.text('bot_token_id'), findsNothing);

      // The attached bot is offered by its resolved username, not its id.
      expect(find.text('@alice_bot'), findsWidgets);
      expect(find.text('tok-a'), findsNothing);
      expect(find.text('Attach bot'), findsWidgets);
    },
  );

  testWidgets('an empty account says so instead of showing a blank picker', (
    tester,
  ) async {
    await _pumpSheet(tester, const Locale('en'), bots: _emptyBotsBody);

    expect(find.text('No bot attached yet'), findsOneWidget);
    // On the bot route with nothing attached, the user is told what to do.
    expect(
      find.textContaining('delivers as a bot but none is attached'),
      findsOneWidget,
    );
  });

  testWidgets('attaching a bot asks for a token and warns that it is one-way', (
    tester,
  ) async {
    await _pumpSheet(tester, const Locale('en'), bots: _emptyBotsBody);

    await tester.tap(find.text('Attach bot').first);
    await tester.pumpAndSettle();

    expect(find.byType(AlertDialog), findsOneWidget);
    expect(find.text('Bot token'), findsOneWidget);
    expect(find.textContaining('never'), findsWidgets);
    // The secret is entered blind: the field must be obscured.
    final secretField = tester
        .widgetList<TextField>(find.byType(TextField))
        .last;
    expect(secretField.obscureText, isTrue);
  });

  testWidgets(
    'the client-authored parts of the field follow the chosen locale',
    (tester) async {
      await _pumpSheet(tester, const Locale('fa'));

      // The group header and the buttons are the client's own strings, so they
      // translate. The field *label* comes from the server's registry and is
      // English on every client (as all ~120 registry labels are), so asserting
      // it away here would be asserting against the sheet's design, not a leak.
      expect(find.text('مسیر ارسال'), findsWidgets);
      expect(find.text('افزودن ربات'), findsWidgets);
      expect(find.text('Attach bot'), findsNothing);
    },
  );
}
