/// The Delivery page: what the engine is doing right now, and what it gave up
/// on.
///
/// Two questions, two tabs:
///   * **Queue** — live counters (`GET /api/queue`) plus one row per task.
///     "In queue" is the number that separates *slow* from *stuck*: a rising
///     queue with flat error counts is latency, a rising dead-letter count is
///     failure. The counters are tenant-scoped server-side from the JWT, so
///     this page can never show another account's throughput.
///   * **Dead letter** — the jobs that exhausted their retries
///     (`GET /api/dlq`), each one replayable (`POST /api/dlq/{id}/retry`) or
///     clearable (`DELETE /api/dlq`, tenant-scoped on the server).
///
/// Three properties this screen deliberately keeps:
///   * a failed read renders as a failure, never as zeroes — a dashboard that
///     prints "0 failed" when the request 500'd is worse than an error banner;
///   * every count reported after an action is re-read from the server: the
///     purge total is the difference between two real reads, not a guess, so
///     "Removed 4 jobs" is a fact the server confirmed;
///   * a purge is confirmed by a dialog naming what will go, and the client
///     never decides the scope — it always asks the server for "everything I
///     own", which is the scope the backend enforces anyway.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_ui.dart';

class DeliveryScreen extends StatefulWidget {
  const DeliveryScreen({super.key});

  @override
  State<DeliveryScreen> createState() => _DeliveryScreenState();
}

class _DeliveryScreenState extends State<DeliveryScreen> {
  /// `null` until a read has actually succeeded. It stays null on failure so
  /// the tiles can show a placeholder instead of a confident zero.
  DeliveryStats? _stats;
  List<DeadLetterJob> _dlq = const [];

  bool _loading = true;
  bool _loadingDlq = true;
  String? _error;
  String? _dlqError;

  /// The job currently being replayed, so only its own button spins.
  String _busyId = '';

  /// Set while a purge is in flight.
  bool _purging = false;

  /// Message from a replay/purge that the server refused.
  String? _actionError;

  @override
  void initState() {
    super.initState();
    // No AppMessages/AppState lookup here: inherited widgets are unavailable in
    // initState. `_loadAll` reads them once it is safe.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _loadAll();
    });
  }

  ApiClient _client() => context.read<AppState>().buildApiClient();

  Future<void> _loadAll() async {
    if (!mounted) return;
    final client = _client();
    setState(() {
      _loading = true;
      _loadingDlq = true;
      _error = null;
      _dlqError = null;
      _actionError = null;
    });
    // Two independent reads: a queue that will not answer must not hide the
    // dead-letter backlog, and vice versa.
    await Future.wait<void>([_loadStats(client), _loadDlq(client)]);
  }

  Future<void> _loadStats(ApiClient client) async {
    try {
      final stats = await client.deliveryStats();
      if (!mounted) return;
      setState(() {
        _stats = stats;
        _error = null;
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() {
        _stats = null;
        _error = exc.error.message.isNotEmpty
            ? exc.error.message
            : AppMessages.of(context).get('state_error');
      });
    } catch (exc) {
      if (!mounted) return;
      setState(() {
        _stats = null;
        _error = '$exc';
      });
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _loadDlq(ApiClient client) async {
    try {
      final jobs = await client.deadLetter();
      if (!mounted) return;
      setState(() {
        _dlq = jobs;
        _dlqError = null;
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() {
        _dlq = const [];
        _dlqError = exc.error.message.isNotEmpty
            ? exc.error.message
            : AppMessages.of(context).get('state_error');
      });
    } catch (exc) {
      if (!mounted) return;
      setState(() {
        _dlq = const [];
        _dlqError = '$exc';
      });
    } finally {
      if (mounted) setState(() => _loadingDlq = false);
    }
  }

  /// A failed action still has to say *something* actionable. The core reports
  /// `not_found` / `replay_failed` / `purge_failed` with a message, so prefer
  /// the message, then the code, then the generic error string.
  String _failureDetail(Map<String, dynamic> resp, String fallback) {
    for (final key in ['message', 'error_code', 'error']) {
      final value = resp[key];
      if (value is String && value.trim().isNotEmpty) return value.trim();
    }
    return fallback;
  }

  String? _ruleName(String ruleId) {
    final stats = _stats;
    if (stats == null) return null;
    for (final stat in stats.rules) {
      if (stat.ruleId == ruleId && stat.ruleName.isNotEmpty) {
        return stat.ruleName;
      }
    }
    return null;
  }

  /// Replays exactly one dead-lettered job.
  ///
  /// The count in the confirmation is 1 by construction — one job was asked
  /// for, and the server answers with success/failure, not a tally. The list is
  /// re-read afterwards, so a replay the server accepted but did not perform
  /// still shows up as the row that stayed.
  Future<void> _replay(DeadLetterJob job) async {
    final t = AppMessages.of(context);
    final client = _client();
    final messenger = ScaffoldMessenger.of(context);
    setState(() {
      _busyId = job.id;
      _actionError = null;
    });
    try {
      final resp = await client.retryDeadLetter(job.id);
      if (!mounted) return;
      if (resp['success'] == false) {
        setState(
          () => _actionError = t.get(
            'delivery_retry_failed',
            params: {'error': _failureDetail(resp, t.get('state_error'))},
          ),
        );
        return;
      }
      messenger.showSnackBar(
        SnackBar(content: Text(t.get('delivery_retry_ok', params: {'n': '1'}))),
      );
      await _loadDlq(client);
      if (mounted) await _loadStats(client);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () => _actionError = t.get(
          'delivery_retry_failed',
          params: {'error': exc.error.message},
        ),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(
        () => _actionError = t.get(
          'delivery_retry_failed',
          params: {'error': '$exc'},
        ),
      );
    } finally {
      if (mounted) setState(() => _busyId = '');
    }
  }

  /// Clears the dead-letter backlog this account can see.
  ///
  /// The number shown afterwards is measured, not assumed: the backlog is read
  /// before and after the delete, and the difference is what gets reported. If
  /// the second read fails there is no honest number to show, so the failure is
  /// surfaced instead.
  Future<void> _purge() async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('delivery_purge')),
        content: Text(t.get('delivery_purge_confirm')),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(t.get('action_cancel')),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(t.get('action_confirm')),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    final client = _client();
    final messenger = ScaffoldMessenger.of(context);
    final before = _dlq.length;
    setState(() {
      _purging = true;
      _actionError = null;
    });
    try {
      final resp = await client.purgeDeadLetter();
      if (!mounted) return;
      if (resp['success'] == false) {
        setState(
          () => _actionError = _failureDetail(resp, t.get('state_error')),
        );
        return;
      }
      await _loadDlq(client);
      if (!mounted) return;
      if (_dlqError != null) return; // the re-read failed: claim nothing
      final removed = before - _dlq.length;
      messenger.showSnackBar(
        SnackBar(
          content: Text(t.get('delivery_purge_ok', params: {'n': '$removed'})),
        ),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() => _actionError = exc.error.message);
    } catch (exc) {
      if (!mounted) return;
      setState(() => _actionError = '$exc');
    } finally {
      if (mounted) setState(() => _purging = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    return DefaultTabController(
      length: 2,
      child: Scaffold(
        appBar: AppBar(
          title: Text(t.get('delivery_title')),
          actions: [
            IconButton(
              icon: const Icon(Icons.refresh),
              tooltip: t.get('action_refresh'),
              onPressed: _loading || _loadingDlq ? null : _loadAll,
            ),
          ],
          bottom: TabBar(
            tabs: [
              Tab(text: t.get('delivery_tab_queue')),
              Tab(text: t.get('delivery_tab_dlq')),
            ],
          ),
        ),
        body: TabBarView(children: [_queueTab(t), _deadLetterTab(t)]),
      ),
    );
  }

  // ------------------------------------------------------------------ queue

  Widget _queueTab(AppMessages t) {
    if (_loading && _stats == null) {
      return const Center(child: CircularProgressIndicator());
    }
    final stats = _stats;
    return RefreshIndicator(
      onRefresh: _loadAll,
      child: ListView(
        padding: AppSpacing.screen,
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          GradientHeader(
            title: t.get('delivery_title'),
            subtitle: t.get('delivery_subtitle'),
            icon: Icons.local_shipping_outlined,
          ),
          const SizedBox(height: AppSpacing.lg),
          if (_error != null)
            // The counters are unknown, not zero. Say which state this is.
            InlineBanner(
              message: _error!,
              onRetry: _loadAll,
              retryLabel: t.get('action_retry'),
            )
          else if (stats == null)
            EmptyStateView(
              message: t.get('state_error'),
              icon: Icons.cloud_off,
              color: AppTheme.error,
            )
          else ...[
            _metrics(t, stats),
            const SizedBox(height: AppSpacing.lg),
            _perTask(t, stats),
            const SizedBox(height: AppSpacing.lg),
            _recentErrors(t, stats),
          ],
        ],
      ),
    );
  }

  /// Metric tiles in rows of two, so an odd count leaves an empty cell instead
  /// of a stretched tile.
  Widget _metrics(AppMessages t, DeliveryStats stats) {
    final tiles = <Widget>[
      StatTile(
        label: t.get('delivery_forwarded'),
        value: '${stats.forwardedTotal}',
        icon: Icons.send,
        gradient: AppGradients.success,
      ),
      StatTile(
        label: t.get('delivery_filtered'),
        value: '${stats.filteredTotal}',
        icon: Icons.filter_alt,
        gradient: AppGradients.cyan,
      ),
      StatTile(
        label: t.get('delivery_failed'),
        value: '${stats.failedTotal}',
        icon: Icons.error_outline,
        gradient: AppGradients.danger,
      ),
      StatTile(
        label: t.get('delivery_in_queue'),
        value: '${stats.inQueue}',
        icon: Icons.hourglass_bottom,
        gradient: AppGradients.gold,
      ),
      StatTile(
        label: t.get('delivery_retried'),
        value: '${stats.retryTotal}',
        icon: Icons.replay,
        gradient: AppGradients.violet,
      ),
    ];
    final rows = <Widget>[];
    for (var i = 0; i < tiles.length; i += 2) {
      final first = tiles[i];
      final second = i + 1 < tiles.length ? tiles[i + 1] : null;
      rows.add(
        Padding(
          padding: const EdgeInsets.only(bottom: AppSpacing.md),
          // IntrinsicHeight, not a fixed tile height: a label that wraps in one
          // language must not clip in another, and the pair still matches.
          child: IntrinsicHeight(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Expanded(child: first),
                const SizedBox(width: AppSpacing.md),
                Expanded(child: second ?? const SizedBox.shrink()),
              ],
            ),
          ),
        ),
      );
    }
    return Column(children: rows);
  }

  Widget _perTask(AppMessages t, DeliveryStats stats) {
    return SectionCard(
      title: t.get('rules_title'),
      icon: Icons.alt_route,
      child: stats.rules.isEmpty
          ? EmptyStateView(
              message: t.get('delivery_no_rules'),
              icon: Icons.send_outlined,
            )
          : Column(
              children: [for (final rule in stats.rules) _ruleRow(t, rule)],
            ),
    );
  }

  Widget _ruleRow(AppMessages t, RuleLiveStat rule) {
    final theme = Theme.of(context);
    final label = rule.ruleName.isNotEmpty ? rule.ruleName : rule.ruleId;
    // Paused outranks active: a task the operator stopped will not deliver, so
    // calling it RUNNING because its counters exist would be a lie.
    final (pillLabel, pillColor) = rule.isPaused
        ? (t.get('rules_status_paused'), AppTheme.warning)
        : rule.isActive
        ? (t.get('rules_status_active'), AppTheme.success)
        : (
            t.get('rules_state_off'),
            AppTheme.statusColor(false, neutralWhenFalse: true),
          );
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.sm),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodyMedium?.copyWith(
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  t.get(
                    'delivery_rule_line',
                    params: {
                      'forwarded': '${rule.forwarded}',
                      'errors': '${rule.errors}',
                    },
                  ),
                  style: theme.textTheme.bodySmall,
                ),
                if (rule.lastError.isNotEmpty) ...[
                  const SizedBox(height: 2),
                  Text(
                    rule.lastError,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.bodySmall?.copyWith(
                      color: AppTheme.error,
                    ),
                  ),
                ],
              ],
            ),
          ),
          const SizedBox(width: AppSpacing.sm),
          StatusPill(label: pillLabel, color: pillColor, dense: true),
        ],
      ),
    );
  }

  Widget _recentErrors(AppMessages t, DeliveryStats stats) {
    final theme = Theme.of(context);
    return SectionCard(
      title: t.get('dash_recent_errors'),
      icon: Icons.report_problem_outlined,
      child: stats.errors.isEmpty
          // Genuinely zero recent errors is good news, and the empty state
          // says exactly that rather than leaving a blank card.
          ? EmptyStateView(
              message: t.get('logs_empty'),
              icon: Icons.check_circle_outline,
              color: AppTheme.success,
            )
          : Column(
              children: [
                for (final err in stats.errors)
                  Padding(
                    padding: const EdgeInsets.only(bottom: AppSpacing.sm),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                err.errorName.isNotEmpty
                                    ? err.errorName
                                    : err.category,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: theme.textTheme.bodyMedium?.copyWith(
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                              if (err.detail.isNotEmpty) ...[
                                const SizedBox(height: 2),
                                Text(
                                  err.detail,
                                  maxLines: 3,
                                  overflow: TextOverflow.ellipsis,
                                  style: theme.textTheme.bodySmall,
                                ),
                              ],
                            ],
                          ),
                        ),
                        const SizedBox(width: AppSpacing.sm),
                        StatusPill(
                          label: err.severity.toUpperCase(),
                          color: AppTheme.severityColor(err.severity),
                          dense: true,
                        ),
                      ],
                    ),
                  ),
              ],
            ),
    );
  }

  // ------------------------------------------------------------ dead letter

  Widget _deadLetterTab(AppMessages t) {
    if (_loadingDlq && _dlq.isEmpty && _dlqError == null) {
      return const Center(child: CircularProgressIndicator());
    }
    return RefreshIndicator(
      onRefresh: () async {
        final client = _client();
        setState(() {
          _loadingDlq = true;
          _dlqError = null;
        });
        await _loadDlq(client);
      },
      child: ListView(
        padding: AppSpacing.screen,
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          if (_actionError != null) ...[
            InlineBanner(message: _actionError!, severity: 'error'),
            const SizedBox(height: AppSpacing.md),
          ],
          if (_dlqError != null)
            InlineBanner(
              message: _dlqError!,
              onRetry: _loadAll,
              retryLabel: t.get('action_retry'),
            )
          else ...[
            Row(
              children: [
                Expanded(
                  child: Text(
                    t.get('delivery_tab_dlq'),
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                if (_dlq.isNotEmpty)
                  OutlinedButton.icon(
                    key: const ValueKey<String>('delivery_purge'),
                    onPressed: _purging ? null : _purge,
                    icon: const Icon(Icons.delete_sweep_outlined, size: 18),
                    label: Text(t.get('delivery_purge')),
                  ),
              ],
            ),
            const SizedBox(height: AppSpacing.md),
            if (_dlq.isEmpty)
              EmptyStateView(
                message: t.get('delivery_dlq_empty'),
                icon: Icons.check_circle_outline,
                color: AppTheme.success,
              )
            else
              for (final job in _dlq) _jobCard(t, job),
          ],
        ],
      ),
    );
  }

  Widget _jobCard(AppMessages t, DeadLetterJob job) {
    final theme = Theme.of(context);
    final ruleLabel = _ruleName(job.ruleId) ?? job.ruleId;
    final busy = _busyId == job.id;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(AppSpacing.md),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Text(
                    ruleLabel,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.titleSmall,
                  ),
                ),
                const SizedBox(width: AppSpacing.sm),
                StatusPill(
                  label: t.get(
                    'delivery_attempts',
                    params: {'n': '${job.attempts}'},
                  ),
                  color: AppTheme.error,
                  dense: true,
                ),
              ],
            ),
            const SizedBox(height: AppSpacing.sm),
            Text(
              '${job.sourceChatId} → ${job.targetChatId}',
              style: theme.textTheme.bodySmall,
            ),
            if (job.errorCategory.isNotEmpty) ...[
              const SizedBox(height: 2),
              Text(
                job.errorCategory,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: AppTheme.warning,
                ),
              ),
            ],
            if (job.lastError.isNotEmpty) ...[
              const SizedBox(height: AppSpacing.sm),
              Text(job.lastError, style: theme.textTheme.bodySmall),
            ],
            const SizedBox(height: AppSpacing.sm),
            Align(
              alignment: AlignmentDirectional.centerEnd,
              child: TextButton.icon(
                key: ValueKey<String>('delivery_replay_${job.id}'),
                onPressed: busy ? null : () => _replay(job),
                icon: busy
                    ? const SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.replay, size: 18),
                label: Text(t.get('delivery_dlq_retry')),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
