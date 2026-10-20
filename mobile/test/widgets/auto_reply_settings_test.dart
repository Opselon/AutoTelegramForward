/// The auto-reply settings screen, driven over a canned transport.
///
/// The screen makes one promise that matters more than its layout: the master
/// switch is the control that decides whether the bot answers anyone. Toggling
/// it must write `enabled` — and *only* `enabled` — so a concurrent edit made
/// elsewhere in the form (or on another device) cannot be clobbered by a stale
/// full-form PUT. This test asserts exactly one PUT and its `update_mask`.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/auto_reply/settings_view.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

const _json = {'content-type': 'application/json'};

typedef _Sent = ({String method, String path, Map<String, dynamic> body});

const String _settingsBody =
    '{"settings":{"enabled":false,"ai_config_id":"cfg-1","prompt_id":"p-1",'
    '"target_mode":"EVERYONE","language":"","tone":"friendly",'
    '"response_length":"medium","temperature":0.7,"max_tokens":400,'
    '"context_messages":8,"memory_enabled":true,"memory_max_facts":20,'
    '"extra_instructions":"","max_reply_chars":1000,"typing_simulation":true,'
    '"typing_delay_min":1,"typing_delay_max":3,"cooldown_seconds":60,'
    '"daily_limit_per_chat":50,"rate_limit_per_minute":6,"max_concurrency":2,'
    '"max_attempts":3,"retry_backoff_seconds":30,"quiet_hours_start":"",'
    '"quiet_hours_end":"","timezone_offset_minutes":210,'
    '"active_days":[0,1,2,3,4,5,6],"fallback_text":"","ignore_bots":true,'
    '"version":4}}';

/// The screen reads three endpoints on first paint: the settings themselves,
/// the AI configurations that fill the provider picker, and the prompt library
/// that fills the prompt picker. [putBody] is what the settings PUT answers.
AppState _state(List<_Sent> log, {String? putBody, bool settingsOk = true}) =>
    AppState(
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
          }) async {
            final decoded = body == null
                ? <String, dynamic>{}
                : body is String && body.isNotEmpty
                ? (jsonDecode(body) as Map).cast<String, dynamic>()
                : (body as Map).cast<String, dynamic>();
            log.add((method: method, path: url.path, body: decoded));
            if (url.path.endsWith('/api/auto-reply/settings')) {
              if (method == 'PUT') {
                return http.Response(
                  putBody ?? _settingsBody,
                  200,
                  headers: _json,
                );
              }
              if (!settingsOk) {
                return http.Response(
                  '{"error":"core unavailable","code":"unavailable"}',
                  502,
                  headers: _json,
                );
              }
              return http.Response(_settingsBody, 200, headers: _json);
            }
            if (url.path.endsWith('/api/ai-configs')) {
              return http.Response(
                '{"configs":[{"id":"cfg-1","provider":"openai",'
                '"model":"gpt-4o-mini","is_enabled":true}]}',
                200,
                headers: _json,
              );
            }
            if (url.path.endsWith('/api/auto-reply/prompts')) {
              return http.Response(
                '{"prompts":[{"id":"p-1","name":"Welcome",'
                '"user_prompt_template":"Answer: {text}","is_default":true}],'
                '"total":1}',
                200,
                headers: _json,
              );
            }
            return http.Response('{"error":"unexpected"}', 500, headers: _json);
          },
    );

Future<void> _pump(WidgetTester tester, AppState state) async {
  tester.view.physicalSize = const Size(1100, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
      value: state,
      child: const MaterialApp(
        locale: Locale('en'),
        localizationsDelegates: [
          AppMessages.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppMessages.supportedLocales,
        home: Scaffold(body: AutoReplySettingsView()),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  final t = AppMessages(const Locale('en'));

  testWidgets(
    'renders the master switch and the settings the server returned',
    (tester) async {
      final log = <_Sent>[];
      await _pump(tester, _state(log));

      expect(find.text(t.get('ar_master_switch')), findsWidgets);
      expect(
        find.byKey(const ValueKey<String>('ar_master_switch')),
        findsOneWidget,
      );
      // The picker labels a configuration by provider and model, not by id.
      expect(find.text('openai · gpt-4o-mini'), findsOneWidget);
      // The reply-behaviour section is there, so the form is not a spinner.
      expect(find.text(t.get('ar_section_reply')), findsOneWidget);
      expect(find.text(t.get('ar_tone')), findsOneWidget);
      expect(find.text(t.get('ar_temperature')), findsOneWidget);

      // First paint reads settings, configs and prompts; no write.
      expect(log.where((c) => c.method != 'GET'), isEmpty);
    },
  );

  testWidgets('toggling the master switch sends exactly one masked PUT', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pump(
      tester,
      _state(
        log,
        putBody: _settingsBody.replaceFirst(
          '"enabled":false',
          '"enabled":true',
        ),
      ),
    );

    final toggle = find.byKey(const ValueKey<String>('ar_master_switch'));
    await tester.tap(toggle);
    await tester.pumpAndSettle();

    final writes = log
        .where((c) => c.path.endsWith('/api/auto-reply/settings'))
        .where((c) => c.method == 'PUT')
        .toList(growable: false);

    expect(writes, hasLength(1), reason: 'one toggle is one write');
    expect(writes.single.body['update_mask'], orderedEquals(const ['enabled']));
    expect(writes.single.body['enabled'], isTrue);

    // The switch now reflects the server's answer, not just the tap.
    final control = tester.widget<Switch>(toggle);
    expect(control.value, isTrue);
  });

  testWidgets('a refused settings read is a banner, never an empty form', (
    tester,
  ) async {
    final log = <_Sent>[];
    await _pump(tester, _state(log, settingsOk: false));

    // The server's own message is shown, because it says why the read failed.
    expect(find.text('core unavailable'), findsOneWidget);
    expect(
      find.byKey(const ValueKey<String>('ar_master_switch')),
      findsNothing,
    );
  });
}
