/// Rules listing screen: view, toggle, pause/resume, delete forwarding tasks.
///
/// Layout follows the adaptive contract: on a phone the screen is one column of
/// task cards with a create action; on an expanded window (>= 840 dp) it becomes
/// a two-pane master–detail — a dense task list beside the selected task's
/// route, status and controls — so a tablet does not waste half its width on
/// stretched cards.
///
/// Every async state is explicit: skeleton while the first page loads, an empty
/// state with a call to action when the account has no tasks, and an error state
/// with Retry when the request failed (a dead server must never look like "you
/// have no tasks").
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/design_tokens.dart';
import '../../core/layout.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_button.dart';
import '../../widgets/app_card.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/empty_state.dart';
import '../../widgets/error_state.dart';
import '../../widgets/loading_list.dart';
import '../../widgets/status_badge.dart';
// Deferred: the editor is the largest screen in the app (1.6k lines) and is
// opened from a button, never on first paint. Deferring it keeps it out of the
// startup kernel/asset load; `loadLibrary()` is a no-op once loaded.
import 'rule_editor_screen.dart' deferred as rule_editor;
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

  /// Selected task in the two-pane layout. Null means "nothing chosen yet"; the
  /// build derives the effective selection so no post-frame setState is needed.
  String? _selectedId;

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

  String _sourceLabel(ForwardRule r, AppMessages t) => _routeLabel(
    r.sourceIds,
    r.sourceChatTitle,
    r.sourceChatId.isNotEmpty ? r.sourceChatId : t.get('rules_source_fallback'),
  );

  String _targetLabel(ForwardRule r, AppMessages t) => _routeLabel(
    r.allTargets,
    r.targetChatTitle,
    r.targetChatId.isNotEmpty ? r.targetChatId : t.get('rules_target_fallback'),
  );

  /// The three states the backend actually distinguishes: running
  /// (active, not paused), paused (active but held) and stopped
  /// (`is_active=false`). A rule that was stopped must never read as Active.
  String _statusLabel(ForwardRule rule, AppMessages t) {
    if (rule.isPaused) return t.get('rules_status_paused');
    return rule.isEnabled
        ? t.get('rules_status_active')
        : t.get('rules_status_stopped');
  }

  StatusTone _statusTone(ForwardRule rule) {
    if (rule.isPaused) return StatusTone.warning;
    return rule.isEnabled ? StatusTone.success : StatusTone.neutral;
  }

  IconData _statusIcon(ForwardRule rule) {
    if (rule.isPaused) return Icons.pause;
    return rule.isEnabled ? Icons.play_arrow : Icons.stop;
  }

  /// The switch is the master switch: off→on resumes a paused task (the
  /// pause flag, not the active flag, is what holds it back) or re-activates
  /// a stopped one; on→off stops it. Pause/resume/stop/restart stay available
  /// as explicit menu actions because they carry intent the switch cannot.
  Future<void> _toggle(ForwardRule rule) async {
    final t = AppMessages.of(context);
    final client = context.read<AppState>().buildApiClient();
    try {
      if (!rule.isEnabled && rule.isPaused) {
        await client.resumeRule(rule.id);
      } else {
        await client.toggleRule(rule.id);
      }
      await _load();
      if (!mounted) return;
      AppToast.success(
        context,
        rule.isEnabled
            ? t.get('rules_status_stopped')
            : t.get('rules_status_active'),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      AppToast.failure(context, exc.error.message);
    }
  }

  Future<void> _lifecycle(
    ForwardRule rule,
    Future<void> Function(String id) action,
  ) async {
    try {
      await action(rule.id);
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      AppToast.failure(context, exc.error.message);
    }
  }

  Future<void> _duplicate(ForwardRule rule) async {
    final client = context.read<AppState>().buildApiClient();
    final t = AppMessages.of(context);
    try {
      await client.duplicateRule(rule.id);
      await _load();
      if (!mounted) return;
      AppToast.success(context, t.get('rules_duplicated'));
    } on ApiException catch (exc) {
      if (!mounted) return;
      AppToast.failure(context, exc.error.message);
    }
  }

  Future<void> _delete(ForwardRule rule) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('rules_delete_confirm')),
        content: Text(
          t.get(
            'rules_route_summary',
            params: {
              'source': _sourceLabel(rule, t),
              'target': _targetLabel(rule, t),
            },
          ),
        ),
        actions: [
          AppButton(
            label: t.get('action_cancel'),
            variant: AppButtonVariant.ghost,
            onPressed: () => Navigator.pop(ctx, false),
          ),
          AppButton(
            label: t.get('action_delete'),
            variant: AppButtonVariant.danger,
            onPressed: () => Navigator.pop(ctx, true),
          ),
        ],
      ),
    );

    if (confirmed != true || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      await client.deleteRule(rule.id);
      if (_selectedId == rule.id) _selectedId = null;
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      AppToast.failure(context, exc.error.message);
    }
  }

  Future<void> _openEditor([ForwardRule? rule]) async {
    await rule_editor.loadLibrary();
    if (!mounted) return;
    final updated = await Navigator.push<bool>(
      context,
      MaterialPageRoute(
        builder: (_) => rule_editor.RuleEditorScreen(rule: rule),
      ),
    );
    if (updated == true) {
      await _load();
    }
  }

  void _openLogs(ForwardRule rule, AppMessages t) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => LogsScreen(
          initialRuleId: rule.id,
          initialRuleLabel: _sourceLabel(rule, t),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final twoPane = AppWindow.of(context).supportsTwoPane;

    final selected = _selectedId == null
        ? (twoPane && _rules.isNotEmpty ? _rules.first.id : null)
        : _rules.where((r) => r.id == _selectedId).map((r) => r.id).firstOrNull;
    final selectedRule = selected == null
        ? null
        : _rules.where((r) => r.id == selected).firstOrNull;

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('rules_title')),
        actions: [
          AppIconButton(
            icon: Icons.refresh,
            tooltip: t.get('action_refresh'),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
      floatingActionButton: twoPane
          ? null
          : FloatingActionButton.extended(
              onPressed: () => _openEditor(),
              icon: const Icon(Icons.add),
              label: Text(t.get('rules_create')),
            ),
      body: MasterDetailPane(
        master: _buildMaster(context, t, dense: twoPane, selectedId: selected),
        detailBuilder: (_) => selectedRule == null
            ? _buildDetailPlaceholder(context, t)
            : _buildDetail(context, t, selectedRule),
      ),
    );
  }

  // ---------------------------------------------------------------- master

  Widget _buildMaster(
    BuildContext context,
    AppMessages t, {
    required bool dense,
    required String? selectedId,
  }) {
    if (_loading && _rules.isEmpty) {
      return LoadingList(rows: dense ? 7 : 4, itemExtent: dense ? 76 : 236);
    }
    if (_error != null && _rules.isEmpty) {
      return ErrorState(message: _error!, onRetry: _load);
    }
    if (_rules.isEmpty) {
      return RefreshIndicator(
        onRefresh: _load,
        child: EmptyState.scrollable(
          title: t.get('rules_empty'),
          subtitle: t.get('rules_empty_sub'),
          icon: Icons.alt_route_rounded,
          actionLabel: t.get('rules_create'),
          onAction: () => _openEditor(),
        ),
      );
    }

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView.builder(
        padding: dense
            ? const EdgeInsets.symmetric(
                horizontal: AppSpacing.sm,
                vertical: AppSpacing.sm,
              )
            : const EdgeInsetsDirectional.fromSTEB(
                AppSpacing.lg,
                AppSpacing.md,
                AppSpacing.lg,
                AppSpacing.xxl * 2,
              ),
        itemCount: _rules.length + (_error != null ? 1 : 0),
        // Fixed extents: the master list is a uniform row, so the viewport can
        // jump-scroll without measuring every child, and only the visible rows
        // are built (no more than ~10 mounted at any size).
        itemExtent: dense ? 76 : null,
        prototypeItem: dense ? null : null,
        itemBuilder: (context, index) {
          if (_error != null && index == 0) {
            return Padding(
              padding: const EdgeInsets.only(bottom: AppSpacing.md),
              child: _errorBanner(t),
            );
          }
          final rule = _rules[_error != null ? index - 1 : index];
          if (dense) {
            return _RuleMasterRow(
              rule: rule,
              title: rule.name.isNotEmpty ? rule.name : '#${rule.shortId}',
              source: _sourceLabel(rule, t),
              target: _targetLabel(rule, t),
              selected: rule.id == selectedId,
              enabledLabel: _statusLabel(rule, t),
              onTap: () => setState(() => _selectedId = rule.id),
            );
          }
          return Padding(
            padding: const EdgeInsets.only(bottom: AppSpacing.md),
            child: _buildDetail(context, t, rule),
          );
        },
      ),
    );
  }

  Widget _errorBanner(AppMessages t) {
    final palette = AppColors.of(context);
    return Container(
      padding: const EdgeInsets.all(AppSpacing.md),
      decoration: BoxDecoration(
        color: palette.danger.withValues(alpha: 0.12),
        borderRadius: AppRadius.allMd,
        border: Border.all(color: palette.danger.withValues(alpha: 0.45)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.error_outline, size: 18, color: palette.danger),
          const SizedBox(width: AppSpacing.sm),
          Expanded(
            child: Text(
              _error!,
              maxLines: 4,
              style: TextStyle(fontSize: AppType.xs, color: palette.text),
            ),
          ),
          AppButton(
            label: t.get('action_retry'),
            variant: AppButtonVariant.link,
            size: AppButtonSize.sm,
            onPressed: _load,
          ),
        ],
      ),
    );
  }

  // ---------------------------------------------------------------- detail

  Widget _buildDetailPlaceholder(BuildContext context, AppMessages t) {
    return EmptyState(
      title: t.get('rules_select_prompt'),
      subtitle: t.get('rules_select_hint'),
      icon: Icons.touch_app_outlined,
    );
  }

  Widget _buildDetail(BuildContext context, AppMessages t, ForwardRule rule) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    final client = context.read<AppState>().buildApiClient();
    final name = rule.name.isNotEmpty ? rule.name : '#${rule.shortId}';

    return AppCard(
      title: name,
      subtitle: t.get('rules_task_id').isEmpty ? null : '#${rule.shortId}',
      trailing: PopupMenuButton<String>(
        tooltip: t.get('rules_more_actions'),
        onSelected: (value) {
          switch (value) {
            case 'pause':
              _lifecycle(rule, client.pauseRule);
            case 'resume':
              _lifecycle(rule, client.resumeRule);
            case 'stop':
              _lifecycle(rule, client.stopRule);
            case 'restart':
              _lifecycle(rule, client.restartRule);
            case 'settings':
              showRuleSettingsSheet(
                context,
                ruleId: rule.id,
                ruleLabel: _sourceLabel(rule, t),
              );
            case 'logs':
              _openLogs(rule, t);
            case 'duplicate':
              _duplicate(rule);
            case 'delete':
              _delete(rule);
          }
        },
        itemBuilder: (ctx) => [
          // The lifecycle actions map one-to-one onto the backend's
          // /rules/{id}/pause|resume|stop|restart endpoints. Pause holds the
          // task without touching the active flag; stop does the opposite;
          // restart re-activates and clears the pause in one call. Only the
          // states each action is valid for expose it: a stopped task offers
          // restart (never resume), a paused one offers resume, a running one
          // offers pause.
          if (rule.isPaused)
            PopupMenuItem(
              value: 'resume',
              child: _menuRow(Icons.play_circle_outline, t.get('rules_resume')),
            )
          else if (rule.isEnabled)
            PopupMenuItem(
              value: 'pause',
              child: _menuRow(Icons.pause_circle_outline, t.get('rules_pause')),
            ),
          if (rule.isEnabled)
            PopupMenuItem(
              value: 'stop',
              child: _menuRow(
                Icons.stop_circle_outlined,
                t.get('rules_stop'),
                color: palette.danger,
              ),
            )
          else
            PopupMenuItem(
              value: 'restart',
              child: _menuRow(Icons.restart_alt, t.get('rules_restart')),
            ),
          const PopupMenuDivider(),
          PopupMenuItem(
            value: 'settings',
            child: _menuRow(Icons.tune_outlined, t.get('rules_full_control')),
          ),
          PopupMenuItem(
            value: 'logs',
            child: _menuRow(
              Icons.receipt_long_outlined,
              t.get('rules_logs_action'),
            ),
          ),
          PopupMenuItem(
            value: 'duplicate',
            child: _menuRow(Icons.copy_outlined, t.get('rules_duplicate_task')),
          ),
          PopupMenuItem(
            value: 'delete',
            child: _menuRow(
              Icons.delete_outline,
              t.get('action_delete'),
              color: palette.danger,
            ),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Status + toggle: one control, one label, one 48 dp target.
          Row(
            children: [
              StatusBadge(
                label: _statusLabel(rule, t),
                tone: _statusTone(rule),
                icon: _statusIcon(rule),
              ),
              const Spacer(),
              Semantics(
                label: t.get('rules_toggle_state'),
                child: Switch(
                  value: rule.isEnabled,
                  onChanged: (_) => _toggle(rule),
                ),
              ),
            ],
          ),
          const SizedBox(height: AppSpacing.md),
          _RouteVisualiser(
            sourceLabel: t.get('rules_col_source'),
            source: _sourceLabel(rule, t),
            targetLabel: t.get('rules_col_target'),
            target: _targetLabel(rule, t),
            fanOut: rule.isFanOut
                ? t.get(
                    'rules_route_fan_out',
                    params: {'count': '${rule.allTargets.length}'},
                  )
                : null,
          ),
          const SizedBox(height: AppSpacing.md),
          _MetaRow(
            icon: Icons.fingerprint,
            label: t.get('rules_task_id'),
            value: '#${rule.shortId}',
            mono: true,
          ),
          _MetaRow(
            icon: Icons.person_outline,
            label: t.get(
              'rules_sender_line',
              params: {'type': rule.senderType},
            ),
            value: rule.debugMode
                ? t.get(
                    'rules_debug_line',
                    params: {'st': t.get('rules_state_on')},
                  )
                : t.get(
                    'rules_debug_line',
                    params: {'st': t.get('rules_state_off')},
                  ),
          ),
          if (rule.topicId > 0)
            _MetaRow(
              icon: Icons.tag,
              label: t.get('rules_forwarding_topic'),
              value: '#${rule.topicId}',
              mono: true,
            ),
          const SizedBox(height: AppSpacing.md),
          // Wrap, not Row: at 320 dp or text scale 1.3 the buttons move to a
          // second line instead of overflowing the card.
          Wrap(
            spacing: AppSpacing.sm,
            runSpacing: AppSpacing.sm,
            children: [
              AppButton(
                label: t.get('action_edit'),
                icon: Icons.edit_outlined,
                onPressed: () => _openEditor(rule),
                size: AppButtonSize.sm,
              ),
              AppButton(
                label: t.get('rules_logs_action'),
                icon: Icons.receipt_long_outlined,
                variant: AppButtonVariant.secondary,
                size: AppButtonSize.sm,
                onPressed: () => _openLogs(rule, t),
              ),
              AppButton(
                label: t.get('rules_duplicate_task'),
                icon: Icons.copy_outlined,
                variant: AppButtonVariant.ghost,
                size: AppButtonSize.sm,
                onPressed: () => _duplicate(rule),
              ),
            ],
          ),
          if (rule.isFanOut) ...[
            const SizedBox(height: AppSpacing.sm),
            Text(
              t.get(
                'rules_route_fan_out',
                params: {'count': '${rule.allTargets.length}'},
              ),
              style: theme.textTheme.bodySmall,
            ),
          ],
        ],
      ),
    );
  }

  Widget _menuRow(IconData icon, String label, {Color? color}) {
    return Row(
      children: [
        Icon(icon, size: 18, color: color),
        const SizedBox(width: AppSpacing.sm),
        Flexible(
          child: Text(
            label,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: color == null ? null : TextStyle(color: color),
          ),
        ),
      ],
    );
  }
}

/// One row of the two-pane master list: status, name, route.
class _RuleMasterRow extends StatelessWidget {
  const _RuleMasterRow({
    required this.rule,
    required this.title,
    required this.source,
    required this.target,
    required this.selected,
    required this.enabledLabel,
    required this.onTap,
  });

  final ForwardRule rule;
  final String title;
  final String source;
  final String target;
  final bool selected;
  final String enabledLabel;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);

    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.xs),
      child: Semantics(
        selected: selected,
        button: true,
        label: '$title. $source → $target. $enabledLabel',
        child: Material(
          color: selected ? palette.surface3 : Colors.transparent,
          borderRadius: AppRadius.allMd,
          child: InkWell(
            onTap: onTap,
            borderRadius: AppRadius.allMd,
            child: Container(
              padding: const EdgeInsets.symmetric(
                horizontal: AppSpacing.md,
                vertical: AppSpacing.sm,
              ),
              decoration: BoxDecoration(
                borderRadius: AppRadius.allMd,
                border: Border.all(
                  color: selected ? palette.primary : Colors.transparent,
                ),
              ),
              child: Row(
                children: [
                  Container(
                    width: 8,
                    height: 8,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: rule.isPaused
                          ? palette.warning
                          : (rule.isEnabled
                                ? palette.success
                                : palette.neutral),
                    ),
                  ),
                  const SizedBox(width: AppSpacing.md),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Text(
                          title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: theme.textTheme.titleSmall,
                        ),
                        const SizedBox(height: 2),
                        Text(
                          '$source → $target',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          textDirection: TextDirection.ltr,
                          style: theme.textTheme.bodySmall,
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: AppSpacing.xs),
                  Icon(
                    AppIcons.forward(context),
                    size: 18,
                    color: palette.textMuted,
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Source → target pair, side by side when there is room and stacked when there
/// is not (320 dp, or text scale 1.3 with long chat titles).
class _RouteVisualiser extends StatelessWidget {
  const _RouteVisualiser({
    required this.sourceLabel,
    required this.source,
    required this.targetLabel,
    required this.target,
    this.fanOut,
  });

  final String sourceLabel;
  final String source;
  final String targetLabel;
  final String target;
  final String? fanOut;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);

    final sourceCol = _Endpoint(label: sourceLabel, value: source);
    final targetCol = _Endpoint(
      label: targetLabel,
      value: target,
      trailing: fanOut,
      alignEnd: true,
    );

    return Container(
      padding: const EdgeInsets.all(AppSpacing.md),
      decoration: BoxDecoration(
        color: palette.surface3,
        borderRadius: AppRadius.allMd,
        border: Border.all(color: palette.border),
      ),
      child: LayoutBuilder(
        builder: (context, constraints) {
          final stack = constraints.maxWidth < 260;
          if (stack) {
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                sourceCol,
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: AppSpacing.sm),
                  child: Icon(
                    Icons.arrow_downward_rounded,
                    size: 16,
                    color: palette.textMuted,
                  ),
                ),
                targetCol,
              ],
            );
          }
          return Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(child: sourceCol),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: AppSpacing.sm),
                child: Icon(
                  // Directional icon: mirrored in RTL by the framework.
                  AppIcons.arrow(context),
                  size: 16,
                  color: palette.textMuted,
                ),
              ),
              Expanded(child: targetCol),
            ],
          );
        },
      ),
    );
  }
}

class _Endpoint extends StatelessWidget {
  const _Endpoint({
    required this.label,
    required this.value,
    this.trailing,
    this.alignEnd = false,
  });

  final String label;
  final String value;
  final String? trailing;
  final bool alignEnd;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cross = alignEnd ? CrossAxisAlignment.end : CrossAxisAlignment.start;
    return Column(
      crossAxisAlignment: cross,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(label, style: theme.textTheme.labelSmall),
        const SizedBox(height: 2),
        Text(
          value,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
          textAlign: alignEnd ? TextAlign.end : TextAlign.start,
          style: theme.textTheme.titleSmall,
        ),
        if (trailing != null)
          Text(
            trailing!,
            style: theme.textTheme.bodySmall?.copyWith(
              color: theme.colorScheme.primary,
            ),
          ),
      ],
    );
  }
}

class _MetaRow extends StatelessWidget {
  const _MetaRow({
    required this.icon,
    required this.label,
    required this.value,
    this.mono = false,
  });

  final IconData icon;
  final String label;
  final String value;
  final bool mono;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        children: [
          Icon(icon, size: 13, color: palette.textMuted),
          const SizedBox(width: AppSpacing.sm),
          Expanded(
            child: Text(label, maxLines: 2, style: theme.textTheme.bodySmall),
          ),
          const SizedBox(width: AppSpacing.sm),
          Text(
            value,
            style: mono
                ? theme.textTheme.bodySmall?.copyWith(fontFamily: 'monospace')
                : theme.textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}
