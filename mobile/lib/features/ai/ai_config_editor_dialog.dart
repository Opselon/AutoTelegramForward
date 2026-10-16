/// Add / edit dialog for AI rewrite configurations.
///
/// Uses the existing `POST/PUT /api/ai-configs` endpoints. The provider API
/// key is intentionally NOT part of this form: keys are managed through the
/// bot/web flows (see AiConfig.toJson docs) and the mobile client never holds
/// or transmits them.
library;

import 'package:flutter/material.dart';

import '../../core/models.dart';

class AiConfigEditorDialog extends StatefulWidget {
  const AiConfigEditorDialog({super.key, this.existing});

  /// When non-null the dialog edits; otherwise it creates.
  final AiConfig? existing;

  @override
  State<AiConfigEditorDialog> createState() => _AiConfigEditorDialogState();
}

class _AiConfigEditorDialogState extends State<AiConfigEditorDialog> {
  late final TextEditingController _name;
  late final TextEditingController _model;
  late final TextEditingController _language;
  late final TextEditingController _baseUrl;
  late final TextEditingController _prompt;
  late bool _isEnabled;

  static const _providers = ['openai', 'gemini', 'anthropic', 'custom'];

  @override
  void initState() {
    super.initState();
    final e = widget.existing;
    _name = TextEditingController(text: e?.provider ?? '');
    _model = TextEditingController(text: e?.model ?? '');
    _language = TextEditingController(text: e?.targetLanguage ?? '');
    _baseUrl = TextEditingController(text: e?.baseUrl ?? '');
    _prompt = TextEditingController(text: e?.prompt ?? '');
    _isEnabled = e?.isEnabled ?? true;
  }

  @override
  void dispose() {
    _name.dispose();
    _model.dispose();
    _language.dispose();
    _baseUrl.dispose();
    _prompt.dispose();
    super.dispose();
  }

  AiConfig _build() {
    final provider = _name.text.trim().isEmpty
        ? 'openai'
        : _name.text.trim().toLowerCase();
    return AiConfig(
      id: widget.existing?.id ?? '',
      provider: provider,
      model: _model.text.trim(),
      targetLanguage: _language.text.trim(),
      prompt: _prompt.text,
      baseUrl: _baseUrl.text.trim(),
      isEnabled: _isEnabled,
      version: widget.existing?.version ?? 0,
    );
  }

  @override
  Widget build(BuildContext context) {
    final isEdit = widget.existing != null;

    return AlertDialog(
      title: Text(isEdit ? 'Edit AI config' : 'New AI config'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            DropdownButtonFormField<String>(
              decoration: const InputDecoration(
                labelText: 'Provider',
                border: OutlineInputBorder(),
              ),
              initialValue: _providers.contains(_name.text.trim().toLowerCase())
                  ? _name.text.trim().toLowerCase()
                  : 'openai',
              items: _providers
                  .map((p) => DropdownMenuItem(value: p, child: Text(p)))
                  .toList(),
              onChanged: (v) {
                if (v != null) _name.text = v;
              },
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _model,
              decoration: const InputDecoration(
                labelText: 'Model (e.g. gpt-4o-mini)',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _language,
              decoration: const InputDecoration(
                labelText: 'Target language (empty = keep original)',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _baseUrl,
              decoration: const InputDecoration(
                labelText: 'Base URL (optional, OpenAI-compatible)',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _prompt,
              maxLines: 3,
              decoration: const InputDecoration(
                labelText: 'System prompt (optional)',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            SwitchListTile(
              title: const Text('Enabled'),
              value: _isEnabled,
              onChanged: (v) => setState(() => _isEnabled = v),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Cancel'),
        ),
        ElevatedButton(
          onPressed: () => Navigator.pop(context, _build()),
          child: Text(isEdit ? 'Save' : 'Create'),
        ),
      ],
    );
  }
}
