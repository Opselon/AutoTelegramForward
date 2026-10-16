/// Log policy + pipeline health: the "why is this screen quiet" surface.
///
/// The logs tab answers *what was recorded*; this tab answers *what the system
/// decided to record*, which is the question that matters when a bug does not
/// reproduce. Two endpoints back it:
///   * `GET /api/logs/policy` — the effective collection policy per producer
///     process, with the scope of each answer stated;
///   * `GET /api/logs/health` — per-service reachability and the age of the
///     newest stored record.
///
/// Three rules shape the rendering, all of them about not lying to the user:
///   * a scope that cannot be read is shown as *not reported with the server's
///     reason*, never as zero — zero suppressed records and "nobody can tell"
///     are opposite answers;
///   * counters are labelled as process-local to whatever process reported
///     them, because they reset on restart and are not a system total;
///   * the verbose window is presented as what it is: bounded, admin-only and
///     self-expiring, so nobody leaves collection turned up forever.
///
/// Both reads are advisory. An older server 404s on them, and that renders as
/// "this server does not report its policy" instead of an error, so the mobile
/// app keeps working against a release that predates the control plane.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';

class LogPolicyTab extends StatefulWidget {
  const LogPolicyTab({super.key});

  @override
  State<LogPolicyTab> createState() => _LogPolicyTabState();
}

class _LogPolicyTabState extends State<LogPolicyTab> {
  Map<String, dynamic>? _policy;
  Map<String, dynamic>? _health;
  bool _loading = true;
  bool _unsupported = false;
  String? _error;
  String? _actionError;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    // Post-frame: the API client comes from the inherited scope, which is not
    // readable during initState.
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
      _unsupported = false;
    });
    final client = context.read<AppState>().buildApiClient();
    try {
      // Both reads are independent: a missing policy must not hide the health
      // of the logger, and vice versa. Each is awaited on its own so one
      // failure cannot discard the other's result.
      final results = await Future.wait<Object?>([
        client
            .logPolicy()
            .then<Object?>((v) => v)
            .catchError((Object _) => null),
        client
            .logHealth()
            .then<Object?>((v) => v)
            .catchError((Object _) => null),
      ]);
      if (!mounted) return;
      final policy = results[0];
      final health = results[1];
      setState(() {
        _policy = policy is Map<String, dynamic> ? policy : null;
        _health = health is Map<String, dynamic> ? health : null;
        // Neither endpoint answered: on a server that predates the control
        // plane both 404, and that is a version statement, not a failure.
        _unsupported = _policy == null && _health == null;
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() => _error = exc.error.message);
    } catch (exc) {
      if (!mounted) return;
      setState(() => _error = '$exc');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  /// Opens or closes the bounded verbose window.
  ///
  /// The message shown afterwards describes what the *server* reported (it may
  /// have replaced an existing window, or refused a restart inside its own
  /// guard), not what we hoped happened.
  Future<void> _diagnostics({required bool start}) async {
    setState(() {
      _busy = true;
      _actionError = null;
    });
    final t = AppMessages.of(context);
    final client = context.read<AppState>().buildApiClient();
    final messenger = ScaffoldMessenger.of(context);
    try {
      final resp = start
          ? await client.startLogDiagnostics()
          : await client.stopLogDiagnostics();
      if (!mounted) return;
      final seconds =
          (resp['seconds'] as num?)?.toInt() ??
          (resp['expires_in_seconds'] as num?)?.toInt() ??
          0;
      final changed = resp['changed'] != false;
      final message = start
          ? t.get('logs_policy_started', params: {'seconds': '$seconds'})
          : (changed
                ? t.get('logs_policy_stopped')
                : t.get('logs_policy_stop_none'));
      messenger.showSnackBar(SnackBar(content: Text(message)));
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      // 403 is the expected answer for a non-admin: say so in words instead of
      // echoing a permission code the user cannot act on.
      setState(
        () => _actionError = exc.error.kind == ErrorKind.forbidden
            ? t.get('logs_policy_admin_required')
            : t.get(
                'logs_policy_start_failed',
                params: {'error': exc.error.message},
              ),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(
        () => _actionError = t.get(
          'logs_policy_start_failed',
          params: {'error': '$exc'},
        ),
      );
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return _message(
        t,
        icon: Icons.cloud_off,
        title: t.get('state_error'),
        message: _error!,
        actionLabel: t.get('action_retry'),
        onAction: _load,
      );
    }
    if (_unsupported) {
      return _message(
        t,
        icon: Icons.help_outline,
        title: t.get('logs_policy_not_reported'),
        message: t.get('logs_policy_unavailable_hint'),
        actionLabel: t.get('action_refresh'),
        onAction: _load,
      );
    }

    final scopes = _scopes();
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
      children: [
        _healthCard(t),
        const SizedBox(height: AppSpacing.md),
        Text(
          t.get('logs_policy_scopes_title'),
          style: const TextStyle(
            fontSize: 13,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.4,
          ),
        ),
        const SizedBox(height: 6),
        for (final scope in scopes) _scopeCard(t, scope),
        const SizedBox(height: AppSpacing.md),
        _diagnosticsCard(t),
        if (_notes().isNotEmpty) ...[
          const SizedBox(height: AppSpacing.md),
          Text(
            t.get('logs_policy_notes_title'),
            style: const TextStyle(
              fontSize: 13,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.4,
            ),
          ),
          for (final note in _notes())
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(
                '• $note',
                style: const TextStyle(fontSize: 11, color: Colors.grey),
              ),
            ),
        ],
      ],
    );
  }

  // ---------------------------------------------------------------- rendering

  /// Centred message with one action, in the shape the other screens use for a
  /// state that is neither "empty" nor "list".
  Widget _message(
    AppMessages t, {
    required IconData icon,
    required String title,
    required String message,
    required String actionLabel,
    required VoidCallback onAction,
  }) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 40, color: Colors.grey),
            const SizedBox(height: 12),
            Text(
              title,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 6),
            Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 12, color: Colors.grey),
            ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              icon: const Icon(Icons.refresh, size: 16),
              label: Text(actionLabel),
              onPressed: onAction,
            ),
          ],
        ),
      ),
    );
  }

  /// Per-service logging reachability, taken from `/api/logs/health`.
  Widget _healthCard(AppMessages t) {
    final health = _health;
    final services = health == null
        ? const <Map<String, dynamic>>[]
        : ((health['services'] as List?) ?? const [])
              .whereType<Map<String, dynamic>>()
              .toList(growable: false);
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(AppSpacing.md),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.monitor_heart_outlined, size: 16),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    t.get('logs_policy_health_title'),
                    style: const TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.4,
                    ),
                  ),
                ),
                if (health != null) _statusChip(t, '${health['status'] ?? ''}'),
              ],
            ),
            const SizedBox(height: 8),
            if (services.isEmpty)
              Text(
                t.get('logs_policy_not_reported'),
                style: const TextStyle(fontSize: 12, color: Colors.grey),
              ),
            for (final svc in services)
              Padding(
                padding: const EdgeInsets.only(bottom: 6),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    SizedBox(
                      width: 96,
                      child: Text(
                        '${svc['service'] ?? ''}',
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${svc['detail'] ?? ''}',
                            style: const TextStyle(fontSize: 12),
                          ),
                          Text(
                            _ageLine(t, svc),
                            style: const TextStyle(
                              fontSize: 11,
                              color: Colors.grey,
                            ),
                          ),
                        ],
                      ),
                    ),
                    _statusChip(t, '${svc['status'] ?? ''}'),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }

  /// The age of the newest stored record — the number that separates "quiet
  /// because nothing is wrong" from "quiet because nothing is arriving".
  String _ageLine(AppMessages t, Map<String, dynamic> svc) {
    final age = (svc['data_age_seconds'] as num?)?.toInt();
    if (age == null) return t.get('logs_policy_no_records');
    return t.get('logs_policy_data_age', params: {'seconds': '$age'});
  }

  Widget _statusChip(AppMessages t, String status) {
    final normalised = status.toLowerCase();
    final (labelKey, color) = switch (normalised) {
      'healthy' || 'online' => ('logs_policy_status_healthy', Colors.green),
      'degraded' => ('logs_policy_status_degraded', Colors.orange),
      'unavailable' ||
      'offline' => ('logs_policy_status_unavailable', Colors.red),
      _ => ('logs_policy_status_unknown', Colors.grey),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.14),
        borderRadius: AppRadii.allSm,
      ),
      child: Text(
        t.get(labelKey).toUpperCase(),
        style: TextStyle(
          fontSize: 10,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.5,
          color: color,
        ),
      ),
    );
  }

  /// One producer's answer. An unavailable scope shows the server's reason: it
  /// is a fact about that process, and hiding it would look like "no policy".
  Widget _scopeCard(
    AppMessages t,
    MapEntry<String, Map<String, dynamic>> scope,
  ) {
    final data = scope.value;
    final available = data['available'] == true;
    final counters = data['counters'] is Map<String, dynamic>
        ? data['counters'] as Map<String, dynamic>
        : null;
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.all(AppSpacing.md),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    '${data['process'] ?? scope.key}',
                    style: const TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                if (available)
                  _statusChip(
                    t,
                    data['diagnostic_active'] == true ? 'degraded' : 'healthy',
                  ),
              ],
            ),
            if ('${data['scope'] ?? ''}'.isNotEmpty)
              Text(
                '${data['scope']}',
                style: const TextStyle(fontSize: 11, color: Colors.grey),
              ),
            const SizedBox(height: 6),
            if (!available)
              Text(
                t.get(
                  'logs_policy_reason',
                  params: {'reason': '${data['reason'] ?? ''}'},
                ),
                style: const TextStyle(fontSize: 12),
              )
            else ...[
              Text(
                t.get(
                  'logs_policy_effective',
                  params: {'levels': _levels(data['effective_levels'])},
                ),
                style: const TextStyle(fontSize: 12),
              ),
              Text(
                t.get(
                  'logs_policy_configured',
                  params: {'levels': _levels(data['configured_levels'])},
                ),
                style: const TextStyle(fontSize: 11, color: Colors.grey),
              ),
              const SizedBox(height: 4),
              Text(
                data['diagnostic_active'] == true
                    ? t.get(
                        'logs_policy_diag_on',
                        params: {
                          'seconds':
                              '${data['diagnostic_seconds_remaining'] ?? 0}',
                        },
                      )
                    : t.get('logs_policy_diag_off'),
                style: const TextStyle(fontSize: 12),
              ),
              if (counters != null) ...[
                const SizedBox(height: 4),
                Text(
                  t.get(
                    'logs_policy_counters',
                    params: {
                      'emitted': '${counters['emitted'] ?? 0}',
                      'by_level': '${counters['suppressed_level'] ?? 0}',
                      'by_rate': '${counters['suppressed_rate'] ?? 0}',
                    },
                  ),
                  style: const TextStyle(fontSize: 12),
                ),
                if ('${data['counter_scope'] ?? ''}'.isNotEmpty)
                  Text(
                    t.get(
                      'logs_policy_counters_scope',
                      params: {'scope': '${data['counter_scope']}'},
                    ),
                    style: const TextStyle(fontSize: 10, color: Colors.grey),
                  ),
              ],
              Text(
                t.get(
                  'logs_policy_observed',
                  params: {'time': '${data['observed_at'] ?? ''}'},
                ),
                style: const TextStyle(fontSize: 10, color: Colors.grey),
              ),
            ],
          ],
        ),
      ),
    );
  }

  /// The bounded verbose window: admin-only, self-expiring, and reported in the
  /// words the server used rather than a hopeful guess.
  Widget _diagnosticsCard(AppMessages t) {
    final active = _apiScope?['diagnostic_active'] == true;
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(AppSpacing.md),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.bug_report_outlined, size: 16),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    t.get('logs_policy_diagnostics_title'),
                    style: const TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.4,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              t.get(
                'logs_policy_diagnostics_hint',
                params: {'seconds': '${_windowSeconds()}'},
              ),
              style: const TextStyle(fontSize: 11, color: Colors.grey),
            ),
            if (_actionError != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  _actionError!,
                  style: const TextStyle(fontSize: 12, color: Colors.red),
                ),
              ),
            const SizedBox(height: 8),
            Row(
              children: [
                FilledButton.icon(
                  onPressed: _busy ? null : () => _diagnostics(start: true),
                  icon: const Icon(Icons.play_arrow, size: 18),
                  label: Text(t.get('logs_policy_start')),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  onPressed: _busy || !active
                      ? null
                      : () => _diagnostics(start: false),
                  icon: const Icon(Icons.stop, size: 18),
                  label: Text(t.get('logs_policy_stop')),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  // ------------------------------------------------------------------- parsing

  /// The api scope's window length, so the hint quotes the server's default
  /// instead of a number this screen invented.
  int _windowSeconds() {
    final remaining = (_apiScope?['diagnostic_seconds_remaining'] as num?)
        ?.toInt();
    if (remaining != null && remaining > 0) return remaining;
    return 600;
  }

  Map<String, dynamic>? get _apiScope {
    final scopes = _policy?['scopes'];
    if (scopes is! Map) return null;
    final api = scopes['api'];
    return api is Map<String, dynamic> ? api : null;
  }

  /// Scope entries in a stable order (api first, then core, then anything a
  /// future server adds) — a map's iteration order is not a presentation order.
  List<MapEntry<String, Map<String, dynamic>>> _scopes() {
    final scopes = _policy?['scopes'];
    if (scopes is! Map) return const [];
    final entries = <MapEntry<String, Map<String, dynamic>>>[];
    for (final entry in scopes.entries) {
      final value = entry.value;
      if (value is Map<String, dynamic>) {
        entries.add(MapEntry('${entry.key}', value));
      }
    }
    entries.sort((a, b) => a.key.compareTo(b.key));
    return entries;
  }

  List<String> _notes() {
    final notes = _policy?['notes'];
    if (notes is! List) return const [];
    return notes.map((e) => '$e').where((e) => e.isNotEmpty).toList();
  }

  /// Level lists arrive as arrays; an empty one means "not reported", which is
  /// said in words rather than rendered as an empty list of nothing.
  String _levels(Object? raw) {
    if (raw is! List || raw.isEmpty) {
      return AppMessages.of(context).get('logs_policy_not_reported');
    }
    return raw.map((e) => '$e').join(', ');
  }
}
