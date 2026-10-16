/// Widget test for the System screen, driven by the real captured payload.
///
/// The screen builds its own client, so the test injects a transport through
/// `AppState.transportOverride` and serves `contracts/fixtures/system_setup.json`
/// verbatim. That is the point: the assertion runs against the bytes the API
/// really returns, not against a hand-written mock that can drift from it.
library;

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/system/system_status_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://official.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
  appVersion: '1.5.0',
);

String _fixture(String name) =>
    File('../contracts/fixtures/$name.json').readAsStringSync();

AppState _state(HttpTransport transport) => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: transport,
);

HttpTransport _serving(String payload, {int status = 200}) =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async => http.Response(
      payload,
      status,
      headers: {'content-type': 'application/json'},
    );

Future<void> _pump(
  WidgetTester tester,
  AppState state, {
  Locale? locale,
}) async {
  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
      value: state,
      child: MaterialApp(
        locale: locale ?? const Locale('en'),
        localizationsDelegates: const [
          AppMessages.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppMessages.supportedLocales,
        home: const SystemStatusScreen(),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'renders readiness, services and the contract from the live payload',
    (tester) async {
      await _pump(tester, _state(_serving(_fixture('system_setup'))));

      // Verdict comes from the payload's `ready` flag.
      expect(find.text('Ready'), findsWidgets);
      // Services are named and probed.
      expect(find.text('CORE'), findsOneWidget);
      expect(find.text('LOGGER'), findsOneWidget);
      // Capability chips and the contract sit below the fold in a lazy list,
      // so the test scrolls to them the way a user would.
      await tester.scrollUntilVisible(
        find.text('Capabilities'),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      expect(find.text('rules'), findsWidgets);

      // Contract keys are shown by name, never their values.
      await tester.scrollUntilVisible(
        find.text('ATF_CORE_GRPC'),
        300,
        scrollable: find.byType(Scrollable).first,
      );
      expect(find.text('ATF_CORE_GRPC'), findsOneWidget);
    },
  );

  testWidgets('an older server is reported as a version gap, not a failure', (
    tester,
  ) async {
    await _pump(tester, _state(_serving('{}', status: 404)));

    expect(find.textContaining('does not expose'), findsOneWidget);
  });

  testWidgets('renders the localized verdict in Italian', (tester) async {
    await _pump(
      tester,
      _state(_serving(_fixture('system_setup'))),
      locale: const Locale('it'),
    );

    expect(find.text('Pronto'), findsWidgets);
    expect(find.textContaining('Microservizi'), findsOneWidget);
    // Persian must never appear in an Italian render.
    expect(find.textContaining('سیستم'), findsNothing);
  });

  testWidgets(
    'a transport failure keeps the screen chrome instead of blanking',
    (tester) async {
      final state = _state(
        (
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async => throw http.ClientException('Connection refused', url),
      );
      await _pump(tester, state);

      // An unreachable server shows an actionable error; the screen must still
      // render its title rather than going blank.
      expect(find.text('System'), findsWidgets);
      expect(find.textContaining('Connection refused'), findsWidgets);
    },
  );
}
