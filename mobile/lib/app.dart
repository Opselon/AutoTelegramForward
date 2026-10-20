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
      // Text scaling is clamped to the range the layouts are verified at
      // (0.8–1.3, per the UI contract). Below 0.8 text is unreadable, and above
      // 1.3 the dense operational rows would have to clip; clamping here keeps
      // one honest rule instead of a different overflow per screen.
      builder: (context, child) {
        final media = MediaQuery.of(context);
        final scaled = media.textScaler.clamp(
          minScaleFactor: 0.8,
          maxScaleFactor: 1.3,
        );
        return MediaQuery(
          data: media.copyWith(textScaler: scaled),
          child: child ?? const SizedBox.shrink(),
        );
      },
      home: _buildHome(state),
    );
  }

  Widget _buildHome(AppState state) {
    switch (state.status) {
      case SessionStatus.unknown:
        return const Scaffold(body: Center(child: CircularProgressIndicator()));
      case SessionStatus.authenticated:
        return const HomeScreen();
      case SessionStatus.expired:
        return const LoginScreen(reason: LoginReason.expired);
      case SessionStatus.unauthenticated:
        return const LoginScreen(reason: LoginReason.fresh);
    }
  }
}
