/// The navigation shell must adapt: seven destinations do not fit a Material
/// bottom bar on a phone, so narrow windows get a NavigationBar and wide
/// windows get a labelled NavigationRail. This locks that behaviour in.
library;

import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/core/theme.dart';
import 'package:autoforward_mobile/features/home/home_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

AppEnv _testEnv() => const AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
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
  );
  await s.restore();
  return s;
}

void main() {
  testWidgets('narrow window uses the bottom navigation bar', (tester) async {
    tester.view.physicalSize = const Size(400, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pump();

    expect(find.byType(NavigationBar), findsOneWidget);
    expect(find.byType(NavigationRail), findsNothing);
    // All seven destinations are reachable.
    final bar = tester.widget<NavigationBar>(find.byType(NavigationBar));
    expect(bar.destinations, hasLength(7));
  });

  testWidgets('wide window uses the labelled navigation rail', (tester) async {
    tester.view.physicalSize = const Size(1400, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_shell(await _state()));
    await tester.pump();

    expect(find.byType(NavigationRail), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
    final rail = tester.widget<NavigationRail>(find.byType(NavigationRail));
    expect(rail.destinations, hasLength(7));
  });
}
