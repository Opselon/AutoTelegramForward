/// Rules: the ordered policy layer between "a message arrived" and "the model
/// answers".
///
/// Priority is the order the server evaluates rules in, so the list is sorted by
/// it and the up/down controls persist a whole new order through
/// `POST /rules/reorder` — the client never renumbers priorities itself, and a
/// failed reorder is reverted by re-reading the server's order.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/auto_reply_models.dart';
import '../../core/design_tokens.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_card.dart';
import '../../widgets/app_field.dart';
import '../../widgets/app_ui.dart';
import '../../widgets/loading_list.dart';
import 'auto_reply_widgets.dart';

class AutoReplyRulesView extends StatefulWidget {
  const AutoReplyRulesView({super.key});

  @override
  State<AutoReplyRulesView> createState() => _AutoReplyRulesViewState();
}

class _AutoReplyRulesViewState extends State<AutoReplyRulesView> {
  List<AutoReplyRule> _rules = const [];
  List<AutoReplyPrompt> _prompts = const [];
  List<AiConfig> _configs = const [];
  bool _loading = true;
  bool _busy = false;
  String? _error;
  bool _bootstrapped = false;

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
      final rules = await client.listAutoReplyRules();
      final prompts = await _orEmpty(client.listAutoReplyPrompts);
      final configs = await _orEmpty(client.listAiConfigs);
      if (!mounted) return;
      rules.sort((a, b) => a.priority.compareTo(b.priority));
      setState(() {
        _rules = List<AutoReplyRule>.unmodifiable(rules);
        _prompts = prompts;
        _configs = configs;
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
      if (!mounted) return;
      setState(() => _error = '$exc');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<List<T>> _orEmpty<T>(Future<List<T>> Function() read) async {
    try {
      return await read();
    } catch (_) {
      return const [];
    }
  }

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  String _failure(ApiException exc, AppMessages t) =>
      exc.error.message.isNotEmpty ? exc.error.message : t.get('state_error');

  Future<void> _openEditor(AutoReplyRule? existing) async {
    final draft = await showDialog<AutoReplyRule>(
      context: context,
      builder: (_) => _RuleEditorDialog(
        existing: existing,
        prompts: _prompts,
        configs: _configs,
        nextPriority: _rules.isEmpty ? 0 : _rules.last.priority + 10,
      ),
    );
    if (draft == null || !mounted) return;
    final t = AppMessages.of(context);
    final client = _client();
    setState(() => _busy = true);
    try {
      if (existing == null) {
        await client.createAutoReplyRule(draft);
      } else {
        await client.updateAutoReplyRule(existing.id, draft);
      }
      await _load();
      _toast(t.get('ar_rule_saved'));
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      _toast(_failure(exc, t));
    } catch (exc) {
      if (!mounted) return;
      _toast('${t.get('state_error')} ($exc)');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _toggle(AutoReplyRule rule, bool enabled) async {
    final t = AppMessages.of(context);
    final client = _client();
    setState(() => _busy = true);
    try {
      await client.updateAutoReplyRule(
        rule.id,
        rule.copyWith(isEnabled: enabled),
      );
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      _toast(_failure(exc, t));
    } catch (exc) {
      if (!mounted) return;
      _toast('${t.get('state_error')} ($exc)');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _delete(AutoReplyRule rule) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('action_delete')),
        content: Text(
          t.get('ar_rule_delete_confirm', params: {'name': rule.name}),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(t.get('action_cancel')),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(t.get('action_delete')),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    final client = _client();
    setState(() => _busy = true);
    try {
      await client.deleteAutoReplyRule(rule.id);
      await _load();
      _toast(t.get('ar_rule_deleted'));
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      _toast(_failure(exc, t));
    } catch (exc) {
      if (!mounted) return;
      _toast('${t.get('state_error')} ($exc)');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _move(int index, int delta) async {
    final target = index + delta;
    if (target < 0 || target >= _rules.length || _busy) return;
    final t = AppMessages.of(context);
    final reordered = [..._rules];
    final moved = reordered.removeAt(index);
    reordered.insert(target, moved);
    final previous = _rules;
    setState(() {
      _rules = List<AutoReplyRule>.unmodifiable(reordered);
      _busy = true;
    });
    final client = _client();
    try {
      await client.reorderAutoReplyRules([
        for (final rule in reordered) rule.id,
      ]);
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() => _rules = previous);
      _toast(_failure(exc, t));
    } catch (exc) {
      if (!mounted) return;
      setState(() => _rules = previous);
      _toast('${t.get('state_error')} ($exc)');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  String? _promptName(String id) {
    for (final prompt in _prompts) {
      if (prompt.id == id) return prompt.name;
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    if (_loading && _rules.isEmpty && _error == null) {
      return const LoadingList(rows: 4, itemExtent: 104, showAvatar: false);
    }
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
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(
              key: const ValueKey<String>('ar_rule_new'),
              onPressed: _busy ? null : () => _openEditor(null),
              icon: const Icon(Icons.add, size: 18),
              label: Text(t.get('ar_rule_new')),
            ),
          ),
          const SizedBox(height: AppSpacing.lg),
          if (_rules.isEmpty)
            EmptyStateView(
              message: t.get('ar_rules_empty'),
              icon: Icons.rule_outlined,
            )
          else
            for (var i = 0; i < _rules.length; i++) _ruleCard(t, _rules[i], i),
        ],
      ),
    );
  }

  Widget _ruleCard(AppMessages t, AutoReplyRule rule, int index) {
    final palette = AppColors.of(context);
    final prompt = _promptName(rule.promptId);
    final keywords = rule.keywords;
    final exclusions = rule.exclusionKeywords;
    final details = <String>[
      if (keywords.isEmpty)
        t.get('ar_rule_match_all')
      else
        t.get(
          'ar_rule_match_keywords',
          params: {'keywords': keywords.join(', ')},
        ),
      if (exclusions.isNotEmpty)
        t.get(
          'ar_rule_exclude_keywords',
          params: {'keywords': exclusions.join(', ')},
        ),
      if (rule.cooldownSeconds >= 0)
        t.get(
          'ar_rule_cooldown_value',
          params: {'seconds': '${rule.cooldownSeconds}'},
        ),
      if (prompt != null)
        t.get('ar_target_prompt_value', params: {'name': prompt}),
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.md),
      child: AppCard(
        accent: rule.isEnabled ? palette.primary : palette.border,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    rule.name,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.titleSmall,
                  ),
                ),
                StatusPill(
                  label: t.get(AutoReplyAction.labelKey(rule.action)),
                  color: rule.action == AutoReplyAction.skip
                      ? palette.warning
                      : palette.success,
                  dense: true,
                ),
                Switch.adaptive(
                  key: ValueKey<String>('ar_rule_switch_${rule.id}'),
                  value: rule.isEnabled,
                  onChanged: _busy ? null : (value) => _toggle(rule, value),
                ),
              ],
            ),
            const SizedBox(height: AppSpacing.xs),
            Text(
              details.join(' · '),
              style: TextStyle(color: palette.textMuted, fontSize: 12),
            ),
            const SizedBox(height: AppSpacing.xs),
            Row(
              children: [
                IconButton(
                  key: ValueKey<String>('ar_rule_up_${rule.id}'),
                  tooltip: t.get('ar_rule_move_up'),
                  onPressed: index == 0 || _busy
                      ? null
                      : () => _move(index, -1),
                  icon: const Icon(Icons.arrow_upward, size: 18),
                ),
                IconButton(
                  key: ValueKey<String>('ar_rule_down_${rule.id}'),
                  tooltip: t.get('ar_rule_move_down'),
                  onPressed: index == _rules.length - 1 || _busy
                      ? null
                      : () => _move(index, 1),
                  icon: const Icon(Icons.arrow_downward, size: 18),
                ),
                const Spacer(),
                TextButton.icon(
                  key: ValueKey<String>('ar_rule_edit_${rule.id}'),
                  onPressed: _busy ? null : () => _openEditor(rule),
                  icon: const Icon(Icons.edit_outlined, size: 18),
                  label: Text(t.get('action_edit')),
                ),
                IconButton(
                  key: ValueKey<String>('ar_rule_delete_${rule.id}'),
                  tooltip: t.get('action_delete'),
                  onPressed: _busy ? null : () => _delete(rule),
                  icon: Icon(Icons.delete_outline, color: palette.danger),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

/// Create / edit one rule.
class _RuleEditorDialog extends StatefulWidget {
  const _RuleEditorDialog({
    required this.existing,
    required this.prompts,
    required this.configs,
    required this.nextPriority,
  });

  final AutoReplyRule? existing;
  final List<AutoReplyPrompt> prompts;
  final List<AiConfig> configs;
  final int nextPriority;

  @override
  State<_RuleEditorDialog> createState() => _RuleEditorDialogState();
}

class _RuleEditorDialogState extends State<_RuleEditorDialog> {
  late final TextEditingController _name;
  late final TextEditingController _keywords;
  late final TextEditingController _exclusions;
  late final TextEditingController _priority;
  late final TextEditingController _temperature;
  late final TextEditingController _maxTokens;

  late String _action;
  late String _promptId;
  late String _configId;
  late String _language;
  late String _tone;
  late String _length;
  late int _cooldown;
  bool _showErrors = false;

  @override
  void initState() {
    super.initState();
    final r = widget.existing;
    _name = TextEditingController(text: r?.name ?? '');
    _keywords = TextEditingController(
      text: (r?.keywords ?? const []).join(', '),
    );
    _exclusions = TextEditingController(
      text: (r?.exclusionKeywords ?? const []).join(', '),
    );
    _priority = TextEditingController(
      text: '${r?.priority ?? widget.nextPriority}',
    );
    _temperature = TextEditingController(text: '${r?.temperature ?? 0}');
    _maxTokens = TextEditingController(text: '${r?.maxTokens ?? 0}');
    _action = r?.action ?? AutoReplyAction.reply;
    _promptId = r?.promptId ?? '';
    _configId = r?.aiConfigId ?? '';
    _language = r?.language ?? '';
    _tone = r?.tone ?? '';
    _length = r?.responseLength ?? '';
    _cooldown = r?.cooldownSeconds ?? -1;
  }

  @override
  void dispose() {
    _name.dispose();
    _keywords.dispose();
    _exclusions.dispose();
    _priority.dispose();
    _temperature.dispose();
    _maxTokens.dispose();
    super.dispose();
  }

  List<String> _split(String raw) => raw
      .split(',')
      .map((e) => e.trim())
      .where((e) => e.isNotEmpty)
      .toList(growable: false);

  void _submit() {
    setState(() => _showErrors = true);
    if (_name.text.trim().isEmpty) return;
    final existing = widget.existing;
    final rule = AutoReplyRule(
      id: existing?.id ?? '',
      name: _name.text.trim(),
      isEnabled: existing?.isEnabled ?? true,
      priority: int.tryParse(_priority.text.trim()) ?? widget.nextPriority,
      action: _action,
      conditions: ruleConditions(_split(_keywords.text)),
      exclusions: ruleConditions(_split(_exclusions.text)),
      promptId: _promptId,
      aiConfigId: _configId,
      cooldownSeconds: _cooldown,
      language: _language,
      tone: _tone,
      responseLength: _length,
      temperature: double.tryParse(_temperature.text.trim()) ?? 0,
      maxTokens: int.tryParse(_maxTokens.text.trim()) ?? 0,
    );
    Navigator.of(context).pop(rule);
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final isEdit = widget.existing != null;
    return AlertDialog(
      title: Text(t.get(isEdit ? 'ar_rule_edit' : 'ar_rule_new')),
      content: SizedBox(
        width: 520,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              AppField(
                key: const ValueKey<String>('ar_rule_name'),
                label: t.get('ar_rule_name'),
                controller: _name,
                errorText: _showErrors && _name.text.trim().isEmpty
                    ? t.get('ar_rule_name_required')
                    : null,
                textCapitalization: TextCapitalization.sentences,
              ),
              const SizedBox(height: AppSpacing.md),
              ArDropdown(
                label: t.get('ar_rule_action'),
                value: _action,
                options: [
                  for (final action in AutoReplyAction.all)
                    ArOption(action, t.get(AutoReplyAction.labelKey(action))),
                ],
                helper: t.get('ar_rule_action_hint'),
                onChanged: (value) => setState(() => _action = value),
              ),
              AppField(
                label: t.get('ar_rule_priority'),
                controller: _priority,
                keyboardType: TextInputType.number,
                helper: t.get('ar_rule_priority_hint'),
              ),
              const SizedBox(height: AppSpacing.md),
              AppField(
                key: const ValueKey<String>('ar_rule_keywords'),
                label: t.get('ar_rule_conditions'),
                controller: _keywords,
                helper: t.get('ar_rule_conditions_hint'),
              ),
              const SizedBox(height: AppSpacing.md),
              AppField(
                label: t.get('ar_rule_exclusions'),
                controller: _exclusions,
                helper: t.get('ar_rule_exclusions_hint'),
              ),
              const SizedBox(height: AppSpacing.md),
              ArDropdown(
                label: t.get('ar_rule_prompt'),
                value: _promptId,
                options: [
                  ArOption('', t.get('ar_inherit')),
                  for (final prompt in widget.prompts)
                    ArOption(prompt.id, prompt.name),
                ],
                onChanged: (value) => setState(() => _promptId = value),
              ),
              ArDropdown(
                label: t.get('ar_rule_provider'),
                value: _configId,
                options: [
                  ArOption('', t.get('ar_inherit')),
                  for (final config in widget.configs)
                    ArOption(
                      config.id,
                      '${config.provider}'
                      '${config.model.isEmpty ? '' : ' · ${config.model}'}',
                    ),
                ],
                onChanged: (value) => setState(() => _configId = value),
              ),
              ArDropdown(
                label: t.get('ar_language'),
                value: _language,
                options: [
                  ArOption('', t.get('ar_inherit')),
                  ...arLanguageOptions(t).where((o) => o.value.isNotEmpty),
                ],
                onChanged: (value) => setState(() => _language = value),
              ),
              ArDropdown(
                label: t.get('ar_tone'),
                value: _tone,
                options: [
                  ArOption('', t.get('ar_inherit')),
                  ...arToneOptions(t),
                ],
                onChanged: (value) => setState(() => _tone = value),
              ),
              ArDropdown(
                label: t.get('ar_length'),
                value: _length,
                options: [
                  ArOption('', t.get('ar_inherit')),
                  ...arLengthOptions(t),
                ],
                onChanged: (value) => setState(() => _length = value),
              ),
              AppField(
                label: t.get('ar_rule_cooldown'),
                initialValue: '$_cooldown',
                keyboardType: const TextInputType.numberWithOptions(
                  signed: true,
                ),
                helper: t.get('ar_rule_cooldown_hint'),
                onChanged: (value) {
                  final parsed = int.tryParse(value.trim());
                  if (parsed != null) _cooldown = parsed;
                },
              ),
              const SizedBox(height: AppSpacing.md),
              Row(
                children: [
                  Expanded(
                    child: AppField(
                      label: t.get('ar_temperature'),
                      controller: _temperature,
                      keyboardType: const TextInputType.numberWithOptions(
                        decimal: true,
                      ),
                      helper: t.get('ar_rule_inherit_zero'),
                    ),
                  ),
                  const SizedBox(width: AppSpacing.md),
                  Expanded(
                    child: AppField(
                      label: t.get('ar_max_tokens'),
                      controller: _maxTokens,
                      keyboardType: TextInputType.number,
                      helper: t.get('ar_rule_inherit_zero'),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: Text(t.get('action_cancel')),
        ),
        FilledButton(
          key: const ValueKey<String>('ar_rule_save'),
          onPressed: _submit,
          child: Text(t.get(isEdit ? 'action_save' : 'action_confirm')),
        ),
      ],
    );
  }
}
