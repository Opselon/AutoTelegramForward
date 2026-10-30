/// Durable operations: the async task queue (`GET /api/tasks`).
///
/// The backend runs long work — session backups, dead-letter replays and
/// purges, account backup exports — as durable tasks instead of blocking an
/// HTTP request. The web dashboard shows them; the mobile app never did, so an
/// operator could not see a backup that failed at 2am without opening a
/// browser. This view lists the caller's own tasks (the server scopes the
/// query to the JWT identity) and drives the two transitions the API offers:
/// cancel a queued/running task, retry a failed or cancelled one.
///
/// Contract notes:
///   * Task status is one of PENDING | RUNNING | RETRY_WAIT | SUCCEEDED |
///     FAILED | CANCELLED (proto enum names, as emitted by `formatRuleJSON`'s
///     sibling task projection). Unknown statuses render as themselves rather
///     than being forced into a known pill — an honest unknown beats a lie.
///   * While any task is non-terminal the view polls every 5 s; a settled
///     list costs no traffic. The timer lives only while this widget is
///     mounted.
///   * A failed read renders as a failure, never as an empty list.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_ui.dart';
import '../../widgets/error_state.dart';
import '../../widgets/loading_list.dart';

class TasksView extends StatefulWidget {
  const TasksView({super.key});

  @override
  State<TasksView> createState() => _TasksViewState();
}

class _TasksViewState extends State<TasksView>
    with AutomaticKeepAliveClientMixin {
  static const Duration _pollInterval = Duration(seconds: 5);

  List<UserTask> _tasks = const [];
  bool _loading = true;
  String? _error;

  /// The task a cancel/retry is currently running on, so only its own button
  /// shows the spinner.
  String _busyId = '';

  String? _actionError;

  Timer? _pollTimer;

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    // No AppMessages/AppState lookup here: inherited widgets are unavailable
    // in initState.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _load();
    });
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    super.dispose();
  }

  bool _isActive(UserTask task) => !task.isTerminal;

  void _schedulePoll() {
    _pollTimer?.cancel();
    if (_tasks.any(_isActive)) {
      _pollTimer = Timer(_pollInterval, () {
        if (mounted) _load(showSkeleton: false);
      });
    }
  }

  Future<void> _load({bool showSkeleton = true}) async {
    if (!mounted) return;
    setState(() {
      if (showSkeleton) _loading = true;
      _error = null;
    });

    final client = context.read<AppState>().buildApiClient();
    try {
      final page = await client.listTasks(limit: 50);
      if (!mounted) return;
      setState(() => _tasks = page.tasks);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () => _error = exc.error.message.isNotEmpty
            ? exc.error.message
            : AppMessages.of(context).get('state_error'),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(() => _error = '$exc');
    } finally {
      if (mounted) {
        setState(() => _loading = false);
        _schedulePoll();
      }
    }
  }

  Future<void> _act(UserTask task, {required bool retry}) async {
    final t = AppMessages.of(context);
    final client = context.read<AppState>().buildApiClient();
    setState(() {
      _busyId = task.id;
      _actionError = null;
    });
    try {
      if (retry) {
        await client.retryTask(task.id);
      } else {
        await client.cancelTask(task.id);
      }
      if (!mounted) return;
      // Re-read: the state shown afterwards is the server's, not a guess.
      await _load(showSkeleton: false);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () => _actionError = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('state_error'),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(() => _actionError = '$exc');
    } finally {
      if (mounted) setState(() => _busyId = '');
    }
  }

  /// One pill per status the backend emits, localized; anything new falls
  /// through to its raw enum name so a server extension never renders blank.
  (String, Color) _statusDisplay(UserTask task) {
    final t = AppMessages.of(context);
    switch (task.status) {
      case 'PENDING':
        return (t.get('tasks_status_pending'), AppTheme.warning);
      case 'RUNNING':
        return (t.get('tasks_status_running'), AppTheme.info);
      case 'RETRY_WAIT':
        return (t.get('tasks_status_retry_wait'), AppTheme.warning);
      case 'SUCCEEDED':
        return (t.get('tasks_status_succeeded'), AppTheme.success);
      case 'FAILED':
        return (t.get('tasks_status_failed'), AppTheme.error);
      case 'CANCELLED':
        return (
          t.get('tasks_status_cancelled'),
          AppTheme.statusColor(false, neutralWhenFalse: true),
        );
    }
    return (task.status, AppTheme.statusColor(false, neutralWhenFalse: true));
  }

  /// The kind label: a catalog entry when the kind is known, the raw kind
  /// string otherwise (server extension kinds must stay readable).
  String _kindLabel(UserTask task) {
    final t = AppMessages.of(context);
    final key = switch (task.kind) {
      'session.backup' => 'tasks_kind_session_backup',
      'dlq.replay' => 'tasks_kind_dlq_replay',
      'dlq.purge' => 'tasks_kind_dlq_purge',
      'backup.export' => 'tasks_kind_backup_export',
      _ => '',
    };
    return key.isEmpty ? task.kind : t.get(key);
  }

  String _formatTime(int unixSeconds) {
    if (unixSeconds <= 0) return '';
    final dt = DateTime.fromMillisecondsSinceEpoch(unixSeconds * 1000)
        .toLocal();
    String two(int n) => n.toString().padLeft(2, '0');
    return '${dt.year}-${two(dt.month)}-${two(dt.day)} '
        '${two(dt.hour)}:${two(dt.minute)}';
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final t = AppMessages.of(context);
    final theme = Theme.of(context);

    if (_loading && _tasks.isEmpty && _error == null) {
      return const LoadingList(rows: 5, itemExtent: 96, showAvatar: false);
    }

    return RefreshIndicator(
      onRefresh: () => _load(),
      child: ListView(
        padding: AppSpacing.screen,
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          if (_actionError != null) ...[
            InlineBanner(message: _actionError!, severity: 'error'),
            AppSpacing.gapMd,
          ],
          if (_error != null)
            ErrorState(message: _error!, onRetry: () => _load())
          else if (_tasks.isEmpty)
            EmptyStateView(
              message: t.get('tasks_empty'),
              icon: Icons.task_outlined,
              color: AppTheme.primary,
            )
          else
            SectionCard(
              title: t.get('tasks_title'),
              icon: Icons.task_outlined,
              child: Column(
                children: [
                  for (var i = 0; i < _tasks.length; i++) ...[
                    if (i > 0) const Divider(height: 1),
                    _taskRow(theme, _tasks[i]),
                  ],
                ],
              ),
            ),
        ],
      ),
    );
  }

  Widget _taskRow(ThemeData theme, UserTask task) {
    final t = AppMessages.of(context);
    final (statusLabel, statusColor) = _statusDisplay(task);
    final busy = _busyId == task.id;
    final canCancel = _isActive(task);
    final canRetry = task.status == 'FAILED' || task.status == 'CANCELLED';
    final created = _formatTime(task.createdAt);
    final finished = _formatTime(task.finishedAt);

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: AppSpacing.sm),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _kindLabel(task),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodyMedium?.copyWith(
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  [
                    '#${task.id}',
                    if (task.maxAttempts > 1)
                      t.get(
                        'tasks_attempt_line',
                        params: {
                          'attempt': '${task.attempt}',
                          'max': '${task.maxAttempts}',
                        },
                      ),
                    if (created.isNotEmpty) created,
                    if (finished.isNotEmpty) finished,
                  ].join(' • '),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodySmall,
                ),
                if (task.error.isNotEmpty) ...[
                  const SizedBox(height: 2),
                  Text(
                    task.error,
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
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              StatusPill(label: statusLabel, color: statusColor, dense: true),
              if (canCancel || canRetry) ...[
                const SizedBox(height: AppSpacing.xs),
                SizedBox(
                  height: 32,
                  child: TextButton(
                    key: ValueKey<String>(
                      'task_${canRetry && !canCancel ? 'retry' : 'cancel'}_${task.id}',
                    ),
                    style: TextButton.styleFrom(
                      padding: const EdgeInsets.symmetric(
                        horizontal: AppSpacing.sm,
                      ),
                    ),
                    onPressed: busy
                        ? null
                        : () => _act(task, retry: canRetry && !canCancel),
                    child: busy
                        ? const SizedBox(
                            width: 14,
                            height: 14,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : Text(
                            canRetry && !canCancel
                                ? t.get('action_retry')
                                : t.get('action_cancel'),
                            style: theme.textTheme.labelMedium,
                          ),
                  ),
                ),
              ],
            ],
          ),
        ],
      ),
    );
  }
}
