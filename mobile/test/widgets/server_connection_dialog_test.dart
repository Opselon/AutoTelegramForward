import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';
import 'package:autoforward_mobile/widgets/server_connection_dialog.dart';

AppEnv _testEnv() => const AppEnv(
  apiBaseUrl: 'https://official.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

AppState _testState() {
  final state = AppState(
    env: _testEnv(),
    tokenStore: MemoryTokenStore(),
    preferenceStore: MemoryPreferenceStore(),
  );
  return state;
}

Future<void> _pumpDialog(
  WidgetTester tester,
  AppState state, {
  HttpTransportForTest? transport,
}) async {
  await state.restore();
  await tester.pumpWidget(
    ChangeNotifierProvider<AppState>.value(
      value: state,
      child: MaterialApp(
        locale: const Locale('en'),
        localizationsDelegates: const [
          AppMessages.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        supportedLocales: AppMessages.supportedLocales,
        home: Builder(
          builder:
              (context) => Scaffold(
                body: ElevatedButton(
                  onPressed:
                      () => showServerConnectionDialog(
                        context,
                        transport: transport,
                      ),
                  child: const Text('open'),
                ),
              ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
}

typedef HttpTransportForTest =
    Future<http.Response> Function(
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    });

void main() {
  group('ServerConnectionDialog', () {
    testWidgets('shows reachable state with versions after test', (
      tester,
    ) async {
      final state = _testState();
      await _pumpDialog(
        tester,
        state,
        transport: (
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async {
          expect(headers?['authorization'], isNull);
          return http.Response(
            jsonEncode({'product_version': '1.5.0', 'api_version': '1.5.0'}),
            200,
          );
        },
      );

      expect(find.byKey(const Key('server_probe_button')), findsOneWidget);
      await tester.tap(find.byKey(const Key('server_probe_button')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('server_probe_ok')), findsOneWidget);
      expect(find.textContaining('1.5.0'), findsWidgets);
    });

    testWidgets('shows unreachable error for connection failure', (
      tester,
    ) async {
      final state = _testState();
      await _pumpDialog(
        tester,
        state,
        transport: (
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async {
          throw http.ClientException('Connection refused', url);
        },
      );

      await tester.tap(find.byKey(const Key('server_probe_button')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('server_probe_error')), findsOneWidget);
      expect(find.textContaining('Cannot reach'), findsOneWidget);
    });

    testWidgets('shows incompatible warning for outdated server', (
      tester,
    ) async {
      final state = _testState();
      await _pumpDialog(
        tester,
        state,
        transport: (
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async {
          return http.Response(
            jsonEncode({'product_version': '0.9.0', 'api_version': '0.9.0'}),
            200,
          );
        },
      );

      await tester.tap(find.byKey(const Key('server_probe_button')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('server_probe_error')), findsOneWidget);
      expect(find.textContaining('older'), findsOneWidget);
    });

    testWidgets('restore default fills the official URL and saves it', (
      tester,
    ) async {
      final state = _testState();
      await state.restore();
      await state.setServerUrl('https://selfhosted.example.com/');
      await tester.pumpWidget(
        ChangeNotifierProvider<AppState>.value(
          value: state,
          child: MaterialApp(
            locale: const Locale('en'),
            localizationsDelegates: const [
              AppMessages.delegate,
              GlobalMaterialLocalizations.delegate,
              GlobalWidgetsLocalizations.delegate,
              GlobalCupertinoLocalizations.delegate,
            ],
            supportedLocales: AppMessages.supportedLocales,
            home: Builder(
              builder:
                  (context) => Scaffold(
                    body: ElevatedButton(
                      onPressed:
                          () => showServerConnectionDialog(
                            context,
                            transport: (
                              Uri url, {
                              required String method,
                              Map<String, String>? headers,
                              Object? body,
                              required Duration timeout,
                            }) async {
                              return http.Response(
                                jsonEncode({
                                  'product_version': '1.5.0',
                                  'api_version': '1.5.0',
                                }),
                                200,
                              );
                            },
                          ),
                      child: const Text('open'),
                    ),
                  ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('server_restore_default_button')));
      await tester.pumpAndSettle();

      final field = tester.widget<TextField>(
        find.byKey(const Key('server_url_field')),
      );
      expect(field.controller?.text, state.env.defaultBaseUrl);

      await tester.tap(find.byKey(const Key('server_save_button')));
      await tester.pumpAndSettle();

      expect(state.serverUrl, state.env.defaultBaseUrl);
    });
  });
}
