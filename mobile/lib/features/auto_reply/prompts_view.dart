/// The prompt library: search, create, edit, duplicate, delete, set default,
/// archive, import and export.
///
/// Import and export both go through the server's own document format
/// (`POST /import`, `GET /export`) rather than a client-side shape, so a
/// document exported here is the same one the web dashboard and the bot
/// exchange. The app has no file-system dependency, so export copies the JSON to
/// the clipboard — the same affordance the log export uses.
library;

import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
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
import 'prompt_editor_dialog.dart';

class AutoReplyPromptsView extends StatefulWidget {
  const AutoReplyPromptsView({super.key});

  @override
  State<AutoReplyPromptsView> createState() => _AutoReplyPromptsViewState();
}

class _AutoReplyPromptsViewState extends State<AutoReplyPromptsView> {
  List<AutoReplyPrompt> _prompts = const [];
  bool _loading = true;
  bool _busy = false;
  bool _includeArchived = false;
  String? _error;
  String _search = '';
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
    try {
      final prompts = await _client().listAutoReplyPrompts(
        includeArchived: _includeArchived,
      );
      if (!mounted) return;
      setState(() => _prompts = prompts);
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

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  /// Runs one mutation, then re-reads the list so the screen shows what the
  /// server stored rather than what was submitted.
  Future<void> _run(
    Future<void> Function(ApiClient client) action, {
    required String successKey,
  }) async {
    if (_busy) return;
    final t = AppMessages.of(context);
    final client = _client();
    setState(() => _busy = true);
    try {
      await action(client);
      await _load();
      _toast(t.get(successKey));
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      _toast(
        exc.error.message.isNotEmpty ? exc.error.message : t.get('state_error'),
      );
    } catch (exc) {
      if (!mounted) return;
      _toast('${t.get('state_error')} ($exc)');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _openEditor(AutoReplyPrompt? existing) async {
    final draft = await showDialog<AutoReplyPrompt>(
      context: context,
      builder: (_) => PromptEditorDialog(existing: existing),
    );
    if (draft == null || !mounted) return;
    await _run(
      (client) => existing == null
          ? client.createAutoReplyPrompt(draft)
          : client.updateAutoReplyPrompt(existing.id, draft),
      successKey: 'ar_prompt_saved',
    );
  }

  Future<void> _duplicate(AutoReplyPrompt prompt) async {
    await _run(
      (client) => client.duplicateAutoReplyPrompt(prompt.id),
      successKey: 'ar_prompt_duplicate_done',
    );
  }

  Future<void> _setDefault(AutoReplyPrompt prompt) async {
    await _run(
      (client) => client.updateAutoReplyPrompt(
        prompt.id,
        prompt.copyWith(isDefault: true),
      ),
      successKey: 'ar_prompt_saved',
    );
  }

  Future<void> _toggleArchived(AutoReplyPrompt prompt) async {
    await _run(
      (client) => client.updateAutoReplyPrompt(
        prompt.id,
        prompt.copyWith(isArchived: !prompt.isArchived),
      ),
      successKey: 'ar_prompt_saved',
    );
  }

  Future<void> _delete(AutoReplyPrompt prompt) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('action_delete')),
        content: Text(
          t.get('ar_prompt_delete_confirm', params: {'name': prompt.name}),
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
    await _run(
      (client) => client.deleteAutoReplyPrompt(prompt.id),
      successKey: 'ar_prompt_deleted',
    );
  }

  Future<void> _export() async {
    final t = AppMessages.of(context);
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final document = await _client().exportAutoReplyPrompts();
      final encoded = const JsonEncoder.withIndent('  ').convert(document);
      await Clipboard.setData(ClipboardData(text: encoded));
      _toast(t.get('ar_prompt_export_copied'));
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      _toast(
        exc.error.message.isNotEmpty ? exc.error.message : t.get('state_error'),
      );
    } catch (exc) {
      if (!mounted) return;
      _toast(t.get('ar_prompt_export_failed', params: {'error': '$exc'}));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _import() async {
    final t = AppMessages.of(context);
    final controller = TextEditingController();
    final raw = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('ar_prompt_import')),
        content: SizedBox(
          width: 480,
          child: TextField(
            key: const ValueKey<String>('ar_prompt_import_json'),
            controller: controller,
            maxLines: 10,
            minLines: 6,
            decoration: InputDecoration(
              labelText: t.get('ar_prompt_import_label'),
              hintText: t.get('ar_prompt_import_hint'),
              border: const OutlineInputBorder(),
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(t.get('action_cancel')),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, controller.text),
            child: Text(t.get('ar_prompt_import_action')),
          ),
        ],
      ),
    );
    if (raw == null || raw.trim().isEmpty || !mounted) return;

    final prompts = _parseDocument(raw);
    if (prompts == null) {
      _toast(t.get('ar_prompt_json_invalid'));
      return;
    }
    if (_busy) return;
    final client = _client();
    setState(() => _busy = true);
    try {
      final result = await client.importAutoReplyPrompts(prompts);
      await _load();
      _toast(
        t.get(
          'ar_prompt_import_done',
          params: {
            'imported': '${result.imported}',
            'skipped': '${result.skipped}',
          },
        ),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      if (exc.error.kind == ErrorKind.unauthorized) {
        context.read<AppState>().markExpired();
        return;
      }
      _toast(
        exc.error.message.isNotEmpty ? exc.error.message : t.get('state_error'),
      );
    } catch (exc) {
      if (!mounted) return;
      _toast(t.get('ar_prompt_import_failed', params: {'error': '$exc'}));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// Accepts the export document (`{"prompts": [...]}`) or a bare array — the
  /// two shapes a user can reasonably paste. Anything else is null, which the
  /// caller reports as invalid JSON rather than importing nothing silently.
  List<AutoReplyPrompt>? _parseDocument(String raw) {
    try {
      final decoded = jsonDecode(raw);
      final list = decoded is Map ? decoded['prompts'] : decoded;
      if (list is! List) return null;
      return [
        for (final entry in list)
          if (entry is Map)
            AutoReplyPrompt.fromJson(entry.cast<String, dynamic>()),
      ];
    } on FormatException {
      return null;
    }
  }

  List<AutoReplyPrompt> get _visible {
    final needle = _search.trim().toLowerCase();
    return [
      for (final prompt in _prompts)
        if (needle.isEmpty ||
            prompt.name.toLowerCase().contains(needle) ||
            prompt.description.toLowerCase().contains(needle) ||
            prompt.category.toLowerCase().contains(needle) ||
            prompt.tags.any((tag) => tag.toLowerCase().contains(needle)))
          prompt,
    ];
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final visible = _visible;

    if (_loading && _prompts.isEmpty) {
      return const LoadingList(rows: 5, itemExtent: 96, showAvatar: false);
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
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  key: const ValueKey<String>('ar_prompt_import'),
                  onPressed: _busy ? null : _import,
                  icon: const Icon(Icons.file_download_outlined, size: 18),
                  label: Text(t.get('ar_prompt_import')),
                ),
              ),
              const SizedBox(width: AppSpacing.sm),
              Expanded(
                child: OutlinedButton.icon(
                  key: const ValueKey<String>('ar_prompt_export'),
                  onPressed: _busy || _prompts.isEmpty ? null : _export,
                  icon: const Icon(Icons.file_upload_outlined, size: 18),
                  label: Text(t.get('ar_prompt_export')),
                ),
              ),
            ],
          ),
          const SizedBox(height: AppSpacing.md),
          Row(
            children: [
              Expanded(
                child: TextField(
                  key: const ValueKey<String>('ar_prompt_search'),
                  decoration: InputDecoration(
                    prefixIcon: const Icon(Icons.search, size: 20),
                    hintText: t.get('ar_prompt_search_hint'),
                    isDense: true,
                    border: OutlineInputBorder(borderRadius: AppRadius.allMd),
                  ),
                  onChanged: (value) => setState(() => _search = value),
                ),
              ),
              const SizedBox(width: AppSpacing.sm),
              IconButton(
                tooltip: t.get('ar_prompt_include_archived'),
                key: const ValueKey<String>('ar_prompt_archived_toggle'),
                isSelected: _includeArchived,
                selectedIcon: const Icon(Icons.inventory_2),
                icon: const Icon(Icons.inventory_2_outlined),
                onPressed: () {
                  setState(() => _includeArchived = !_includeArchived);
                  _load();
                },
              ),
            ],
          ),
          const SizedBox(height: AppSpacing.md),
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(
              key: const ValueKey<String>('ar_prompt_new'),
              onPressed: _busy ? null : () => _openEditor(null),
              icon: const Icon(Icons.add, size: 18),
              label: Text(t.get('ar_prompt_new')),
            ),
          ),
          const SizedBox(height: AppSpacing.lg),
          if (visible.isEmpty)
            EmptyStateView(
              message: t.get(
                _search.trim().isEmpty
                    ? 'ar_prompts_empty'
                    : 'ar_prompts_no_match',
              ),
              icon: Icons.auto_awesome_outlined,
            )
          else
            for (final prompt in visible) _promptCard(t, prompt),
        ],
      ),
    );
  }

  Widget _promptCard(AppMessages t, AutoReplyPrompt prompt) {
    final palette = AppColors.of(context);
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: AppSpacing.md),
      child: AppCard(
        accent: prompt.isDefault ? palette.primary : null,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Text(
                    prompt.name,
                    style: theme.textTheme.titleSmall,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                if (prompt.isDefault)
                  Padding(
                    padding: const EdgeInsetsDirectional.only(
                      start: AppSpacing.sm,
                    ),
                    child: StatusPill(
                      label: t.get('ar_prompt_default_badge'),
                      color: palette.primary,
                      dense: true,
                    ),
                  ),
                if (prompt.isArchived)
                  Padding(
                    padding: const EdgeInsetsDirectional.only(
                      start: AppSpacing.xs,
                    ),
                    child: StatusPill(
                      label: t.get('ar_prompt_archived_badge'),
                      color: palette.neutral,
                      dense: true,
                    ),
                  ),
                PopupMenuButton<String>(
                  key: ValueKey<String>('ar_prompt_menu_${prompt.id}'),
                  tooltip: t.get('rules_more_actions'),
                  onSelected: (value) {
                    switch (value) {
                      case 'edit':
                        _openEditor(prompt);
                      case 'duplicate':
                        _duplicate(prompt);
                      case 'default':
                        _setDefault(prompt);
                      case 'archive':
                        _toggleArchived(prompt);
                      case 'delete':
                        _delete(prompt);
                    }
                  },
                  itemBuilder: (_) => [
                    PopupMenuItem(
                      value: 'edit',
                      child: Text(t.get('action_edit')),
                    ),
                    PopupMenuItem(
                      value: 'duplicate',
                      child: Text(t.get('ar_prompt_duplicate')),
                    ),
                    if (!prompt.isDefault)
                      PopupMenuItem(
                        value: 'default',
                        child: Text(t.get('ar_prompt_set_default')),
                      ),
                    PopupMenuItem(
                      value: 'archive',
                      child: Text(
                        t.get(
                          prompt.isArchived
                              ? 'ar_prompt_unarchive'
                              : 'ar_prompt_archive',
                        ),
                      ),
                    ),
                    PopupMenuItem(
                      value: 'delete',
                      child: Text(t.get('action_delete')),
                    ),
                  ],
                ),
              ],
            ),
            if (prompt.description.trim().isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 2),
                child: Text(
                  prompt.description,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: palette.textMuted, fontSize: 12),
                ),
              ),
            const SizedBox(height: AppSpacing.sm),
            Text(
              prompt.userPromptTemplate,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: palette.textMuted,
                fontSize: 12,
                fontFamily: 'monospace',
              ),
            ),
            const SizedBox(height: AppSpacing.sm),
            Wrap(
              spacing: AppSpacing.sm,
              runSpacing: AppSpacing.xs,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                if (prompt.category.trim().isNotEmpty)
                  StatusPill(
                    label: prompt.category,
                    color: palette.info,
                    dense: true,
                  ),
                for (final tag in prompt.tags)
                  StatusPill(label: tag, color: palette.neutral, dense: true),
                Text(
                  t.get(
                    'ar_prompt_usage',
                    params: {'n': '${prompt.usageCount}'},
                  ),
                  style: TextStyle(color: palette.textMuted, fontSize: 11),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
