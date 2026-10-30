/// Production entry point for the AutoTelegramForward mobile application.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import 'app.dart';
import 'core/app_state.dart';
import 'core/env.dart';
import 'core/secure_storage_prod.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Draw behind the Android system bars: the M3 surface colour extends under
  // the status and navigation bars instead of the app sitting in a letterboxed
  // rectangle. Flutter's MediaQuery already reports the bar insets, and every
  // screen consumes them through SafeArea/Scaffold, so this is a pure visual
  // upgrade with no layout change. On iOS the system already draws edge to
  // edge; the call is a no-op there.
  await SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  // Match the status-bar icons to the app's surfaces: dark icons on the light
  // theme and light icons in dark mode (the platform brightness decides until
  // the user picks a fixed theme, at which point the AppBar theme governs).
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarBrightness: Brightness.light,
      statusBarIconBrightness: Brightness.dark,
    ),
  );

  final env = await AppEnv.load();
  final tokenStore = SecureStorageProd();
  final preferenceStore = PreferenceStoreProd();

  final appState = AppState(
    env: env,
    tokenStore: tokenStore,
    preferenceStore: preferenceStore,
  );

  await appState.restore();

  runApp(
    ChangeNotifierProvider<AppState>.value(
      value: appState,
      child: const AutoTelegramForwardApp(),
    ),
  );
}
