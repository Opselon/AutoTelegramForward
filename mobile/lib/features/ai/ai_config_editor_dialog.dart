/// Add / edit dialog for AI rewrite configurations.
///
/// Uses the existing `POST/PUT /api/ai-configs` endpoints. A provider API key
/// typed here travels once, to the server, and is never stored on the device or
/// returned by the API — the field is write-only, and leaving it blank keeps the
/// key already stored. The "check" action runs the probe on the *server* so the
/// key is never sent anywhere from this app and the tenant learns whether the
/// endpoint it cannot otherwise see is actually reachable.
library;

import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';

class AiConfigEditorDialog extends StatefulWidget {
  const AiConfigEditorDialog({
    super.key,
    this.existing,
    this.providers = const [],
    this.client,
  });

  /// When non-null the dialog edits; otherwise it creates.
  final AiConfig? existing;

  /// The provider catalogue, from the core. Empty is a normal state (an older
  /// server, or the fetch failed) and falls back to a static list.
  final List<AiProviderInfo> providers;

  /// Used for the key/endpoint check. Null disables that button rather than
  /// failing when pressed.
  final ApiClient? client;

  @override
  State<AiConfigEditorDialog> createState() => _AiConfigEditorDialogState();
}

class _AiConfigEditorDialogState extends State<AiConfigEditorDialog> {
  late final TextEditingController _model;
  late final TextEditingController _language;
  late final TextEditingController _baseUrl;
  late final TextEditingController _apiKey;
  late final TextEditingController _prompt;
  late String _provider;
  late bool _isEnabled;

  bool _checking = false;
  KeyCheckResult? _check;
  String _checkError = '';

  /// Kept as a fallback only: the catalogue is the core's list.
  static const _fallbackProviders = ['openai', 'gemini', 'anthropic', 'custom'];

  @override
  void initState() {
    super.initState();
    final e = widget.existing;
    _provider = (e?.provider ?? '').trim().toLowerCase();
    _model = TextEditingController(text: e?.model ?? '');
    _language = TextEditingController(text: e?.targetLanguage ?? '');
    _baseUrl = TextEditingController(text: e?.baseUrl ?? '');
    // Never seeded from the stored config: the server does not send a key back.
    _apiKey = TextEditingController();
    _prompt = TextEditingController(text: e?.prompt ?? '');
    _isEnabled = e?.isEnabled ?? true;
  }

  @override
  void dispose() {
    _model.dispose();
    _language.dispose();
    _baseUrl.dispose();
    _apiKey.dispose();
    _prompt.dispose();
    super.dispose();
  }

  /// The provider the form would submit.
  ///
  /// The select shows the first catalogue entry when nothing was chosen, so an
  /// untouched form must send *that* — reading `_provider` alone would post an
  /// empty provider and the server would fall back to its own default, a
  /// different provider from the one on screen.
  String get _effectiveProvider {
    final chosen = _provider.trim().toLowerCase();
    final options = _options;
    if (chosen.isNotEmpty && options.any((o) => o.id.toLowerCase() == chosen)) {
      return chosen;
    }
    return options.isEmpty ? chosen : options.first.id.toLowerCase();
  }

  AiConfig _build() => AiConfig(
    id: widget.existing?.id ?? '',
    provider: _effectiveProvider,
    model: _model.text.trim(),
    targetLanguage: _language.text.trim(),
    prompt: _prompt.text,
    baseUrl: _baseUrl.text.trim(),
    apiKey: _apiKey.text.trim(),
    isEnabled: _isEnabled,
    version: widget.existing?.version ?? 0,
  );

  List<AiProviderInfo> get _options {
    if (widget.providers.isNotEmpty) return widget.providers;
    return _fallbackProviders
        .map((id) => AiProviderInfo(id: id, label: id))
        .toList();
  }

  AiProviderInfo? get _selected {
    for (final p in _options) {
      if (p.id.toLowerCase() == _provider.toLowerCase()) return p;
    }
    return null;
  }

  Future<void> _runCheck() async {
    final client = widget.client;
    if (client == null) {
      setState(
        () => _checkError = AppMessages.of(context).get('ai_check_failed'),
      );
      return;
    }
    setState(() {
      _checking = true;
      _checkError = '';
      _check = null;
    });
    try {
      final result = await client.checkProviderKey(
        aiConfigId: widget.existing?.id ?? '',
        provider: _effectiveProvider,
        model: _model.text.trim(),
        baseUrl: _baseUrl.text.trim(),
        apiKey: _apiKey.text.trim(),
      );
      if (!mounted) return;
      setState(() => _check = result);
    } on ApiException catch (exc) {
      // The refusal is the answer: show it instead of an empty result.
      if (!mounted) return;
      setState(() => _checkError = exc.error.message);
    } catch (exc) {
      if (!mounted) return;
      setState(() => _checkError = exc.toString().split('(').first.trim());
    } finally {
      if (mounted) setState(() => _checking = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final isEdit = widget.existing != null;
    final selected = _selected;
    final defaultUrl = selected?.defaultBaseUrl ?? '';
    final hints = selected?.models ?? const <String>[];

    return AlertDialog(
      title: Text(t.get(isEdit ? 'ai_form_edit' : 'ai_form_new')),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            DropdownButtonFormField<String>(
              key: const ValueKey('ai_provider_select'),
              decoration: InputDecoration(
                labelText: t.get('ai_field_provider'),
                border: const OutlineInputBorder(),
              ),
              initialValue:
                  _options.any(
                    (p) => p.id.toLowerCase() == _provider.toLowerCase(),
                  )
                  ? _provider.toLowerCase()
                  : _options.first.id,
              items: _options
                  .map(
                    (p) => DropdownMenuItem(
                      value: p.id.toLowerCase(),
                      child: Text(p.label),
                    ),
                  )
                  .toList(),
              onChanged: (v) {
                if (v != null) setState(() => _provider = v);
              },
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _model,
              decoration: InputDecoration(
                labelText: t.get('ai_field_model'),
                hintText: hints.isNotEmpty
                    ? hints.first
                    : t.get('ai_field_model_hint'),
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _baseUrl,
              decoration: InputDecoration(
                labelText: t.get('ai_field_endpoint'),
                hintText: defaultUrl.isNotEmpty
                    ? defaultUrl
                    : 'https://api.example.com/v1',
                border: const OutlineInputBorder(),
              ),
              onChanged: (_) {
                // The verdict belonged to the previous address.
                if (_check != null) setState(() => _check = null);
              },
            ),
            Padding(
              padding: const EdgeInsets.only(top: 4, bottom: 8),
              child: Text(
                defaultUrl.isNotEmpty
                    ? t.get(
                        'ai_field_endpoint_default',
                        params: {'url': defaultUrl},
                      )
                    : t.get('ai_field_endpoint_custom'),
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
            TextField(
              key: const ValueKey('ai_api_key_field'),
              controller: _apiKey,
              obscureText: true,
              decoration: InputDecoration(
                labelText: t.get('ai_field_api_key'),
                border: const OutlineInputBorder(),
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(
                t.get(
                  isEdit ? 'ai_field_api_key_keep' : 'ai_field_api_key_note',
                ),
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                OutlinedButton.icon(
                  key: const ValueKey('ai_check_button'),
                  onPressed: _checking ? null : _runCheck,
                  icon: _checking
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.monitor_heart_outlined, size: 18),
                  label: Text(
                    t.get(_checking ? 'ai_check_running' : 'ai_check_run'),
                  ),
                ),
              ],
            ),
            if (_checkError.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  _checkError,
                  style: const TextStyle(color: Colors.red, fontSize: 12),
                ),
              ),
            if (_check != null)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      _check!.healthy
                          ? t.get(
                              'ai_check_ok',
                              params: {
                                'ms': _check!.latencyMs.round().toString(),
                              },
                            )
                          : t.get(
                              _check!.reasonKey.isNotEmpty
                                  ? _check!.reasonKey
                                  : 'ai_check_failed',
                            ),
                      style: TextStyle(
                        color: _check!.healthy ? Colors.green : Colors.red,
                        fontSize: 12,
                      ),
                    ),
                    if (!_check!.healthy && _check!.detail.isNotEmpty)
                      Text(
                        _check!.detail,
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                  ],
                ),
              ),
            const SizedBox(height: 12),
            TextField(
              controller: _language,
              decoration: InputDecoration(
                labelText: t.get('ai_field_language'),
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _prompt,
              maxLines: 3,
              decoration: InputDecoration(
                labelText: t.get('ai_field_prompt'),
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            SwitchListTile(
              title: Text(t.get('ai_enabled')),
              value: _isEnabled,
              onChanged: (v) => setState(() => _isEnabled = v),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(t.get('ai_cancel')),
        ),
        ElevatedButton(
          onPressed: () => Navigator.pop(context, _build()),
          child: Text(t.get(isEdit ? 'ai_save' : 'ai_create')),
        ),
      ],
    );
  }
}
