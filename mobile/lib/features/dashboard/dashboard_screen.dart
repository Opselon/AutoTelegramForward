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
import '../../l10n/app_messages.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  SystemStats? _stats;
  List<ServiceHealth> _services = const [];
  List<LogEntry> _recentErrors = const [];
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
      final statsFuture = client.stats();
      final healthFuture = client.servicesHealth().catchError(
        (_) => <ServiceHealth>[],
      );
      final errorsFuture = client
          .queryLogs(limit: 5, level: 'error')
          .catchError((_) => <LogEntry>[]);

      final results = await Future.wait([
        statsFuture,
        healthFuture,
        errorsFuture,
      ]);

      if (!mounted) return;
      setState(() {
        _stats = results[0] as SystemStats;
        _services = results[1] as List<ServiceHealth>;
        _recentErrors = results[2] as List<LogEntry>;
      });
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

  Widget _buildStatCard(
    BuildContext context, {
    required String label,
    required String value,
    required IconData icon,
    Color? color,
  }) {
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  label,
                  style: Theme.of(
                    context,
                  ).textTheme.bodySmall?.copyWith(color: Colors.grey),
                ),
                Icon(
                  icon,
                  size: 20,
                  color: color ?? Theme.of(context).colorScheme.primary,
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              value,
              style: Theme.of(
                context,
              ).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.bold),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final stats = _stats;

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
          padding: const EdgeInsets.all(16),
          children: [
            if (_error != null)
              Container(
                padding: const EdgeInsets.all(12),
                margin: const EdgeInsets.only(bottom: 16),
                decoration: BoxDecoration(
                  color: Colors.red.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.red),
                ),
                child: Row(
                  children: [
                    const Icon(Icons.error_outline, color: Colors.red),
                    const SizedBox(width: 8),
                    Expanded(child: Text(_error!)),
                    TextButton(
                      onPressed: _load,
                      child: Text(t.get('action_retry')),
                    ),
                  ],
                ),
              ),

            // Top Stat Grid
            GridView.count(
              crossAxisCount: 2,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              crossAxisSpacing: 12,
              mainAxisSpacing: 12,
              childAspectRatio: 1.6,
              children: [
                _buildStatCard(
                  context,
                  label: t.get('dash_total_rules'),
                  value:
                      stats != null
                          ? '${stats.activeRules} / ${stats.totalRules}'
                          : '—',
                  icon: Icons.alt_route_rounded,
                  color: const Color(0xFF10B981),
                ),
                _buildStatCard(
                  context,
                  label: t.get('dash_sessions'),
                  value:
                      stats != null
                          ? '${stats.activeSessions} / ${stats.totalSessions}'
                          : '—',
                  icon: Icons.send_rounded,
                  color: const Color(0xFF06B6D4),
                ),
                _buildStatCard(
                  context,
                  label: t.get('dash_delivered'),
                  value: stats != null ? '${stats.deliveredTotal}' : '—',
                  icon: Icons.check_circle_outline,
                  color: Colors.green,
                ),
                _buildStatCard(
                  context,
                  label: t.get('dash_failed'),
                  value: stats != null ? '${stats.failedTotal}' : '—',
                  icon: Icons.error_outline,
                  color:
                      stats != null && stats.failedTotal > 0
                          ? Colors.red
                          : Colors.grey,
                ),
              ],
            ),
            const SizedBox(height: 24),

            // Service Health
            Text(
              t.get('dash_health'),
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 8),
            if (_services.isEmpty && _loading)
              const Center(
                child: Padding(
                  padding: EdgeInsets.all(24),
                  child: CircularProgressIndicator(),
                ),
              )
            else
              Card(
                margin: EdgeInsets.zero,
                child: Column(
                  children:
                      _services.map((svc) {
                        return ListTile(
                          dense: true,
                          leading: Icon(
                            svc.online ? Icons.check_circle : Icons.cancel,
                            color: svc.online ? Colors.green : Colors.red,
                            size: 20,
                          ),
                          title: Text(
                            svc.name.toUpperCase(),
                            style: const TextStyle(fontWeight: FontWeight.bold),
                          ),
                          subtitle: Text(
                            svc.detail.isNotEmpty
                                ? svc.detail
                                : (svc.online ? 'reachable' : 'unreachable'),
                          ),
                          trailing: Text(
                            '${svc.latencyMs} ms',
                            style: Theme.of(
                              context,
                            ).textTheme.bodySmall?.copyWith(color: Colors.grey),
                          ),
                        );
                      }).toList(),
                ),
              ),
            const SizedBox(height: 24),

            // Recent errors (if any)
            Text(
              t.get('dash_recent_errors'),
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 8),
            if (_recentErrors.isEmpty)
              Card(
                margin: EdgeInsets.zero,
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.sentiment_satisfied_alt,
                        color: Colors.green,
                      ),
                      const SizedBox(width: 8),
                      Text(t.get('logs_empty')),
                    ],
                  ),
                ),
              )
            else
              Card(
                margin: EdgeInsets.zero,
                child: Column(
                  children:
                      _recentErrors.map((err) {
                        return ListTile(
                          leading: const Icon(
                            Icons.warning,
                            color: Colors.amber,
                            size: 20,
                          ),
                          title: Text(
                            err.message,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                          ),
                          subtitle: Text('${err.category} • ${err.timestamp}'),
                        );
                      }).toList(),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
