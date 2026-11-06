/// The AI rewrite prompt library on mobile — the same `/api/prompts` surface
/// the dashboard's Prompt Editor and the Telegram bot's prompt manager use.
///
/// What this section gives a phone-only operator:
///
/// * the whole template list (shared system templates are marked, not hidden),
/// * create / edit (name, description, system prompt, user template) with the
///   same `{text}` placeholder rule the server enforces,
/// * delete with confirmation (system templates are refused server-side and
///   the button is hidden for them),
/// * a one-shot test that runs the selected template through the owner's
///   first enabled AI config — the same `TestPrompt` path the dashboard uses.
///
/// A disabled template still lists here: the wire contract does not carry the
/// engine's enable flag, and rendering a switch that cannot be saved would be
/// worse than not rendering it.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/prompt_models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_card.dart';
import '../../widgets/app_ui.dart';

class PromptTemplatesSection extends StatefulWidget {
  const PromptTemplatesSection({super.key});

  @override
  State<PromptTemplatesSection> createState() => _PromptTemplatesSectionState();
}

class _PromptTemplatesSectionState extends State<PromptTemplatesSection> {
  List<PromptTemplate> _templates = const [];
  bool _loading = false;
  String? _error;
  bool _bootstrapped = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_bootstrapped) return;
    _bootstrapped = true;
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
      final list = await client.listPromptTemplates();
      if (!mounted) return;
      setState(() => _templates = list);
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
      setState(
        () => _error =
            '${t.get('state_error')} '
            '(${exc.toString().split('(').first.trim()})',
      );
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _openEditor([PromptTemplate? existing]) async {
    final t = AppMessages.of(context);
    final result = await showDialog<PromptTemplate>(
      context: context,
      builder: (_) => _PromptEditorDialog(existing: existing),
    );
    if (result == null || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      if (existing == null) {
        await client.createPromptTemplate(result);
      } else {
        await client.updatePromptTemplate(existing.id, result);
      }
      await _load();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            t.get(existing == null ? 'prm_saved_created' : 'prm_saved_updated'),
          ),
        ),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.error.message)));
    } catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '${t.get('prm_save_failed')} '
            '(${exc.toString().split('(').first.trim()})',
          ),
        ),
      );
    }
  }

  Future<void> _delete(PromptTemplate template) async {
    final t = AppMessages.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('prm_delete_title')),
        content: Text(template.name),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(t.get('action_close')),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(t.get('ai_delete_action')),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    final client = context.read<AppState>().buildApiClient();
    try {
      await client.deletePromptTemplate(template.id);
      await _load();
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(t.get('prm_deleted'))));
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.error.message)));
    } catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '${t.get('prm_delete_failed')} '
            '(${exc.toString().split('(').first.trim()})',
          ),
        ),
      );
    }
  }

  Future<void> _test(PromptTemplate template) async {
    final t = AppMessages.of(context);
    final client = context.read<AppState>().buildApiClient();
    String? configId;
    try {
      final configs = await client.listAiConfigs();
      final enabled = configs.where((c) => c.isEnabled).toList();
      configId = enabled.isEmpty ? null : enabled.first.id;
    } catch (_) {
      // Fall through: the null config id below reports the actionable state.
      configId = null;
    }
    if (!mounted) return;
    if (configId == null) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(t.get('prm_test_no_config'))));
      return;
    }
    final controller = TextEditingController(
      text: t.get('prm_test_sample_default'),
    );
    final sample = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.get('prm_test_title')),
        content: TextField(
          controller: controller,
          maxLines: 4,
          decoration: InputDecoration(hintText: t.get('prm_test_sample_hint')),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(t.get('action_close')),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(ctx, controller.text.trim()),
            child: Text(t.get('prm_test_action')),
          ),
        ],
      ),
    );
    if (sample == null || sample.isEmpty || !mounted) return;

    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(t.get('prm_test_running'))));
    try {
      final result = await client.testPromptTemplate(
        promptId: template.id,
        aiConfigId: configId,
        sampleText: sample,
      );
      if (!mounted) return;
      await showDialog<void>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: Text(
            result.success
                ? t.get('prm_test_result_title')
                : t.get('prm_test_failed_title'),
          ),
          content: SingleChildScrollView(
            child: Text(
              result.success ? result.transformedText : result.errorMessage,
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: Text(t.get('action_close')),
            ),
          ],
        ),
      );
    } on ApiException catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.error.message)));
    } catch (exc) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '${t.get('prm_test_failed_title')} '
            '(${exc.toString().split('(').first.trim()})',
          ),
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final colors = AppColors.of(context);

    return AppCard(
      accent: colors.primary,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  t.get('prm_section_title'),
                  style: Theme.of(context).textTheme.titleMedium,
                ),
              ),
              IconButton(
                tooltip: t.get('prm_new'),
                icon: const Icon(Icons.add),
                onPressed: () => _openEditor(),
              ),
            ],
          ),
          const SizedBox(height: AppSpacing.sm),
          Text(
            t.get('prm_section_sub'),
            style: Theme.of(context).textTheme.bodySmall,
          ),
          const SizedBox(height: AppSpacing.md),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(bottom: AppSpacing.md),
              child: InlineBanner(
                message: _error!,
                onRetry: _load,
                retryLabel: t.get('action_retry'),
              ),
            ),
          if (_loading && _templates.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: AppSpacing.lg),
              child: Center(child: CircularProgressIndicator()),
            )
          else if (!_loading && _templates.isEmpty)
            Text(
              t.get('prm_empty'),
              style: Theme.of(context).textTheme.bodySmall,
            )
          else
            ..._templates.map(
              (tpl) => ListTile(
                contentPadding: EdgeInsets.zero,
                dense: true,
                leading: Icon(
                  tpl.isSystem ? Icons.public : Icons.edit_note_outlined,
                  color: colors.primary,
                ),
                title: Text(
                  tpl.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
                subtitle: Text(
                  tpl.isSystem
                      ? '${t.get('prm_system_tag')} · v${tpl.currentVersion}'
                      : 'v${tpl.currentVersion}',
                ),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    IconButton(
                      tooltip: t.get('prm_test_action'),
                      icon: const Icon(Icons.science_outlined),
                      onPressed: () => _test(tpl),
                    ),
                    if (!tpl.isSystem)
                      IconButton(
                        tooltip: t.get('action_delete'),
                        icon: Icon(Icons.delete_outline, color: colors.danger),
                        onPressed: () => _delete(tpl),
                      ),
                  ],
                ),
                onTap: () =>
                    tpl.isSystem ? _showSystemInfo(tpl) : _openEditor(tpl),
              ),
            ),
        ],
      ),
    );
  }

  void _showSystemInfo(PromptTemplate tpl) {
    final t = AppMessages.of(context);
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(tpl.name),
        content: SingleChildScrollView(child: Text(t.get('prm_system_info'))),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(t.get('action_close')),
          ),
        ],
      ),
    );
  }
}

class _PromptEditorDialog extends StatefulWidget {
  const _PromptEditorDialog({this.existing});

  final PromptTemplate? existing;

  @override
  State<_PromptEditorDialog> createState() => _PromptEditorDialogState();
}

class _PromptEditorDialogState extends State<_PromptEditorDialog> {
  late final TextEditingController _name;
  late final TextEditingController _description;
  late final TextEditingController _system;
  late final TextEditingController _user;
  String? _problem;

  @override
  void initState() {
    super.initState();
    final existing = widget.existing;
    _name = TextEditingController(text: existing?.name ?? '');
    _description = TextEditingController(text: existing?.description ?? '');
    _system = TextEditingController(text: existing?.systemPrompt ?? '');
    _user = TextEditingController(
      text: existing?.userPromptTemplate ?? '{text}',
    );
  }

  @override
  void dispose() {
    _name.dispose();
    _description.dispose();
    _system.dispose();
    _user.dispose();
    super.dispose();
  }

  String? _validate() {
    final t = AppMessages.of(context);
    if (_name.text.trim().isEmpty || _name.text.trim().length > 80) {
      return t.get('prm_invalid_name');
    }
    if (_system.text.trim().isEmpty) {
      return t.get('prm_invalid_system');
    }
    if (!_user.text.contains('{text}')) {
      return t.get('prm_invalid_user');
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    return AlertDialog(
      title: Text(
        widget.existing == null ? t.get('prm_new') : t.get('prm_edit'),
      ),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              TextField(
                controller: _name,
                decoration: InputDecoration(labelText: t.get('prm_field_name')),
              ),
              const SizedBox(height: AppSpacing.md),
              TextField(
                controller: _description,
                decoration: InputDecoration(
                  labelText: t.get('prm_field_description'),
                ),
              ),
              const SizedBox(height: AppSpacing.md),
              TextField(
                controller: _system,
                maxLines: 5,
                decoration: InputDecoration(
                  labelText: t.get('prm_field_system'),
                  alignLabelWithHint: true,
                ),
              ),
              const SizedBox(height: AppSpacing.md),
              TextField(
                controller: _user,
                maxLines: 3,
                decoration: InputDecoration(
                  labelText: t.get('prm_field_user'),
                  alignLabelWithHint: true,
                ),
              ),
              if (_problem != null) ...[
                const SizedBox(height: AppSpacing.md),
                Text(
                  _problem!,
                  style: TextStyle(color: AppColors.of(context).danger),
                ),
              ],
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(t.get('action_close')),
        ),
        ElevatedButton(
          onPressed: () {
            final problem = _validate();
            if (problem != null) {
              setState(() => _problem = problem);
              return;
            }
            Navigator.pop(
              context,
              (widget.existing ?? const PromptTemplate()).copyWith(
                name: _name.text.trim(),
                description: _description.text.trim(),
                systemPrompt: _system.text.trim(),
                userPromptTemplate: _user.text.trim(),
              ),
            );
          },
          child: Text(t.get('action_save')),
        ),
      ],
    );
  }
}
