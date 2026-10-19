/// Rules listing screen: view, toggle, pause/resume, delete forwarding rules.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';
import 'rule_editor_screen.dart';
import 'rule_settings_sheet.dart';
import '../logs/logs_screen.dart';

class RulesScreen extends StatefulWidget {
  const RulesScreen({super.key});

  @override
  State<RulesScreen> createState() => _RulesScreenState();
}

class _RulesScreenState extends State<RulesScreen> {
  List<ForwardRule> _rules = const [];
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
      final list = await client.listRules();
      if (!mounted) return;
      setState(() => _rules = list);
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
      // Never swallow a network/timeout failure as "no rules": surface it so
      // an unreachable server is not indistinguishable from an empty account.
      if (!mounted) return;
      setState(
        () => _error =
            '${t.get('state_error')} '
            '(${exc.toString().split('(').first.trim()})',
      );
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  /// Primary endpoint keeps its channel title; the remaining entries of a
  /// multi-endpoint route are appended as ids, so the card never hides the fact
  /// that a message fans out to more than one chat.
  String _routeLabel(List<String> ids, String primaryTitle, String fallback) {
    if (ids.isEmpty) return fallback;
    final primary = primaryTitle.isNotEmpty ? primaryTitle : ids.first;
    if (ids.length == 1) return primary;
    return <String>[primary, ...ids.skip(1)].join(', ');
  }

  Future<void> _toggle(ForwardRule rule) async {
    final client = context.read<AppState>().buildApiClient();
    try {
      await client.toggleRule(rule.id);
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.error.message)));
    }
  }

  Future<void> _duplicate(ForwardRule rule) async {
    final client = context.read<AppState>().buildApiClient();
    final t = AppMessages.of(context);
    try {
      await client.duplicateRule(rule.id);
      await _load();
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(t.get('rules_duplicated'))));
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.error.message)));
    }
  }

  Future<void> _delete(ForwardRule rule) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('rules_delete_confirm')),
        content: Text('${rule.sourceChatTitle} -> ${rule.targetChatTitle}'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(t.get('action_cancel')),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(t.get('action_delete')),
          ),
        ],
      ),
    );

    if (confirmed != true || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      await client.deleteRule(rule.id);
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.error.message)));
    }
  }

  Future<void> _openEditor([ForwardRule? rule]) async {
    final updated = await Navigator.push<bool>(
      context,
      MaterialPageRoute(builder: (_) => RuleEditorScreen(rule: rule)),
    );
    if (updated == true) {
      await _load();
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('rules_title')),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: t.get('action_refresh'),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _openEditor(),
        icon: const Icon(Icons.add),
        label: Text(t.get('rules_create')),
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: Column(
          children: [
            if (_error != null)
              Container(
                padding: const EdgeInsets.all(12),
                margin: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: Colors.red.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.red),
                ),
                child: Text(_error!, style: const TextStyle(color: Colors.red)),
              ),
            Expanded(
              child: _rules.isEmpty && !_loading
                  ? ListView(
                      children: [
                        Padding(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 32,
                            vertical: 64,
                          ),
                          child: Column(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              const Icon(
                                Icons.alt_route_rounded,
                                size: 64,
                                color: Colors.grey,
                              ),
                              const SizedBox(height: 16),
                              Text(
                                t.get('rules_empty'),
                                textAlign: TextAlign.center,
                                style: Theme.of(context).textTheme.bodyMedium
                                    ?.copyWith(color: Colors.grey),
                              ),
                              const SizedBox(height: 24),
                              ElevatedButton.icon(
                                onPressed: () => _openEditor(),
                                icon: const Icon(Icons.add),
                                label: Text(t.get('rules_create')),
                              ),
                            ],
                          ),
                        ),
                      ],
                    )
                  : ListView.builder(
                      padding: const EdgeInsets.all(16),
                      itemCount: _rules.length,
                      itemBuilder: (context, index) {
                        final r = _rules[index];
                        final taskName = r.name.isNotEmpty
                            ? r.name
                            : (r.id.length >= 8
                                  ? 'af_route_lab_${r.id.substring(0, 4)}'
                                  : 'af_route_lab_b');
                        final srcTitle = r.sourceChatTitle.isNotEmpty
                            ? r.sourceChatTitle
                            : (r.sourceChatId.isNotEmpty
                                  ? r.sourceChatId
                                  : t.get('rules_source_fallback'));
                        final dstTitle = r.targetChatTitle.isNotEmpty
                            ? r.targetChatTitle
                            : (r.targetChatId.isNotEmpty
                                  ? r.targetChatId
                                  : t.get('rules_target_fallback'));
                        // A task can hold several sources and several targets;
                        // the card shows the whole fan-out so a multi-entry
                        // route is visible without opening the task.
                        final srcDisplay = _routeLabel(
                          r.sourceIds,
                          r.sourceChatTitle,
                          srcTitle,
                        );
                        final dstDisplay = _routeLabel(
                          r.allTargets,
                          r.targetChatTitle,
                          dstTitle,
                        );

                        return Card(
                          margin: const EdgeInsets.only(bottom: 16),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(16),
                            side: BorderSide(
                              color: r.isEnabled
                                  ? const Color(0xFF10B981)
                                        .withValues(alpha: 0.3)
                                  : Colors.grey.withValues(alpha: 0.2),
                            ),
                          ),
                          elevation: 2,
                          child: Padding(
                            padding: const EdgeInsets.all(16),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                // Task Header
                                Row(
                                  mainAxisAlignment:
                                      MainAxisAlignment.spaceBetween,
                                  children: [
                                    Row(
                                      children: [
                                        const Icon(
                                          Icons.sync_alt_rounded,
                                          size: 18,
                                          color: Color(0xFF38BDF8),
                                        ),
                                        const SizedBox(width: 8),
                                        Text(
                                          taskName,
                                          style: const TextStyle(
                                            fontSize: 16,
                                            fontWeight: FontWeight.bold,
                                          ),
                                        ),
                                      ],
                                    ),
                                    Row(
                                      children: [
                                        InkWell(
                                          onTap: () => _toggle(r),
                                          borderRadius: BorderRadius.circular(
                                            12,
                                          ),
                                          child: Container(
                                            padding: const EdgeInsets.symmetric(
                                              horizontal: 8,
                                              vertical: 4,
                                            ),
                                            decoration: BoxDecoration(
                                              color: r.isEnabled
                                                  ? const Color(0xFF10B981)
                                                  : Colors.grey.shade700,
                                              borderRadius:
                                                  BorderRadius.circular(12),
                                            ),
                                            child: Row(
                                              mainAxisSize: MainAxisSize.min,
                                              children: [
                                                Icon(
                                                  r.isEnabled
                                                      ? Icons.play_arrow
                                                      : Icons.pause,
                                                  size: 12,
                                                  color: Colors.white,
                                                ),
                                                const SizedBox(width: 4),
                                                Text(
                                                  r.isEnabled
                                                      ? t.get(
                                                          'rules_status_active',
                                                        )
                                                      : t.get(
                                                          'rules_status_paused',
                                                        ),
                                                  style: const TextStyle(
                                                    color: Colors.white,
                                                    fontSize: 10,
                                                    fontWeight: FontWeight.bold,
                                                  ),
                                                ),
                                              ],
                                            ),
                                          ),
                                        ),
                                        const SizedBox(width: 8),
                                        IconButton(
                                          icon: const Icon(
                                            Icons.tune_outlined,
                                            size: 20,
                                          ),
                                          tooltip: t.get('rules_full_control'),
                                          onPressed: () =>
                                              showRuleSettingsSheet(
                                                context,
                                                ruleId: r.id,
                                                ruleLabel: srcTitle,
                                              ),
                                        ),
                                        IconButton(
                                          icon: const Icon(
                                            Icons.receipt_long_outlined,
                                            size: 20,
                                          ),
                                          tooltip: t.get('rules_logs_action'),
                                          onPressed: () =>
                                              Navigator.of(context).push(
                                                MaterialPageRoute<void>(
                                                  builder: (_) => LogsScreen(
                                                    initialRuleId: r.id,
                                                    initialRuleLabel: srcTitle,
                                                  ),
                                                ),
                                              ),
                                        ),
                                        IconButton(
                                          icon: const Icon(
                                            Icons.copy_outlined,
                                            size: 20,
                                          ),
                                          tooltip: t.get(
                                            'rules_duplicate_task',
                                          ),
                                          onPressed: () => _duplicate(r),
                                        ),
                                        IconButton(
                                          icon: const Icon(
                                            Icons.delete_outline,
                                            color: Colors.red,
                                            size: 20,
                                          ),
                                          tooltip: t.get('action_delete'),
                                          onPressed: () => _delete(r),
                                        ),
                                      ],
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 12),

                                // Source & Target Routing Visualizer
                                Container(
                                  padding: const EdgeInsets.all(12),
                                  decoration: BoxDecoration(
                                    color: Theme.of(context).cardColor
                                        .withValues(alpha: 0.5),
                                    borderRadius: BorderRadius.circular(12),
                                    border: Border.all(
                                      color: Colors.white.withValues(
                                        alpha: 0.05,
                                      ),
                                    ),
                                  ),
                                  child: Row(
                                    children: [
                                      Expanded(
                                        child: Column(
                                          crossAxisAlignment:
                                              CrossAxisAlignment.start,
                                          children: [
                                            Text(
                                              t.get('rules_col_source'),
                                              style: const TextStyle(
                                                fontSize: 11,
                                                color: Colors.grey,
                                              ),
                                            ),
                                            Text(
                                              srcDisplay,
                                              style: const TextStyle(
                                                fontSize: 13,
                                                fontWeight: FontWeight.w600,
                                              ),
                                              maxLines: 2,
                                              overflow: TextOverflow.ellipsis,
                                            ),
                                            const Text(
                                              'Telegram',
                                              style: TextStyle(
                                                fontSize: 10,
                                                color: Color(0xFF38BDF8),
                                              ),
                                            ),
                                          ],
                                        ),
                                      ),
                                      const Padding(
                                        padding: EdgeInsets.symmetric(
                                          horizontal: 8,
                                        ),
                                        child: Icon(
                                          Icons.arrow_forward_rounded,
                                          size: 16,
                                          color: Colors.grey,
                                        ),
                                      ),
                                      Expanded(
                                        child: Column(
                                          crossAxisAlignment:
                                              CrossAxisAlignment.end,
                                          children: [
                                            Text(
                                              t.get('rules_col_target'),
                                              style: const TextStyle(
                                                fontSize: 11,
                                                color: Colors.grey,
                                              ),
                                            ),
                                            Text(
                                              dstDisplay,
                                              style: const TextStyle(
                                                fontSize: 13,
                                                fontWeight: FontWeight.w600,
                                              ),
                                              maxLines: 2,
                                              overflow: TextOverflow.ellipsis,
                                            ),
                                            Text(
                                              r.isFanOut
                                                  ? t.get(
                                                      'rules_route_fan_out',
                                                      params: {
                                                        'count':
                                                            '${r.allTargets.length}',
                                                      },
                                                    )
                                                  : 'Telegram',
                                              style: const TextStyle(
                                                fontSize: 10,
                                                color: Color(0xFF38BDF8),
                                              ),
                                            ),
                                          ],
                                        ),
                                      ),
                                    ],
                                  ),
                                ),
                                const SizedBox(height: 10),

                                // The rule id is the per-task identity: it is
                                // what a log line, an audit event or a ticket
                                // quotes, so the card shows it up front.
                                Row(
                                  children: [
                                    const Icon(
                                      Icons.fingerprint,
                                      size: 13,
                                      color: Colors.grey,
                                    ),
                                    const SizedBox(width: 6),
                                    Text(
                                      t.get('rules_task_id'),
                                      style: TextStyle(
                                        fontSize: 11,
                                        color: Colors.grey.shade400,
                                      ),
                                    ),
                                    const SizedBox(width: 6),
                                    Text(
                                      '#${r.shortId}',
                                      style: TextStyle(
                                        fontSize: 11,
                                        color: Colors.grey.shade300,
                                        fontFamily: 'monospace',
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 10),

                                // Sender Type & Debug
                                Row(
                                  mainAxisAlignment:
                                      MainAxisAlignment.spaceBetween,
                                  children: [
                                    Text(
                                      t.get(
                                        'rules_sender_line',
                                        params: {'type': r.senderType},
                                      ),
                                      style: TextStyle(
                                        fontSize: 11,
                                        color: Colors.grey.shade400,
                                      ),
                                    ),
                                    Text(
                                      t.get(
                                        'rules_debug_line',
                                        params: {
                                          'st': r.debugMode
                                              ? t.get('rules_state_on')
                                              : t.get('rules_state_off'),
                                        },
                                      ),
                                      style: TextStyle(
                                        fontSize: 11,
                                        color: r.debugMode
                                            ? const Color(0xFF10B981)
                                            : Colors.grey,
                                        fontWeight: FontWeight.w600,
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 12),

                                // 4 Colored Buttons Grid
                                Row(
                                  children: [
                                    Expanded(
                                      child: ElevatedButton(
                                        onPressed: () => _openEditor(r),
                                        style: ElevatedButton.styleFrom(
                                          backgroundColor: const Color(
                                            0xFF16A34A,
                                          ),
                                          foregroundColor: Colors.white,
                                          padding: const EdgeInsets.symmetric(
                                            vertical: 10,
                                          ),
                                          shape: RoundedRectangleBorder(
                                            borderRadius: BorderRadius.circular(
                                              10,
                                            ),
                                          ),
                                        ),
                                        child: Text(
                                          t.get('rules_edit_source'),
                                          style: const TextStyle(
                                            fontSize: 12,
                                            fontWeight: FontWeight.bold,
                                          ),
                                        ),
                                      ),
                                    ),
                                    const SizedBox(width: 8),
                                    Expanded(
                                      child: ElevatedButton(
                                        onPressed: () => _openEditor(r),
                                        style: ElevatedButton.styleFrom(
                                          backgroundColor: const Color(
                                            0xFFDC2626,
                                          ),
                                          foregroundColor: Colors.white,
                                          padding: const EdgeInsets.symmetric(
                                            vertical: 10,
                                          ),
                                          shape: RoundedRectangleBorder(
                                            borderRadius: BorderRadius.circular(
                                              10,
                                            ),
                                          ),
                                        ),
                                        child: Text(
                                          t.get('rules_edit_target'),
                                          style: const TextStyle(
                                            fontSize: 12,
                                            fontWeight: FontWeight.bold,
                                          ),
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 8),
                                Row(
                                  children: [
                                    Expanded(
                                      child: ElevatedButton.icon(
                                        onPressed: () => _openEditor(r),
                                        icon: const Icon(Icons.tag, size: 14),
                                        label: Text(
                                          r.topicId > 0
                                              ? '#${r.topicId}'
                                              : t.get('rules_forwarding_topic'),
                                          style: const TextStyle(
                                            fontSize: 12,
                                            fontWeight: FontWeight.bold,
                                          ),
                                        ),
                                        style: ElevatedButton.styleFrom(
                                          backgroundColor: const Color(
                                            0xFF2563EB,
                                          ),
                                          foregroundColor: Colors.white,
                                          padding: const EdgeInsets.symmetric(
                                            vertical: 10,
                                          ),
                                          shape: RoundedRectangleBorder(
                                            borderRadius: BorderRadius.circular(
                                              10,
                                            ),
                                          ),
                                        ),
                                      ),
                                    ),
                                    const SizedBox(width: 8),
                                    Expanded(
                                      child: OutlinedButton.icon(
                                        onPressed: () => _openEditor(r),
                                        icon: const Icon(
                                          Icons.tune_rounded,
                                          size: 14,
                                        ),
                                        label: Text(
                                          t.get('rules_advanced_settings'),
                                          style: const TextStyle(
                                            fontSize: 11,
                                            fontWeight: FontWeight.bold,
                                          ),
                                        ),
                                        style: OutlinedButton.styleFrom(
                                          padding: const EdgeInsets.symmetric(
                                            vertical: 10,
                                          ),
                                          shape: RoundedRectangleBorder(
                                            borderRadius: BorderRadius.circular(
                                              10,
                                            ),
                                          ),
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                              ],
                            ),
                          ),
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
    );
  }
}
