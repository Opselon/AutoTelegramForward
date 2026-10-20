/// Auto-reply settings: the master switch and everything it governs.
///
/// Two save paths on purpose:
///   * the master switch writes immediately with `update_mask: ["enabled"]`,
///     because it is the one control whose whole point is instant effect — and
///     a masked single-field write cannot clobber a concurrent edit elsewhere;
///   * the form's Save button writes every field the screen owns.
///
/// A failed write reverts the control it came from: a switch that stays "on"
/// after the server refused would tell the operator replies are being sent when
/// they are not.
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
import '../../widgets/app_ui.dart';
import '../../widgets/loading_list.dart';
import 'auto_reply_widgets.dart';

/// Every field the form can write, used as the Save button's update mask.
const List<String> kAutoReplySettingsFields = [
  'enabled',
  'ai_config_id',
  'prompt_id',
  'target_mode',
  'language',
  'tone',
  'response_length',
  'temperature',
  'max_tokens',
  'context_messages',
  'memory_enabled',
  'memory_max_facts',
  'extra_instructions',
  'max_reply_chars',
  'typing_simulation',
  'typing_delay_min',
  'typing_delay_max',
  'cooldown_seconds',
  'daily_limit_per_chat',
  'rate_limit_per_minute',
  'max_concurrency',
  'max_attempts',
  'retry_backoff_seconds',
  'quiet_hours_start',
  'quiet_hours_end',
  'timezone_offset_minutes',
  'active_days',
  'fallback_text',
  'ignore_bots',
];

class AutoReplySettingsView extends StatefulWidget {
  const AutoReplySettingsView({super.key});

  @override
  State<AutoReplySettingsView> createState() => _AutoReplySettingsViewState();
}

class _AutoReplySettingsViewState extends State<AutoReplySettingsView> {
  AutoReplySettings? _draft;
  List<AiConfig> _aiConfigs = const [];
  List<AutoReplyPrompt> _prompts = const [];

  bool _loading = true;
  bool _saving = false;
  String? _error;
  String? _actionError;
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
    final client = _client();
    final t = AppMessages.of(context);
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final settings = await client.autoReplySettings();
      final configs = await _orEmpty(client.listAiConfigs);
      final prompts = await _orEmpty(client.listAutoReplyPrompts);
      if (!mounted) return;
      setState(() {
        _draft = settings;
        _aiConfigs = configs;
        _prompts = prompts;
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

  /// The pickers are advisory: a server that cannot list AI configs or prompts
  /// must not blank the settings form, it just offers fewer choices.
  Future<List<T>> _orEmpty<T>(Future<List<T>> Function() read) async {
    try {
      return await read();
    } catch (_) {
      return const [];
    }
  }

  void _update(AutoReplySettings next) {
    setState(() => _draft = next);
  }

  /// Saves one field immediately (the master switch).
  Future<void> _saveField(String field, AutoReplySettings next) async {
    final previous = _draft;
    if (previous == null || _saving) return;
    final t = AppMessages.of(context);
    final client = _client();
    setState(() {
      _draft = next;
      _saving = true;
      _actionError = null;
    });
    try {
      final saved = await client.saveAutoReplySettings(next, fields: [field]);
      if (!mounted) return;
      setState(() => _draft = saved);
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() {
        _draft = previous;
        _actionError = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('ar_save_failed');
      });
    } catch (exc) {
      if (!mounted) return;
      setState(() {
        _draft = previous;
        _actionError = '${t.get('ar_save_failed')} ($exc)';
      });
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _saveAll() async {
    final current = _draft;
    if (current == null || _saving) return;
    final t = AppMessages.of(context);
    final client = _client();
    final messenger = ScaffoldMessenger.of(context);
    setState(() {
      _saving = true;
      _actionError = null;
    });
    try {
      final saved = await client.saveAutoReplySettings(
        current,
        fields: kAutoReplySettingsFields,
      );
      if (!mounted) return;
      setState(() => _draft = saved);
      messenger.showSnackBar(SnackBar(content: Text(t.get('ar_saved'))));
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(
        () => _actionError = exc.error.message.isNotEmpty
            ? exc.error.message
            : t.get('ar_save_failed'),
      );
    } catch (exc) {
      if (!mounted) return;
      setState(() => _actionError = '${t.get('ar_save_failed')} ($exc)');
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    if (_loading && _draft == null) {
      return const LoadingList(rows: 6, itemExtent: 88, showAvatar: false);
    }
    final draft = _draft;
    if (draft == null) {
      return ListView(
        padding: AppSpacing.screen,
        children: [
          InlineBanner(
            message: _error ?? t.get('ar_load_failed'),
            onRetry: _load,
            retryLabel: t.get('action_retry'),
          ),
        ],
      );
    }

    return Column(
      children: [
        Expanded(
          child: RefreshIndicator(
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
                if (_actionError != null) ...[
                  InlineBanner(message: _actionError!, severity: 'warning'),
                  const SizedBox(height: AppSpacing.md),
                ],
                _masterCard(t, draft),
                const SizedBox(height: AppSpacing.lg),
                _replyCard(t, draft),
                const SizedBox(height: AppSpacing.lg),
                _contextCard(t, draft),
                const SizedBox(height: AppSpacing.lg),
                _pacingCard(t, draft),
                const SizedBox(height: AppSpacing.lg),
                _scheduleCard(t, draft),
                const SizedBox(height: AppSpacing.lg),
                _safetyCard(t, draft),
                const SizedBox(height: AppSpacing.lg),
              ],
            ),
          ),
        ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(
              AppSpacing.lg,
              AppSpacing.sm,
              AppSpacing.lg,
              AppSpacing.md,
            ),
            child: SizedBox(
              width: double.infinity,
              child: FilledButton.icon(
                key: const ValueKey<String>('ar_settings_save'),
                onPressed: _saving ? null : _saveAll,
                icon: _saving
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.save_outlined, size: 18),
                label: Text(t.get('action_save')),
              ),
            ),
          ),
        ),
      ],
    );
  }

  Widget _masterCard(AppMessages t, AutoReplySettings draft) {
    final palette = AppColors.of(context);
    return AppCard(
      accent: draft.enabled ? palette.success : palette.border,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(
                draft.enabled
                    ? Icons.mark_chat_read_outlined
                    : Icons.do_not_disturb_on_outlined,
                color: draft.enabled ? palette.success : palette.neutral,
              ),
              const SizedBox(width: AppSpacing.sm),
              Expanded(
                child: Text(
                  t.get('ar_master_switch'),
                  style: Theme.of(context).textTheme.titleMedium,
                ),
              ),
              Switch.adaptive(
                key: const ValueKey<String>('ar_master_switch'),
                value: draft.enabled,
                onChanged: _saving
                    ? null
                    : (value) =>
                          _saveField('enabled', draft.copyWith(enabled: value)),
              ),
            ],
          ),
          const SizedBox(height: AppSpacing.xs),
          Text(
            t.get('ar_master_hint'),
            style: TextStyle(color: palette.textMuted, fontSize: 12),
          ),
          if (_saving) ...[
            const SizedBox(height: AppSpacing.sm),
            LinearProgressIndicator(
              minHeight: 2,
              backgroundColor: palette.surface3,
            ),
          ],
        ],
      ),
    );
  }

  Widget _replyCard(AppMessages t, AutoReplySettings draft) {
    final configOptions = <ArOption>[
      ArOption('', t.get('ar_provider_none')),
      for (final config in _aiConfigs)
        ArOption(
          config.id,
          '${config.provider}${config.model.isEmpty ? '' : ' · ${config.model}'}',
        ),
    ];
    final promptOptions = <ArOption>[
      ArOption('', t.get('ar_prompt_default')),
      for (final prompt in _prompts) ArOption(prompt.id, prompt.name),
    ];
    return SectionCard(
      title: t.get('ar_section_reply'),
      icon: Icons.auto_awesome,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ArDropdown(
            label: t.get('ar_provider'),
            value: draft.aiConfigId,
            options: configOptions,
            helper: t.get('ar_provider_hint'),
            onChanged: (value) => _update(draft.copyWith(aiConfigId: value)),
          ),
          ArDropdown(
            label: t.get('ar_prompt'),
            value: draft.promptId,
            options: promptOptions,
            helper: t.get('ar_prompt_hint'),
            onChanged: (value) => _update(draft.copyWith(promptId: value)),
          ),
          ArDropdown(
            label: t.get('ar_language'),
            value: draft.language,
            options: arLanguageOptions(t),
            onChanged: (value) => _update(draft.copyWith(language: value)),
          ),
          const SizedBox(height: AppSpacing.sm),
          _FieldLabel(t.get('ar_tone')),
          const SizedBox(height: AppSpacing.sm),
          ArChips(
            keyPrefix: 'ar_tone',
            options: arToneOptions(t),
            selected: draft.tone,
            onSelected: (value) => _update(draft.copyWith(tone: value)),
          ),
          const SizedBox(height: AppSpacing.md),
          _FieldLabel(t.get('ar_length')),
          const SizedBox(height: AppSpacing.sm),
          ArChips(
            keyPrefix: 'ar_length',
            options: arLengthOptions(t),
            selected: draft.responseLength,
            onSelected: (value) =>
                _update(draft.copyWith(responseLength: value)),
          ),
          const SizedBox(height: AppSpacing.md),
          ArTextField(
            label: t.get('ar_extra_instructions'),
            value: draft.extraInstructions,
            helper: t.get('ar_extra_hint'),
            maxLines: 4,
            minLines: 2,
            onChanged: (value) =>
                _update(draft.copyWith(extraInstructions: value)),
          ),
        ],
      ),
    );
  }

  Widget _contextCard(AppMessages t, AutoReplySettings draft) {
    return SectionCard(
      title: t.get('ar_section_context'),
      icon: Icons.psychology_outlined,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ArNumberField(
            label: t.get('ar_context'),
            value: draft.contextMessages,
            helper: t.get('ar_context_hint'),
            onChanged: (value) =>
                _update(draft.copyWith(contextMessages: value.toInt())),
          ),
          ArSwitch(
            title: t.get('ar_memory'),
            subtitle: t.get('ar_memory_hint'),
            value: draft.memoryEnabled,
            onChanged: (value) => _update(draft.copyWith(memoryEnabled: value)),
          ),
          if (draft.memoryEnabled)
            ArNumberField(
              label: t.get('ar_memory_facts'),
              value: draft.memoryMaxFacts,
              onChanged: (value) =>
                  _update(draft.copyWith(memoryMaxFacts: value.toInt())),
            ),
          ArNumberField(
            label: t.get('ar_temperature'),
            value: draft.temperature,
            decimal: true,
            helper: t.get('ar_temperature_hint'),
            onChanged: (value) =>
                _update(draft.copyWith(temperature: value.toDouble())),
          ),
          ArNumberField(
            label: t.get('ar_max_tokens'),
            value: draft.maxTokens,
            onChanged: (value) =>
                _update(draft.copyWith(maxTokens: value.toInt())),
          ),
          ArNumberField(
            label: t.get('ar_max_reply_chars'),
            value: draft.maxReplyChars,
            helper: t.get('ar_max_reply_chars_hint'),
            onChanged: (value) =>
                _update(draft.copyWith(maxReplyChars: value.toInt())),
          ),
        ],
      ),
    );
  }

  Widget _pacingCard(AppMessages t, AutoReplySettings draft) {
    return SectionCard(
      title: t.get('ar_section_pacing'),
      icon: Icons.timer_outlined,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ArSwitch(
            title: t.get('ar_typing'),
            subtitle: t.get('ar_typing_hint'),
            value: draft.typingSimulation,
            onChanged: (value) =>
                _update(draft.copyWith(typingSimulation: value)),
          ),
          if (draft.typingSimulation) ...[
            ArNumberField(
              label: t.get('ar_typing_delay_min'),
              value: draft.typingDelayMin,
              onChanged: (value) =>
                  _update(draft.copyWith(typingDelayMin: value.toInt())),
            ),
            ArNumberField(
              label: t.get('ar_typing_delay_max'),
              value: draft.typingDelayMax,
              onChanged: (value) =>
                  _update(draft.copyWith(typingDelayMax: value.toInt())),
            ),
          ],
          ArNumberField(
            label: t.get('ar_cooldown'),
            value: draft.cooldownSeconds,
            helper: t.get('ar_cooldown_hint'),
            onChanged: (value) =>
                _update(draft.copyWith(cooldownSeconds: value.toInt())),
          ),
          ArNumberField(
            label: t.get('ar_daily_limit'),
            value: draft.dailyLimitPerChat,
            onChanged: (value) =>
                _update(draft.copyWith(dailyLimitPerChat: value.toInt())),
          ),
          ArNumberField(
            label: t.get('ar_rate_limit'),
            value: draft.rateLimitPerMinute,
            onChanged: (value) =>
                _update(draft.copyWith(rateLimitPerMinute: value.toInt())),
          ),
          ArNumberField(
            label: t.get('ar_max_concurrency'),
            value: draft.maxConcurrency,
            onChanged: (value) =>
                _update(draft.copyWith(maxConcurrency: value.toInt())),
          ),
        ],
      ),
    );
  }

  Widget _scheduleCard(AppMessages t, AutoReplySettings draft) {
    final days = draft.activeDays;
    return SectionCard(
      title: t.get('ar_section_schedule'),
      icon: Icons.schedule_outlined,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ArTextField(
            label: t.get('ar_quiet_start'),
            value: draft.quietHoursStart,
            hint: t.get('ar_quiet_hint'),
            onChanged: (value) =>
                _update(draft.copyWith(quietHoursStart: value)),
          ),
          ArTextField(
            label: t.get('ar_quiet_end'),
            value: draft.quietHoursEnd,
            hint: t.get('ar_quiet_hint'),
            onChanged: (value) => _update(draft.copyWith(quietHoursEnd: value)),
          ),
          ArNumberField(
            label: t.get('ar_timezone_offset'),
            value: draft.timezoneOffsetMinutes,
            helper: t.get('ar_timezone_hint'),
            onChanged: (value) =>
                _update(draft.copyWith(timezoneOffsetMinutes: value.toInt())),
          ),
          const SizedBox(height: AppSpacing.sm),
          _FieldLabel(t.get('ar_active_days')),
          const SizedBox(height: AppSpacing.sm),
          Wrap(
            spacing: AppSpacing.sm,
            runSpacing: AppSpacing.sm,
            children: [
              for (final option in arDayOptions(t))
                FilterChip(
                  key: ValueKey<String>('ar_day_${option.value}'),
                  label: Text(option.label),
                  selected: days.contains(int.parse(option.value)),
                  onSelected: (selected) {
                    final day = int.parse(option.value);
                    final next = [...days];
                    if (selected) {
                      if (!next.contains(day)) next.add(day);
                    } else {
                      next.remove(day);
                    }
                    next.sort();
                    _update(draft.copyWith(activeDays: next));
                  },
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _safetyCard(AppMessages t, AutoReplySettings draft) {
    return SectionCard(
      title: t.get('ar_section_safety'),
      icon: Icons.shield_outlined,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ArNumberField(
            label: t.get('ar_max_attempts'),
            value: draft.maxAttempts,
            onChanged: (value) =>
                _update(draft.copyWith(maxAttempts: value.toInt())),
          ),
          ArNumberField(
            label: t.get('ar_backoff'),
            value: draft.retryBackoffSeconds,
            onChanged: (value) =>
                _update(draft.copyWith(retryBackoffSeconds: value.toInt())),
          ),
          ArSwitch(
            title: t.get('ar_ignore_bots'),
            subtitle: t.get('ar_ignore_bots_hint'),
            value: draft.ignoreBots,
            onChanged: (value) => _update(draft.copyWith(ignoreBots: value)),
          ),
          ArTextField(
            label: t.get('ar_fallback'),
            value: draft.fallbackText,
            helper: t.get('ar_fallback_hint'),
            maxLines: 3,
            minLines: 1,
            onChanged: (value) => _update(draft.copyWith(fallbackText: value)),
          ),
        ],
      ),
    );
  }
}

/// In-card field label, matching the weight of a form control's own label.
class _FieldLabel extends StatelessWidget {
  const _FieldLabel(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    final palette = AppColors.of(context);
    return Text(
      text,
      style: TextStyle(
        color: palette.textMuted,
        fontSize: 12,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.2,
      ),
    );
  }
}
