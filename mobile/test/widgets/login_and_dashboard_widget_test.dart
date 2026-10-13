import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:autoforward_mobile/core/app_state.dart';
import 'package:autoforward_mobile/core/env.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:autoforward_mobile/features/auth/login_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

AppEnv _testEnv() => const AppEnv(
  apiBaseUrl: 'https://atf.example.com/',
  apiBaseUrlDev: 'http://10.0.2.2:8080/',
  minSupportedApiVersion: '1.0.0',
  documentationUrl: 'https://example.com/docs',
  repositoryUrl: 'https://example.com/repo',
);

Widget createTestApp({required AppState state, required Widget home}) {
  return ChangeNotifierProvider<AppState>.value(
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
      home: home,
    ),
  );
}

void main() {
  testWidgets('LoginScreen renders fields and validates empty inputs', (
    tester,
  ) async {
    final state = AppState(
      env: _testEnv(),
      tokenStore: MemoryTokenStore(),
      preferenceStore: MemoryPreferenceStore(),
    );
    await state.restore();

    await tester.pumpWidget(
      createTestApp(state: state, home: const LoginScreen()),
    );
    await tester.pumpAndSettle();

    expect(find.byType(TextFormField), findsNWidgets(2)); // username & password
    expect(find.byType(ElevatedButton), findsOneWidget); // Sign In button
  });
}
