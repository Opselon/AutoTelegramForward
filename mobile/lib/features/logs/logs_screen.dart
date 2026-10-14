/// Operational logs explorer + centralized audit events.
///
/// Two sources are merged into one searchable view:
///   * /api/logs — the Central Logger stream (debug/diagnostic events)
///   * /api/audit/events — the durable PostgreSQL audit trail (execution
///     assurance: actual outcomes, correlation IDs, reason codes)
///
/// Both are loaded independently; a failure in one must not blank the other,
/// and a network failure must never be presented as "no data".
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/list_state_view.dart';

class LogsScreen extends StatefulWidget {
  const LogsScreen({super.key});

  @override
  State<LogsScreen> createState() => _LogsScreenState();
}

class _LogsScreenState extends State<LogsScreen> {
  List<LogEntry> _logs = const [];
  List<AuditEvent> _audit = const [];
  bool _loading = false;
  bool _loadingAudit = false;
  String? _error;
  String? _auditError;
  String _levelFilter = '';
  String _search = '';
  String _correlationSearch = '';

  @override
  void initState() {
    super.initState();
    _load();
    _loadAudit();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    final client = context.read<AppState>().buildApiClient();
    try {
      final list = await client.queryLogs(limit: 50, level: _levelFilter);
      if (!mounted) return;
      setState(() => _logs = list);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () =>
            _error =
                exc.error.message.isNotEmpty
                    ? exc.error.message
                    : 'Logger request failed',
      );
    } catch (exc) {
      if (!mounted) return;
      // Never swallow: a dead endpoint must not masquerade as "no logs".
      setState(
        () =>
            _error =
                'Cannot reach the log service: '
                '${exc.toString().split('(').first.trim()}',
      );
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _loadAudit() async {
    setState(() {
      _loadingAudit = true;
      _auditError = null;
    });
    final client = context.read<AppState>().buildApiClient();
    try {
      final list = await client.queryAuditEvents(
        limit: 50,
        correlationId: _correlationSearch,
      );
      if (!mounted) return;
      setState(() => _audit = list);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      // Audit events are a newer endpoint; an older server may not ship it.
      // Degrade to "unavailable", do not block the log view.
      setState(
        () =>
            _auditError =
                exc.error.message.isNotEmpty
                    ? exc.error.message
                    : 'Audit events unavailable on this server',
      );
    } catch (exc) {
      if (!mounted) return;
      setState(
        () =>
            _auditError =
                'Audit events unavailable: '
                '${exc.toString().split('(').first.trim()}',
      );
    } finally {
      if (mounted) setState(() => _loadingAudit = false);
    }
  }

  Future<void> _reloadAll() async {
    await Future.wait([_load(), _loadAudit()]);
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return DefaultTabController(
      length: 2,
      child: Scaffold(
        appBar: AppBar(
          title: Text(t.get('logs_title')),
          actions: [
            IconButton(
              icon: const Icon(Icons.refresh),
              tooltip: t.get('action_refresh'),
              onPressed: (_loading || _loadingAudit) ? null : _reloadAll,
            ),
          ],
          bottom: const TabBar(
            tabs: [
              Tab(icon: Icon(Icons.history_outlined), text: 'Logs'),
              Tab(icon: Icon(Icons.verified_outlined), text: 'Audit'),
            ],
          ),
        ),
        body: TabBarView(children: [_buildLogsTab(t), _buildAuditTab(t)]),
      ),
    );
  }

  // ----------------------------------------------------------------------- //
  // Logs tab (Central Logger stream)
  // ----------------------------------------------------------------------- //
  Widget _buildLogsTab(AppMessages t) {
    final filtered =
        _search.isEmpty
            ? _logs
            : _logs
                .where(
                  (l) =>
                      l.message.toLowerCase().contains(_search.toLowerCase()) ||
                      l.category.toLowerCase().contains(_search.toLowerCase()),
                )
                .toList();

    return Column(
      children: [
        // Level chips + search
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          child: Row(
            children: [
              ChoiceChip(
                label: const Text('All'),
                selected: _levelFilter.isEmpty,
                onSelected: (val) {
                  if (val) {
                    setState(() => _levelFilter = '');
                    _load();
                  }
                },
              ),
              const SizedBox(width: 8),
              ChoiceChip(
                label: const Text('Errors'),
                selected: _levelFilter == 'ERROR',
                onSelected: (val) {
                  if (val) {
                    setState(() => _levelFilter = 'ERROR');
                    _load();
                  }
                },
              ),
              const SizedBox(width: 8),
              ChoiceChip(
                label: const Text('Warnings'),
                selected: _levelFilter == 'WARN',
                onSelected: (val) {
                  if (val) {
                    setState(() => _levelFilter = 'WARN');
                    _load();
                  }
                },
              ),
              const SizedBox(width: 8),
              ChoiceChip(
                label: const Text('Info'),
                selected: _levelFilter == 'INFO',
                onSelected: (val) {
                  if (val) {
                    setState(() => _levelFilter = 'INFO');
                    _load();
                  }
                },
              ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: TextField(
            decoration: const InputDecoration(
              isDense: true,
              prefixIcon: Icon(Icons.search, size: 18),
              hintText: 'Search messages…',
              border: OutlineInputBorder(),
            ),
            onChanged: (v) => setState(() => _search = v),
          ),
        ),
        const SizedBox(height: 8),
        const Divider(height: 1),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _reloadAll,
            child:
                _logs.isEmpty && !_loading
                    ? ListStateView(
                      isEmpty: true,
                      isLoading: false,
                      error: _error,
                      emptyTitle: t.get('logs_empty'),
                      onRetry: _reloadAll,
                    )
                    : _error != null && filtered.isEmpty
                    ? ListStateView(
                      isEmpty: false,
                      isLoading: false,
                      error: _error,
                      emptyTitle: t.get('logs_empty'),
                      onRetry: _reloadAll,
                    )
                    : ListView.separated(
                      padding: const EdgeInsets.all(16),
                      itemCount: filtered.length,
                      separatorBuilder: (_, __) => const Divider(height: 1),
                      itemBuilder: (context, index) {
                        final log = filtered[index];
                        final isErr =
                            log.level == 'error' || log.level == 'ERROR';
                        final isWarn =
                            log.level == 'warning' || log.level == 'WARN';
                        return ListTile(
                          leading: Icon(
                            isErr
                                ? Icons.error
                                : (isWarn ? Icons.warning : Icons.info_outline),
                            color:
                                isErr
                                    ? Colors.red
                                    : (isWarn ? Colors.amber : Colors.blue),
                            size: 20,
                          ),
                          title: Text(
                            log.message,
                            style: const TextStyle(fontSize: 14),
                          ),
                          subtitle: Text(
                            '${log.category} • ${log.timestamp}',
                            style: const TextStyle(
                              fontSize: 12,
                              color: Colors.grey,
                            ),
                          ),
                        );
                      },
                    ),
          ),
        ),
      ],
    );
  }

  // ----------------------------------------------------------------------- //
  // Audit tab (durable execution-assurance events)
  // ----------------------------------------------------------------------- //
  Widget _buildAuditTab(AppMessages t) {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          child: TextField(
            decoration: const InputDecoration(
              isDense: true,
              prefixIcon: Icon(Icons.link, size: 18),
              hintText: 'Search by correlation ID…',
              border: OutlineInputBorder(),
            ),
            onChanged: (v) => setState(() => _correlationSearch = v),
            onSubmitted: (_) => _loadAudit(),
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _reloadAll,
            child:
                _audit.isEmpty && !_loadingAudit
                    ? ListStateView(
                      isEmpty: true,
                      isLoading: false,
                      error: _auditError,
                      emptyTitle: 'No audit events yet',
                      emptySubtitle:
                          'Execution-assurance events appear here as messages '
                          'flow through the pipeline.\n'
                          'Failures show the reason code; pending work stays '
                          'distinct from success.',
                      onRetry: _loadAudit,
                    )
                    : ListView.separated(
                      padding: const EdgeInsets.all(16),
                      itemCount: _audit.length,
                      separatorBuilder: (_, __) => const Divider(height: 1),
                      itemBuilder: (context, index) {
                        final evt = _audit[index];
                        final outcome = evt.outcome.toUpperCase();
                        final isFail = outcome == 'FAILED';
                        final isUnconfirmed = outcome == 'OUTCOME_UNCONFIRMED';
                        final isOk = outcome == 'SUCCEEDED';
                        return ListTile(
                          leading: Icon(
                            isOk
                                ? Icons.check_circle
                                : (isFail
                                    ? Icons.cancel
                                    : (isUnconfirmed
                                        ? Icons.help_outline
                                        : Icons.history)),
                            color:
                                isOk
                                    ? Colors.green
                                    : (isFail
                                        ? Colors.red
                                        : (isUnconfirmed
                                            ? Colors.amber
                                            : Colors.grey)),
                            size: 20,
                          ),
                          title: Text(
                            evt.eventType,
                            style: const TextStyle(
                              fontSize: 14,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          subtitle: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              if (evt.reasonCode.isNotEmpty)
                                Text(
                                  evt.reasonCode,
                                  style: TextStyle(
                                    fontSize: 11,
                                    color: Colors.red.shade300,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              Text(
                                '${evt.service} • ${evt.timestamp} • ${evt.outcome}',
                                style: const TextStyle(
                                  fontSize: 12,
                                  color: Colors.grey,
                                ),
                              ),
                              if (evt.resultSummary.isNotEmpty)
                                Text(
                                  evt.resultSummary,
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    fontSize: 12,
                                    color: Colors.grey,
                                  ),
                                ),
                            ],
                          ),
                          trailing:
                              evt.correlationId.isNotEmpty
                                  ? Text(
                                    evt.correlationId.length > 8
                                        ? evt.correlationId.substring(0, 8)
                                        : evt.correlationId,
                                    style: const TextStyle(
                                      fontSize: 11,
                                      fontFamily: 'monospace',
                                      color: Colors.grey,
                                    ),
                                  )
                                  : null,
                          onTap: () => _showAuditDetail(context, evt),
                        );
                      },
                    ),
          ),
        ),
      ],
    );
  }

  void _showAuditDetail(BuildContext context, AuditEvent evt) {
    final fields = <(String, String)>[
      ('event_type', evt.eventType),
      ('outcome', evt.outcome),
      ('service', evt.service),
      ('severity', evt.severity),
      ('reason_code', evt.reasonCode),
      ('correlation_id', evt.correlationId),
      ('rule_id', evt.ruleId),
      ('feature_id', evt.featureId),
      ('duration_ms', evt.durationMs > 0 ? evt.durationMs.toString() : ''),
      ('error_code', evt.errorCode),
      ('result_summary', evt.resultSummary),
    ];

    showModalBottomSheet(
      context: context,
      showDragHandle: true,
      builder:
          (ctx) => SafeArea(
            child: ListView(
              shrinkWrap: true,
              padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
              children: [
                Text(
                  evt.eventType,
                  style: const TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 12),
                ...fields
                    .where((f) => f.$2.isNotEmpty)
                    .map(
                      (f) => Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            SizedBox(
                              width: 112,
                              child: Text(
                                f.$1,
                                style: const TextStyle(
                                  fontSize: 12,
                                  color: Colors.grey,
                                ),
                              ),
                            ),
                            Expanded(
                              child: Text(
                                f.$2,
                                style: TextStyle(
                                  fontSize: 12,
                                  fontFamily:
                                      f.$1.contains('id') ? 'monospace' : null,
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
              ],
            ),
          ),
    );
  }
}
