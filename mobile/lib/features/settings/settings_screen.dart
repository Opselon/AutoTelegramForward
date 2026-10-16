/// Settings screen: theme, language (all ten shipped locales), server switch,
/// logout.
///
/// The picker is generated from [AppMessages.supportedLocales] so a locale
/// cannot exist in the catalogs yet be unreachable in the UI - which is exactly
/// what happened when only English and Persian were listed here.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/app_state.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/server_connection_dialog.dart';

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
          Padding(
            padding: const EdgeInsets.symmetric(
              horizontal: AppSpacing.lg,
              vertical: AppSpacing.sm,
            ),
            child: Row(
              children: [
                const Icon(Icons.language_rounded, size: 16),
                const SizedBox(width: AppSpacing.sm),
                Expanded(
                  child: Text(
                    t.get('settings_language_hint'),
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
              ],
            ),
          ),
          // Since Flutter 3.32 a radio no longer carries its own groupValue /
          // onChanged: one RadioGroup owns the selection for the whole subtree
          // below it, so the group must be the ancestor of every option it
          // manages (and only of them).
          RadioGroup<String>(
            groupValue: state.locale.languageCode,
            onChanged: (v) {
              if (v != null) state.setLocale(Locale(v));
            },
            child: Column(
              children: [
                for (final locale in AppMessages.supportedLocales)
                  RadioListTile<String>(
                    dense: true,
                    title: Text(
                      t.get('lang_native_${locale.languageCode}'),
                      textDirection: TextDirection.ltr,
                    ),
                    subtitle: Text(locale.languageCode.toUpperCase()),
                    value: locale.languageCode,
                  ),
              ],
            ),
          ),
          const Divider(),

          // Theme
          ListTile(
            title: Text(
              t.get('settings_theme'),
              style: const TextStyle(fontWeight: FontWeight.bold),
            ),
          ),
          RadioGroup<ThemeMode>(
            groupValue: state.themeMode,
            onChanged: (v) {
              if (v != null) state.setThemeMode(v);
            },
            child: Column(
              children: [
                RadioListTile<ThemeMode>(
                  title: Text(t.get('settings_theme_system')),
                  value: ThemeMode.system,
                ),
                RadioListTile<ThemeMode>(
                  title: Text(t.get('settings_theme_light')),
                  value: ThemeMode.light,
                ),
                RadioListTile<ThemeMode>(
                  title: Text(t.get('settings_theme_dark')),
                  value: ThemeMode.dark,
                ),
              ],
            ),
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
            subtitle: Text(
              state.env.appVersion.isNotEmpty
                  ? '${t.get('settings_version')}: ${state.env.appVersion}'
                  : t.get('settings_version'),
            ),
          ),
        ],
      ),
    );
  }

  void _changeServer(BuildContext context) {
    showServerConnectionDialog(context);
  }

  Future<void> _confirmLogout(BuildContext context) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
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
