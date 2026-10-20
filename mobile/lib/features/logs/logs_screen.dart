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

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/design_tokens.dart';
import '../../core/layout.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_button.dart';
import '../../widgets/app_field.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/app_ui.dart';
import '../../widgets/empty_state.dart';
import '../../widgets/error_state.dart';
import '../../widgets/loading_list.dart';
import 'log_policy_tab.dart';

class LogsScreen extends StatefulWidget {
  const LogsScreen({
    super.key,
    this.initialRuleId = '',
    this.initialRuleLabel = '',
  });

  /// Open the stream already scoped to one task. Empty means all tasks, which
  /// is what the Logs tab of the home screen uses.
  final String initialRuleId;

  /// Display name for [initialRuleId]; the API only ever returns ids.
  final String initialRuleLabel;

  @override
  State<LogsScreen> createState() => _LogsScreenState();
}

class _LogsScreenState extends State<LogsScreen> {
  List<LogEntry> _logs = const [];
  List<AuditEvent> _audit = const [];
  bool _loading = false;
  bool _loadingAudit = false;
  bool _exporting = false;
  String? _error;
  String? _auditError;
  String _levelFilter = '';
  String _search = '';
  String _correlationSearch = '';

  /// Entry shown in the expanded layout's detail pane. Null means "nothing
  /// selected yet" and the pane shows the select prompt.
  LogEntry? _selectedLog;

  // --- v18 history controls -------------------------------------------- //
  /// Real match count for the current filter, across the whole archive.
  int _total = 0;
  int _offset = 0;
  int _pageSize = 50;
  bool _ascending = false;
  bool _hasMore = false;

  /// Scope the stream to one task or one service.
  String _ruleFilter = '';
  String _serviceFilter = '';

  /// Oldest record the archive still holds; tells the operator how far back
  /// paging can actually go instead of implying the history is complete.
  int _oldestTs = 0;

  /// Rules offered in the task picker. Loaded once, tolerated to fail: an
  /// older server without /api/rules must not break the log view.
  List<ForwardRule> _rules = const [];

  /// One-shot guard: `didChangeDependencies` can fire more than once, and a
  /// second fetch on every theme/locale change would hammer the API.
  bool _bootstrapped = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_bootstrapped) return;
    _bootstrapped = true;
    // Must run here, not in initState: this screen reads AppMessages (via
    // Localizations) and AppState (via Provider) — both are inherited widgets,
    // and depending on them before initState completes throws.
    if (widget.initialRuleId.isNotEmpty) {
      _ruleFilter = widget.initialRuleId;
    }
    _load();
    _loadAudit();
    _loadRuleIndex();
  }

  /// Fetches "history from" depth for the current filter. Never throws: the
  /// badge is decoration, and losing it must not fail the log fetch.
  Future<void> _loadOldest() async {
    final client = context.read<AppState>().buildApiClient();
    try {
      final stats = await client.logStats(ruleId: _ruleFilter);
      if (!mounted) return;
      setState(() => _oldestTs = stats.oldestTs);
    } catch (_) {
      // Cosmetic only.
    }
  }

  /// Loads the task list powering the picker. Best-effort: a denial or an older
  /// server leaves the picker empty and the rest of the screen unaffected.
  Future<void> _loadRuleIndex() async {
    final client = context.read<AppState>().buildApiClient();
    try {
      final rules = await client.listRules();
      if (!mounted) return;
      setState(() => _rules = rules);
    } catch (_) {
      // Intentionally silent — the task filter is optional.
    }
  }

  /// Exports the current filter as JSON or CSV and shows it for copying.
  Future<void> _export(String format) async {
    final t = AppMessages.of(context);
    setState(() => _exporting = true);
    final client = context.read<AppState>().buildApiClient();
    try {
      final body = await client.exportLogs(
        format: format,
        level: _levelFilter,
        service: _serviceFilter,
        search: _search.trim(),
        ruleId: _ruleFilter,
      );
      if (!mounted) return;
      await showModalBottomSheet<void>(
        context: context,
        showDragHandle: true,
        isScrollControlled: true,
        builder: (ctx) => SafeArea(
          child: Padding(
            padding: const EdgeInsetsDirectional.fromSTEB(16, 0, 16, 16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        t.get(
                          'logs_export_summary',
                          params: {
                            'format': format.toUpperCase(),
                            'count': '${body.length}',
                          },
                        ),
                        style: const TextStyle(
                          fontSize: 15,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    TextButton.icon(
                      icon: const Icon(Icons.copy, size: 16),
                      label: Text(t.get('logs_copy')),
                      onPressed: () async {
                        await Clipboard.setData(ClipboardData(text: body));
                        if (ctx.mounted) Navigator.of(ctx).pop();
                        if (mounted) {
                          AppToast.success(
                            context,
                            t.get('logs_export_copied'),
                          );
                        }
                      },
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                ConstrainedBox(
                  constraints: const BoxConstraints(maxHeight: 380),
                  child: SingleChildScrollView(
                    child: SelectableText(
                      body.length > 20000
                          ? '${body.substring(0, 20000)}\n'
                                '${t.get('logs_truncated_display')}'
                          : body,
                      style: const TextStyle(
                        fontSize: 11,
                        fontFamily: 'monospace',
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            t.get('logs_export_failed', params: {'error': exc.error.message}),
          ),
        ),
      );
    } catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(t.get('logs_export_failed', params: {'error': '$exc'})),
        ),
      );
    } finally {
      if (mounted) setState(() => _exporting = false);
    }
  }

  // ----------------------------------------------------------------------- //
  // Log detail (bottom sheet on a phone, inline pane on an expanded window)
  // ----------------------------------------------------------------------- //

  /// Pretty-prints the payload when it is JSON; otherwise verbatim.
  String _prettyDetail(LogEntry log) {
    var pretty = log.detail;
    if (pretty.isNotEmpty) {
      try {
        pretty = const JsonEncoder.withIndent('  ').convert(jsonDecode(pretty));
      } catch (_) {
        // Not JSON — show it verbatim.
      }
    }
    return pretty;
  }

  /// Phone: a bottom sheet. Expanded: the persistent detail pane, so reading
  /// through a stream does not open and close a modal per entry.
  void _openLogDetail(LogEntry log) {
    if (AppWindow.of(context).supportsTwoPane) {
      setState(() => _selectedLog = log);
      return;
    }

    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsetsDirectional.fromSTEB(
            AppSpacing.xl,
            0,
            AppSpacing.xl,
            AppSpacing.xl,
          ),
          child: SingleChildScrollView(
            child: _LogDetailBody(
              log: log,
              meta: _logMeta(log),
              pretty: _prettyDetail(log),
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _load({int? offset}) async {
    if (offset != null) _offset = offset < 0 ? 0 : offset;
    setState(() {
      _loading = true;
      _error = null;
    });
    final client = context.read<AppState>().buildApiClient();
    try {
      final page = await client.queryLogsPage(
        limit: _pageSize,
        offset: _offset,
        ascending: _ascending,
        level: _levelFilter,
        search: _search.trim(),
        ruleId: _ruleFilter,
        service: _serviceFilter,
      );
      if (!mounted) return;
      setState(() {
        _logs = page.logs;
        _total = page.total;
        _hasMore = page.hasMore;
      });
      // Stats are cosmetic; a failure here must not blank the rows.
      unawaited(_loadOldest());
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      final t = AppMessages.of(context);
      setState(
        () => _error = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('logs_logger_request_failed'),
      );
    } catch (exc) {
      if (!mounted) return;
      // Never swallow: a dead endpoint must not masquerade as "no logs".
      final t = AppMessages.of(context);
      setState(
        () => _error = t.get(
          'logs_service_unreachable',
          params: {'error': exc.toString().split('(').first.trim()},
        ),
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
      final t = AppMessages.of(context);
      setState(
        () => _auditError = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('logs_audit_unavailable'),
      );
    } catch (exc) {
      if (!mounted) return;
      final t = AppMessages.of(context);
      setState(
        () => _auditError = t.get(
          'logs_audit_unavailable_detail',
          params: {'error': exc.toString().split('(').first.trim()},
        ),
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
      length: 3,
      child: Scaffold(
        appBar: AppBar(
          title: Text(
            widget.initialRuleId.isEmpty
                ? t.get('logs_title')
                : (widget.initialRuleLabel.isNotEmpty
                      ? widget.initialRuleLabel
                      : AppMessages.of(context).get('rules_logs_title')),
          ),
          actions: [
            if (_ruleFilter.isNotEmpty)
              IconButton(
                icon: const Icon(Icons.filter_alt_off_outlined),
                tooltip: AppMessages.of(context).get('logs_show_all_tasks'),
                onPressed: () {
                  setState(() {
                    _ruleFilter = '';
                    _offset = 0;
                  });
                  _load(offset: 0);
                },
              ),
            IconButton(
              icon: const Icon(Icons.refresh),
              tooltip: t.get('action_refresh'),
              onPressed: (_loading || _loadingAudit) ? null : _reloadAll,
            ),
          ],
          // Tab labels are catalog keys: the app ships ten locales, and a
          // hardcoded 'Logs' is the one string a screenshot would betray.
          bottom: TabBar(
            tabs: [
              Tab(
                icon: const Icon(Icons.history_outlined),
                text: t.get('logs_tab_stream'),
              ),
              Tab(
                icon: const Icon(Icons.verified_outlined),
                text: t.get('logs_tab_audit'),
              ),
              Tab(
                icon: const Icon(Icons.tune_outlined),
                text: t.get('logs_tab_policy'),
              ),
            ],
          ),
        ),
        // The policy tab loads on its own: the header refresh reloads the
        // stream and the audit trail, and forcing a policy read with it would
        // make a slow producer block the tab the user actually asked for.
        body: TabBarView(
          children: [_buildLogsTab(t), _buildAuditTab(t), const LogPolicyTab()],
        ),
      ),
    );
  }

  // ----------------------------------------------------------------------- //
  // Logs tab (Central Logger stream)
  // ----------------------------------------------------------------------- //

  /// Filters: level chips, task and service scope, search, order, page size and
  /// export. Every one of these is a server-side filter, so paging stays correct
  /// after a change.
  Widget _buildFilters(AppMessages t) {
    final palette = AppColors.of(context);
    return Column(
      children: [
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(
            horizontal: AppSpacing.lg,
            vertical: AppSpacing.sm,
          ),
          child: Row(
            children: [
              for (final entry in const [
                ('logs_all', ''),
                ('logs_level_errors', 'ERROR'),
                ('logs_level_warnings', 'WARN'),
                ('logs_level_info', 'INFO'),
                ('logs_level_debug', 'DEBUG'),
              ])
                Padding(
                  padding: const EdgeInsetsDirectional.only(end: AppSpacing.sm),
                  child: ChoiceChip(
                    label: Text(_levelLabel(t, entry.$1)),
                    selected: _levelFilter == entry.$2,
                    onSelected: (val) {
                      if (!val) return;
                      setState(() {
                        _levelFilter = entry.$2;
                        _offset = 0;
                      });
                      _load(offset: 0);
                    },
                  ),
                ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: AppSpacing.lg),
          child: Column(
            children: [
              Row(
                children: [
                  Expanded(
                    child: DropdownButtonFormField<String>(
                      isExpanded: true,
                      initialValue: _ruleFilter,
                      isDense: true,
                      decoration: InputDecoration(
                        isDense: true,
                        labelText: t.get('logs_scope_task'),
                      ),
                      items: [
                        DropdownMenuItem(
                          value: '',
                          child: Text(t.get('logs_all_tasks')),
                        ),
                        ..._rules.map(
                          (r) => DropdownMenuItem(
                            value: r.id,
                            child: Text(
                              (r.sourceChatTitle.isNotEmpty
                                      ? r.sourceChatTitle
                                      : r.id)
                                  .substring(
                                    0,
                                    (r.sourceChatTitle.isNotEmpty
                                            ? r.sourceChatTitle
                                            : r.id)
                                        .length
                                        .clamp(0, 22),
                                  ),
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                        ),
                      ],
                      onChanged: (v) {
                        setState(() {
                          _ruleFilter = v ?? '';
                          _offset = 0;
                        });
                        _load(offset: 0);
                      },
                    ),
                  ),
                  const SizedBox(width: AppSpacing.sm),
                  Expanded(
                    child: DropdownButtonFormField<String>(
                      isExpanded: true,
                      initialValue: _serviceFilter,
                      isDense: true,
                      decoration: InputDecoration(
                        isDense: true,
                        labelText: t.get('logs_scope_service'),
                      ),
                      items: [
                        DropdownMenuItem(
                          value: '',
                          child: Text(t.get('logs_all')),
                        ),
                        // Service names are identifiers, not labels: they are
                        // the exact strings the logger stores.
                        const DropdownMenuItem(
                          value: 'core',
                          child: Text('core'),
                        ),
                        const DropdownMenuItem(
                          value: 'api',
                          child: Text('api'),
                        ),
                        const DropdownMenuItem(
                          value: 'logger',
                          child: Text('logger'),
                        ),
                        const DropdownMenuItem(
                          value: 'web',
                          child: Text('web'),
                        ),
                      ],
                      onChanged: (v) {
                        setState(() {
                          _serviceFilter = v ?? '';
                          _offset = 0;
                        });
                        _load(offset: 0);
                      },
                    ),
                  ),
                ],
              ),
              const SizedBox(height: AppSpacing.sm),
              AppField(
                label: t.get('logs_search_hint'),
                prefixIcon: Icons.search,
                onChanged: (v) => setState(() => _search = v),
                onSubmitted: (_) {
                  _offset = 0;
                  _load(offset: 0);
                },
              ),
              Row(
                children: [
                  AppIconButton(
                    icon: _ascending
                        ? Icons.arrow_upward
                        : Icons.arrow_downward,
                    tooltip: t.get('logs_order_toggle_desc'),
                    onPressed: () {
                      setState(() {
                        _ascending = !_ascending;
                        _offset = 0;
                      });
                      _load(offset: 0);
                    },
                  ),
                  const SizedBox(width: AppSpacing.xs),
                  DropdownButton<int>(
                    value: _pageSize,
                    underline: const SizedBox.shrink(),
                    style: const TextStyle(fontSize: AppType.xs),
                    items: [
                      for (final n in const [25, 50, 100, 200])
                        DropdownMenuItem(
                          value: n,
                          child: Text(
                            t.get('logs_page_size', params: {'n': '$n'}),
                          ),
                        ),
                    ],
                    onChanged: (v) {
                      if (v == null) return;
                      setState(() {
                        _pageSize = v;
                        _offset = 0;
                      });
                      _load(offset: 0);
                    },
                  ),
                  const Spacer(),
                  AppIconButton(
                    icon: Icons.download_outlined,
                    tooltip: t.get('logs_export_evidence'),
                    onPressed: _exporting ? null : () => _showExportOptions(),
                  ),
                ],
              ),
            ],
          ),
        ),
        const SizedBox(height: AppSpacing.xs),
        Divider(height: 1, color: palette.border),
      ],
    );
  }

  /// Range line: "first–last / total", history depth, and the page arrows.
  Widget _buildRangeRow(AppMessages t, int first, int last) {
    final palette = AppColors.of(context);
    return Padding(
      padding: const EdgeInsetsDirectional.fromSTEB(
        AppSpacing.lg,
        AppSpacing.sm,
        AppSpacing.sm,
        AppSpacing.xs,
      ),
      child: Row(
        children: [
          Flexible(
            child: Text(
              _total == 0
                  ? t.get('logs_empty')
                  : '$first–$last / ${_total.toString()}',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: AppType.xs, color: palette.textMuted),
            ),
          ),
          if (_oldestTs > 0) ...[
            const SizedBox(width: AppSpacing.sm),
            Flexible(
              child: Text(
                t.get(
                  'logs_history_from',
                  params: {
                    'date': DateTime.fromMillisecondsSinceEpoch(
                      _oldestTs * 1000,
                    ).toIso8601String().substring(0, 10),
                  },
                ),
                style: TextStyle(
                  fontSize: AppType.xs,
                  color: palette.textMuted,
                ),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
          const Spacer(),
          AppIconButton(
            icon: Icons.keyboard_arrow_up,
            tooltip: t.get('logs_newer'),
            size: 20,
            onPressed: _offset == 0 || _loading
                ? null
                : () => _load(offset: _offset - _pageSize),
          ),
          AppIconButton(
            icon: Icons.keyboard_arrow_down,
            tooltip: _hasMore
                ? t.get('logs_older')
                : t.get('logs_end_of_history'),
            size: 20,
            onPressed: !_hasMore || _loading
                ? null
                : () => _load(offset: _offset + _pageSize),
          ),
        ],
      ),
    );
  }

  /// The rows, with all four async states explicit.
  Widget _buildRows(AppMessages t) {
    if (_loading && _logs.isEmpty) {
      return const LoadingList(
        rows: 8,
        itemExtent: 84,
        padding: EdgeInsetsDirectional.fromSTEB(
          AppSpacing.lg,
          AppSpacing.sm,
          AppSpacing.lg,
          AppSpacing.xl,
        ),
      );
    }
    if (_error != null && _logs.isEmpty) {
      return ErrorState(message: _error!, onRetry: _reloadAll);
    }
    if (_logs.isEmpty) {
      return RefreshIndicator(
        onRefresh: _reloadAll,
        child: EmptyState.scrollable(
          title: t.get('logs_empty'),
          subtitle: t.get('logs_empty_sub'),
          icon: Icons.history_toggle_off,
          actionLabel: t.get('action_refresh'),
          actionIcon: Icons.refresh,
          onAction: _reloadAll,
        ),
      );
    }

    return RefreshIndicator(
      onRefresh: _reloadAll,
      // Virtualized: only the rows in (and just around) the viewport are
      // mounted. No fixed itemExtent on purpose - a row carries optional
      // attribution chips and must be allowed to grow at text scale 1.3.
      child: ListView.builder(
        padding: const EdgeInsetsDirectional.fromSTEB(
          AppSpacing.lg,
          AppSpacing.sm,
          AppSpacing.lg,
          AppSpacing.xl,
        ),
        itemCount: _logs.length,
        itemBuilder: (context, index) {
          final log = _logs[index];
          return RepaintBoundary(
            child:
                _headerBanner(t, log, index) ??
                _LogRow(
                  log: log,
                  selected: identical(_selectedLog, log),
                  onTap: () => _openLogDetail(log),
                  onFilterRule: (id) {
                    setState(() {
                      _ruleFilter = id;
                      _offset = 0;
                    });
                    _load(offset: 0);
                  },
                  onFilterCorrelation: (corr) {
                    setState(() {
                      _search = corr;
                      _offset = 0;
                    });
                    _load(offset: 0);
                  },
                ),
          );
        },
      ),
    );
  }

  /// The first row of a stale-but-populated list carries the error banner, so a
  /// refresh failure is visible without hiding data that is still good.
  Widget? _headerBanner(AppMessages t, LogEntry log, int index) {
    if (_error == null || index != 0) return null;
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.sm),
      child: InlineBanner(
        message: _error!,
        onRetry: _reloadAll,
        retryLabel: t.get('action_retry'),
      ),
    );
  }

  Widget _buildLogsTab(AppMessages t) {
    final first = _total == 0 ? 0 : _offset + 1;
    final last = _offset + _logs.length;
    final twoPane = AppWindow.of(context).supportsTwoPane;

    final master = Column(
      children: [
        _buildFilters(t),
        _buildRangeRow(t, first, last),
        Expanded(child: _buildRows(t)),
      ],
    );

    if (!twoPane) {
      // Single column: centred and width-capped so a tablet does not stretch a
      // log line across 1200 dp.
      return AppPageBody(
        padding: EdgeInsets.zero,
        maxWidth: 900,
        child: master,
      );
    }

    return Row(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SizedBox(
          width: 420.0.clamp(320.0, MediaQuery.sizeOf(context).width * 0.45),
          child: master,
        ),
        const VerticalDivider(width: 1),
        Expanded(child: _buildLogDetailPane(t)),
      ],
    );
  }

  /// Detail pane for the expanded layout: the same body the phone shows in a
  /// bottom sheet, so the two layouts cannot disagree about a log entry.
  Widget _buildLogDetailPane(AppMessages t) {
    final log = _selectedLog;
    if (log == null) {
      return EmptyState(
        title: t.get('logs_select_prompt'),
        subtitle: t.get('logs_select_hint'),
        icon: Icons.receipt_long_outlined,
      );
    }
    return AppDetailPane(
      title: log.message,
      subtitle: [
        log.levelUpper,
        if (log.service.isNotEmpty) log.service,
        if (log.timestamp.isNotEmpty) log.timestamp,
      ].join(' • '),
      actions: [
        AppIconButton(
          icon: Icons.close,
          tooltip: t.get('action_close'),
          onPressed: () => setState(() => _selectedLog = null),
        ),
      ],
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(AppSpacing.lg),
        child: _LogDetailBody(
          log: log,
          meta: _logMeta(log),
          pretty: _prettyDetail(log),
        ),
      ),
    );
  }

  List<(String, String)> _logMeta(LogEntry log) => [
    ('level', log.levelUpper),
    ('service', log.service),
    ('category', log.category),
    ('time', log.timestamp),
    ('rule_id', log.ruleId),
    ('user_id', log.userId > 0 ? '${log.userId}' : ''),
    ('correlation_id', log.correlationId),
  ];

  /// Log-level chip label. Levels absent from the catalog fall back to the raw
  /// uppercase name rather than showing a key.
  String _levelLabel(AppMessages t, String key) {
    final value = t.get(key);
    return value == key ? key.toUpperCase() : value;
  }

  /// Export format picker. The archive can be megabytes, so the user chooses
  /// the shape before the request is issued rather than after.
  void _showExportOptions() {
    final t = AppMessages.of(context);
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.data_object),
              title: Text(t.get('logs_export_json')),
              subtitle: Text(t.get('logs_export_json_sub')),
              onTap: () {
                Navigator.of(ctx).pop();
                _export('json');
              },
            ),
            ListTile(
              leading: const Icon(Icons.table_chart_outlined),
              title: Text(t.get('logs_export_csv')),
              subtitle: Text(t.get('logs_export_csv_sub')),
              onTap: () {
                Navigator.of(ctx).pop();
                _export('csv');
              },
            ),
          ],
        ),
      ),
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
            decoration: InputDecoration(
              isDense: true,
              prefixIcon: const Icon(Icons.link, size: 18),
              hintText: t.get('logs_audit_search_hint'),
              border: const OutlineInputBorder(),
            ),
            onChanged: (v) => setState(() => _correlationSearch = v),
            onSubmitted: (_) => _loadAudit(),
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _reloadAll,
            child: _audit.isEmpty && _loadingAudit
                ? const LoadingList(rows: 5, itemExtent: 76)
                : _audit.isEmpty && _auditError != null
                ? ErrorState(message: _auditError!, onRetry: _loadAudit)
                : _audit.isEmpty
                ? EmptyState.scrollable(
                    title: t.get('logs_audit_empty_title'),
                    subtitle: t.get('logs_audit_empty_sub'),
                    icon: Icons.verified_outlined,
                    actionLabel: t.get('action_refresh'),
                    actionIcon: Icons.refresh,
                    onAction: _loadAudit,
                  )
                : ListView.separated(
                    padding: const EdgeInsets.all(16),
                    itemCount: _audit.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
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
                          color: isOk
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
                        trailing: evt.correlationId.isNotEmpty
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
      builder: (ctx) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          padding: const EdgeInsetsDirectional.fromSTEB(20, 8, 20, 24),
          children: [
            Text(
              evt.eventType,
              style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
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
                              fontFamily: f.$1.contains('id')
                                  ? 'monospace'
                                  : null,
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

/// One log entry in the stream.
///
/// A row, not a [ListTile]: the attribution chips need to sit under the meta
/// line and the whole row must stay ≥ 48 dp at text scale 1.3 without clipping.
class _LogRow extends StatelessWidget {
  const _LogRow({
    required this.log,
    required this.selected,
    required this.onTap,
    this.onFilterRule,
    this.onFilterCorrelation,
  });

  final LogEntry log;
  final bool selected;
  final VoidCallback onTap;
  final ValueChanged<String>? onFilterRule;
  final ValueChanged<String>? onFilterCorrelation;

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    final tone = log.isError
        ? palette.danger
        : (log.isWarning ? palette.warning : palette.info);
    final when = log.when;

    final meta = [
      if (when != null) when.toIso8601String().substring(11, 19),
      log.service.isEmpty ? 'core' : log.service,
      if (log.category.isNotEmpty) log.category,
    ].join(' • ');

    return Semantics(
      button: true,
      selected: selected,
      label: '${log.levelUpper}. ${log.message}',
      child: Material(
        color: selected ? palette.surface3 : Colors.transparent,
        borderRadius: AppRadius.allMd,
        child: InkWell(
          onTap: onTap,
          borderRadius: AppRadius.allMd,
          child: Container(
            constraints: const BoxConstraints(
              minHeight: AppBreakpoints.touchTarget,
            ),
            padding: const EdgeInsets.symmetric(
              horizontal: AppSpacing.sm,
              vertical: AppSpacing.sm,
            ),
            decoration: BoxDecoration(
              borderRadius: AppRadius.allMd,
              border: Border(
                bottom: BorderSide(
                  color: palette.border.withValues(alpha: 0.5),
                ),
                left: BorderSide(
                  color: selected ? palette.primary : Colors.transparent,
                  width: 2,
                ),
              ),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsetsDirectional.only(top: 2),
                  child: Icon(
                    log.isError
                        ? Icons.error
                        : (log.isWarning ? Icons.warning : Icons.info_outline),
                    color: tone,
                    size: 18,
                  ),
                ),
                const SizedBox(width: AppSpacing.sm),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        log.message,
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                        style: theme.textTheme.bodyMedium,
                      ),
                      const SizedBox(height: 2),
                      Text(
                        meta,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: theme.textTheme.bodySmall,
                      ),
                      if (log.ruleId.isNotEmpty || log.correlationId.isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(top: AppSpacing.xs),
                          child: Wrap(
                            spacing: 6,
                            runSpacing: 4,
                            children: [
                              if (log.ruleId.isNotEmpty)
                                _AttributionChip(
                                  label: t.get(
                                    'logs_scope_task_chip',
                                    params: {
                                      'id': log.ruleId.length > 8
                                          ? log.ruleId.substring(0, 8)
                                          : log.ruleId,
                                    },
                                  ),
                                  onTap: onFilterRule == null
                                      ? null
                                      : () => onFilterRule!(log.ruleId),
                                ),
                              if (log.correlationId.isNotEmpty)
                                _AttributionChip(
                                  label: t.get(
                                    'logs_scope_corr_chip',
                                    params: {
                                      'id': log.correlationId.length > 8
                                          ? log.correlationId.substring(0, 8)
                                          : log.correlationId,
                                    },
                                  ),
                                  onTap: onFilterCorrelation == null
                                      ? null
                                      : () => onFilterCorrelation!(
                                          log.correlationId,
                                        ),
                                ),
                            ],
                          ),
                        ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Compact tappable attribution token (task id / correlation id).
class _AttributionChip extends StatelessWidget {
  const _AttributionChip({required this.label, this.onTap});

  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    return Semantics(
      button: onTap != null,
      label: label,
      child: InkWell(
        onTap: onTap,
        borderRadius: AppRadius.allFull,
        child: Container(
          padding: const EdgeInsets.symmetric(
            horizontal: AppSpacing.sm,
            vertical: 2,
          ),
          decoration: BoxDecoration(
            color: palette.primary.withValues(alpha: 0.12),
            borderRadius: AppRadius.allFull,
            border: Border.all(color: palette.primary.withValues(alpha: 0.35)),
          ),
          child: Text(
            label,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              fontSize: 10,
              fontWeight: FontWeight.w600,
              fontFamily: 'monospace',
              color: palette.primary,
            ),
          ),
        ),
      ),
    );
  }
}

/// The body of a log detail: message, attribution fields, structured payload.
class _LogDetailBody extends StatelessWidget {
  const _LogDetailBody({
    required this.log,
    required this.meta,
    required this.pretty,
  });

  final LogEntry log;
  final List<(String, String)> meta;
  final String pretty;

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final palette = AppColors.of(context);
    final theme = Theme.of(context);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(log.message, style: theme.textTheme.titleMedium),
        const SizedBox(height: AppSpacing.md),
        for (final entry in meta.where((m) => m.$2.isNotEmpty))
          Padding(
            padding: const EdgeInsetsDirectional.only(bottom: AppSpacing.sm),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 112,
                  child: Text(
                    entry.$1,
                    maxLines: 2,
                    style: theme.textTheme.bodySmall,
                  ),
                ),
                Expanded(
                  child: SelectableText(
                    entry.$2,
                    style: theme.textTheme.bodySmall?.copyWith(
                      fontFamily: entry.$1.contains('id') ? 'monospace' : null,
                      color: palette.text,
                    ),
                  ),
                ),
              ],
            ),
          ),
        if (pretty.isNotEmpty) ...[
          const SizedBox(height: AppSpacing.sm),
          Text(t.get('logs_detail_label'), style: theme.textTheme.labelSmall),
          const SizedBox(height: AppSpacing.xs),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(AppSpacing.md),
            decoration: BoxDecoration(
              color: palette.surface3,
              borderRadius: AppRadius.allMd,
              border: Border.all(color: palette.border),
            ),
            child: SelectableText(
              pretty,
              style: const TextStyle(
                fontSize: 11,
                fontFamily: 'monospace',
                height: 1.45,
              ),
            ),
          ),
        ],
      ],
    );
  }
}
