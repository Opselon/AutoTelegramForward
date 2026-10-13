/// Shared server-connection dialog used by the login screen and Settings.
///
/// Lets the user edit the server URL, run an unauthenticated connection
/// test ([ApiClient.probeServer] — no token is ever attached), and restore
/// the official default from [AppEnv]. Saving switches the server via
/// [AppState.setServerUrl], which clears the session and forces re-login.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/env.dart';
import '../../l10n/app_messages.dart';

/// Edits the server URL with live connection feedback.
///
/// [initialUrl] pre-fills the field. [transport] overrides the HTTP layer
/// (widget tests inject a fake; production uses a real client).
class ServerConnectionDialog extends StatefulWidget {
  const ServerConnectionDialog({
    super.key,
    required this.initialUrl,
    this.transport,
  });

  final String initialUrl;
  final HttpTransport? transport;

  @override
  State<ServerConnectionDialog> createState() => _ServerConnectionDialogState();
}

class _ServerConnectionDialogState extends State<ServerConnectionDialog> {
  late final TextEditingController _ctrl;
  ProbeResult? _probe;
  bool _probing = false;
  String? _saveError;

  @override
  void initState() {
    super.initState();
    _ctrl = TextEditingController(text: widget.initialUrl);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  Future<void> _test() async {
    final t = AppMessages.of(context);
    final state = context.read<AppState>();
    final raw = _ctrl.text;
    if (ServerUrl.tryParse(raw) == null) {
      setState(() {
        _probing = false;
        _probe = ProbeResult.unreachable(
          'validation',
          t.get('login_server_invalid'),
        );
      });
      return;
    }
    setState(() {
      _probing = true;
      _probe = null;
      _saveError = null;
    });
    final result = await ApiClient.probeServer(
      raw,
      minSupportedApiVersion: state.env.minSupportedApiVersion,
      transport: widget.transport,
    );
    if (!mounted) return;
    setState(() {
      _probing = false;
      _probe = result;
    });
  }

  void _restoreDefault() {
    final state = context.read<AppState>();
    setState(() {
      _ctrl.text = state.env.defaultBaseUrl;
      _probe = null;
      _saveError = null;
    });
    _test();
  }

  Future<void> _save() async {
    final t = AppMessages.of(context);
    final parsed = ServerUrl.tryParse(_ctrl.text);
    if (parsed == null) {
      setState(() => _saveError = t.get('login_server_invalid'));
      return;
    }
    await context.read<AppState>().setServerUrl(parsed.normalized);
    if (!mounted) return;
    Navigator.pop(context, true);
  }

  /// Maps a failed probe to an actionable, localised sentence.
  String _probeMessage(AppMessages t, AppState state, ProbeResult probe) {
    switch (probe.errorKind) {
      case 'validation':
        return t.get('login_server_invalid');
      case 'timeout':
      case 'network':
        if (probe.errorMessage == 'server_tls_error') {
          return t.get('server_probe_tls_error');
        }
        return t.get('server_probe_unreachable');
      case 'not_found':
        return t.get('server_probe_no_version');
      case 'incompatible_server':
        return t.get(
          'incompatible_server',
          params: {'version': state.env.minSupportedApiVersion},
        );
      default:
        if (probe.errorMessage.isNotEmpty &&
            probe.errorMessage != 'probe_invalid_json' &&
            !probe.errorMessage.startsWith('probe_')) {
          return probe.errorMessage;
        }
        return t.get('server_probe_bad_response');
    }
  }

  Widget _buildStatus(AppMessages t, AppState state) {
    if (_probing) {
      return Row(
        key: const Key('server_probe_loading'),
        children: [
          const SizedBox(
            width: 16,
            height: 16,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
          const SizedBox(width: 8),
          Expanded(child: Text(t.get('server_probe_testing'))),
        ],
      );
    }
    final probe = _probe;
    if (probe == null) return const SizedBox.shrink();
    if (probe.ok) {
      final versions = [
        if (probe.productVersion.isNotEmpty) probe.productVersion,
        if (probe.apiVersion.isNotEmpty) 'API ${probe.apiVersion}',
      ].join(' • ');
      return Row(
        key: const Key('server_probe_ok'),
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.check_circle_outline, color: Colors.green),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              versions.isEmpty
                  ? t.get('server_probe_reachable')
                  : '${t.get('server_probe_reachable')}: $versions',
            ),
          ),
        ],
      );
    }
    final amber = probe.errorKind == 'incompatible_server';
    return Row(
      key: const Key('server_probe_error'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(
          amber ? Icons.warning_amber_rounded : Icons.error_outline,
          color: amber ? Colors.amber : Colors.red,
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            _probeMessage(t, state, probe),
            style: TextStyle(color: amber ? null : Colors.red),
          ),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final state = context.watch<AppState>();

    return AlertDialog(
      title: Text(t.get('login_server_change')),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              '${t.get('login_server_label')}:',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 8),
            TextField(
              key: const Key('server_url_field'),
              controller: _ctrl,
              autocorrect: false,
              enableSuggestions: false,
              keyboardType: TextInputType.url,
              decoration: InputDecoration(
                hintText: 'https://atf.example.com',
                errorText:
                    ServerUrl.tryParse(_ctrl.text) == null &&
                            _ctrl.text.isNotEmpty
                        ? t.get('login_server_invalid')
                        : _saveError,
              ),
              onChanged: (_) {
                // Clear a stale probe verdict as soon as the URL is edited.
                if (_probe != null) setState(() => _probe = null);
              },
            ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              key: const Key('server_probe_button'),
              onPressed: _probing ? null : _test,
              icon: const Icon(Icons.network_check_outlined),
              label: Text(t.get('server_probe_test')),
            ),
            const SizedBox(height: 8),
            _buildStatus(t, state),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton(
                key: const Key('server_restore_default_button'),
                onPressed: _probing ? null : _restoreDefault,
                child: Text(t.get('server_restore_default')),
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context, false),
          child: Text(t.get('action_cancel')),
        ),
        ElevatedButton(
          key: const Key('server_save_button'),
          onPressed: _save,
          child: Text(t.get('action_save')),
        ),
      ],
    );
  }
}

/// Shows the [ServerConnectionDialog]. Returns true when the server was
/// changed (the session was cleared and the user must log in again).
Future<bool> showServerConnectionDialog(
  BuildContext context, {
  HttpTransport? transport,
}) async {
  final state = context.read<AppState>();
  final saved = await showDialog<bool>(
    context: context,
    builder:
        (_) => ChangeNotifierProvider<AppState>.value(
          value: state,
          child: ServerConnectionDialog(
            initialUrl: state.serverUrl,
            transport: transport,
          ),
        ),
  );
  return saved ?? false;
}
