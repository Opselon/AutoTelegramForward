/// Delivery activity: the counters, the recent jobs, and the two controls an
/// operator needs when something is stuck — retry and cancel.
///
/// The counters are read from `/api/auto-reply/stats` and never derived from the
/// page of jobs below them: a page is capped, and "pending 0" computed from 50
/// visible rows would be a confident lie. A failed read renders as a failure,
/// not as zeroes.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/auto_reply_models.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_card.dart';
import '../../widgets/app_ui.dart';
import '../../widgets/loading_list.dart';

class AutoReplyActivityView extends StatefulWidget {
  const AutoReplyActivityView({super.key});

  @override
  State<AutoReplyActivityView> createState() => _AutoReplyActivityViewState();
}

class _AutoReplyActivityViewState extends State<AutoReplyActivityView> {
  AutoReplyStats? _stats;
  List<AutoReplyJob> _jobs = const [];

  /// '' means "every status"; otherwise the value sent to the API verbatim.
  String _filter = '';
  bool _loading = true;
  String? _error;
  String _busyId = '';
  bool _bootstrapped = false;

  /// The filters the screen offers, in the order they appear.
  static const List<String> _filters = [
    '',
    'PENDING',
    'RETRYING',
    'DEAD_LETTER',
    'SENT',
    'FAILED',
  ];

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_bootstrapped) return;
    _bootstrapped = true;
    _load();
  }

  ApiClient _client() => context.read<AppState>().buildApiClient();

  Future<void> _load() async {
    if (!mounted) return;
    final t = AppMessages.of(context);
    setState(() {
      _loading = true;
      _error = null;
    });
    final client = _client();
    try {
      final stats = await client.autoReplyStats();
      final page = await client.listAutoReplyJobs(status: _filter);
      if (!mounted) return;
      setState(() {
        _stats = stats;
        _jobs = page.jobs;
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
            : t.get('state_error');
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

  Future<void> _setFilter(String value) async {
    if (_filter == value) return;
    setState(() => _filter = value);
    final client = _client();
    final t = AppMessages.of(context);
    setState(() => _loading = true);
    try {
      final page = await client.listAutoReplyJobs(status: _filter);
      if (!mounted) return;
      setState(() => _jobs = page.jobs);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () => _error = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('state_error'),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(() => _error = '$exc');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _act(AutoReplyJob job, {required bool retry}) async {
    final t = AppMessages.of(context);
    final client = _client();
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _busyId = job.id);
    try {
      final resp = retry
          ? await client.retryAutoReplyJob(job.id)
          : await client.cancelAutoReplyJob(job.id);
      if (!mounted) return;
      // Only an explicit `false` is a refusal; the contract answers
      // `{"success": true}` and older releases may omit the field entirely.
      if (resp['success'] == false) {
        final detail = resp['error'] is String && '${resp['error']}'.isNotEmpty
            ? '${resp['error']}'
            : t.get('state_error');
        messenger.showSnackBar(
          SnackBar(
            content: Text(
              t.get('ar_job_action_failed', params: {'error': detail}),
            ),
          ),
        );
        return;
      }
      messenger.showSnackBar(
        SnackBar(
          content: Text(t.get(retry ? 'ar_job_retried' : 'ar_job_cancelled')),
        ),
      );
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            t.get(
              'ar_job_action_failed',
              params: {
                'error': exc.error.message.isNotEmpty
                    ? exc.error.message
                    : t.get('state_error'),
              },
            ),
          ),
        ),
      );
    } catch (exc) {
      if (!mounted) return;
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            t.get('ar_job_action_failed', params: {'error': '$exc'}),
          ),
        ),
      );
    } finally {
      if (mounted) setState(() => _busyId = '');
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    if (_loading && _stats == null && _jobs.isEmpty) {
      return const LoadingList(rows: 6, itemExtent: 84, showAvatar: false);
    }
    final stats = _stats;
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: AppSpacing.screen,
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          if (_error != null) ...[
            InlineBanner(
              message: _error!,
              onRetry: _load,
              retryLabel: t.get('action_retry'),
            ),
            const SizedBox(height: AppSpacing.md),
          ],
          if (stats != null) ...[
            _statGrid(t, stats),
            const SizedBox(height: AppSpacing.lg),
          ],
          Wrap(
            spacing: AppSpacing.sm,
            runSpacing: AppSpacing.sm,
            children: [
              for (final filter in _filters)
                ChoiceChip(
                  key: ValueKey<String>(
                    'ar_job_filter_${filter.isEmpty ? 'all' : filter}',
                  ),
                  label: Text(t.get(_filterLabelKey(filter))),
                  selected: _filter == filter,
                  onSelected: (_) => _setFilter(filter),
                ),
            ],
          ),
          const SizedBox(height: AppSpacing.lg),
          if (_jobs.isEmpty)
            EmptyStateView(
              message: t.get('ar_jobs_empty'),
              icon: Icons.inbox_outlined,
            )
          else
            for (final job in _jobs) _jobCard(t, job),
        ],
      ),
    );
  }

  /// Filter chips reuse the status labels themselves: "Pending" as a filter and
  /// "Pending" as a job status must mean the same thing, and two catalogs of
  /// near-synonyms is how they stop doing that.
  String _filterLabelKey(String filter) => switch (filter) {
    'PENDING' => 'ar_status_pending',
    'RETRYING' => 'ar_status_retrying',
    'DEAD_LETTER' => 'ar_status_dead',
    'SENT' => 'ar_status_sent',
    'FAILED' => 'ar_status_failed',
    _ => 'logs_all',
  };

  Widget _statGrid(AppMessages t, AutoReplyStats stats) {
    final tiles = <Widget>[
      StatTile(
        label: t.get('ar_stat_sent_today'),
        value: '${stats.sentToday}',
        icon: Icons.send,
        gradient: AppGradients.success,
      ),
      StatTile(
        label: t.get('ar_stat_pending'),
        value: '${stats.pending}',
        icon: Icons.hourglass_bottom,
        gradient: AppGradients.gold,
      ),
      StatTile(
        label: t.get('ar_stat_retrying'),
        value: '${stats.retrying}',
        icon: Icons.replay,
        gradient: AppGradients.violet,
      ),
      StatTile(
        label: t.get('ar_stat_dead'),
        value: '${stats.deadLetter}',
        icon: Icons.error_outline,
        gradient: AppGradients.danger,
      ),
      StatTile(
        label: t.get('ar_stat_chats'),
        value: '${stats.chatsEnabled}',
        icon: Icons.forum_outlined,
        gradient: AppGradients.cyan,
      ),
      StatTile(
        label: t.get('ar_stat_avg_latency'),
        value: t.get(
          'sys_latency',
          params: {'ms': '${stats.averageLatencyMs.round()}'},
        ),
        icon: Icons.speed,
        gradient: AppGradients.brand,
      ),
    ];
    final rows = <Widget>[];
    for (var i = 0; i < tiles.length; i += 2) {
      rows.add(
        Padding(
          padding: const EdgeInsets.only(bottom: AppSpacing.md),
          child: IntrinsicHeight(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Expanded(child: tiles[i]),
                const SizedBox(width: AppSpacing.md),
                Expanded(
                  child: i + 1 < tiles.length
                      ? tiles[i + 1]
                      : const SizedBox.shrink(),
                ),
              ],
            ),
          ),
        ),
      );
    }
    return Column(children: rows);
  }

  Widget _jobCard(AppMessages t, AutoReplyJob job) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    final statusKey = autoReplyStatusKey(job.status);
    final busy = _busyId == job.id;
    final canRetry = job.isActive || job.isDeadLetter;
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.md),
      child: AppCard(
        accent: job.isDeadLetter ? palette.danger : null,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Text(
                    job.chatLabel,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.titleSmall,
                  ),
                ),
                const SizedBox(width: AppSpacing.sm),
                StatusPill(
                  label: statusKey.isEmpty ? job.status : t.get(statusKey),
                  color: job.isDeadLetter
                      ? palette.danger
                      : (job.isActive ? palette.warning : palette.success),
                  dense: true,
                ),
              ],
            ),
            if (job.maxAttempts > 0) ...[
              const SizedBox(height: 2),
              Text(
                t.get(
                  'ar_job_attempt',
                  params: {'n': '${job.attempt}', 'max': '${job.maxAttempts}'},
                ),
                style: TextStyle(color: palette.textMuted, fontSize: 11),
              ),
            ],
            if (job.inboundText.trim().isNotEmpty) ...[
              const SizedBox(height: AppSpacing.sm),
              _quote(t, t.get('ar_job_inbound'), job.inboundText),
            ],
            const SizedBox(height: AppSpacing.xs),
            _quote(
              t,
              t.get('ar_job_reply'),
              job.replyText.trim().isEmpty
                  ? t.get('ar_job_no_reply')
                  : job.replyText,
            ),
            if (job.error.trim().isNotEmpty) ...[
              const SizedBox(height: AppSpacing.sm),
              Text(
                job.errorCode.trim().isEmpty
                    ? job.error
                    : '${job.error} (${job.errorCode})',
                style: TextStyle(color: palette.danger, fontSize: 12),
              ),
            ],
            const SizedBox(height: AppSpacing.sm),
            Row(
              children: [
                if (job.latencyMs > 0)
                  Text(
                    t.get('sys_latency', params: {'ms': '${job.latencyMs}'}),
                    style: TextStyle(color: palette.textMuted, fontSize: 11),
                  ),
                const Spacer(),
                if (canRetry)
                  TextButton.icon(
                    key: ValueKey<String>('ar_job_retry_${job.id}'),
                    onPressed: busy ? null : () => _act(job, retry: true),
                    icon: const Icon(Icons.replay, size: 18),
                    label: Text(t.get('action_retry')),
                  ),
                if (job.isActive)
                  TextButton.icon(
                    key: ValueKey<String>('ar_job_cancel_${job.id}'),
                    onPressed: busy ? null : () => _act(job, retry: false),
                    icon: const Icon(Icons.cancel_outlined, size: 18),
                    label: Text(t.get('action_cancel')),
                  ),
                if (busy)
                  const Padding(
                    padding: EdgeInsetsDirectional.only(start: AppSpacing.sm),
                    child: SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _quote(AppMessages t, String label, String text) {
    final palette = AppColors.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: TextStyle(
            color: palette.textMuted,
            fontSize: 10,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.3,
          ),
        ),
        Text(
          text,
          maxLines: 3,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontSize: 12),
        ),
      ],
    );
  }
}
