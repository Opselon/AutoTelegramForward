/// Operational dashboard: live counts, service topology, recent errors.
///
/// Every statistic here comes from the real backend:
///   * Counts: `GET /api/stats` (scoped to the authenticated tenant)
///   * Health: `GET /api/v1/health/services` (parallel probe of core/api/logger/web)
///   * Errors: `GET /api/logs?level=error` (central logger microservice)
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_ui.dart';
import '../system/system_status_screen.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  SystemStats? _stats;
  List<ServiceHealth> _services = const [];
  List<LogEntry> _recentErrors = const [];
  DiagnosticsReport? _diagnostics;
  SetupReport? _setup;
  bool _loading = false;
  String? _error;

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
      final statsFuture = client.stats();
      final healthFuture = client.servicesHealth().catchError(
        (_) => <ServiceHealth>[],
      );
      final errorsFuture = client
          .queryLogs(limit: 5, level: 'error')
          .catchError((_) => <LogEntry>[]);
      // The readiness report drives the hero. It is advisory: a server that
      // predates /api/system/setup answers 404 and the hero simply reports the
      // stack as unverified instead of failing the dashboard.
      final setupFuture = client
          .systemSetup()
          .then<SetupReport?>((report) => report)
          .catchError((_) => null);

      final results = await Future.wait([
        statsFuture,
        healthFuture,
        errorsFuture,
        setupFuture,
      ]);

      if (!mounted) return;
      setState(() {
        _stats = results[0] as SystemStats;
        _services = results[1] as List<ServiceHealth>;
        _recentErrors = results[2] as List<LogEntry>;
        _setup = results[3] as SetupReport?;
      });

      // Preflight is advisory: an older server without /api/diagnostics must
      // degrade to "no panel", never to a dashboard-level error.
      try {
        final report = await client.diagnostics();
        if (mounted) setState(() => _diagnostics = report);
      } catch (_) {
        if (mounted) setState(() => _diagnostics = null);
      }
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
                    : t.get('state_error'),
      );
    } catch (exc) {
      // Never swallow: a network failure must not look like "no data".
      if (!mounted) return;
      setState(
        () =>
            _error =
                '${t.get('state_error')} '
                '(${exc.toString().split('(').first.trim()})',
      );
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  /// Splits "3 / 12" style counters so the numerator is emphasised.
  ({String value, String? caption}) _counter(
    int? active,
    int? total,
    String Function(int) format,
  ) {
    if (total == null) return (value: '—', caption: null);
    if (active == null) return (value: format(total), caption: null);
    return (value: format(active), caption: '/ ${format(total)}');
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final stats = _stats;
    final theme = Theme.of(context);

    final rules = _counter(stats?.activeRules, stats?.totalRules, (n) => '$n');
    final sessions = _counter(
      stats?.activeSessions,
      stats?.totalSessions,
      (n) => '$n',
    );
    final failed = stats?.failedTotal ?? 0;

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('dash_title')),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: t.get('action_refresh'),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          padding: AppSpacing.screen,
          children: [
            if (_error != null) ...[
              InlineBanner(
                message: _error!,
                onRetry: _load,
                retryLabel: t.get('action_retry'),
              ),
              AppSpacing.gapLg,
            ],

            GradientHeader(
              title: t.get('dash_title'),
              subtitle:
                  stats != null
                      ? '${t.get('dash_total_rules')} • ${t.get('dash_sessions')}'
                      : null,
              icon: Icons.insights_rounded,
            ),
            AppSpacing.gapLg,

            // Readiness first: "is this thing set up?" outranks any counter.
            if (_setup != null) ...[
              ReadinessHero(
                ready: _setup!.ready,
                title:
                    _setup!.ready ? t.get('sys_ready') : t.get('sys_not_ready'),
                subtitle:
                    _setup!.deployment.isEmpty
                        ? t.get('sys_subtitle')
                        : '${t.get('sys_deployment')}: ${_setup!.deployment}',
                verdictLabel:
                    _setup!.ready ? t.get('sys_ready') : t.get('sys_not_ready'),
                serviceSummary: t.get(
                  'sys_services_online',
                  params: {
                    'online': '${_setup!.onlineServices}',
                    'total': '${_setup!.services.length}',
                  },
                ),
                footnote:
                    _setup!.missingRequired.isEmpty
                        ? null
                        : t.get(
                          'sys_missing_required',
                          params: {
                            'count': '${_setup!.missingRequired.length}',
                          },
                        ),
                openLabel: t.get('sys_open'),
                onOpen:
                    () => Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (_) => const SystemStatusScreen(),
                      ),
                    ),
              ),
              AppSpacing.gapLg,
            ],

            // Metric grid
            GridView.count(
              crossAxisCount: 2,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              crossAxisSpacing: AppSpacing.md,
              mainAxisSpacing: AppSpacing.md,
              childAspectRatio: 1.45,
              children: [
                StatTile(
                  label: t.get('dash_total_rules'),
                  value: rules.value,
                  caption: rules.caption,
                  icon: Icons.alt_route_rounded,
                  gradient: AppGradients.brand,
                ),
                StatTile(
                  label: t.get('dash_sessions'),
                  value: sessions.value,
                  caption: sessions.caption,
                  icon: Icons.send_rounded,
                  gradient: AppGradients.cyan,
                ),
                StatTile(
                  label: t.get('dash_delivered'),
                  value: stats != null ? '${stats.deliveredTotal}' : '—',
                  icon: Icons.check_circle_outline,
                  gradient: AppGradients.success,
                ),
                StatTile(
                  label: t.get('dash_failed'),
                  value: stats != null ? '$failed' : '—',
                  icon: Icons.error_outline,
                  gradient:
                      failed > 0 ? AppGradients.danger : AppGradients.gold,
                ),
              ],
            ),
            AppSpacing.gapXl,

            // Preflight diagnostics — only shown when the server answered.
            if (_diagnostics != null)
              SectionCard(
                title: t.get('diag_title'),
                icon: Icons.health_and_safety_outlined,
                trailing: StatusPill(
                  label: '${_diagnostics!.score}/100',
                  color: AppTheme.severityColor(
                    _diagnostics!.isHealthy
                        ? 'info'
                        : (_diagnostics!.errorCount > 0 ? 'error' : 'warning'),
                  ),
                  icon:
                      _diagnostics!.isHealthy
                          ? Icons.verified_outlined
                          : Icons.report_problem_outlined,
                ),
                child:
                    _diagnostics!.findings.isEmpty
                        ? EmptyStateView(
                          message: t.get('diag_all_ok'),
                          icon: Icons.verified_outlined,
                          color: AppTheme.primary,
                        )
                        : Column(
                          children: [
                            for (
                              var i = 0;
                              i < _diagnostics!.findings.length;
                              i++
                            ) ...[
                              if (i > 0) const Divider(height: 1),
                              _FindingRow(finding: _diagnostics!.findings[i]),
                            ],
                          ],
                        ),
              ),
            if (_diagnostics != null) AppSpacing.gapLg,

            // Service topology
            SectionCard(
              title: t.get('dash_health'),
              icon: Icons.hub_outlined,
              child:
                  _services.isEmpty && _loading
                      ? const Center(
                        child: Padding(
                          padding: EdgeInsets.all(AppSpacing.lg),
                          child: CircularProgressIndicator(),
                        ),
                      )
                      : _services.isEmpty
                      ? EmptyStateView(
                        message: t.get('state_empty'),
                        icon: Icons.cloud_off_outlined,
                        color: AppTheme.warning,
                      )
                      : Column(
                        children: [
                          for (var i = 0; i < _services.length; i++) ...[
                            if (i > 0) const Divider(height: 1),
                            ServiceTile(
                              name: _services[i].name,
                              online: _services[i].online,
                              detail: _services[i].detail,
                              latencyMs: _services[i].latencyMs,
                              onlineLabel: t.get('status_online'),
                              offlineLabel: t.get('status_offline'),
                              latencyLabel: t.get(
                                'sys_latency',
                                params: {'ms': '${_services[i].latencyMs}'},
                              ),
                            ),
                          ],
                        ],
                      ),
            ),
            AppSpacing.gapLg,

            // Recent errors
            SectionCard(
              title: t.get('dash_recent_errors'),
              icon: Icons.report_gmailerrorred_outlined,
              child:
                  _recentErrors.isEmpty
                      ? EmptyStateView(
                        message: t.get('logs_empty'),
                        icon: Icons.sentiment_satisfied_alt,
                        color: AppTheme.primary,
                      )
                      : Column(
                        children: [
                          for (var i = 0; i < _recentErrors.length; i++) ...[
                            if (i > 0) const Divider(height: 1),
                            ListTile(
                              contentPadding: EdgeInsets.zero,
                              leading: Container(
                                padding: const EdgeInsets.all(6),
                                decoration: BoxDecoration(
                                  color: AppTheme.warning.withValues(
                                    alpha: 0.14,
                                  ),
                                  borderRadius: AppRadii.allSm,
                                ),
                                child: const Icon(
                                  Icons.warning_amber_rounded,
                                  color: AppTheme.warning,
                                  size: 18,
                                ),
                              ),
                              title: Text(
                                _recentErrors[i].message,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: theme.textTheme.bodyMedium,
                              ),
                              subtitle: Text(
                                '${_recentErrors[i].category} • ${_recentErrors[i].timestamp}',
                                style: theme.textTheme.bodySmall,
                              ),
                            ),
                          ],
                        ],
                      ),
            ),
            AppSpacing.gapXl,
          ],
        ),
      ),
    );
  }
}

/// One preflight finding: severity icon, title, detail and remediation.
class _FindingRow extends StatelessWidget {
  const _FindingRow({required this.finding});

  final DiagnosticFinding finding;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = AppTheme.severityColor(finding.severity);
    final icon =
        finding.isError
            ? Icons.error_outline
            : finding.isWarning
            ? Icons.warning_amber_rounded
            : Icons.info_outline;

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: AppSpacing.sm),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, color: color, size: 18),
          const SizedBox(width: AppSpacing.sm),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  finding.title,
                  style: theme.textTheme.titleSmall?.copyWith(color: color),
                ),
                if (finding.detail.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      finding.detail,
                      style: theme.textTheme.bodySmall,
                    ),
                  ),
                if (finding.remediation.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      '💡 ${finding.remediation}',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.primary,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
