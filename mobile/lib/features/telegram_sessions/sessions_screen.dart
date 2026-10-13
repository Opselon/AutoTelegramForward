/// Telegram sessions management screen.
///
/// Features:
///   * Displays only the sessions owned by the authenticated tenant.
///   * Disconnect / terminate session with explicit user confirmation.
///   * Triggers the interactive [TelegramLoginDialog] for linking accounts.
///   * Reflects real MTProto connection status.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';
import 'telegram_login_dialog.dart';

class SessionsScreen extends StatefulWidget {
  const SessionsScreen({super.key});

  @override
  State<SessionsScreen> createState() => _SessionsScreenState();
}

class _SessionsScreenState extends State<SessionsScreen> {
  List<TelegramSession> _sessions = const [];
  bool _loading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });

    final client = context.read<AppState>().buildApiClient();
    final t = AppMessages.of(context);

    try {
      final list = await client.listSessions();
      if (!mounted) return;
      setState(() => _sessions = list);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() => _error = exc.error.message.isNotEmpty ? exc.error.message : t.get('state_error'));
    } catch (_) {
      if (!mounted) return;
      setState(() => _error = t.get('state_error'));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _terminate(TelegramSession session) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('sessions_terminate')),
        content: Text(t.get('sessions_terminate_confirm')),
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

    if (confirmed != true || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      await client.terminateSession(session.id);
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(exc.error.message.isNotEmpty ? exc.error.message : t.get('state_error'))),
      );
    }
  }

  Future<void> _addSession() async {
    final linked = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (_) => const TelegramLoginDialog(),
    );
    if (linked == true) {
      if (!mounted) return;
      final t = AppMessages.of(context);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(t.get('tg_login_success'))),
      );
      await _load();
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('sessions_title')),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: t.get('action_refresh'),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _addSession,
        icon: const Icon(Icons.add),
        label: Text(t.get('sessions_add')),
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: Column(
          children: [
            if (_error != null)
              Container(
                padding: const EdgeInsets.all(12),
                margin: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: Colors.red.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.red),
                ),
                child: Text(_error!, style: const TextStyle(color: Colors.red)),
              ),
            Expanded(
              child: _sessions.isEmpty && !_loading
                  ? ListView(
                children: [
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 64),
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        const Icon(Icons.phonelink_erase_rounded, size: 64, color: Colors.grey),
                        const SizedBox(height: 16),
                        Text(
                          t.get('sessions_empty'),
                          textAlign: TextAlign.center,
                          style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: Colors.grey),
                        ),
                        const SizedBox(height: 24),
                        ElevatedButton.icon(
                          onPressed: _addSession,
                          icon: const Icon(Icons.add),
                          label: Text(t.get('sessions_add')),
                        ),
                      ],
                    ),
                  ),
                ],
              )
            : ListView.builder(
                padding: const EdgeInsets.all(16),
                itemCount: _sessions.length,
                itemBuilder: (context, index) {
                  final s = _sessions[index];
                  return Card(
                    child: ListTile(
                      leading: CircleAvatar(
                        backgroundColor: s.isActive ? Colors.green.withValues(alpha: 0.2) : Colors.grey.withValues(alpha: 0.2),
                        child: Icon(
                          Icons.send_rounded,
                          color: s.isActive ? Colors.green : Colors.grey,
                          size: 20,
                        ),
                      ),
                      title: Text(s.displayName, style: const TextStyle(fontWeight: FontWeight.bold)),
                      subtitle: Text(
                        '${s.phone.isNotEmpty ? s.phone : s.id} • ${s.isActive ? t.get('sessions_active') : t.get('sessions_inactive')}',
                      ),
                      trailing: IconButton(
                        icon: const Icon(Icons.link_off, color: Colors.red),
                        tooltip: t.get('sessions_terminate'),
                        onPressed: () => _terminate(s),
                      ),
                    ),
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}
