/// The chat list must stay virtualized.
///
/// A Telegram account can hold thousands of chats. The page used to render
/// every row it had: a `Column` of tiles inside the page's `ListView`, so a
/// 400-chat payload built and laid out 400 rows on every frame — the classic
/// "the list is fine until the account is not new" regression.
///
/// This test pins the fix: the payload is 400 chats, the viewport is a phone,
/// and only the rows in view (plus the cache margin) may be mounted. Counting
/// mounted rows is the measurement — a golden or a screenshot would not catch
/// a slow list, and a comment cannot fail a build.
library;

import 'dart:io';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/core/theme.dart';
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

/// How many chats the fake account has.
const _chatCount = 400;

String get _chatsPayload {
  final rows = <String>[
    for (var i = 0; i < _chatCount; i++)
      '{"chat_id":"-10010$i","title":"Channel $i","username":"chan_$i",'
          '"chat_type":"channel","members":${100 + i}}',
  ];
  return '{"chats":[${rows.join(',')}],"count":$_chatCount,'
      '"session_id":"s1","total":$_chatCount,"truncated":false}';
}

HttpTransport _transport() =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async {
      if (url.path.endsWith('/chats')) {
        return http.Response(_chatsPayload, 200, headers: _json);
      }
      if (url.path == '/api/sessions') {
        // The captured live payload: the page needs a session before it will
        // ask for that session's chats.
        return http.Response(
          File('test/fixtures/live_api/sessions.json').readAsStringSync(),
          200,
          headers: _json,
        );
      }
      if (url.path == '/api/rules') {
        return http.Response(
          File('test/fixtures/live_api/rules.json').readAsStringSync(),
          200,
          headers: _json,
        );
      }
      return http.Response('{}', 200, headers: _json);
    };

/// Rows the viewport has actually built, by tile key.
int _mountedTiles(WidgetTester tester) {
  final finder = find.byWidgetPredicate((widget) {
    final key = widget.key;
    return key is ValueKey<String> && key.value.startsWith('chat_tile_');
  });
  return finder.evaluate().length;
}

void main() {
  testWidgets('a 400-chat payload mounts only the rows in view', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final state = AppState(
      env: _env,
      tokenStore: MemoryTokenStore(),
      preferenceStore: MemoryPreferenceStore(),
      transportOverride: _transport(),
    );
    await state.restore();

    await tester.pumpWidget(
      ChangeNotifierProvider<AppState>.value(
        value: state,
        child: MaterialApp(
          theme: AppTheme.dark(),
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
      ),
    );
    await tester.pumpAndSettle();

    expect(
      find.byType(CustomScrollView),
      findsOneWidget,
      reason: 'the page must scroll as slivers, not as a Column of tiles',
    );

    final firstFrame = _mountedTiles(tester);
    expect(
      firstFrame,
      greaterThan(0),
      reason: 'the list must actually render something',
    );
    expect(
      firstFrame,
      lessThan(40),
      reason:
          'a phone viewport cannot show 400 chats; $firstFrame rows mounted '
          'means the list is building its whole payload',
    );

    debugPrint(
      'virtualization: $_chatCount chats, $firstFrame rows mounted on the '
      'first frame',
    );

    // Scrolling to the end must recycle, not accumulate: the mounted count
    // stays bounded even though 400 distinct chats have now been on screen.
    await tester.fling(
      find.byType(CustomScrollView),
      const Offset(0, -6000),
      4000,
    );
    await tester.pumpAndSettle();

    final afterScroll = _mountedTiles(tester);
    debugPrint(
      'virtualization: $afterScroll rows mounted after a 6000 px fling',
    );
    expect(
      afterScroll,
      lessThan(60),
      reason: 'rows must be recycled while scrolling, not kept mounted',
    );
  });
}
