/// Application root widget with theme, localization and auth routing.
library;

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:provider/provider.dart';

import 'core/app_state.dart';
import 'core/theme.dart';
import 'features/auth/login_screen.dart';
import 'features/home/home_screen.dart';
import 'l10n/app_messages.dart';

class AutoTelegramForwardApp extends StatelessWidget {
  const AutoTelegramForwardApp({super.key});

  @override
  Widget build(BuildContext context) {
    final state = context.watch<AppState>();

    return MaterialApp(
      title: 'AutoTelegramForward',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light(),
      darkTheme: AppTheme.dark(),
      themeMode: state.themeMode,
      locale: state.locale,
      supportedLocales: AppMessages.supportedLocales,
      localizationsDelegates: const [
        AppMessages.delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      home: _buildHome(state),
    );
  }

  Widget _buildHome(AppState state) {
    switch (state.status) {
      case SessionStatus.unknown:
        return const Scaffold(
          body: Center(child: CircularProgressIndicator()),
        );
      case SessionStatus.authenticated:
        return const HomeScreen();
      case SessionStatus.expired:
        return const LoginScreen(reason: LoginReason.expired);
      case SessionStatus.unauthenticated:
        return const LoginScreen(reason: LoginReason.fresh);
    }
  }
}
