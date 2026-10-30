/// Settings screen: theme, language (all ten shipped locales), server switch,
/// account (password change), logout.
///
/// The picker is generated from [AppMessages.supportedLocales] so a locale
/// cannot exist in the catalogs yet be unreachable in the UI - which is exactly
/// what happened when only English and Persian were listed here.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/design_tokens.dart';
import '../../core/layout.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_button.dart';
import '../../widgets/server_connection_dialog.dart';

class SettingsScreen extends StatelessWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final state = context.watch<AppState>();
    final palette = AppColors.of(context);

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
            trailing: Icon(AppIcons.forward(context)),
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
                  : t.get(
                      'settings_user_fallback',
                      params: {'id': '${state.userId}'},
                    ),
            ),
            subtitle: Text(
              state.isAdmin
                  ? t.get('settings_role_admin')
                  : t.get('settings_role_user'),
            ),
          ),
          ListTile(
            leading: const Icon(Icons.password_outlined),
            title: Text(t.get('settings_change_password')),
            subtitle: Text(t.get('settings_change_password_hint')),
            onTap: () => _changePassword(context),
          ),
          ListTile(
            leading: Icon(Icons.logout, color: palette.danger),
            title: Text(
              t.get('settings_logout'),
              style: TextStyle(color: palette.danger),
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

  Future<void> _changePassword(BuildContext context) async {
    final changed = await showDialog<bool>(
      context: context,
      builder: (_) => const _ChangePasswordDialog(),
    );
    if (changed != true || !context.mounted) return;
    // The saved credentials hold the old password for silent re-login; after
    // a successful change they are wrong by definition. Signing the user out
    // is the honest outcome — the dialog already said so.
    await context.read<AppState>().logout();
  }

  Future<void> _confirmLogout(BuildContext context) async {
    final t = AppMessages.of(context);
    final palette = AppColors.of(context);
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
            style: ElevatedButton.styleFrom(backgroundColor: palette.danger),
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

/// The change-password flow against `PATCH /api/auth/change-password`.
///
/// The dialog walks through three states — form, submitting, done — and the
/// done state is explicit: the stored credentials are stale once the server
/// accepted the new password, so the dialog tells the user to sign in again
/// and the confirm button performs that sign-out. Swallowing this would leave
/// the app silently re-logging in with the old password on next start.
class _ChangePasswordDialog extends StatefulWidget {
  const _ChangePasswordDialog();

  @override
  State<_ChangePasswordDialog> createState() => _ChangePasswordDialogState();
}

class _ChangePasswordDialogState extends State<_ChangePasswordDialog> {
  final _oldCtrl = TextEditingController();
  final _newCtrl = TextEditingController();
  final _confirmCtrl = TextEditingController();
  final _formKey = GlobalKey<FormState>();

  bool _busy = false;
  bool _done = false;
  String? _error;

  @override
  void dispose() {
    _oldCtrl.dispose();
    _newCtrl.dispose();
    _confirmCtrl.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final t = AppMessages.of(context);
    if (!(_formKey.currentState?.validate() ?? false)) return;
    setState(() {
      _busy = true;
      _error = null;
    });

    final state = context.read<AppState>();
    final client = state.buildApiClient();
    try {
      await client.changePassword(
        userId: state.userId,
        oldPassword: _oldCtrl.text,
        newPassword: _newCtrl.text,
      );
      if (!mounted) return;
      setState(() {
        _busy = false;
        _done = true;
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      // A session that died mid-flow is the app-level expiry, not a form
      // error: hand it to the same expiry path every screen uses.
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() {
        _busy = false;
        _error = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('state_error');
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = t.get('state_error');
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final palette = AppColors.of(context);

    return AlertDialog(
      key: const ValueKey<String>('change_password_dialog'),
      title: Text(t.get('settings_change_password')),
      content: _done
          ? Text(t.get('settings_password_changed'))
          : Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (_error != null) ...[
                  Container(
                    padding: const EdgeInsets.all(AppSpacing.sm),
                    margin: const EdgeInsets.only(bottom: AppSpacing.md),
                    decoration: BoxDecoration(
                      color: palette.danger.withValues(alpha: 0.12),
                      borderRadius: AppRadius.allSm,
                      border: Border.all(
                        color: palette.danger.withValues(alpha: 0.45),
                      ),
                    ),
                    child: Text(
                      _error!,
                      style: TextStyle(
                        fontSize: AppType.xs,
                        color: palette.danger,
                      ),
                    ),
                  ),
                ],
                Form(
                  key: _formKey,
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      TextFormField(
                        key: const ValueKey<String>('cp_old_field'),
                        controller: _oldCtrl,
                        obscureText: true,
                        enabled: !_busy,
                        autofocus: true,
                        decoration: InputDecoration(
                          labelText: t.get('settings_password_old'),
                          prefixIcon: const Icon(Icons.lock_outline),
                        ),
                        validator: (v) => (v == null || v.isEmpty)
                            ? t.get('settings_password_required')
                            : null,
                      ),
                      AppSpacing.gapMd,
                      TextFormField(
                        key: const ValueKey<String>('cp_new_field'),
                        controller: _newCtrl,
                        obscureText: true,
                        enabled: !_busy,
                        decoration: InputDecoration(
                          labelText: t.get('settings_password_new'),
                          prefixIcon: const Icon(Icons.password_outlined),
                        ),
                        validator: (v) => (v == null || v.length < 8)
                            ? t.get('settings_password_too_short')
                            : null,
                      ),
                      AppSpacing.gapMd,
                      TextFormField(
                        key: const ValueKey<String>('cp_confirm_field'),
                        controller: _confirmCtrl,
                        obscureText: true,
                        enabled: !_busy,
                        decoration: InputDecoration(
                          labelText: t.get('settings_password_confirm'),
                          prefixIcon: const Icon(Icons.password_outlined),
                        ),
                        validator: (v) => (v != _newCtrl.text)
                            ? t.get('settings_password_mismatch')
                            : null,
                      ),
                    ],
                  ),
                ),
              ],
            ),
      actions: _done
          ? [
              AppButton(
                label: t.get('settings_sign_in_again'),
                onPressed: () => Navigator.pop(context, true),
              ),
            ]
          : [
              AppButton(
                label: t.get('action_cancel'),
                variant: AppButtonVariant.ghost,
                onPressed: _busy ? null : () => Navigator.pop(context, false),
              ),
              AppButton(
                key: const ValueKey<String>('cp_submit'),
                label: t.get('action_save'),
                onPressed: _busy ? null : _submit,
              ),
            ],
    );
  }
}
