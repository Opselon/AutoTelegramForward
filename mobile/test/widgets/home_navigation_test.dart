/// The navigation shell must adapt: nine labelled destinations do not fit a
/// phone's width.
///
/// Material's [NavigationBar] gives every destination an equal slice of the
/// bar, so with nine entries each slice is narrower than its label: the first
/// and last labels were clipped by the screen edges and the row overflowed the
/// viewport. Narrow windows now get a horizontally scrollable bar of
/// fixed-width destinations — every entry stays reachable at its full label —
/// and wide windows keep the labelled [NavigationRail]. This locks that in.
library;

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/core/theme.dart';
import 'package:autoforward_mobile/features/chats/chats_screen.dart';
import 'package:autoforward_mobile/features/home/home_screen.dart';
import 'package:autoforward_mobile/features/settings/settings_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';

AppEnv _testEnv() => const AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

/// An empty-but-valid server: the shell is what is under test, not the screens
/// it holds, and a screen that cannot reach its endpoint must not be able to
/// fail this test for the wrong reason.
HttpTransport _quietTransport() =>
    (
      Uri url, {
      required String method,
      Map<String, String>? headers,
      Object? body,
      required Duration timeout,
    }) async => http.Response(
      '{}',
      200,
      headers: const {'content-type': 'application/json'},
    );

Widget _shell(AppState state) {
  return ChangeNotifierProvider<AppState>.value(
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
      home: const HomeScreen(),
    ),
  );
}

Future<AppState> _state() async {
  final s = AppState(
    env: _testEnv(),
    tokenStore: MemoryTokenStore(),
    preferenceStore: MemoryPreferenceStore(),
    transportOverride: _quietTransport(),
  );
  await s.restore();
  return s;
}

Finder _key(String key) => find.byKey(ValueKey<String>(key));

/// The nine destinations, in order, by the l10n key of their label.
const _labels = [
  'nav_dashboard',
  'nav_rules',
  'nav_chats',
  'nav_sessions',
  'nav_filters',
  'nav_ai',
  'nav_logs',
  'nav_delivery',
  'nav_settings',
];

void main() {
  testWidgets('a narrow window gets a scrollable bar with every destination '
      'reachable at its full label', (tester) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pump();

    // Nine entries cannot fit, so the bar scrolls rather than clipping: the
    // strip is present, and every destination is built inside it.
    expect(_key('nav_bar_scroll'), findsOneWidget);
    for (final label in _labels) {
      expect(_key('nav_item_$label'), findsOneWidget, reason: label);
    }

    // The last destination starts beyond the right edge of the 400dp viewport —
    // which is exactly the access the old equal-slice bar denied.
    expect(_key('nav_item_nav_dashboard'), findsOneWidget);
    final first = tester.getRect(_key('nav_item_nav_dashboard'));
    final last = tester.getRect(_key('nav_item_nav_settings'));
    expect(first.left, greaterThanOrEqualTo(0));
    expect(
      last.right,
      greaterThan(400),
      reason: 'with nine destinations the strip must be wider than the phone',
    );

    // Labels are one line and full-width: no wrapping mid-word, no clipping.
    final label = tester.widget<Text>(
      find.descendant(
        of: _key('nav_item_nav_dashboard'),
        matching: find.text(
          AppMessages(const Locale('en')).get('nav_dashboard'),
        ),
      ),
    );
    expect(label.maxLines, 1);
    expect(label.overflow, TextOverflow.ellipsis);

    // Scrolling the strip reaches the entries that were off-screen, and they
    // are tappable once there.
    await tester.drag(_key('nav_bar_scroll'), const Offset(-600, 0));
    await tester.pumpAndSettle();
    await tester.tap(_key('nav_item_nav_settings'));
    await tester.pumpAndSettle();

    expect(find.byType(SettingsScreen), findsOneWidget);
  });

  testWidgets('the bar is not used where it cannot fit: a wide window gets the '
      'labelled rail', (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pump();

    expect(find.byType(NavigationRail), findsOneWidget);
    expect(_key('nav_bar_scroll'), findsNothing);
    final rail = tester.widget<NavigationRail>(find.byType(NavigationRail));
    expect(rail.destinations, hasLength(_labels.length));
  });

  testWidgets('a selection survives a rotation into the other layout', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pump();

    await tester.tap(_key('nav_item_nav_chats'));
    await tester.pumpAndSettle();

    // Rotate to a wide window: the rail must show the same selected page.
    tester.view.physicalSize = const Size(1400, 900);
    await tester.pumpAndSettle();

    final rail = tester.widget<NavigationRail>(find.byType(NavigationRail));
    expect(rail.selectedIndex, 2);
    // Chats is the page on screen — the stacked, still-alive Dashboard is
    // off-stage and only reachable from the bar.
    expect(find.byType(ChatsScreen), findsOneWidget);
  });
}
