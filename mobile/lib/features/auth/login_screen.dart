/// Login and registration screen.
///
/// Handles:
///   * Standard username / password authentication against `/api/auth/login`.
///   * Account registration via `/api/auth/register`.
///   * Live server URL switcher with HTTPS enforcement for remote hosts.
///   * Clear error presentation (unauthorized, unreachable, network).
///   * Session restoration / expiry messaging.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/server_connection_dialog.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key, this.reason = LoginReason.fresh});

  final LoginReason reason;

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _usernameCtrl = TextEditingController();
  final _passwordCtrl = TextEditingController();

  bool _isRegister = false;
  bool _loading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final state = context.read<AppState>();
    if (state.savedUsername.isNotEmpty) {
      _usernameCtrl.text = state.savedUsername;
    }
    if (state.savedPassword.isNotEmpty) {
      _passwordCtrl.text = state.savedPassword;
    }
  }

  @override
  void dispose() {
    _usernameCtrl.dispose();
    _passwordCtrl.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final t = AppMessages.of(context);
    if (!_formKey.currentState!.validate()) return;

    setState(() {
      _loading = true;
      _error = null;
    });

    final state = context.read<AppState>();
    final client = state.buildApiClient();

    try {
      if (_isRegister) {
        await client.register(
          username: _usernameCtrl.text.trim(),
          password: _passwordCtrl.text,
        );
      }
      final session = await client.login(
        username: _usernameCtrl.text.trim(),
        password: _passwordCtrl.text,
      );
      if (!mounted) return;
      await state.onLoginSuccess(session, password: _passwordCtrl.text);
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(() {
        if (exc.error.kind == ErrorKind.unauthorized) {
          _error = t.get('login_error_unauthorized');
        } else if (exc.error.kind == ErrorKind.network ||
            exc.error.kind == ErrorKind.timeout) {
          _error = t.get('login_error_network');
        } else {
          _error =
              exc.error.message.isNotEmpty
                  ? exc.error.message
                  : t.get('state_error');
        }
      });
    } catch (exc) {
      if (!mounted) return;
      setState(() => _error = t.get('state_error'));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _showServerDialog() async {
    await showServerConnectionDialog(context);
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final state = context.watch<AppState>();

    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Form(
                key: _formKey,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const Icon(
                      Icons.send_rounded,
                      size: 56,
                      color: Color(0xFF10B981),
                    ),
                    const SizedBox(height: 16),
                    Text(
                      t.get('app_name'),
                      textAlign: TextAlign.center,
                      style: Theme.of(context).textTheme.headlineMedium
                          ?.copyWith(fontWeight: FontWeight.bold),
                    ),
                    const SizedBox(height: 6),
                    Text(
                      t.get('app_tagline'),
                      textAlign: TextAlign.center,
                      style: Theme.of(
                        context,
                      ).textTheme.bodyMedium?.copyWith(color: Colors.grey),
                    ),
                    const SizedBox(height: 32),

                    if (widget.reason == LoginReason.expired)
                      Container(
                        padding: const EdgeInsets.all(12),
                        margin: const EdgeInsets.only(bottom: 16),
                        decoration: BoxDecoration(
                          color: Colors.amber.withValues(alpha: 0.15),
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: Colors.amber),
                        ),
                        child: Row(
                          children: [
                            const Icon(
                              Icons.warning_amber_rounded,
                              color: Colors.amber,
                            ),
                            const SizedBox(width: 8),
                            Expanded(
                              child: Text(t.get('login_session_expired')),
                            ),
                          ],
                        ),
                      ),

                    if (_error != null)
                      Container(
                        padding: const EdgeInsets.all(12),
                        margin: const EdgeInsets.only(bottom: 16),
                        decoration: BoxDecoration(
                          color: Colors.red.withValues(alpha: 0.15),
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: Colors.red),
                        ),
                        child: Text(
                          _error!,
                          style: const TextStyle(color: Colors.red),
                        ),
                      ),

                    TextFormField(
                      controller: _usernameCtrl,
                      autocorrect: false,
                      enableSuggestions: false,
                      decoration: InputDecoration(
                        labelText: t.get('login_username'),
                        prefixIcon: const Icon(Icons.person_outline),
                      ),
                      validator:
                          (v) =>
                              (v == null || v.trim().isEmpty)
                                  ? t.get('login_username')
                                  : null,
                    ),
                    const SizedBox(height: 16),
                    TextFormField(
                      controller: _passwordCtrl,
                      obscureText: true,
                      decoration: InputDecoration(
                        labelText: t.get('login_password'),
                        prefixIcon: const Icon(Icons.lock_outline),
                      ),
                      validator:
                          (v) =>
                              (v == null || v.isEmpty)
                                  ? t.get('login_password')
                                  : null,
                    ),
                    const SizedBox(height: 24),

                    ElevatedButton(
                      onPressed: _loading ? null : _submit,
                      child:
                          _loading
                              ? const SizedBox(
                                width: 20,
                                height: 20,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  color: Colors.white,
                                ),
                              )
                              : Text(
                                _isRegister
                                    ? t.get('login_register')
                                    : t.get('login_submit'),
                              ),
                    ),
                    const SizedBox(height: 12),

                    TextButton(
                      onPressed:
                          () => setState(() {
                            _isRegister = !_isRegister;
                            _error = null;
                          }),
                      child: Text(
                        _isRegister
                            ? t.get('login_have_account')
                            : t.get('login_no_account'),
                      ),
                    ),
                    const Divider(height: 36),

                    // Server selection footer
                    Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        const Icon(
                          Icons.dns_outlined,
                          size: 16,
                          color: Colors.grey,
                        ),
                        const SizedBox(width: 6),
                        Flexible(
                          child: Text(
                            state.serverUrl,
                            overflow: TextOverflow.ellipsis,
                            style: Theme.of(
                              context,
                            ).textTheme.bodySmall?.copyWith(color: Colors.grey),
                          ),
                        ),
                        TextButton(
                          onPressed: _showServerDialog,
                          child: Text(t.get('login_server_change')),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
