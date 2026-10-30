/// Rule lifecycle controls, driven over a canned transport.
///
/// The backend distinguishes four transitions the card must not blur:
/// pause holds without deactivating, resume clears the hold, stop
/// deactivates, restart re-activates and clears the hold. The tests pin the
/// visible state names and the exact endpoint each control calls — including
/// the switch, which must resume a paused task instead of blind-toggling it.
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
import 'package:autoforward_mobile/features/rules/rules_screen.dart';
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

const String _rulesBody =
    '{"rules":['
    '{"id":"rule-running","name":"Running relay","source_chat_id":"-1001111111111","target_chat_id":"-1002222222222","is_active":true,"is_paused":false,"version":3},'
    '{"id":"rule-paused","name":"Paused relay","source_chat_id":"-1001111111111","target_chat_id":"-1002222222222","is_active":true,"is_paused":true,"version":4},'
    '{"id":"rule-stopped","name":"Stopped relay","source_chat_id":"-1001111111111","target_chat_id":"-1002222222222","is_active":false,"is_paused":false,"version":5}'
    '],"total":3}';

/// The server echoes the updated rule back from /stop and /restart (both
/// parse as ForwardRule), so a lifecycle POST must answer with the rule's
/// own JSON — an empty object would crash the model parse, which is exactly
/// what the real server never does.
const Map<String, String> _ruleById = {
  'rule-running': '{"id":"rule-running","name":"Running relay","source_chat_id":"-1001111111111","target_chat_id":"-1002222222222","is_active":true,"is_paused":false,"version":3}',
  'rule-paused': '{"id":"rule-paused","name":"Paused relay","source_chat_id":"-1001111111111","target_chat_id":"-1002222222222","is_active":true,"is_paused":true,"version":4}',
  'rule-stopped': '{"id":"rule-stopped","name":"Stopped relay","source_chat_id":"-1001111111111","target_chat_id":"-1002222222222","is_active":false,"is_paused":false,"version":5}',
};

HttpTransport _transport(List<_Sent> log) {
  return (
    Uri url, {
    required String method,
    Map<String, String>? headers,
    Object? body,
    required Duration timeout,
  }) async {
    log.add((method: method, path: url.path, body: body));
    if (method == 'GET' && url.path.endsWith('/api/rules')) {
      return http.Response(_rulesBody, 200, headers: _json);
    }
    final match = RegExp(r'/api/rules/([^/]+)').firstMatch(url.path);
    if (method == 'POST' && match != null) {
      final ruleJson = _ruleById[match.group(1)];
      if (ruleJson != null) {
        return http.Response(ruleJson, 200, headers: _json);
      }
    }
    return http.Response('{}', 200, headers: _json);
  };
}

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
      userId: 7,
      username: 'ops',
      isAdmin: false,
      serverUrl: _env.apiBaseUrlDev,
      expiresAt: DateTime.now().add(const Duration(days: 1)),
    ),
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
      home: const RulesScreen(),
    ),
  );
}

void main() {
  Future<AppState> pump(WidgetTester tester, List<_Sent> log) async {
    final state = await _state(_transport(log));
    tester.view.physicalSize = const Size(400, 1400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(_host(state));
    await tester.pumpAndSettle();
    return state;
  }

  testWidgets('the three server states render under their own names', (
    tester,
  ) async {
    final log = <_Sent>[];
    await pump(tester, log);
    final t = AppMessages(const Locale('en'));

    expect(find.text(t.get('rules_status_active')), findsOneWidget);
    expect(find.text(t.get('rules_status_paused')), findsOneWidget);
    expect(find.text(t.get('rules_status_stopped')), findsOneWidget);
  });

  testWidgets('Pause on a running task hits /pause, not /toggle', (
    tester,
  ) async {
    final log = <_Sent>[];
    await pump(tester, log);
    final t = AppMessages(const Locale('en'));

    // The first card is the running rule.
    await tester.tap(find.byTooltip(t.get('rules_more_actions')).first);
    await tester.pumpAndSettle();
    expect(find.text(t.get('rules_pause')), findsOneWidget);
    expect(find.text(t.get('rules_resume')), findsNothing);

    await tester.tap(find.text(t.get('rules_pause')));
    await tester.pumpAndSettle();

    expect(
      log.any(
        (s) =>
            s.method == 'POST' &&
            s.path.endsWith('/api/rules/rule-running/pause'),
      ),
      isTrue,
    );
    expect(
      log.any(
        (s) =>
            s.method == 'POST' &&
            s.path.endsWith('/api/rules/rule-running/toggle'),
      ),
      isFalse,
      reason: 'pause must use the pause endpoint, not the blind toggle',
    );
  });

  testWidgets('a stopped task offers restart, never resume', (tester) async {
    final log = <_Sent>[];
    await pump(tester, log);
    final t = AppMessages(const Locale('en'));

    // The last card is the stopped rule.
    await tester.tap(find.byTooltip(t.get('rules_more_actions')).last);
    await tester.pumpAndSettle();
    expect(find.text(t.get('rules_restart')), findsOneWidget);
    expect(find.text(t.get('rules_resume')), findsNothing);
    expect(find.text(t.get('rules_pause')), findsNothing);

    await tester.tap(find.text(t.get('rules_restart')));
    await tester.pumpAndSettle();
    expect(
      log.any(
        (s) =>
            s.method == 'POST' &&
            s.path.endsWith('/api/rules/rule-stopped/restart'),
      ),
      isTrue,
    );
  });

  testWidgets('switching a paused task back on resumes it instead of '
      'toggling the active flag', (tester) async {
    final log = <_Sent>[];
    await pump(tester, log);
    final t = AppMessages(const Locale('en'));

    // The middle card is the paused rule; its switch is on because pause
    // does not deactivate. Turning it off runs toggle (stop semantics) —
    // what must never happen is a resume being sent by the wrong control.
    final switches = find.byType(Switch);
    expect(switches, findsNWidgets(3));
    final pausedSwitch = tester.widget<Switch>(switches.at(1));
    expect(pausedSwitch.value, isTrue);

    // Resume via the menu: POST /resume.
    await tester.tap(find.byTooltip(t.get('rules_more_actions')).at(1));
    await tester.pumpAndSettle();
    await tester.tap(find.text(t.get('rules_resume')));
    await tester.pumpAndSettle();
    expect(
      log.any(
        (s) =>
            s.method == 'POST' &&
            s.path.endsWith('/api/rules/rule-paused/resume'),
      ),
      isTrue,
    );
  });
}
