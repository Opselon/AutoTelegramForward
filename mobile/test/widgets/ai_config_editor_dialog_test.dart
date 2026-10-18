/// The AI config editor: the endpoint, the key, and the check.
///
/// The reported defect was "user cannot add the endpoint". Three things have to
/// hold for that to be true again, and each fails silently on its own:
///
/// * the provider list comes from the server's catalogue, so a provider offered
///   here is one the rewrite path accepts;
/// * the key field is write-only — typed, sent once, and never seeded from the
///   stored config (the server does not send one back);
/// * the check names its refusal: a blocked endpoint must not read as a bad key,
///   or the tenant "fixes" a key that was never the problem.
library;

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/models.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/ai/ai_config_editor_dialog.dart';
import 'package:autoforward_mobile/l10n/app_fa.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

class _CheckCall {
  _CheckCall(this.body);

  final Map<String, dynamic>? body;
}

class _Calls {
  final List<_CheckCall> checks = <_CheckCall>[];

  /// What the check endpoint answers; swapped per test.
  String checkBody =
      '{"success":true,"healthy":true,"latency_ms":215,'
      '"detail":"","error_code":""}';
}

HttpTransport _transport(_Calls calls) =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async {
      const json = {'content-type': 'application/json'};
      if (url.path.endsWith('/api/ai-configs/check-key')) {
        calls.checks.add(
          _CheckCall(body is Map ? body.cast<String, dynamic>() : null),
        );
        return http.Response(calls.checkBody, 200, headers: json);
      }
      if (url.path.endsWith('/api/ai-configs/providers')) {
        return http.Response(
          '{"providers":[{'
          '"id":"openai","label":"OpenAI","default_base_url":"https://api.openai.com/v1",'
          '"models":["gpt-4o-mini","gpt-4o"]},'
          '{"id":"groq","label":"Groq","default_base_url":"https://api.groq.com/openai/v1",'
          '"models":["llama-3.1-70b"]}]}',
          200,
          headers: json,
        );
      }
      return http.Response('{}', 200, headers: json);
    };

ApiClient _client(_Calls calls) => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: _transport(calls),
).buildApiClient();

Future<List<AiProviderInfo>> _catalogue(_Calls calls) =>
    _client(calls).listAiProviders();

Future<void> _pump(
  WidgetTester tester, {
  required _Calls calls,
  required List<AiProviderInfo> providers,
  AiConfig? existing,
  Locale locale = const Locale('en'),
}) async {
  tester.view.physicalSize = const Size(1000, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  await tester.pumpWidget(
    MaterialApp(
      locale: locale,
      localizationsDelegates: const <LocalizationsDelegate<dynamic>>[
        AppMessages.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppMessages.supportedLocales,
      home: Scaffold(
        body: AiConfigEditorDialog(
          existing: existing,
          providers: providers,
          client: _client(calls),
        ),
      ),
    ),
  );
  // A localization delegate resolves one frame late; without this the first
  // `find.text` sees nothing.
  await tester.pump();
}

void main() {
  testWidgets('the provider list is the server catalogue, not a local table', (
    tester,
  ) async {
    final calls = _Calls();
    final providers = await _catalogue(calls);
    await _pump(tester, calls: calls, providers: providers);

    await tester.tap(find.byKey(const ValueKey('ai_provider_select')));
    await tester.pumpAndSettle();

    // `groq` exists only in the catalogue payload.
    expect(find.text('Groq').hitTestable(), findsWidgets);
    expect(find.text('OpenAI').hitTestable(), findsWidgets);
  });

  testWidgets('a typed key and endpoint reach the check, and it says so', (
    tester,
  ) async {
    final calls = _Calls();
    final providers = await _catalogue(calls);
    await _pump(tester, calls: calls, providers: providers);

    await tester.enterText(
      find.widgetWithText(TextField, AppMessagesEn().get('ai_field_model')),
      'gpt-4o-mini',
    );
    await tester.enterText(
      find.byKey(const ValueKey('ai_api_key_field')),
      'sk-tenant-secret',
    );
    await tester.tap(find.byKey(const ValueKey('ai_check_button')));
    await tester.pumpAndSettle();

    expect(calls.checks, hasLength(1));
    expect(calls.checks.single.body?['api_key'], 'sk-tenant-secret');
    expect(calls.checks.single.body?['provider'], 'openai');
    expect(calls.checks.single.body?['model'], 'gpt-4o-mini');
    expect(find.textContaining('215 ms'), findsOneWidget);
  });

  testWidgets('a rejected key is named as a key, not as an endpoint', (
    tester,
  ) async {
    final calls = _Calls()
      ..checkBody =
          '{"success":false,"healthy":false,"latency_ms":90,'
          '"detail":"Incorrect API key provided: [REDACTED]",'
          '"error_code":"invalid_key"}';
    final providers = await _catalogue(calls);
    await _pump(tester, calls: calls, providers: providers);

    await tester.enterText(
      find.byKey(const ValueKey('ai_api_key_field')),
      'sk-wrong',
    );
    await tester.tap(find.byKey(const ValueKey('ai_check_button')));
    await tester.pumpAndSettle();

    expect(
      find.text(AppMessagesEn().get('ai_check_invalid_key')),
      findsOneWidget,
    );
    // The provider's own words come through, already redacted server-side.
    expect(find.textContaining('Incorrect API key'), findsOneWidget);
  });

  testWidgets('an unreachable endpoint is not reported as a bad key', (
    tester,
  ) async {
    final calls = _Calls()
      ..checkBody =
          '{"success":false,"healthy":false,"latency_ms":0,"detail":"",'
          '"error_code":"endpoint_blocked"}';
    final providers = await _catalogue(calls);
    await _pump(tester, calls: calls, providers: providers);

    await tester.tap(find.byKey(const ValueKey('ai_check_button')));
    await tester.pumpAndSettle();

    final t = AppMessagesEn();
    expect(find.text(t.get('ai_check_endpoint_blocked')), findsOneWidget);
    expect(find.text(t.get('ai_check_invalid_key')), findsNothing);
  });

  testWidgets('the stored key is never seeded back into the field', (
    tester,
  ) async {
    final calls = _Calls();
    final providers = await _catalogue(calls);
    await _pump(
      tester,
      calls: calls,
      providers: providers,
      existing: const AiConfig(
        id: 'cfg-1',
        provider: 'openai',
        model: 'gpt-4o-mini',
        baseUrl: 'https://gateway.example.com/v1',
        isEnabled: true,
      ),
    );

    final field = tester.widget<TextField>(
      find.byKey(const ValueKey('ai_api_key_field')),
    );
    expect(field.controller?.text, isEmpty);
    // An edit keeps the stored key rather than blanking it.
    expect(
      find.text(AppMessagesEn().get('ai_field_api_key_keep')),
      findsOneWidget,
    );
  });

  testWidgets('the verdict is shown in the UI language', (tester) async {
    final calls = _Calls()
      ..checkBody =
          '{"success":false,"healthy":false,"latency_ms":0,"detail":"",'
          '"error_code":"invalid_key"}';
    final providers = await _catalogue(calls);
    await _pump(
      tester,
      calls: calls,
      providers: providers,
      locale: const Locale('fa'),
    );

    await tester.tap(find.byKey(const ValueKey('ai_check_button')));
    await tester.pumpAndSettle();

    expect(
      find.text(AppMessagesFa.instance.get('ai_check_invalid_key')),
      findsOneWidget,
    );
    expect(
      find.text(AppMessagesEn().get('ai_check_invalid_key')),
      findsNothing,
    );
  });

  testWidgets('saving hands back a config that carries the typed key once', (
    tester,
  ) async {
    final calls = _Calls();
    final providers = await _catalogue(calls);
    AiConfig? popped;

    tester.view.physicalSize = const Size(1000, 2600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    // The dialog is pushed as a second route so its `Navigator.pop` has
    // somewhere to land and the test can read what it returned.
    await tester.pumpWidget(
      MaterialApp(
        locale: const Locale('en'),
        localizationsDelegates: const <LocalizationsDelegate<dynamic>>[
          AppMessages.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppMessages.supportedLocales,
        home: Builder(
          builder: (ctx) => Scaffold(
            body: ElevatedButton(
              onPressed: () async {
                popped = await showDialog<AiConfig>(
                  context: ctx,
                  builder: (_) => AiConfigEditorDialog(
                    providers: providers,
                    client: _client(calls),
                  ),
                );
              },
              child: const Text('open'),
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.enterText(
      find.widgetWithText(TextField, AppMessagesEn().get('ai_field_model')),
      'gpt-4o',
    );
    await tester.enterText(
      find.byKey(const ValueKey('ai_api_key_field')),
      'sk-typed',
    );
    await tester.tap(
      find.widgetWithText(ElevatedButton, AppMessagesEn().get('ai_create')),
    );
    await tester.pumpAndSettle();

    expect(popped, isNotNull);
    expect(popped!.model, 'gpt-4o');
    expect(popped!.apiKey, 'sk-typed');
    expect(popped!.toJson()['api_key'], 'sk-typed');
  });

  testWidgets('a blank key on an edit means keep the stored one', (
    tester,
  ) async {
    final calls = _Calls();
    final providers = await _catalogue(calls);
    AiConfig? popped;

    tester.view.physicalSize = const Size(1000, 2600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(
      MaterialApp(
        locale: const Locale('en'),
        localizationsDelegates: const <LocalizationsDelegate<dynamic>>[
          AppMessages.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppMessages.supportedLocales,
        home: Builder(
          builder: (ctx) => Scaffold(
            body: ElevatedButton(
              onPressed: () async {
                popped = await showDialog<AiConfig>(
                  context: ctx,
                  builder: (_) => AiConfigEditorDialog(
                    existing: const AiConfig(
                      id: 'cfg-1',
                      provider: 'openai',
                      model: 'gpt-4o-mini',
                      isEnabled: true,
                      version: 4,
                    ),
                    providers: providers,
                    client: _client(calls),
                  ),
                );
              },
              child: const Text('open'),
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(
      find.widgetWithText(ElevatedButton, AppMessagesEn().get('ai_save')),
    );
    await tester.pumpAndSettle();

    expect(popped, isNotNull);
    // Absent from the payload: the server keeps the key it already holds.
    expect(popped!.toJson().containsKey('api_key'), isFalse);
    expect(
      popped!.version,
      4,
      reason: 'the optimistic-concurrency version survives',
    );
  });
}
