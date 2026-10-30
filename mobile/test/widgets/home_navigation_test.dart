/// The navigation shell must adapt, not just shrink.
///
/// Eleven labelled destinations cannot fit a Material bottom bar on a phone:
/// M3 recommends three to five, and eleven equal slices were narrower than
/// their own
/// labels (the first and last were clipped by the screen edges). The shell now
/// follows the size classes in `docs/ui-ux-system.md`:
///
///   * compact (< 600 dp) — four daily destinations plus **More**, which opens a
///     sheet with the rest, so every destination is still one tap away;
///   * medium/expanded (>= 600 dp) — the labelled [NavigationRail].
///
/// These tests lock in that contract, the 48 dp targets, and the fact that a
/// selection survives a rotation between the two layouts.
library;

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/layout.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/core/theme.dart';
import 'package:autoforward_mobile/features/ai/ai_screen.dart';
import 'package:autoforward_mobile/features/chats/chats_screen.dart';
import 'package:autoforward_mobile/features/home/home_screen.dart';
import 'package:autoforward_mobile/features/rules/rules_screen.dart';
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

/// The eleven destinations, in order, by the l10n key of their label.
const _labels = [
  'nav_dashboard',
  'nav_rules',
  'nav_replace',
  'nav_chats',
  'nav_sessions',
  'nav_filters',
  'nav_ai',
  'nav_auto_reply',
  'nav_logs',
  'nav_delivery',
  'nav_settings',
];

/// The six the bottom bar cannot hold; they live behind **More**.
const _secondary = [
  'nav_chats',
  'nav_sessions',
  'nav_filters',
  'nav_ai',
  'nav_auto_reply',
  'nav_delivery',
];

void main() {
  testWidgets('a compact window gets four daily destinations plus More, and '
      'every destination is one tap away', (tester) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pump();

    final bar = tester.widget<NavigationBar>(find.byType(NavigationBar));
    expect(bar.destinations, hasLength(5));
    expect(find.byType(NavigationRail), findsNothing);

    // Four primary slots, each a real NavigationDestination with its own key.
    for (final label in [
      'nav_dashboard',
      'nav_rules',
      'nav_logs',
      'nav_settings',
    ]) {
      expect(_key('nav_dest_$label'), findsOneWidget, reason: label);
    }
    expect(_key('nav_dest_more'), findsOneWidget);

    // Every visible slot is a 48 dp target: M3's minimum, and the a11y floor
    // in the UI contract.
    for (final label in [
      'nav_dashboard',
      'nav_rules',
      'nav_logs',
      'nav_settings',
    ]) {
      expect(
        tester.getSize(_key('nav_dest_$label')).height,
        greaterThanOrEqualTo(48),
        reason: '$label must be at least 48 dp tall',
      );
    }

    // Nothing is clipped: the bar lays out inside the window.
    expect(tester.takeException(), isNull);
    final barRect = tester.getRect(find.byType(NavigationBar));
    expect(barRect.right, lessThanOrEqualTo(400));

    // More opens a sheet naming the rest of the destinations.
    await tester.tap(_key('nav_dest_more'));
    await tester.pumpAndSettle();

    expect(_key('nav_more_sheet'), findsOneWidget);
    expect(
      find.text(AppMessages(const Locale('en')).get('nav_more_title')),
      findsOneWidget,
    );
    for (final label in _secondary) {
      expect(_key('nav_more_item_$label'), findsOneWidget, reason: label);
    }

    // Picking one opens its screen, closes the sheet, and the More slot then
    // names the page you are on instead of hiding it behind a generic label.
    await tester.tap(_key('nav_more_item_nav_ai'));
    await tester.pumpAndSettle();

    expect(_key('nav_more_sheet'), findsNothing);
    expect(find.byType(AiScreen), findsOneWidget);
    final after = tester.widget<NavigationBar>(find.byType(NavigationBar));
    expect(after.selectedIndex, 4);
    expect(
      find.descendant(
        of: _key('nav_dest_more'),
        matching: find.text(AppMessages(const Locale('en')).get('nav_ai')),
      ),
      findsOneWidget,
    );
  });

  testWidgets('the bar is not used where it cannot fit: a medium window gets '
      'the labelled rail with all eleven destinations', (tester) async {
    tester.view.physicalSize = const Size(700, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pump();

    expect(find.byType(NavigationBar), findsNothing);
    expect(find.byType(NavigationRail), findsOneWidget);
    final rail = tester.widget<NavigationRail>(find.byType(NavigationRail));
    expect(rail.destinations, hasLength(_labels.length));
    expect(rail.labelType, NavigationRailLabelType.all);

    // All eleven labels are present in the rail, none truncated into a sheet.
    final t = AppMessages(const Locale('en'));
    for (final label in _labels) {
      expect(find.text(t.get(label)), findsWidgets, reason: label);
    }
  });

  testWidgets('an expanded window keeps the rail and gives rules a two-pane '
      'master-detail', (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pumpAndSettle();

    expect(find.byType(NavigationRail), findsOneWidget);

    await tester.tap(
      find.descendant(
        of: find.byType(NavigationRail),
        matching: find.text(AppMessages(const Locale('en')).get('nav_rules')),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.byType(RulesScreen), findsOneWidget);
    expect(
      find.byType(MasterDetailPane),
      findsOneWidget,
      reason:
          'an expanded window shows the list beside the detail, not a stack',
    );
  });

  testWidgets('a selection survives a rotation into the other layout', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pump();

    await tester.tap(_key('nav_dest_more'));
    await tester.pumpAndSettle();
    await tester.tap(_key('nav_more_item_nav_chats'));
    await tester.pumpAndSettle();
    expect(find.byType(ChatsScreen), findsOneWidget);

    // Rotate to a wide window: the rail must show the same selected page.
    tester.view.physicalSize = const Size(1400, 900);
    await tester.pumpAndSettle();

    final rail = tester.widget<NavigationRail>(find.byType(NavigationRail));
    expect(rail.selectedIndex, 3);
    expect(find.byType(ChatsScreen), findsOneWidget);
  });
}
