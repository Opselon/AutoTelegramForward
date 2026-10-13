/// Settings screen: theme, language (EN / FA RTL), server switch, logout.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../core/env.dart';
import '../../l10n/app_messages.dart';

class SettingsScreen extends StatelessWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final state = context.watch<AppState>();

    return Scaffold(
      appBar: AppBar(title: Text(t.get('settings_title'))),
      body: ListView(
        children: [
          // Server Section
          ListTile(
            title: Text(
              t.get('settings_server'),
              style: const TextStyle(fontWeight: FontWeight.bold),
            ),
          ),
          ListTile(
            leading: const Icon(Icons.dns_outlined),
            title: Text(t.get('settings_server_current')),
            subtitle: Text(state.serverUrl),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => _changeServer(context),
          ),
          const Divider(),

          // Appearance & Language
          ListTile(
            title: Text(
              t.get('settings_language'),
              style: const TextStyle(fontWeight: FontWeight.bold),
            ),
          ),
          RadioListTile<String>(
            title: const Text('English'),
            value: 'en',
            groupValue: state.locale.languageCode,
            onChanged: (v) {
              if (v != null) state.setLocale(Locale(v));
            },
          ),
          RadioListTile<String>(
            title: const Text('فارسی (Persian)'),
            value: 'fa',
            groupValue: state.locale.languageCode,
            onChanged: (v) {
              if (v != null) state.setLocale(Locale(v));
            },
          ),
          const Divider(),

          // Theme
          ListTile(
            title: Text(
              t.get('settings_theme'),
              style: const TextStyle(fontWeight: FontWeight.bold),
            ),
          ),
          RadioListTile<ThemeMode>(
            title: Text(t.get('settings_theme_system')),
            value: ThemeMode.system,
            groupValue: state.themeMode,
            onChanged: (v) {
              if (v != null) state.setThemeMode(v);
            },
          ),
          RadioListTile<ThemeMode>(
            title: Text(t.get('settings_theme_light')),
            value: ThemeMode.light,
            groupValue: state.themeMode,
            onChanged: (v) {
              if (v != null) state.setThemeMode(v);
            },
          ),
          RadioListTile<ThemeMode>(
            title: Text(t.get('settings_theme_dark')),
            value: ThemeMode.dark,
            groupValue: state.themeMode,
            onChanged: (v) {
              if (v != null) state.setThemeMode(v);
            },
          ),
          const Divider(),

          // Account & Session
          ListTile(
            leading: const Icon(Icons.person_outline),
            title: Text(
              state.username.isNotEmpty
                  ? state.username
                  : 'User #${state.userId}',
            ),
            subtitle: Text(state.isAdmin ? 'Administrator' : 'Standard User'),
          ),
          ListTile(
            leading: const Icon(Icons.logout, color: Colors.red),
            title: Text(
              t.get('settings_logout'),
              style: const TextStyle(color: Colors.red),
            ),
            onTap: () => _confirmLogout(context),
          ),
          const Divider(),

          // About
          ListTile(
            title: Text(
              t.get('settings_about'),
              style: const TextStyle(fontWeight: FontWeight.bold),
            ),
          ),
          ListTile(
            leading: const Icon(Icons.info_outline),
            title: Text(t.get('app_name')),
            subtitle: Text('${t.get('settings_version')}: 1.4.1'),
          ),
        ],
      ),
    );
  }

  void _changeServer(BuildContext context) {
    final state = context.read<AppState>();
    final ctrl = TextEditingController(text: state.serverUrl);
    final t = AppMessages.of(context);

    showDialog<void>(
      context: context,
      builder:
          (ctx) => AlertDialog(
            title: Text(t.get('login_server_change')),
            content: TextField(
              controller: ctrl,
              decoration: InputDecoration(hintText: 'https://atf.example.com'),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx),
                child: Text(t.get('action_cancel')),
              ),
              ElevatedButton(
                onPressed: () async {
                  final parsed = ServerUrl.tryParse(ctrl.text);
                  if (parsed == null) {
                    ScaffoldMessenger.of(context).showSnackBar(
                      SnackBar(content: Text(t.get('login_server_invalid'))),
                    );
                    return;
                  }
                  Navigator.pop(ctx);
                  await state.setServerUrl(parsed.normalized);
                },
                child: Text(t.get('action_save')),
              ),
            ],
          ),
    );
  }

  Future<void> _confirmLogout(BuildContext context) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder:
          (ctx) => AlertDialog(
            title: Text(t.get('settings_logout')),
            content: Text(t.get('settings_logout_confirm')),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx, false),
                child: Text(t.get('action_cancel')),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
                onPressed: () => Navigator.pop(ctx, true),
                child: Text(t.get('action_confirm')),
              ),
            ],
          ),
    );

    if (confirmed == true && context.mounted) {
      await context.read<AppState>().logout();
    }
  }
}
