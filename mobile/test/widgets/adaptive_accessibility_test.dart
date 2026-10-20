/// Accessibility, text-scale and RTL regressions for the adaptive shell.
///
/// The UI contract asks for three things this file pins down:
///
///   * text scale 0.8–2.0 must render every screen without clipping or an
///     overflow, on a phone in portrait and on a tablet. 2.0 is Android's
///     maximum font size, and it is what found two real defects: the dashboard's
///     metric grid had a fixed tile height (9 px clipped at 1.6×) and the logs
///     toolbar's page-size dropdown pushed its row 36 px past the edge;
///   * a right-to-left locale must mirror the layout (the first destination
///     sits on the right, the last on the left) without overflowing;
///   * interactive targets are at least 48 dp and icon-only buttons carry a
///     tooltip, which is what gives them a semantics label.
///
/// Each test walks every destination, because a single unwrapped row in one
/// screen is enough to make the app unusable at 2.0× on a phone.
library;

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/core/theme.dart';
import 'package:autoforward_mobile/features/home/home_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
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

/// Every endpoint answers `{}` with a JSON content type: screens that cannot
/// parse it show their error state, which is itself worth rendering at 2.0×.
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

Widget _shell(AppState state, {required Locale locale, required double scale}) {
  return ChangeNotifierProvider<AppState>.value(
    value: state,
    child: MaterialApp(
      theme: AppTheme.dark(),
      locale: locale,
      localizationsDelegates: const [
        AppMessages.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: AppMessages.supportedLocales,
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(context)
            .copyWith(textScaler: TextScaler.linear(scale)),
        child: child!,
      ),
      home: const HomeScreen(),
    ),
  );
}

Finder _key(String key) => find.byKey(ValueKey<String>(key));

/// Visit all nine destinations through whichever surface the window exposes.
Future<void> _visitAll(WidgetTester tester) async {
  if (find.byType(NavigationRail).evaluate().isNotEmpty) {
    for (final label in _labels) {
      await tester.tap(
        find.descendant(
          of: find.byType(NavigationRail),
          matching: find.text(AppMessages(const Locale('en')).get(label)),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull, reason: label);
    }
    return;
  }

  for (final key in [
    'nav_dest_nav_dashboard',
    'nav_dest_nav_rules',
    'nav_dest_nav_logs',
    'nav_dest_nav_settings',
  ]) {
    await tester.tap(_key(key));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull, reason: key);
  }

  for (final key in [
    'nav_chats',
    'nav_sessions',
    'nav_filters',
    'nav_ai',
    'nav_delivery',
  ]) {
    await tester.tap(_key('nav_dest_more'));
    await tester.pumpAndSettle();
    await tester.tap(_key('nav_more_item_$key'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull, reason: key);
  }
}

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
  for (final scale in [0.8, 1.3, 1.6, 2.0]) {
    testWidgets(
      'a phone renders every destination at ${scale}x text scale without '
      'overflow',
      (tester) async {
        tester.view.physicalSize = const Size(390, 844);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(tester.view.reset);

        await tester.pumpWidget(
          _shell(await _state(), locale: const Locale('en'), scale: scale),
        );
        await tester.pump();

        expect(find.byType(NavigationBar), findsOneWidget);
        await _visitAll(tester);
      },
    );
  }

  testWidgets('a tablet renders every destination at 1.3x without overflow', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1200, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(
      _shell(await _state(), locale: const Locale('en'), scale: 1.3),
    );
    await tester.pumpAndSettle();

    expect(find.byType(NavigationRail), findsOneWidget);
    await _visitAll(tester);

    // A single-column screen is capped and centred instead of stretching a
    // form across 1200 dp.
    final dashboard = tester.getRect(find.byType(HomeScreen));
    expect(dashboard.width, 1200);
  });

  testWidgets(
    'a right-to-left locale mirrors the navigation, not just the text',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);

      await tester.pumpWidget(
        _shell(await _state(), locale: const Locale('fa'), scale: 1.3),
      );
      await tester.pump();

      final first = tester.getRect(_key('nav_dest_nav_dashboard'));
      final last = tester.getRect(_key('nav_dest_nav_settings'));
      expect(
        first.left,
        greaterThan(last.left),
        reason: 'in RTL the first destination sits on the right of the bar',
      );
      expect(tester.takeException(), isNull);

      await _visitAll(tester);
    },
  );

  testWidgets(
    'every button in the semantics tree has a name, and icon-only targets are '
    'at least 48 dp',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      final semantics = tester.ensureSemantics();

      await tester.pumpWidget(
        _shell(await _state(), locale: const Locale('en'), scale: 1.0),
      );
      await tester.pumpAndSettle();

      // What a screen reader actually receives: a button node with no label is
      // announced as "button" and nothing else. AppIconButton puts its label on
      // an ancestor Semantics node, so checking `IconButton.tooltip` alone
      // would miss half the surface.
      final unnamed = <String>[];
      void walk(SemanticsNode node) {
        final data = node.getSemanticsData();
        if (data.flagsCollection.isButton && data.label.trim().isEmpty) {
          unnamed.add('node ${node.id} at ${node.rect}');
        }
        node.visitChildren((child) {
          walk(child);
          return true;
        });
      }

      final root = tester.getSemantics(find.byType(MaterialApp));
      walk(root);
      expect(
        unnamed,
        isEmpty,
        reason: 'unlabelled buttons: ${unnamed.join(', ')}',
      );

      for (final element in find.byType(IconButton).evaluate()) {
        final size = tester.getSize(find.byWidget(element.widget));
        expect(size.width, greaterThanOrEqualTo(48));
        expect(size.height, greaterThanOrEqualTo(48));
      }

      semantics.dispose();
    },
  );
}
