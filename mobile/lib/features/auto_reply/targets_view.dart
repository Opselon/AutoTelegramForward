/// Who the bot answers: the targeting mode, the contact list, per-chat prompt
/// overrides, and bulk removal.
///
/// The mode is not decoration — it changes what the list *means*, so the hint
/// under the selector states it and the list is filtered accordingly
/// ([filterAutoReplyTargets]): under SELECTED_ONLY the screen lists the chats
/// that are answered, under ALL_EXCEPT it lists the exceptions. Showing every
/// row in every mode is how an operator comes to believe a disabled chat is
/// being ignored when the mode answers it anyway.
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

class AutoReplyTargetsView extends StatefulWidget {
  const AutoReplyTargetsView({super.key});

  @override
  State<AutoReplyTargetsView> createState() => _AutoReplyTargetsViewState();
}

class _AutoReplyTargetsViewState extends State<AutoReplyTargetsView> {
  AutoReplyTargetPage? _page;
  AutoReplySettings? _settings;
  List<AutoReplyPrompt> _prompts = const [];

  /// The account the resolve endpoint speaks for. Empty means no Telegram
  /// session is connected, and adding by username cannot work.
  String _sessionId = '';

  final Set<String> _selected = {};
  String _search = '';
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
      final settings = await client.autoReplySettings();
      final page = await client.listAutoReplyTargets(mode: settings.targetMode);
      final prompts = await _orEmpty(client.listAutoReplyPrompts);
      final sessions = await _orEmpty(client.listSessions);
      if (!mounted) return;
      setState(() {
        _settings = settings;
        _page = page;
        _prompts = prompts;
        _sessionId = _preferredSession(sessions);
        // A removed chat cannot stay selected.
        _selected.removeWhere(
          (id) => !page.targets.any((target) => target.chatId == id),
        );
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

  /// An active session answers for the account; the first inactive one is a
  /// better guess than nothing, because resolve reports its own failure.
  String _preferredSession(List<TelegramSession> sessions) {
    for (final session in sessions) {
      if (session.isActive) return session.id;
    }
    return sessions.isEmpty ? '' : sessions.first.id;
  }

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  String _failure(ApiException exc, AppMessages t) =>
      exc.error.message.isNotEmpty ? exc.error.message : t.get('state_error');

  Future<void> _setMode(String mode) async {
    final settings = _settings;
    if (settings == null || _busy) return;
    if (settings.targetMode == mode) return;
    final t = AppMessages.of(context);
    final client = _client();
    final previous = settings;
    setState(() {
      _settings = settings.copyWith(targetMode: mode);
      _busy = true;
    });
    try {
      final saved = await client.saveAutoReplySettings(
        settings.copyWith(targetMode: mode),
        fields: const ['target_mode'],
      );
      if (!mounted) return;
      setState(() => _settings = saved);
      await _load();
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      setState(() => _settings = previous);
      _toast(_failure(exc, t));
    } catch (exc) {
      if (!mounted) return;
      setState(() => _settings = previous);
      _toast('${t.get('ar_save_failed')} ($exc)');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _toggleTarget(AutoReplyTarget target, bool enabled) async {
    final t = AppMessages.of(context);
    final client = _client();
    setState(() => _busy = true);
    try {
      await client.updateAutoReplyTarget(
        target.chatId,
        target.copyWith(isEnabled: enabled),
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

  Future<void> _editTarget(AutoReplyTarget target) async {
    final updated = await showDialog<AutoReplyTarget>(
      context: context,
      builder: (_) => _TargetEditorDialog(target: target, prompts: _prompts),
    );
    if (updated == null || !mounted) return;
    final t = AppMessages.of(context);
    final client = _client();
    setState(() => _busy = true);
    try {
      await client.updateAutoReplyTarget(target.chatId, updated);
      await _load();
      _toast(t.get('ar_target_saved'));
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

  Future<void> _addTarget() async {
    final t = AppMessages.of(context);
    if (_sessionId.isEmpty) {
      _toast(t.get('ar_target_need_session'));
      return;
    }
    final controller = TextEditingController();
    final query = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('ar_target_add_title')),
        content: TextField(
          key: const ValueKey<String>('ar_target_query'),
          controller: controller,
          autofocus: true,
          decoration: InputDecoration(
            labelText: t.get('ar_target_query'),
            hintText: t.get('ar_target_query_hint'),
            helperText: t.get('ar_target_query_help'),
            border: const OutlineInputBorder(),
          ),
          onSubmitted: (value) => Navigator.pop(ctx, value),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(t.get('action_cancel')),
          ),
          FilledButton(
            key: const ValueKey<String>('ar_target_resolve'),
            onPressed: () => Navigator.pop(ctx, controller.text),
            child: Text(t.get('ar_target_resolve')),
          ),
        ],
      ),
    );
    if (query == null || query.trim().isEmpty || !mounted) return;

    final client = _client();
    setState(() => _busy = true);
    try {
      final resolved = await client.resolveAutoReplyTarget(
        sessionId: _sessionId,
        query: query,
      );
      final target = resolved.target;
      if (!resolved.found || target == null) {
        _toast(t.get('ar_target_not_found'));
        return;
      }
      await client.addAutoReplyTargets([target]);
      await _load();
      _toast(t.get('ar_target_added'));
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

  Future<void> _removeSelected() async {
    final t = AppMessages.of(context);
    final ids = _selected.toList(growable: false);
    if (ids.isEmpty) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('ar_target_remove')),
        content: Text(
          t.get('ar_target_remove_confirm', params: {'n': '${ids.length}'}),
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
      final removed = await client.deleteAutoReplyTargets(ids);
      if (!mounted) return;
      setState(() => _selected.clear());
      await _load();
      _toast(t.get('ar_target_removed', params: {'n': '$removed'}));
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

  String? _promptName(String promptId) {
    for (final prompt in _prompts) {
      if (prompt.id == promptId) return prompt.name;
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final settings = _settings;
    final page = _page;

    if (_loading && page == null) {
      return const LoadingList(rows: 6, itemExtent: 84);
    }

    final visible = filterAutoReplyTargets(
      page?.targets ?? const [],
      search: _search,
      mode: settings?.targetMode ?? AutoReplyTargetMode.everyone,
    );

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
                if (settings != null) ...[
                  _modeCard(t, settings),
                  const SizedBox(height: AppSpacing.lg),
                ],
                Row(
                  children: [
                    Expanded(
                      child: TextField(
                        key: const ValueKey<String>('ar_targets_search'),
                        decoration: InputDecoration(
                          prefixIcon: const Icon(Icons.search, size: 20),
                          hintText: t.get('ar_targets_search_hint'),
                          isDense: true,
                          border: OutlineInputBorder(
                            borderRadius: AppRadius.allMd,
                          ),
                        ),
                        onChanged: (value) => setState(() => _search = value),
                      ),
                    ),
                    const SizedBox(width: AppSpacing.sm),
                    IconButton.filledTonal(
                      key: const ValueKey<String>('ar_target_add'),
                      tooltip: t.get('ar_target_add'),
                      onPressed: _busy ? null : _addTarget,
                      icon: const Icon(Icons.person_add_alt_1),
                    ),
                  ],
                ),
                const SizedBox(height: AppSpacing.sm),
                Text(
                  t.get(
                    'ar_targets_count',
                    params: {
                      'enabled': '${page?.enabled ?? 0}',
                      'total': '${page?.total ?? 0}',
                    },
                  ),
                  style: TextStyle(
                    color: AppColors.of(context).textMuted,
                    fontSize: 12,
                  ),
                ),
                const SizedBox(height: AppSpacing.md),
                if (visible.isEmpty)
                  EmptyStateView(
                    message: t.get(
                      _search.trim().isEmpty
                          ? 'ar_targets_empty'
                          : 'ar_targets_no_match',
                    ),
                    icon: Icons.manage_accounts_outlined,
                  )
                else
                  for (final target in visible) _targetCard(t, target),
              ],
            ),
          ),
        ),
        if (_selected.isNotEmpty)
          SafeArea(
            top: false,
            child: Container(
              padding: const EdgeInsets.symmetric(
                horizontal: AppSpacing.lg,
                vertical: AppSpacing.sm,
              ),
              color: AppColors.of(context).surface2,
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      t.get(
                        'ar_target_selection',
                        params: {'n': '${_selected.length}'},
                      ),
                      style: Theme.of(context).textTheme.bodyMedium,
                    ),
                  ),
                  TextButton(
                    key: const ValueKey<String>('ar_target_clear'),
                    onPressed: () => setState(() => _selected.clear()),
                    child: Text(t.get('chats_picker_clear')),
                  ),
                  const SizedBox(width: AppSpacing.sm),
                  FilledButton.icon(
                    key: const ValueKey<String>('ar_target_remove_selected'),
                    onPressed: _busy ? null : _removeSelected,
                    icon: const Icon(Icons.delete_outline, size: 18),
                    label: Text(t.get('ar_target_remove')),
                  ),
                ],
              ),
            ),
          ),
      ],
    );
  }

  Widget _modeCard(AppMessages t, AutoReplySettings settings) {
    final mode = settings.targetMode;
    final palette = AppColors.of(context);
    return SectionCard(
      title: t.get('ar_targets_mode'),
      icon: Icons.groups_outlined,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ArChips(
            keyPrefix: 'ar_mode',
            options: [
              for (final value in AutoReplyTargetMode.all)
                ArOption(value, t.get(AutoReplyTargetMode.labelKey(value))),
            ],
            selected: mode,
            onSelected: _setMode,
          ),
          const SizedBox(height: AppSpacing.sm),
          Text(
            t.get(AutoReplyTargetMode.hintKey(mode)),
            style: TextStyle(color: palette.textMuted, fontSize: 12),
          ),
        ],
      ),
    );
  }

  Widget _targetCard(AppMessages t, AutoReplyTarget target) {
    final palette = AppColors.of(context);
    final selected = _selected.contains(target.chatId);
    final prompt = _promptName(target.promptId);
    final details = [
      if (target.username.isNotEmpty) '@${target.username}',
      if (prompt != null)
        t.get('ar_target_prompt_value', params: {'name': prompt}),
      if (target.note.trim().isNotEmpty) target.note,
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.sm),
      child: AppCard(
        padding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.sm,
          vertical: AppSpacing.xs,
        ),
        onTap: () => _editTarget(target),
        accent: target.isEnabled ? palette.success : palette.border,
        child: Row(
          children: [
            Checkbox(
              key: ValueKey<String>('ar_target_select_${target.chatId}'),
              value: selected,
              onChanged: (value) {
                setState(() {
                  if (value == true) {
                    _selected.add(target.chatId);
                  } else {
                    _selected.remove(target.chatId);
                  }
                });
              },
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    target.label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.bodyMedium
                        ?.copyWith(fontWeight: FontWeight.w600),
                  ),
                  Text(
                    details.isEmpty
                        ? target.chatId
                        : '${target.chatId} · ${details.join(' · ')}',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: palette.textMuted, fontSize: 11),
                  ),
                ],
              ),
            ),
            Switch.adaptive(
              key: ValueKey<String>('ar_target_switch_${target.chatId}'),
              value: target.isEnabled,
              onChanged: _busy ? null : (value) => _toggleTarget(target, value),
            ),
          ],
        ),
      ),
    );
  }
}

/// Per-chat overrides: a prompt, a note and a priority.
class _TargetEditorDialog extends StatefulWidget {
  const _TargetEditorDialog({required this.target, required this.prompts});

  final AutoReplyTarget target;
  final List<AutoReplyPrompt> prompts;

  @override
  State<_TargetEditorDialog> createState() => _TargetEditorDialogState();
}

class _TargetEditorDialogState extends State<_TargetEditorDialog> {
  late final TextEditingController _note;
  late final TextEditingController _priority;
  late String _promptId;

  @override
  void initState() {
    super.initState();
    _note = TextEditingController(text: widget.target.note);
    _priority = TextEditingController(text: '${widget.target.priority}');
    _promptId = widget.target.promptId;
  }

  @override
  void dispose() {
    _note.dispose();
    _priority.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    return AlertDialog(
      title: Text(widget.target.label),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            ArDropdown(
              label: t.get('ar_target_prompt'),
              value: _promptId,
              options: [
                ArOption('', t.get('ar_target_prompt_inherit')),
                for (final prompt in widget.prompts)
                  ArOption(prompt.id, prompt.name),
              ],
              onChanged: (value) => setState(() => _promptId = value),
            ),
            AppField(
              label: t.get('ar_target_note'),
              controller: _note,
              maxLines: 3,
              minLines: 1,
              textCapitalization: TextCapitalization.sentences,
            ),
            const SizedBox(height: AppSpacing.md),
            AppField(
              label: t.get('ar_target_priority'),
              controller: _priority,
              keyboardType: TextInputType.number,
              helper: t.get('ar_target_priority_hint'),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: Text(t.get('action_cancel')),
        ),
        FilledButton(
          key: const ValueKey<String>('ar_target_save'),
          onPressed: () => Navigator.of(context).pop(
            widget.target.copyWith(
              promptId: _promptId,
              note: _note.text.trim(),
              priority:
                  int.tryParse(_priority.text.trim()) ?? widget.target.priority,
            ),
          ),
          child: Text(t.get('action_save')),
        ),
      ],
    );
  }
}
