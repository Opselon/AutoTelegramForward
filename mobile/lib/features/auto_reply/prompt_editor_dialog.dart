/// Create / edit one prompt.
///
/// The template must contain `{text}` — that placeholder is where the inbound
/// message is substituted, and a prompt without it would send the model a
/// question it can never answer. The check is local so the user learns it before
/// a round trip, and the server enforces the same rule.
library;

import 'package:flutter/material.dart';

import '../../core/auto_reply_models.dart';
import '../../core/design_tokens.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_field.dart';
import 'auto_reply_widgets.dart';

class PromptEditorDialog extends StatefulWidget {
  const PromptEditorDialog({super.key, this.existing});

  final AutoReplyPrompt? existing;

  @override
  State<PromptEditorDialog> createState() => _PromptEditorDialogState();
}

class _PromptEditorDialogState extends State<PromptEditorDialog> {
  late final TextEditingController _name;
  late final TextEditingController _description;
  late final TextEditingController _system;
  late final TextEditingController _template;
  late final TextEditingController _tags;
  late final TextEditingController _category;

  late String _language;
  late String _tone;
  late String _length;
  bool _showErrors = false;

  @override
  void initState() {
    super.initState();
    final p = widget.existing;
    _name = TextEditingController(text: p?.name ?? '');
    _description = TextEditingController(text: p?.description ?? '');
    _system = TextEditingController(text: p?.systemPrompt ?? '');
    _template = TextEditingController(text: p?.userPromptTemplate ?? '');
    _tags = TextEditingController(text: (p?.tags ?? const []).join(', '));
    _category = TextEditingController(text: p?.category ?? '');
    _language = p?.language ?? '';
    _tone = p?.tone ?? '';
    _length = p?.responseLength ?? '';
  }

  @override
  void dispose() {
    _name.dispose();
    _description.dispose();
    _system.dispose();
    _template.dispose();
    _tags.dispose();
    _category.dispose();
    super.dispose();
  }

  bool get _nameMissing => _name.text.trim().isEmpty;

  bool get _templateInvalid => !_template.text.contains('{text}');

  void _submit() {
    setState(() => _showErrors = true);
    if (_nameMissing || _templateInvalid) return;
    final tags = _tags.text
        .split(',')
        .map((e) => e.trim())
        .where((e) => e.isNotEmpty)
        .toList(growable: false);
    final existing = widget.existing;
    final result = AutoReplyPrompt(
      id: existing?.id ?? '',
      name: _name.text.trim(),
      description: _description.text.trim(),
      systemPrompt: _system.text.trim(),
      userPromptTemplate: _template.text,
      language: _language,
      tone: _tone,
      responseLength: _length,
      tags: tags,
      category: _category.text.trim(),
      isDefault: existing?.isDefault ?? false,
      isArchived: existing?.isArchived ?? false,
      usageCount: existing?.usageCount ?? 0,
      createdAt: existing?.createdAt ?? 0,
      updatedAt: existing?.updatedAt ?? 0,
    );
    Navigator.of(context).pop(result);
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final isEdit = widget.existing != null;
    return AlertDialog(
      title: Text(t.get(isEdit ? 'ar_prompt_edit' : 'ar_prompt_new')),
      content: SizedBox(
        width: 520,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              AppField(
                key: const ValueKey<String>('ar_prompt_name'),
                label: t.get('ar_prompt_name'),
                controller: _name,
                errorText: _showErrors && _nameMissing
                    ? t.get('ar_prompt_required_name')
                    : null,
                textCapitalization: TextCapitalization.sentences,
              ),
              const SizedBox(height: AppSpacing.md),
              AppField(
                label: t.get('ar_prompt_description'),
                controller: _description,
                maxLines: 2,
                minLines: 1,
                textCapitalization: TextCapitalization.sentences,
              ),
              const SizedBox(height: AppSpacing.md),
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
                label: t.get('ar_prompt_system'),
                controller: _system,
                maxLines: 5,
                minLines: 3,
                helper: t.get('ar_prompt_system_hint'),
                textCapitalization: TextCapitalization.sentences,
              ),
              const SizedBox(height: AppSpacing.md),
              AppField(
                key: const ValueKey<String>('ar_prompt_template'),
                label: t.get('ar_prompt_template'),
                controller: _template,
                maxLines: 6,
                minLines: 3,
                helper: t.get('ar_prompt_template_hint'),
                errorText: _showErrors && _templateInvalid
                    ? t.get('ar_prompt_template_required')
                    : null,
                textCapitalization: TextCapitalization.sentences,
              ),
              const SizedBox(height: AppSpacing.md),
              AppField(
                label: t.get('ar_prompt_category'),
                controller: _category,
                textCapitalization: TextCapitalization.sentences,
              ),
              const SizedBox(height: AppSpacing.md),
              AppField(
                label: t.get('ar_prompt_tags'),
                controller: _tags,
                helper: t.get('ar_prompt_tags_hint'),
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
          key: const ValueKey<String>('ar_prompt_save'),
          onPressed: _submit,
          child: Text(t.get(isEdit ? 'action_save' : 'action_confirm')),
        ),
      ],
    );
  }
}
