/// Interactive MTProto login flow dialog.
///
/// Drives the server-side challenge lifecycle through `/api/telegram-login/*`:
///   1. Phone input -> `POST /api/telegram-login/start`
///   2. Server returns `awaiting_code` (or terminal/rate-limit error)
///   3. User enters code -> `POST /api/telegram-login/{id}/code`
///   4. If server returns `awaiting_password` -> user enters 2FA password
///   5. -> `POST /api/telegram-login/{id}/password`
///   6. Server finishes -> returns `completed` + new session id
///
/// All MTProto cryptography and state live in the Python core; the mobile
/// client only drives the challenges. Secrets and 2FA passwords are never logged.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../l10n/app_messages.dart';

class TelegramLoginDialog extends StatefulWidget {
  const TelegramLoginDialog({super.key});

  @override
  State<TelegramLoginDialog> createState() => _TelegramLoginDialogState();
}

class _TelegramLoginDialogState extends State<TelegramLoginDialog> {
  final _phoneCtrl = TextEditingController();
  final _codeCtrl = TextEditingController();
  final _passCtrl = TextEditingController();

  LoginChallenge? _challenge;
  bool _submitting = false;
  String? _stepError;
  int _cooldown = 0;
  Timer? _cooldownTimer;

  @override
  void dispose() {
    _cooldownTimer?.cancel();
    _phoneCtrl.dispose();
    _codeCtrl.dispose();
    _passCtrl.dispose();
    super.dispose();
  }

  void _startCooldown(int seconds) {
    _cooldownTimer?.cancel();
    setState(() => _cooldown = seconds);
    _cooldownTimer = Timer.periodic(const Duration(seconds: 1), (t) {
      if (!mounted) {
        t.cancel();
        return;
      }
      if (_cooldown <= 1) {
        t.cancel();
        setState(() => _cooldown = 0);
      } else {
        setState(() => _cooldown--);
      }
    });
  }

  Future<void> _startLogin() async {
    final t = AppMessages.of(context);
    final phone = _phoneCtrl.text.trim();
    if (!isValidPhone(phone)) {
      setState(() => _stepError = t.get('tg_login_phone_invalid'));
      return;
    }

    setState(() {
      _submitting = true;
      _stepError = null;
    });

    final state = context.read<AppState>();
    final client = state.buildApiClient();

    try {
      final challenge = await client.startTelegramLogin(
        userId: state.userId,
        phoneNumber: normalizePhone(phone),
      );
      if (!mounted) return;
      setState(() {
        _challenge = challenge;
        if (challenge.retryAfterSeconds > 0) {
          _startCooldown(challenge.retryAfterSeconds);
        }
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(
        () => _stepError = exc.error.message.isNotEmpty
            ? t.loginMessage(exc.error.message)
            : t.get('state_error'),
      );
    } catch (_) {
      if (!mounted) return;
      setState(() => _stepError = t.get('state_error'));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _submitCode() async {
    final challenge = _challenge;
    if (challenge == null) return;
    final t = AppMessages.of(context);
    final code = _codeCtrl.text.trim();
    if (!isValidVerificationCode(code)) {
      setState(() => _stepError = t.get('tg_login_code_invalid'));
      return;
    }

    setState(() {
      _submitting = true;
      _stepError = null;
    });

    final client = context.read<AppState>().buildApiClient();

    try {
      final updated = await client.submitTelegramCode(
        challengeId: challenge.id,
        code: code,
      );
      if (!mounted) return;
      setState(() {
        _challenge = updated;
        if (updated.retryAfterSeconds > 0) {
          _startCooldown(updated.retryAfterSeconds);
        }
      });
      if (updated.state == LoginChallengeState.completed) {
        Navigator.pop(context, true);
      }
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(() => _stepError = t.loginMessage(exc.error.message));
    } catch (_) {
      if (!mounted) return;
      setState(() => _stepError = t.get('state_error'));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _submitPassword() async {
    final challenge = _challenge;
    if (challenge == null) return;
    final t = AppMessages.of(context);
    final password = _passCtrl.text;
    if (password.isEmpty) {
      setState(() => _stepError = t.get('login_password'));
      return;
    }

    setState(() {
      _submitting = true;
      _stepError = null;
    });

    final client = context.read<AppState>().buildApiClient();

    try {
      final updated = await client.submitTelegramPassword(
        challengeId: challenge.id,
        password: password,
      );
      if (!mounted) return;
      setState(() {
        _challenge = updated;
        if (updated.retryAfterSeconds > 0) {
          _startCooldown(updated.retryAfterSeconds);
        }
      });
      if (updated.state == LoginChallengeState.completed) {
        Navigator.pop(context, true);
      }
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(() => _stepError = t.loginMessage(exc.error.message));
    } catch (_) {
      if (!mounted) return;
      setState(() => _stepError = t.get('state_error'));
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _cancel() async {
    final challenge = _challenge;
    if (challenge != null) {
      try {
        final client = context.read<AppState>().buildApiClient();
        await client.cancelTelegramLogin(challenge.id);
      } catch (_) {
        // cancellation is best-effort
      }
    }
    if (mounted) Navigator.pop(context, false);
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final challenge = _challenge;

    Widget body;
    if (challenge == null) {
      // Step 1: Phone input
      body = Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          TextField(
            controller: _phoneCtrl,
            keyboardType: TextInputType.phone,
            autofocus: true,
            decoration: InputDecoration(
              labelText: t.get('tg_login_phone'),
              hintText: t.get('tg_login_phone_hint'),
              prefixIcon: const Icon(Icons.phone_outlined),
            ),
          ),
          const SizedBox(height: 16),
          ElevatedButton(
            onPressed: _submitting ? null : _startLogin,
            child: _submitting
                ? const SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: Colors.white,
                    ),
                  )
                : Text(t.get('tg_login_send_code')),
          ),
        ],
      );
    } else if (challenge.state == LoginChallengeState.awaitingCode) {
      // Step 2: Verification code
      body = Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            t.loginState(challenge, _phoneCtrl.text),
            style: Theme.of(context).textTheme.bodyMedium,
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _codeCtrl,
            keyboardType: TextInputType.number,
            autofocus: true,
            decoration: InputDecoration(
              labelText: t.get('tg_login_code'),
              hintText: t.get('tg_login_code_hint'),
              prefixIcon: const Icon(Icons.pin_outlined),
            ),
          ),
          const SizedBox(height: 16),
          ElevatedButton(
            onPressed: _submitting ? null : _submitCode,
            child: _submitting
                ? const SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: Colors.white,
                    ),
                  )
                : Text(t.get('tg_login_verify')),
          ),
          if (_cooldown > 0)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                t.get('login_cooldown', params: {'seconds': '$_cooldown'}),
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodySmall
                    ?.copyWith(color: Colors.grey),
              ),
            ),
        ],
      );
    } else if (challenge.state == LoginChallengeState.awaitingPassword) {
      // Step 3: Two-step verification password
      body = Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            t.get('tg_login_state_awaiting_password'),
            style: Theme.of(context).textTheme.bodyMedium,
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _passCtrl,
            obscureText: true,
            autofocus: true,
            decoration: InputDecoration(
              labelText: t.get('tg_login_password'),
              prefixIcon: const Icon(Icons.lock_outline),
            ),
          ),
          const SizedBox(height: 16),
          ElevatedButton(
            onPressed: _submitting ? null : _submitPassword,
            child: _submitting
                ? const SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: Colors.white,
                    ),
                  )
                : Text(t.get('tg_login_verify')),
          ),
        ],
      );
    } else {
      // Terminal state (failed / expired / rate_limited)
      body = Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Icon(
            challenge.state == LoginChallengeState.completed
                ? Icons.check_circle
                : Icons.error_outline,
            size: 48,
            color: challenge.state == LoginChallengeState.completed
                ? Colors.green
                : Colors.red,
          ),
          const SizedBox(height: 12),
          Text(
            t.loginState(challenge, _phoneCtrl.text),
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodyMedium,
          ),
          const SizedBox(height: 16),
          if (challenge.state != LoginChallengeState.completed)
            ElevatedButton(
              onPressed: () => setState(() {
                _challenge = null;
                _codeCtrl.clear();
                _passCtrl.clear();
                _stepError = null;
              }),
              child: Text(t.get('action_retry')),
            ),
        ],
      );
    }

    return AlertDialog(
      title: Text(t.get('tg_login_title')),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (_stepError != null)
              Container(
                padding: const EdgeInsets.all(10),
                margin: const EdgeInsets.only(bottom: 12),
                decoration: BoxDecoration(
                  color: Colors.red.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.red),
                ),
                child: Text(
                  _stepError!,
                  style: const TextStyle(color: Colors.red, fontSize: 13),
                ),
              ),
            body,
          ],
        ),
      ),
      actions: [
        TextButton(onPressed: _cancel, child: Text(t.get('action_cancel'))),
      ],
    );
  }
}
