/// The Chats page is the read side of routing: which channels, groups and users
/// the account has, and what routes through each one.
///
/// It broke in the field in two ways that are worth locking down separately:
///
///  1. Rule parsing threw on the server's own payload (`topic_id: ""`), and the
///     page loads rules and sessions in one `Future.wait`. A `TypeError` is not
///     an `ApiException`, so nothing caught it — the page stayed on its spinner
///     and the chat list, the users and the groups never loaded.
///  2. Even a legitimate failure of that secondary request must not be able to
///     take the primary content down with it: the rules are only there to badge
///     each chat with its usage count.
///
/// The payloads here are the byte-for-byte captures in `test/fixtures/live_api/`
/// that reproduced the field failure.
library;

import 'dart:io';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/chats/chats_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

const _env = AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

const _json = {'content-type': 'application/json; charset=utf-8'};

String _fixture(String name) =>
    File('test/fixtures/live_api/$name').readAsStringSync();

typedef _Sent = ({String method, String path});

/// The live stack, as the client saw it: the captured rule payload (with
/// `topic_id: ""`) and the captured chat list. [rulesStatus] simulates the
/// secondary request failing on its own.
HttpTransport _transport(List<_Sent> log, {int rulesStatus = 200}) =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async {
      log.add((method: method, path: url.path));
      if (url.path == '/api/rules') {
        if (rulesStatus != 200) {
          return http.Response(
            '{"error":"rules unavailable"}',
            rulesStatus,
            headers: _json,
          );
        }
        return http.Response(_fixture('rules.json'), 200, headers: _json);
      }
      if (url.path == '/api/sessions') {
        return http.Response(_fixture('sessions.json'), 200, headers: _json);
      }
      if (url.path.endsWith('/chats')) {
        return http.Response(_fixture('chats.json'), 200, headers: _json);
      }
      return http.Response(
        '{"error":"unexpected ${url.path}"}',
        404,
        headers: _json,
      );
    };

AppState _state(List<_Sent> log, {int rulesStatus = 200}) => AppState(
  env: _env,
  tokenStore: MemoryTokenStore(),
  preferenceStore: MemoryPreferenceStore(),
  transportOverride: _transport(log, rulesStatus: rulesStatus),
);

Widget _app(AppState state) => ChangeNotifierProvider<AppState>.value(
  value: state,
  child: MaterialApp(
    theme: ThemeData.dark(),
    locale: const Locale('en'),
    localizationsDelegates: const [
      AppMessages.delegate,
      GlobalMaterialLocalizations.delegate,
      GlobalWidgetsLocalizations.delegate,
      GlobalCupertinoLocalizations.delegate,
    ],
    supportedLocales: AppMessages.supportedLocales,
    home: const ChatsScreen(),
  ),
);

Finder _key(String key) => find.byKey(ValueKey<String>(key));

Future<List<_Sent>> _pumpChats(
  WidgetTester tester, {
  int rulesStatus = 200,
}) async {
  tester.view.physicalSize = const Size(1000, 2400);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  final log = <_Sent>[];
  await tester.pumpWidget(_app(_state(log, rulesStatus: rulesStatus)));
  await tester.pumpAndSettle();
  return log;
}

void main() {
  testWidgets(
    'the channels, groups and users load on the server\'s own payload',
    (tester) async {
      final log = await _pumpChats(tester);

      // The page asked the server for exactly what it needs.
      expect(log.map((e) => e.path), contains('/api/sessions'));
      expect(log.map((e) => e.path), contains('/api/rules'));
      expect(
        log.map((e) => e.path),
        contains('/api/sessions/1d12518c-039c-437a-aebd-7b9b7c720a18/chats'),
      );

      // ...and rendered the chats instead of staying on the spinner.
      expect(_key('chats_row_-1004400615270'), findsOneWidget);
      expect(_key('chats_row_-1003915313919'), findsOneWidget);
      expect(_key('chats_row_-1001957768371'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(
        find.text(AppMessages(const Locale('en')).get('chats_empty')),
        findsNothing,
      );
    },
  );

  testWidgets('the type filters narrow the list to each kind of chat', (
    tester,
  ) async {
    await _pumpChats(tester);

    // All six captured chats are channels.
    await tester.tap(_key('chats_filter_channels'));
    await tester.pumpAndSettle();
    expect(_key('chats_row_-1004400615270'), findsOneWidget);

    // No users in this account: the filter says so instead of showing channels.
    await tester.tap(_key('chats_filter_users'));
    await tester.pumpAndSettle();
    expect(_key('chats_row_-1004400615270'), findsNothing);
    expect(
      find.text(
        AppMessages(const Locale('en')).get('chats_picker_filtered_empty'),
      ),
      findsOneWidget,
    );
  });

  testWidgets('a failing secondary request does not blank the chat list', (
    tester,
  ) async {
    final log = await _pumpChats(tester, rulesStatus: 500);

    expect(log.map((e) => e.path), contains('/api/rules'));
    // Rules are only used for the usage badges; their failure must not stop the
    // chats from loading, and it must not leave the page on a spinner either.
    expect(_key('chats_row_-1004400615270'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets(
    'a server that cannot list chats explains itself and stays usable',
    (tester) async {
      tester.view.physicalSize = const Size(1000, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);

      final state = AppState(
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
              if (url.path == '/api/sessions') {
                return http.Response(
                  _fixture('sessions.json'),
                  200,
                  headers: _json,
                );
              }
              if (url.path == '/api/rules') {
                return http.Response(
                  _fixture('rules.json'),
                  200,
                  headers: _json,
                );
              }
              return http.Response(
                '{"error":"not found"}',
                404,
                headers: _json,
              );
            },
      );
      await tester.pumpWidget(_app(state));
      await tester.pumpAndSettle();

      final t = AppMessages(const Locale('en'));
      expect(find.textContaining(t.get('chats_unavailable')), findsWidgets);
      // The refresh affordance is still there, so the page is not a dead end.
      expect(_key('chats_refresh'), findsOneWidget);
    },
  );
}
