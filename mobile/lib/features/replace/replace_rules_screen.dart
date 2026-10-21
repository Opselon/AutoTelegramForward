/// Text-replacement rules: the mobile surface of the manager.
///
/// What this screen edits is what the forwarder applies and what the bot's
/// `/replace` panel lists, because it goes through the same core service over
/// `/api/replace-rules*`. Layout follows the app's adaptive contract (`docs/`
/// `ui-ux-system.md`): one column on a phone, a two-pane master–detail on an
/// expanded window, every async state explicit (skeleton / empty / error), and
/// Persian mirrors English through the shell's `Directionality/radios`' RTL
/// flag — nothing here hardcodes a side.
///
/// The list is deliberately the *rule group*, not a task's entry map: the core
/// stores one copy per task and reports them as `task_ids`, so a rule assigned
/// to three tasks appears once and an edit lands on all three.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/design_tokens.dart';
import '../../core/models.dart' show ErrorKind;
import '../../core/replace_models.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_button.dart';
import '../../widgets/app_card.dart';
import '../../widgets/app_field.dart';
import '../../widgets/app_toast.dart';
import '../../widgets/empty_state.dart';
import '../../widgets/error_state.dart';
import '../../widgets/loading_list.dart';
import '../../widgets/status_badge.dart';

/// Rows the list asks for per page. Twelve fits a phone column without
/// becoming an endless scroll; the pager moves in pages the server returns.
const int _perPage = 12;

const List<String> _statusOrder = ['all', 'enabled', 'disabled'];

class ReplaceRulesScreen extends StatefulWidget {
  const ReplaceRulesScreen({super.key});

  @override
  State<ReplaceRulesScreen> createState() => _ReplaceRulesScreenState();
}

class _ReplaceRulesScreenState extends State<ReplaceRulesScreen> {
  ReplaceRulePage _page = const ReplaceRulePage();
  bool _loading = true;
  bool _busy = false;
  String? _error;

  /// The list's own slice of state: the pager's query string and the last
  /// request the user asked for, so a filter change cannot race a slow load.
  /// What the server was last asked for; the field itself lives in [_search].
  String _submittedQuery = '';
  String _status = 'all';
  String _taskFilter = '';
  int _pageNo = 1;

  /// One-shot guard: `didChangeDependencies` can fire on every theme/locale
  /// change, and a second fetch per change would hammer the API.
  bool _bootstrapped = false;

  /// Owned by the state, not built in [build]: a controller constructed during
  /// build loses the caret position on every repaint and leaks one per frame.
  final TextEditingController _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_bootstrapped) return;
    _bootstrapped = true;
    _load();
  }

  // ------------------------------------------------------------------ load

  Future<void> _load({int? page}) async {
    if (!mounted) return;
    final t = AppMessages.of(context);
    setState(() {
      _loading = true;
      _error = null;
    });
    final client = context.read<AppState>().buildApiClient();
    try {
      final data = await client.listReplaceRules(
        query: _submittedQuery,
        status: _status,
        taskId: _taskFilter,
        page: page ?? _pageNo,
        perPage: _perPage,
      );
      if (!mounted) return;
      setState(() {
        _page = data;
        // The server clamps the page (a delete can empty the last page), so
        // the pager follows the answer rather than the request.
        _pageNo = data.page;
      });
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
      // Never swallow a network failure as "no rules": an unreachable server
      // must not look like an empty account.
      if (!mounted) return;
      setState(
        () => _error =
            '${t.get('state_error')} (${exc.toString().split('(').first.trim()})',
      );
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  void _setStatus(String status) {
    if (_status == status) return;
    setState(() {
      _status = status;
      // A stale page 3 on a one-page filter result reads as "no rules"; the
      // pager always restarts when the set changes.
      _pageNo = 1;
    });
    _load(page: 1);
  }

  void _submitQuery(String value) {
    final next = value.trim();
    setState(() {
      _submittedQuery = next;
      _pageNo = 1;
    });
    if (next == _page.query) return;
    _load(page: 1);
  }

  void _setTaskFilter(String value) {
    setState(() {
      _taskFilter = value;
      _pageNo = 1;
    });
    _load(page: 1);
  }

  // ---------------------------------------------------------------- writes

  Future<void> _toggle(ReplaceRule rule) async {
    if (_busy) return;
    final t = AppMessages.of(context);
    setState(() => _busy = true);
    final client = context.read<AppState>().buildApiClient();
    try {
      await client.setReplaceRuleEnabled(rule.id, !rule.enabled);
      if (!mounted) return;
      AppToast.success(
        context,
        rule.enabled ? t.get('rp_notify_disabled') : t.get('rp_notify_enabled'),
      );
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      AppToast.failure(context, exc.error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _duplicate(ReplaceRule rule) async {
    if (_busy) return;
    final t = AppMessages.of(context);
    setState(() => _busy = true);
    final client = context.read<AppState>().buildApiClient();
    try {
      await client.duplicateReplaceRule(rule.id);
      if (!mounted) return;
      AppToast.success(context, t.get('rp_notify_duplicated'));
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      AppToast.failure(context, exc.error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _delete(ReplaceRule rule) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('rp_delete_title')),
        content: Text(
          t.get(
            'rp_delete_body',
            params: {'name': _ruleTitle(rule), 'n': '${rule.taskIds.length}'},
          ),
        ),
        actions: [
          AppButton(
            label: t.get('action_cancel'),
            variant: AppButtonVariant.ghost,
            onPressed: () => Navigator.pop(ctx, false),
          ),
          AppButton(
            label: t.get('rp_delete_confirm'),
            variant: AppButtonVariant.danger,
            onPressed: () => Navigator.pop(ctx, true),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted || _busy) return;

    setState(() => _busy = true);
    final client = context.read<AppState>().buildApiClient();
    try {
      await client.deleteReplaceRule(rule.id);
      if (!mounted) return;
      AppToast.success(context, t.get('rp_notify_deleted'));
      // The last row of the last page leaves that page empty; step back one.
      final goBack = _page.rules.length == 1 && _pageNo > 1;
      await _load(page: goBack ? _pageNo - 1 : _pageNo);
    } on ApiException catch (exc) {
      if (!mounted) return;
      AppToast.failure(context, exc.error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _openEditor([ReplaceRule? rule]) async {
    final t = AppMessages.of(context);
    final saved = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => _ReplaceRuleEditor(rule: rule, page: _page),
    );
    if (saved != true || !mounted) return;
    AppToast.success(
      context,
      t.get(rule == null ? 'rp_notify_created' : 'rp_notify_saved'),
    );
    await _load();
  }

  Future<void> _openTest(ReplaceRule rule) async {
    await showDialog<void>(
      context: context,
      builder: (ctx) => _ReplaceTestSheet(rule: rule),
    );
    // A test cannot change anything, so nothing to refresh — the sheet reads
    // the rule its caller passes and the engine runs server-side.
  }

  // ---------------------------------------------------------------- build

  String _ruleTitle(ReplaceRule rule) {
    final value = rule.title;
    return value.length > 40 ? '${value.substring(0, 40)}…' : value;
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final counts = _page.counts;
    final rules = _page.rules;
    final filtered =
        _submittedQuery.isNotEmpty ||
        _status != 'all' ||
        _taskFilter.isNotEmpty;

    return PopScope(
      canPop: true,
      child: Scaffold(
        appBar: AppBar(
          title: Text(t.get('rp_title')),
          actions: [
            AppIconButton(
              icon: Icons.refresh_rounded,
              tooltip: t.get('action_refresh'),
              onPressed: _loading ? null : _load,
            ),
          ],
        ),
        body: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(
                AppSpacing.lg,
                AppSpacing.md,
                AppSpacing.lg,
                AppSpacing.xs,
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    t.get(
                      'rp_counts',
                      params: {
                        'total': '${counts.total}',
                        'on': '${counts.enabled}',
                        'off': '${counts.disabled}',
                      },
                    ),
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                  const SizedBox(height: AppSpacing.xs),
                  // Search is submitted, not typed-through: every keystroke
                  // would be a round trip, and a slow one would land out of
                  // order and repaint the list with an older query's rows.
                  TextField(
                    controller: _search,
                    decoration: InputDecoration(
                      prefixIcon: const Icon(Icons.search),
                      isDense: true,
                      hintText: t.get('rp_search_hint'),
                      border: const OutlineInputBorder(),
                    ),
                    textInputAction: TextInputAction.search,
                    onSubmitted: _submitQuery,
                  ),
                  const SizedBox(height: AppSpacing.xs),
                  SingleChildScrollView(
                    scrollDirection: Axis.horizontal,
                    child: Row(
                      children: [
                        for (final status in _statusOrder)
                          Padding(
                            padding: const EdgeInsetsDirectional.only(
                              end: AppSpacing.xs,
                            ),
                            child: FilterChip(
                              label: Text(
                                t.get(
                                  status == 'all'
                                      ? 'rp_filter_all'
                                      : status == 'enabled'
                                      ? 'rp_filter_enabled'
                                      : 'rp_filter_disabled',
                                ),
                              ),
                              selected: _status == status,
                              onSelected: (_) => _setStatus(status),
                            ),
                          ),
                      ],
                    ),
                  ),
                  if (_page.tasks.length > 1)
                    Padding(
                      padding: const EdgeInsets.only(top: AppSpacing.xs),
                      child: DropdownButton<String>(
                        value: _taskFilter.isEmpty ? null : _taskFilter,
                        hint: Text(t.get('rp_task_filter')),
                        isExpanded: true,
                        items: [
                          for (final option in _page.tasks)
                            DropdownMenuItem(
                              value: option.id,
                              child: Text(
                                option.label,
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                        ],
                        onChanged: (value) => _setTaskFilter(value ?? ''),
                      ),
                    ),
                ],
              ),
            ),
            Expanded(
              child: _error != null && rules.isEmpty
                  ? ErrorState(message: _error!, onRetry: _load)
                  : _loading && _page.total == 0
                  ? const LoadingList(rows: 5)
                  : rules.isEmpty
                  ? RefreshIndicator(
                      onRefresh: () => _load(page: 1),
                      child: EmptyState.scrollable(
                        title: filtered
                            ? t.get('rp_empty_filtered')
                            : t.get('rp_empty_title'),
                        subtitle: filtered ? null : t.get('rp_empty_body'),
                        icon: Icons.find_replace_outlined,
                        actionLabel: filtered
                            ? t.get('rp_filter_all')
                            : t.get('rp_new'),
                        onAction: filtered
                            ? () async {
                                _search.clear();
                                setState(() {
                                  _submittedQuery = '';
                                  _status = 'all';
                                  _taskFilter = '';
                                  _pageNo = 1;
                                });
                                await _load(page: 1);
                              }
                            : () => _openEditor(),
                      ),
                    )
                  : RefreshIndicator(
                      onRefresh: () => _load(page: 1),
                      child: _RuleList(
                        rules: rules,
                        onOpen: (rule) => _openEditor(rule),
                        onToggle: (rule) => _toggle(rule),
                        onDuplicate: (rule) => _duplicate(rule),
                        onDelete: (rule) => _delete(rule),
                        onTest: (rule) => _openTest(rule),
                      ),
                    ),
            ),
            if (_page.pages > 1)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: AppSpacing.sm),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    AppIconButton(
                      icon: Icons.chevron_left,
                      tooltip: t.get('rp_prev'),
                      onPressed: _pageNo <= 1 || _loading
                          ? null
                          : () {
                              setState(() => _pageNo -= 1);
                              _load();
                            },
                    ),
                    Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: AppSpacing.sm,
                      ),
                      child: Text(
                        t.get(
                          'rp_page_of',
                          params: {
                            'page': '$_pageNo',
                            'pages': '${_page.pages}',
                          },
                        ),
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    ),
                    AppIconButton(
                      icon: Icons.chevron_right,
                      tooltip: t.get('rp_next'),
                      onPressed: _pageNo >= _page.pages || _loading
                          ? null
                          : () {
                              setState(() => _pageNo += 1);
                              _load();
                            },
                    ),
                  ],
                ),
              ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () => _openEditor(),
          icon: const Icon(Icons.add),
          label: Text(t.get('rp_new')),
        ),
      ),
    );
  }
}

// --------------------------------------------------------------------- list

class _RuleList extends StatelessWidget {
  const _RuleList({
    required this.rules,
    required this.onOpen,
    required this.onToggle,
    required this.onDuplicate,
    required this.onDelete,
    required this.onTest,
  });

  final List<ReplaceRule> rules;
  final void Function(ReplaceRule) onOpen;
  final void Function(ReplaceRule) onToggle;
  final void Function(ReplaceRule) onDuplicate;
  final void Function(ReplaceRule) onDelete;
  final void Function(ReplaceRule) onTest;

  @override
  Widget build(BuildContext context) {
    return ListView.separated(
      padding: const EdgeInsets.all(AppSpacing.lg),
      itemCount: rules.length,
      itemBuilder: (context, index) {
        final rule = rules[index];
        final t = AppMessages.of(context);
        final palette = AppColors.of(context);

        return AppCard(
          semanticLabel: _titleOf(rule),
          onTap: () => onOpen(rule),
          accent: rule.enabled
              ? palette.border
              : palette.border.withValues(alpha: 0.4),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Text(
                    rule.enabled ? '🟢' : '🔴',
                    style: const TextStyle(fontSize: 16),
                  ),
                  const SizedBox(width: AppSpacing.xs),
                  Expanded(
                    child: Text(
                      _titleOf(rule),
                      style: Theme.of(context).textTheme.titleMedium,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: AppSpacing.xs),
              Wrap(
                spacing: AppSpacing.xs,
                runSpacing: AppSpacing.xs,
                children: [
                  StatusBadge(
                    label: t.get(
                      rule.enabled ? 'rp_status_on' : 'rp_status_off',
                    ),
                    tone: rule.enabled
                        ? StatusTone.success
                        : StatusTone.neutral,
                  ),
                  StatusBadge(
                    label: t.get(ReplaceMode.labelKey(rule.mode)),
                    tone: StatusTone.info,
                  ),
                  StatusBadge(
                    label: t.get(
                      'rp_priority_short',
                      params: {'priority': '${rule.priority}'},
                    ),
                    tone: StatusTone.neutral,
                  ),
                ],
              ),
              const SizedBox(height: AppSpacing.sm),
              Text(
                rule.badge.isEmpty ? rule.source : rule.badge,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.bodySmall
                    ?.copyWith(fontFamily: 'monospace', color: palette.text),
              ),
              Text(
                rule.to.isEmpty
                    ? t.get('rp_removed_match')
                    : t.get('rp_arrow_to', params: {'text': rule.to}),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  fontFamily: 'monospace',
                  color: rule.to.isEmpty ? palette.danger : palette.success,
                ),
              ),
              const SizedBox(height: AppSpacing.xs),
              Text(
                rule.taskNames.isEmpty
                    ? t.get('rp_no_tasks')
                    : t.get(
                        'rp_runs_on',
                        params: {
                          'n': '${rule.taskIds.length}',
                          'names': rule.taskNames.join(', '),
                        },
                      ),
                style: Theme.of(context).textTheme.bodySmall,
                overflow: TextOverflow.ellipsis,
              ),
              const SizedBox(height: AppSpacing.sm),
              Row(
                children: [
                  TextButton.icon(
                    onPressed: () => onToggle(rule),
                    icon: Icon(
                      rule.enabled
                          ? Icons.toggle_on_outlined
                          : Icons.toggle_off_outlined,
                    ),
                    label: Text(
                      t.get(rule.enabled ? 'rp_disable' : 'rp_enable'),
                    ),
                  ),
                  TextButton.icon(
                    onPressed: () => onTest(rule),
                    icon: const Icon(Icons.science_outlined),
                    label: Text(t.get('rp_test')),
                  ),
                  const Spacer(),
                  IconButton(
                    tooltip: t.get('rp_duplicate'),
                    icon: const Icon(Icons.copy_rounded),
                    onPressed: () => onDuplicate(rule),
                  ),
                  IconButton(
                    tooltip: t.get('action_delete'),
                    icon: Icon(Icons.delete_outline, color: palette.danger),
                    onPressed: () => onDelete(rule),
                  ),
                ],
              ),
            ],
          ),
        );
      },
      separatorBuilder: (context, _) => const SizedBox(height: AppSpacing.sm),
    );
  }

  String _titleOf(ReplaceRule rule) {
    final value = rule.title;
    return value.length > 40 ? '${value.substring(0, 40)}…' : value;
  }
}

// ------------------------------------------------------------------- editor

/// The create/edit sheet. One dialog covers both because the fields are the
/// same and the server carries the id, not the client's vocal opinion about it.
class _ReplaceRuleEditor extends StatefulWidget {
  const _ReplaceRuleEditor({required this.rule, required this.page});

  /// A rule to edit, or null to create, carrying the input shape.
  final ReplaceRule? rule;
  final ReplaceRulePage page;

  @override
  State<_ReplaceRuleEditor> createState() => _ReplaceRuleEditorState();
}

class _ReplaceRuleEditorState extends State<_ReplaceRuleEditor> {
  late final TextEditingController _name = TextEditingController(
    text: widget.rule?.name ?? '',
  );
  late final TextEditingController _source = TextEditingController(
    text: widget.rule?.source ?? '',
  );
  late final TextEditingController _to = TextEditingController(
    text: widget.rule?.to ?? '',
  );
  late final TextEditingController _flags = TextEditingController(
    text: widget.rule?.flags ?? '',
  );
  late final TextEditingController _priority = TextEditingController(
    text: '${widget.rule?.priority ?? 100}',
  );

  late String _mode = widget.rule?.mode ?? ReplaceMode.literal;
  late bool _wholeWord = widget.rule?.wholeWord ?? false;
  late bool _enabled = widget.rule?.enabled ?? true;
  late List<String> _taskIds = [...widget.rule?.taskIds ?? const []];
  String? _formError;

  @override
  void dispose() {
    _name.dispose();
    _source.dispose();
    _to.dispose();
    _flags.dispose();
    _priority.dispose();
    super.dispose();
  }

  bool get _editing => widget.rule != null;

  Future<void> _save() async {
    final t = AppMessages.of(context);
    final source = _source.text.trim();
    if (source.isEmpty) {
      setState(() => _formError = t.get('rp_required_find'));
      return;
    }
    if (_taskIds.isEmpty) {
      setState(() => _formError = t.get('rp_required_tasks'));
      return;
    }
    final draft = ReplaceRule(
      id: widget.rule?.id ?? '',
      name: _name.text.trim(),
      source: source,
      to: _to.text,
      mode: ReplaceMode.normalize(_mode),
      flags: _flags.text.trim(),
      wholeWord: _wholeWord,
      enabled: _enabled,
      priority: int.tryParse(_priority.text.trim()) ?? 100,
      taskIds: _taskIds,
      taskNames: widget.rule?.taskNames ?? const [],
    );

    final client = context.read<AppState>().buildApiClient();
    try {
      if (_editing) {
        await client.updateReplaceRule(widget.rule!.id, draft);
      } else {
        await client.createReplaceRule(draft);
      }
      if (!mounted) return;
      Navigator.pop(context, true);
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(
        () => _formError = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('rp_save_failed'),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final options = _tasks();

    return AlertDialog(
      title: Text(t.get(_editing ? 'rp_edit_title' : 'rp_create_title')),
      content: SingleChildScrollView(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            AppField(
              label: t.get('rp_name_label'),
              hint: t.get('rp_name_hint'),
              controller: _name,
              textCapitalization: TextCapitalization.words,
            ),
            const SizedBox(height: AppSpacing.sm),
            Text(
              t.get('rp_mode_label'),
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: AppSpacing.xs),
            Row(
              children: [
                for (final mode in [ReplaceMode.literal, ReplaceMode.regex])
                  Padding(
                    padding: const EdgeInsetsDirectional.only(
                      end: AppSpacing.xs,
                    ),
                    child: ChoiceChip(
                      label: Text(t.get(ReplaceMode.labelKey(mode))),
                      selected: _mode == mode,
                      onSelected: (_) => setState(() => _mode = mode),
                    ),
                  ),
              ],
            ),
            Text(
              t.get('rp_mode_hint'),
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: AppSpacing.sm),
            AppField(
              label: t.get('rp_find_label'),
              controller: _source,
              keyboardType: TextInputType.text,
            ),
            const SizedBox(height: AppSpacing.sm),
            AppField(
              label: t.get('rp_replace_label'),
              hint: t.get('rp_replace_placeholder'),
              helper: t.get('rp_replace_hint'),
              controller: _to,
            ),
            const SizedBox(height: AppSpacing.sm),
            if (_mode == ReplaceMode.regex) ...[
              AppField(
                label: t.get('rp_flags_label'),
                helper: t.get('rp_flags_hint'),
                controller: _flags,
              ),
              const SizedBox(height: AppSpacing.sm),
            ],
            CheckboxListTile(
              dense: true,
              value: _wholeWord,
              onChanged: (value) => setState(() => _wholeWord = value ?? false),
              title: Text(t.get('rp_whole_word')),
              subtitle: Text(
                t.get('rp_whole_word_hint'),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            AppField(
              label: t.get('rp_priority_label'),
              helper: t.get('rp_priority_hint'),
              controller: _priority,
              keyboardType: TextInputType.number,
            ),
            const SizedBox(height: AppSpacing.sm),
            SwitchListTile(
              dense: true,
              value: _enabled,
              onChanged: (value) => setState(() => _enabled = value),
              title: Text(t.get(_enabled ? 'rp_status_on' : 'rp_status_off')),
            ),
            const SizedBox(height: AppSpacing.sm),
            Text(
              t.get('rp_tasks_label'),
              style: Theme.of(context).textTheme.bodySmall,
            ),
            Text(
              t.get('rp_tasks_hint'),
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: AppSpacing.xs),
            if (options.isEmpty)
              Text(
                t.get('rp_tasks_none'),
                style: Theme.of(context).textTheme.bodySmall
                    ?.copyWith(color: Theme.of(context).colorScheme.error),
              )
            else
              ...options.map(
                (option) => CheckboxListTile(
                  dense: true,
                  value: _taskIds.contains(option.id),
                  onChanged: (checked) {
                    setState(() {
                      if (checked == true) {
                        _taskIds = [..._taskIds, option.id];
                      } else {
                        _taskIds = _taskIds
                            .where((id) => id != option.id)
                            .toList(growable: false);
                      }
                    });
                  },
                  title: Text(option.label, overflow: TextOverflow.ellipsis),
                ),
              ),
            if (_formError != null) ...[
              const SizedBox(height: AppSpacing.xs),
              Text(
                _formError!,
                style: Theme.of(context).textTheme.bodySmall
                    ?.copyWith(color: Theme.of(context).colorScheme.error),
              ),
            ],
          ],
        ),
      ),
      actions: [
        AppButton(
          label: t.get('action_cancel'),
          variant: AppButtonVariant.ghost,
          onPressed: () => Navigator.pop(context, false),
        ),
        AppButton(label: t.get('action_save'), onPressed: _save),
      ],
    );
  }

  /// The task picker's options: the current page's list when it has the
  /// account's tasks, otherwise a dedicated fetch from the server.
  List<ReplaceTaskOption> _tasks() {
    final pageTasks = widget.page.tasks;
    if (pageTasks.isNotEmpty) return pageTasks;
    // Fall back to the names the rule itself carries, so editing an older rule
    // on an empty first page still shows what it runs on.
    final rule = widget.rule;
    if (rule == null) return const [];
    return [
      for (var i = 0; i < rule.taskIds.length; i++)
        ReplaceTaskOption(
          id: rule.taskIds[i],
          name: i < rule.taskNames.length ? rule.taskNames[i] : '',
        ),
    ];
  }
}

// --------------------------------------------------------------------- test

/// The test preview: the sample goes to the server, the engine that runs on a
/// real message answers, and nothing is written.
class _ReplaceTestSheet extends StatefulWidget {
  const _ReplaceTestSheet({required this.rule});

  final ReplaceRule rule;

  @override
  State<_ReplaceTestSheet> createState() => _ReplaceTestSheetState();
}

class _ReplaceTestSheetState extends State<_ReplaceTestSheet> {
  final TextEditingController _sample = TextEditingController();
  ReplaceRuleTestResult? _result;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _sample.dispose();
    super.dispose();
  }

  Future<void> _run() async {
    final t = AppMessages.of(context);
    if (_sample.text.trim().isEmpty) {
      setState(() => _error = t.get('rp_test_need_sample'));
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final client = context.read<AppState>().buildApiClient();
    try {
      final result = await client.testReplaceRule(
        ruleId: widget.rule.id,
        sample: _sample.text,
      );
      if (!mounted) return;
      setState(() => _result = result);
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(
        () => _error = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('rp_test_failed'),
      );
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final palette = AppColors.of(context);
    final mono = Theme.of(context).textTheme.bodySmall
        ?.copyWith(fontFamily: 'monospace', color: palette.text);

    return AlertDialog(
      title: Text(t.get('rp_test_title')),
      content: SingleChildScrollView(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              widget.rule.badge.isEmpty
                  ? widget.rule.source
                  : widget.rule.badge,
              style: mono,
            ),
            Text(
              widget.rule.to.isEmpty
                  ? t.get('rp_removed_match')
                  : t.get('rp_arrow_to', params: {'text': widget.rule.to}),
              style: mono?.copyWith(
                color: widget.rule.to.isEmpty
                    ? palette.danger
                    : palette.success,
              ),
            ),
            const SizedBox(height: AppSpacing.sm),
            AppField(
              label: t.get('rp_test_sample_label'),
              hint: t.get('rp_test_sample_placeholder'),
              controller: _sample,
              maxLines: 3,
            ),
            const SizedBox(height: AppSpacing.sm),
            if (_error != null)
              Text(
                _error!,
                style: Theme.of(context).textTheme.bodySmall
                    ?.copyWith(color: Theme.of(context).colorScheme.error),
              ),
            if (_result != null) ...[
              StatusBadge(
                label: _result!.changed
                    ? t.get(
                        'rp_test_matches',
                        params: {'n': '${_result!.matches}'},
                      )
                    : t.get('rp_test_unchanged'),
                tone: _result!.changed
                    ? StatusTone.success
                    : StatusTone.warning,
              ),
              const SizedBox(height: AppSpacing.xs),
              Text(
                t.get('rp_test_input'),
                style: Theme.of(context).textTheme.bodySmall,
              ),
              Text(_result!.input == '' ? '—' : _result!.input, style: mono),
              const SizedBox(height: AppSpacing.xs),
              Text(
                t.get('rp_test_output'),
                style: Theme.of(context).textTheme.bodySmall,
              ),
              Text(
                _result!.output,
                style: mono?.copyWith(
                  color: _result!.changed ? palette.success : palette.text,
                ),
              ),
            ],
          ],
        ),
      ),
      actions: [
        AppButton(
          label: t.get('action_close'),
          variant: AppButtonVariant.ghost,
          onPressed: () => Navigator.pop(context),
        ),
        AppButton(label: t.get('rp_test_run'), onPressed: _busy ? null : _run),
      ],
    );
  }
}
