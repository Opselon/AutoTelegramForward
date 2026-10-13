/// Production entry point for the AutoTelegramForward mobile application.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'app.dart';
import 'core/app_state.dart';
import 'core/env.dart';
import 'core/secure_storage_prod.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

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
