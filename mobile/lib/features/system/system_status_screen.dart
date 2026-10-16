/// System status: the deep view behind the dashboard's readiness hero.
///
/// Everything on this page comes from `GET /api/system/setup`, the endpoint the
/// `atf` CLI wizard and the web dashboard also read, so all three surfaces agree
/// on what "set up" means:
///   * `ready`               — the server's verdict, not a client guess;
///   * `services[]`          — live probes of core / logger / api / web;
///   * `checks[]`            — the configuration contract, booleans only;
///   * `features[]`          — what this build implements;
///   * `next_steps[]`        — what to do when it is not ready.
///
/// Degradation matters here: a server older than this endpoint answers 404, and
/// that must read as "update the server", never as a broken deployment.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_ui.dart';

class SystemStatusScreen extends StatefulWidget {
  const SystemStatusScreen({super.key});

  @override
  State<SystemStatusScreen> createState() => _SystemStatusScreenState();
}

class _SystemStatusScreenState extends State<SystemStatusScreen> {
  SetupReport? _report;
  bool _loading = false;
  bool _unsupported = false;
  String? _error;

  /// `didChangeDependencies` can fire more than once (theme, locale); without a
  /// guard every rebuild re-probes the whole stack.
  bool _bootstrapped = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_bootstrapped) return;
    _bootstrapped = true;
    // Inherited widgets (AppMessages via Localizations, AppState via Provider)
    // are not readable from initState.
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
      _unsupported = false;
    });

    final state = context.read<AppState>();
    final client = state.buildApiClient();
    final t = AppMessages.of(context);

    try {
      final report = await client.systemSetup();
      if (!mounted) return;
      setState(() => _report = report);
    } on ApiException catch (exc) {
      if (!mounted) return;
      switch (exc.error.kind) {
        case ErrorKind.unauthorized:
          state.markExpired();
          return;
        case ErrorKind.notFound:
          // Older server: report it as a version gap, not as a failure.
          setState(() => _unsupported = true);
          return;
        default:
          setState(() => _error = exc.error.message);
      }
    } catch (_) {
      if (!mounted) return;
      // Older servers may answer with a non-JSON body; treat that as unsupported
      // rather than surfacing a parse error the user cannot act on.
      setState(() => _unsupported = true);
    } finally {
      if (mounted) setState(() => _loading = false);
    }

    if (!mounted) return;
    if (_error != null && _error!.isEmpty) {
      setState(() => _error = t.get('state_error'));
    }
  }

  String _levelLabel(AppMessages t, String level) {
    switch (level) {
      case 'required':
        return t.get('sys_level_required');
      case 'recommended':
        return t.get('sys_level_recommended');
      default:
        return t.get('sys_level_optional');
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final theme = Theme.of(context);
    final report = _report;

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('sys_title')),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh_rounded),
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
            GradientHeader(
              title: t.get('sys_title'),
              subtitle: t.get('sys_subtitle'),
              icon: Icons.hub_rounded,
            ),
            AppSpacing.gapLg,

            if (_loading && report == null)
              const Center(
                child: Padding(
                  padding: EdgeInsets.all(AppSpacing.xl),
                  child: CircularProgressIndicator(),
                ),
              ),

            if (_unsupported)
              SectionCard(
                title: t.get('sys_title'),
                icon: Icons.system_update_alt_rounded,
                child: InlineBanner(
                  message: t.get('sys_unavailable'),
                  onRetry: _load,
                  retryLabel: t.get('action_retry'),
                ),
              ),

            if (_error != null && _error!.isNotEmpty) ...[
              InlineBanner(
                message: _error!,
                onRetry: _load,
                retryLabel: t.get('action_retry'),
              ),
              AppSpacing.gapLg,
            ],

            if (report != null) ..._reportBody(context, t, theme, report),

            AppSpacing.gapXl,
          ],
        ),
      ),
    );
  }

  List<Widget> _reportBody(
    BuildContext context,
    AppMessages t,
    ThemeData theme,
    SetupReport report,
  ) {
    final online = report.onlineServices;
    final total = report.services.length;

    return [
      ReadinessHero(
        ready: report.ready,
        title: report.ready ? t.get('sys_ready') : t.get('sys_not_ready'),
        subtitle: t.get('sys_subtitle'),
        verdictLabel:
            report.ready ? t.get('sys_ready') : t.get('sys_not_ready'),
        serviceSummary: t.get(
          'sys_services_online',
          params: {'online': '$online', 'total': '$total'},
        ),
        footnote:
            report.missingRequired.isEmpty
                ? null
                : t.get(
                  'sys_missing_required',
                  params: {'count': '${report.missingRequired.length}'},
                ),
      ),
      AppSpacing.gapLg,

      // Build identity: which server, which version, which commit. Support
      // conversations start here.
      SectionCard(
        title: t.get('sys_product_version'),
        icon: Icons.info_outline_rounded,
        child: Column(
          children: [
            _DetailRow(
              label: t.get('sys_deployment'),
              value: report.deployment.isEmpty ? '—' : report.deployment,
            ),
            _DetailRow(
              label: t.get('sys_product_version'),
              value:
                  report.productVersion.isEmpty ? '—' : report.productVersion,
            ),
            _DetailRow(
              label: t.get('sys_api_version'),
              value: report.apiVersion.isEmpty ? '—' : report.apiVersion,
            ),
            _DetailRow(
              label: t.get('sys_commit'),
              value: report.commitSha.isEmpty ? '—' : report.commitSha,
            ),
            if (report.time.isNotEmpty)
              _DetailRow(
                label: t.get('sys_probed_at', params: {'time': report.time}),
                value: '',
              ),
          ],
        ),
      ),
      AppSpacing.gapLg,

      SectionCard(
        title: t.get('sys_services_title'),
        icon: Icons.dns_rounded,
        child:
            report.services.isEmpty
                ? EmptyStateView(
                  message: t.get('state_empty'),
                  icon: Icons.cloud_off_rounded,
                  color: AppTheme.warning,
                )
                : Column(
                  children: [
                    for (var i = 0; i < report.services.length; i++) ...[
                      if (i > 0) const Divider(height: 1),
                      ServiceTile(
                        name: report.services[i].name,
                        online: report.services[i].online,
                        detail: report.services[i].detail,
                        latencyMs: report.services[i].latencyMs,
                        onlineLabel: t.get('status_online'),
                        offlineLabel: t.get('status_offline'),
                        latencyLabel: t.get(
                          'sys_latency',
                          params: {'ms': '${report.services[i].latencyMs}'},
                        ),
                      ),
                    ],
                  ],
                ),
      ),
      AppSpacing.gapLg,

      if (report.features.isNotEmpty) ...[
        SectionCard(
          title: t.get('sys_features'),
          icon: Icons.extension_outlined,
          child: FeatureChips(
            features: report.features,
            emptyLabel: t.get('state_empty'),
          ),
        ),
        AppSpacing.gapLg,
      ],

      if (report.checks.isNotEmpty)
        SectionCard(
          title: t.get('sys_contract'),
          icon: Icons.rule_folder_outlined,
          child: Column(
            children: [
              for (var i = 0; i < report.checks.length; i++) ...[
                if (i > 0) const Divider(height: 1),
                ContractRow(
                  keyName: report.checks[i].key,
                  levelLabel: _levelLabel(t, report.checks[i].level),
                  reason: report.checks[i].reason,
                  hint: report.checks[i].hint,
                  isSet: report.checks[i].set,
                  stateLabel:
                      report.checks[i].set
                          ? t.get('sys_check_set')
                          : t.get('sys_check_unset'),
                ),
              ],
            ],
          ),
        ),
      if (report.checks.isNotEmpty) AppSpacing.gapLg,

      if (report.nextSteps.isNotEmpty)
        SectionCard(
          title: t.get('sys_next_steps'),
          icon: Icons.checklist_rounded,
          child: Column(
            children: [
              for (final step in report.nextSteps)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: AppSpacing.xs),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Padding(
                        padding: EdgeInsets.only(top: 3),
                        child: Icon(
                          Icons.chevron_right_rounded,
                          size: 16,
                          color: AppTheme.sky,
                        ),
                      ),
                      Expanded(
                        child: Text(step, style: theme.textTheme.bodySmall),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
    ];
  }
}

/// One label/value line in the build-identity card.
class _DetailRow extends StatelessWidget {
  const _DetailRow({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        children: [
          Expanded(child: Text(label, style: theme.textTheme.bodySmall)),
          if (value.isNotEmpty)
            Text(
              value,
              style: theme.textTheme.bodyMedium?.copyWith(
                fontFeatures: const [FontFeature.tabularFigures()],
              ),
            ),
        ],
      ),
    );
  }
}
