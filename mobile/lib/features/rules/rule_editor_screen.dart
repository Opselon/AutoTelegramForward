/// Forwarding rule editor (create / edit).
///
/// Features:
///   * Session picker (populated from real `/api/sessions`).
///   * Source & Destination channel IDs and titles.
///   * Intermediate VIP channel routing.
///   * Filters, header/footer templates, delay.
///   * Optimistic concurrency (submits `expected_version` on edit,
///     handles 409 Conflict).
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../l10n/app_messages.dart';

class RuleEditorScreen extends StatefulWidget {
  const RuleEditorScreen({super.key, this.rule});

  final ForwardRule? rule;

  @override
  State<RuleEditorScreen> createState() => _RuleEditorScreenState();
}

class _RuleEditorScreenState extends State<RuleEditorScreen> {
  final _formKey = GlobalKey<FormState>();

  late TextEditingController _nameCtrl;
  late TextEditingController _topicCtrl;
  late TextEditingController _sourceIdCtrl;
  late TextEditingController _sourceTitleCtrl;
  late TextEditingController _targetIdCtrl;
  late TextEditingController _targetTitleCtrl;
  late TextEditingController _intermediateCtrl;
  late TextEditingController _filtersCtrl;
  late TextEditingController _delayCtrl;
  late TextEditingController _editTimeCtrl;
  late TextEditingController _headerCtrl;
  late TextEditingController _footerCtrl;
  late TextEditingController _watermarkCtrl;

  String? _selectedSessionId;
  String _filterMode = 'all';
  String _mediaMode = 'all';
  bool _useVipRouting = false;
  bool _isEnabled = true;
  bool _debugMode = false;
  String _senderType = 'Personal Telegram Account';
  bool _duplicateFilters = false;
  bool _cryptoMode = false;
  bool _autoPosting = false;
  bool _powerControl = false;
  bool _processRawText = false;
  bool _buttonToText = false;
  bool _ocrImageAi = false;
  bool _smartAiRewrite = false;
  bool _newsDigest = false;
  Map<String, bool> _paidAddons = {};
  bool _blockVoice = false;
  bool _blockStickers = false;
  bool _removeEmojis = false;
  bool _syncDeletes = false;
  bool _ignoreEdits = false;
  String _albumMode = 'album';

  /// Per-task text replace rules (find -> replace), edited in order.
  List<MapEntry<String, String>> _replacements = [];

  /// Tenant's AI configs for the per-task AI picker.
  List<AiConfig> _aiConfigs = const [];
  String _selectedAiConfigId = '';

  List<TelegramSession> _sessions = const [];
  bool _saving = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final r = widget.rule;
    _nameCtrl = TextEditingController(text: r?.name ?? '');
    _topicCtrl = TextEditingController(
      text: r?.topicId != null && r!.topicId > 0 ? '${r.topicId}' : '',
    );
    _sourceIdCtrl = TextEditingController(text: r?.sourceChatId ?? '');
    _sourceTitleCtrl = TextEditingController(text: r?.sourceChatTitle ?? '');
    _targetIdCtrl = TextEditingController(text: r?.targetChatId ?? '');
    _targetTitleCtrl = TextEditingController(text: r?.targetChatTitle ?? '');
    _intermediateCtrl = TextEditingController(
      text: r?.intermediateChannelId ?? '',
    );
    _filtersCtrl = TextEditingController(text: r?.filters.join(', ') ?? '');
    _delayCtrl = TextEditingController(text: '${r?.delaySeconds ?? 0}');
    _editTimeCtrl = TextEditingController(
      text: '${r?.editTimeLimitSeconds ?? 0}',
    );
    _headerCtrl = TextEditingController(text: r?.headerTemplate ?? '');
    _footerCtrl = TextEditingController(text: r?.footerTemplate ?? '');
    _watermarkCtrl = TextEditingController(text: r?.watermark ?? '');

    _selectedSessionId = r?.sessionId;
    _filterMode = r?.filterMode ?? 'all';
    _mediaMode = r?.mediaMode ?? 'all';
    _useVipRouting =
        r?.useVipRouting ?? (r?.intermediateChannelId.isNotEmpty ?? false);
    _isEnabled = r?.isEnabled ?? true;
    _debugMode = r?.debugMode ?? false;
    _senderType = r?.senderType ?? 'Personal Telegram Account';
    _duplicateFilters = r?.duplicateFilters ?? false;
    _cryptoMode = r?.cryptoMode ?? false;
    _autoPosting = r?.autoPosting ?? false;
    _powerControl = r?.powerControl ?? false;
    _processRawText = r?.processRawText ?? false;
    _buttonToText = r?.buttonToText ?? false;
    _ocrImageAi = r?.ocrImageAi ?? false;
    _smartAiRewrite = r?.smartAiRewrite ?? false;
    _newsDigest = r?.newsDigest ?? false;
    _paidAddons = Map.of(r?.paidAddons ?? {});
    _replacements = List.of(
      r?.replacements.entries.toList() ?? const <MapEntry<String, String>>[],
    );
    _selectedAiConfigId = r?.aiConfigId ?? '';
    _blockVoice = r?.blockVoice ?? false;
    _blockStickers = r?.blockStickers ?? false;
    _removeEmojis = r?.removeEmojis ?? false;
    _syncDeletes = r?.syncDeletes ?? false;
    _ignoreEdits = r?.ignoreEdits ?? false;
    _albumMode = r?.albumMode ?? 'album';

    _loadSessions();
    _loadAiConfigs();
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    _topicCtrl.dispose();
    _sourceIdCtrl.dispose();
    _sourceTitleCtrl.dispose();
    _targetIdCtrl.dispose();
    _targetTitleCtrl.dispose();
    _intermediateCtrl.dispose();
    _filtersCtrl.dispose();
    _delayCtrl.dispose();
    _editTimeCtrl.dispose();
    _headerCtrl.dispose();
    _footerCtrl.dispose();
    _watermarkCtrl.dispose();
    super.dispose();
  }

  Future<void> _loadSessions() async {
    final client = context.read<AppState>().buildApiClient();
    try {
      final list = await client.listSessions();
      if (!mounted) return;
      setState(() {
        _sessions = list;
        if (_selectedSessionId == null && list.isNotEmpty) {
          _selectedSessionId = list.first.id;
        }
      });
    } catch (_) {
      // Sessions loading failure handled gracefully
    }
  }

  Future<void> _loadAiConfigs() async {
    final client = context.read<AppState>().buildApiClient();
    try {
      final list = await client.listAiConfigs();
      if (!mounted) return;
      setState(() => _aiConfigs = list);
    } catch (_) {
      // AI picker is optional: leave the list empty on failure.
    }
  }

  String _aiConfigLabel(String id) {
    for (final c in _aiConfigs) {
      if (c.id == id) {
        return c.model.isNotEmpty ? '${c.provider} · ${c.model}' : c.provider;
      }
    }
    return id;
  }

  Future<void> _addReplacementDialog(AppMessages t) async {
    final findCtrl = TextEditingController();
    final replaceCtrl = TextEditingController();
    final formKey = GlobalKey<FormState>();
    final ok = await showDialog<bool>(
      context: context,
      builder:
          (ctx) => AlertDialog(
            title: Text(t.get('rules_replace_add')),
            content: Form(
              key: formKey,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextFormField(
                    controller: findCtrl,
                    autofocus: true,
                    decoration: InputDecoration(
                      labelText: t.get('rules_replace_find'),
                    ),
                    validator:
                        (v) =>
                            (v == null || v.trim().isEmpty)
                                ? t.get('rules_replace_find_required')
                                : null,
                  ),
                  const SizedBox(height: 8),
                  TextFormField(
                    controller: replaceCtrl,
                    decoration: InputDecoration(
                      labelText: t.get('rules_replace_with'),
                      helperText: t.get('rules_replace_with_hint'),
                    ),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx, false),
                child: Text(t.get('action_cancel')),
              ),
              FilledButton(
                onPressed: () {
                  if (formKey.currentState!.validate()) {
                    Navigator.pop(ctx, true);
                  }
                },
                child: Text(t.get('action_save')),
              ),
            ],
          ),
    );
    if (ok == true) {
      setState(() {
        _replacements = List.of(_replacements)
          ..add(MapEntry(findCtrl.text.trim(), replaceCtrl.text));
      });
    }
  }

  Future<void> _save() async {
    final t = AppMessages.of(context);
    if (!_formKey.currentState!.validate()) return;

    final sid = _selectedSessionId ?? '';
    if (sid.isEmpty && _sessions.isNotEmpty) {
      setState(() => _error = t.get('rules_session_required'));
      return;
    }

    setState(() {
      _saving = true;
      _error = null;
    });

    final client = context.read<AppState>().buildApiClient();
    final filters =
        _filtersCtrl.text
            .split(',')
            .map((s) => s.trim())
            .where((s) => s.isNotEmpty)
            .toList();

    final ruleData = ForwardRule(
      id: widget.rule?.id ?? '',
      sessionId: sid,
      sourceChatId: _sourceIdCtrl.text.trim(),
      sourceChatTitle:
          _sourceTitleCtrl.text.trim().isNotEmpty
              ? _sourceTitleCtrl.text.trim()
              : _sourceIdCtrl.text.trim(),
      targetChatId: _targetIdCtrl.text.trim(),
      targetChatTitle:
          _targetTitleCtrl.text.trim().isNotEmpty
              ? _targetTitleCtrl.text.trim()
              : _targetIdCtrl.text.trim(),
      intermediateChannelId:
          _useVipRouting ? _intermediateCtrl.text.trim() : '',
      useVipRouting: _useVipRouting,
      filters: filters,
      filterMode: _filterMode,
      mediaMode: _mediaMode,
      delaySeconds: int.tryParse(_delayCtrl.text.trim()) ?? 0,
      headerTemplate: _headerCtrl.text,
      footerTemplate: _footerCtrl.text,
      name: _nameCtrl.text.trim(),
      topicId: int.tryParse(_topicCtrl.text.trim()) ?? 0,
      debugMode: _debugMode,
      senderType: _senderType,
      duplicateFilters: _duplicateFilters,
      cryptoMode: _cryptoMode,
      autoPosting: _autoPosting,
      powerControl: _powerControl,
      editTimeLimitSeconds: int.tryParse(_editTimeCtrl.text.trim()) ?? 0,
      processRawText: _processRawText,
      buttonToText: _buttonToText,
      ocrImageAi: _ocrImageAi,
      smartAiRewrite: _smartAiRewrite,
      newsDigest: _newsDigest,
      watermark: _watermarkCtrl.text.trim(),
      replacements: Map.fromEntries(_replacements),
      aiConfigId: _selectedAiConfigId,
      paidAddons: _paidAddons,
      blockVoice: _blockVoice,
      blockStickers: _blockStickers,
      removeEmojis: _removeEmojis,
      syncDeletes: _syncDeletes,
      ignoreEdits: _ignoreEdits,
      albumMode: _albumMode,
      version: widget.rule?.version ?? 0,
      isEnabled: _isEnabled,
    );

    try {
      if (widget.rule == null) {
        await client.createRule(ruleData);
      } else {
        await client.updateRule(widget.rule!.id, ruleData);
      }
      if (!mounted) return;
      Navigator.pop(context, true);
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(() {
        if (exc.error.kind == ErrorKind.conflict) {
          _error = t.get('rules_concurrent_edit');
        } else if (exc.error.kind == ErrorKind.validation) {
          _error = t.get('rules_validation_failed');
        } else {
          _error =
              exc.error.message.isNotEmpty
                  ? exc.error.message
                  : t.get('state_error');
        }
      });
    } catch (_) {
      if (!mounted) return;
      setState(() => _error = t.get('state_error'));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final isEditing = widget.rule != null;

    return Scaffold(
      appBar: AppBar(
        title: Text(isEditing ? t.get('action_edit') : t.get('rules_create')),
        actions: [
          IconButton(
            icon: const Icon(Icons.check),
            tooltip: t.get('action_save'),
            onPressed: _saving ? null : _save,
          ),
        ],
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Form(
          key: _formKey,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (_error != null)
                Container(
                  padding: const EdgeInsets.all(12),
                  margin: const EdgeInsets.only(bottom: 16),
                  decoration: BoxDecoration(
                    color: Colors.red.withValues(alpha: 0.15),
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: Colors.red),
                  ),
                  child: Text(
                    _error!,
                    style: const TextStyle(color: Colors.red),
                  ),
                ),

              // Session Selector
              if (_sessions.isNotEmpty)
                DropdownButtonFormField<String>(
                  value: _selectedSessionId,
                  decoration: InputDecoration(
                    labelText: t.get('rules_session'),
                    prefixIcon: const Icon(Icons.send_rounded),
                  ),
                  items:
                      _sessions.map((s) {
                        return DropdownMenuItem(
                          value: s.id,
                          child: Text(s.displayName),
                        );
                      }).toList(),
                  onChanged: (val) => setState(() => _selectedSessionId = val),
                ),
              const SizedBox(height: 16),

              // Task Name & Topic ID
              Row(
                children: [
                  Expanded(
                    flex: 3,
                    child: TextFormField(
                      controller: _nameCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Task Name',
                        hintText: 'af_route_lab_b',
                        prefixIcon: Icon(Icons.label_outline),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    flex: 2,
                    child: TextFormField(
                      controller: _topicCtrl,
                      keyboardType: TextInputType.number,
                      decoration: const InputDecoration(
                        labelText: 'Topic #',
                        hintText: '0 (disabled)',
                        prefixIcon: Icon(Icons.tag),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),

              // Sender Type
              DropdownButtonFormField<String>(
                value: _senderType,
                decoration: const InputDecoration(
                  labelText: 'Sender Type',
                  prefixIcon: Icon(Icons.person_outline),
                ),
                items: const [
                  DropdownMenuItem(
                    value: 'Personal Telegram Account',
                    child: Text('Personal Telegram Account'),
                  ),
                  DropdownMenuItem(
                    value: 'Bot Account',
                    child: Text('Bot Account'),
                  ),
                  DropdownMenuItem(
                    value: 'MTProto Premium Userbot',
                    child: Text('MTProto Premium Userbot'),
                  ),
                ],
                onChanged:
                    (val) => setState(
                      () => _senderType = val ?? 'Personal Telegram Account',
                    ),
              ),
              const SizedBox(height: 12),

              // Status & Debug Mode
              Row(
                children: [
                  Expanded(
                    child: SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      title: Text(t.get('rules_sec_status')),
                      subtitle: Text(_isEnabled ? 'RUNNING' : 'PAUSED'),
                      value: _isEnabled,
                      onChanged: (v) => setState(() => _isEnabled = v),
                    ),
                  ),
                  Expanded(
                    child: SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      title: Text(t.get('rules_sec_debug')),
                      subtitle: Text(_debugMode ? 'ON' : 'OFF'),
                      value: _debugMode,
                      onChanged: (v) => setState(() => _debugMode = v),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 16),

              // Source Chat
              Row(
                children: [
                  Expanded(
                    flex: 2,
                    child: TextFormField(
                      controller: _sourceIdCtrl,
                      decoration: InputDecoration(
                        labelText: t.get('rules_source'),
                        hintText: '-1001234567890',
                      ),
                      validator:
                          (v) =>
                              (v == null || v.trim().isEmpty)
                                  ? t.get('rules_source_required')
                                  : null,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    flex: 3,
                    child: TextFormField(
                      controller: _sourceTitleCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Title / Channel name',
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 16),

              // Target Chat
              Row(
                children: [
                  Expanded(
                    flex: 2,
                    child: TextFormField(
                      controller: _targetIdCtrl,
                      decoration: InputDecoration(
                        labelText: t.get('rules_target'),
                        hintText: '-1009876543210',
                      ),
                      validator:
                          (v) =>
                              (v == null || v.trim().isEmpty)
                                  ? t.get('rules_target_required')
                                  : null,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    flex: 3,
                    child: TextFormField(
                      controller: _targetTitleCtrl,
                      decoration: const InputDecoration(
                        labelText: 'Title / Channel name',
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 16),

              // Intermediate VIP Channel
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(t.get('rules_intermediate')),
                value: _useVipRouting,
                onChanged: (val) => setState(() => _useVipRouting = val),
              ),
              if (_useVipRouting)
                TextFormField(
                  controller: _intermediateCtrl,
                  decoration: const InputDecoration(
                    labelText: 'VIP Intermediate Channel ID',
                    hintText: '-1000000000000',
                  ),
                ),
              const SizedBox(height: 16),

              // Filters & Delay
              TextFormField(
                controller: _filtersCtrl,
                decoration: InputDecoration(
                  labelText: t.get('rules_filters'),
                  hintText: 'word1, word2, badlink',
                ),
              ),
              const SizedBox(height: 16),

              TextFormField(
                controller: _delayCtrl,
                keyboardType: TextInputType.number,
                decoration: InputDecoration(
                  labelText: t.get('rules_delay'),
                  hintText: '0',
                ),
              ),
              const SizedBox(height: 16),

              // Templates
              TextFormField(
                controller: _headerCtrl,
                maxLines: 2,
                decoration: InputDecoration(
                  labelText: t.get('rules_header'),
                  hintText: 'Header text or {source_title}',
                ),
              ),
              const SizedBox(height: 16),

              TextFormField(
                controller: _footerCtrl,
                maxLines: 2,
                decoration: InputDecoration(
                  labelText: t.get('rules_footer'),
                  hintText: 'Footer text',
                ),
              ),
              const SizedBox(height: 16),

              // Media & Sync Settings (Bot / Web parity)
              Card(
                margin: EdgeInsets.zero,
                elevation: 0,
                color: Theme.of(
                  context,
                ).colorScheme.surfaceContainerHighest.withValues(alpha: 0.3),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        t.get('rules_media_toggles_title'),
                        style: const TextStyle(
                          fontWeight: FontWeight.bold,
                          fontSize: 13,
                        ),
                      ),
                      const SizedBox(height: 8),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_block_voice')),
                        secondary: const Text(
                          '🎙',
                          style: TextStyle(fontSize: 18),
                        ),
                        value: _blockVoice,
                        onChanged: (v) => setState(() => _blockVoice = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_block_stickers')),
                        secondary: const Text(
                          '🎭',
                          style: TextStyle(fontSize: 18),
                        ),
                        value: _blockStickers,
                        onChanged: (v) => setState(() => _blockStickers = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_remove_emojis')),
                        secondary: const Text(
                          '😀',
                          style: TextStyle(fontSize: 18),
                        ),
                        value: _removeEmojis,
                        onChanged: (v) => setState(() => _removeEmojis = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_sync_deletes')),
                        secondary: const Text(
                          '🗑',
                          style: TextStyle(fontSize: 18),
                        ),
                        value: _syncDeletes,
                        onChanged: (v) => setState(() => _syncDeletes = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_ignore_edits')),
                        secondary: const Text(
                          '✏️',
                          style: TextStyle(fontSize: 18),
                        ),
                        value: _ignoreEdits,
                        onChanged: (v) => setState(() => _ignoreEdits = v),
                      ),
                      const SizedBox(height: 8),
                      DropdownButtonFormField<String>(
                        value: _albumMode,
                        decoration: InputDecoration(
                          labelText: t.get('rules_album_mode'),
                          prefixIcon: const Icon(Icons.photo_library_outlined),
                        ),
                        items: [
                          DropdownMenuItem(
                            value: 'album',
                            child: Text(t.get('rules_album_full')),
                          ),
                          DropdownMenuItem(
                            value: 'first',
                            child: Text(t.get('rules_album_first')),
                          ),
                          DropdownMenuItem(
                            value: 'split',
                            child: Text(t.get('rules_album_split')),
                          ),
                        ],
                        onChanged: (v) {
                          if (v != null) setState(() => _albumMode = v);
                        },
                      ),
                    ],
                  ),
                ),
              ),
              // Advanced Settings Parity Sections (Screens 2, 3, 4)
              Card(
                margin: const EdgeInsets.only(top: 16),
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        '🎚 Advanced Settings Categories',
                        style: TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                      const SizedBox(height: 12),

                      // Filters Category
                      ExpansionTile(
                        leading: const Icon(
                          Icons.shield_outlined,
                          color: Colors.blue,
                        ),
                        title: Text(t.get('rules_sec_filters')),
                        subtitle: Text(
                          'Duplicate: ${_duplicateFilters ? "ON" : "OFF"} • Crypto: ${_cryptoMode ? "ON" : "OFF"}',
                        ),
                        children: [
                          SwitchListTile(
                            title: Text(t.get('rules_filters_dup')),
                            subtitle: const Text(
                              'Media & text deep fingerprint deduplication',
                            ),
                            value: _duplicateFilters,
                            onChanged:
                                (v) => setState(() => _duplicateFilters = v),
                          ),
                          SwitchListTile(
                            title: Text(t.get('rules_filters_crypto')),
                            subtitle: const Text(
                              'Signal detection, wallet cleaner, pair parsing',
                            ),
                            value: _cryptoMode,
                            onChanged: (v) => setState(() => _cryptoMode = v),
                          ),
                        ],
                      ),

                      // Schedules Category
                      ExpansionTile(
                        leading: const Icon(
                          Icons.schedule_rounded,
                          color: Colors.amber,
                        ),
                        title: Text(t.get('rules_sec_schedules')),
                        subtitle: Text(
                          'Auto Posting: ${_autoPosting ? "ON" : "OFF"} • Power: ${_powerControl ? "ON" : "OFF"}',
                        ),
                        children: [
                          SwitchListTile(
                            title: Text(t.get('rules_sched_auto')),
                            value: _autoPosting,
                            onChanged: (v) => setState(() => _autoPosting = v),
                          ),
                          SwitchListTile(
                            title: Text(t.get('rules_sched_power')),
                            value: _powerControl,
                            onChanged: (v) => setState(() => _powerControl = v),
                          ),
                        ],
                      ),

                      // Time Forward Category
                      ExpansionTile(
                        leading: const Icon(
                          Icons.timer_outlined,
                          color: Colors.purple,
                        ),
                        title: Text(t.get('rules_sec_time_forward')),
                        subtitle: Text('Delay & Edit Time limits'),
                        children: [
                          Padding(
                            padding: const EdgeInsets.all(8),
                            child: Row(
                              children: [
                                Expanded(
                                  child: TextFormField(
                                    controller: _delayCtrl,
                                    keyboardType: TextInputType.number,
                                    decoration: const InputDecoration(
                                      labelText: 'Set Delay (seconds)',
                                    ),
                                  ),
                                ),
                                const SizedBox(width: 12),
                                Expanded(
                                  child: TextFormField(
                                    controller: _editTimeCtrl,
                                    keyboardType: TextInputType.number,
                                    decoration: const InputDecoration(
                                      labelText: 'Max Edit Time (seconds)',
                                    ),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),

                      // Processing Messages Category
                      ExpansionTile(
                        leading: const Icon(
                          Icons.settings_suggest_outlined,
                          color: Colors.teal,
                        ),
                        title: Text(t.get('rules_sec_processing')),
                        subtitle: Text(
                          'Raw Text: ${_processRawText ? "ON" : "OFF"} • Btn2Txt: ${_buttonToText ? "ON" : "OFF"}',
                        ),
                        children: [
                          SwitchListTile(
                            title: Text(t.get('rules_proc_raw')),
                            value: _processRawText,
                            onChanged:
                                (v) => setState(() => _processRawText = v),
                          ),
                          SwitchListTile(
                            title: Text(t.get('rules_proc_btn2txt')),
                            value: _buttonToText,
                            onChanged: (v) => setState(() => _buttonToText = v),
                          ),
                        ],
                      ),

                      // Modify Content Category
                      ExpansionTile(
                        leading: const Icon(
                          Icons.edit_note_rounded,
                          color: Colors.orange,
                        ),
                        title: Text(t.get('rules_sec_modify')),
                        subtitle: Text(t.get('rules_sec_modify_sub')),
                        children: [
                          Padding(
                            padding: const EdgeInsets.all(8),
                            child: Column(
                              children: [
                                TextFormField(
                                  controller: _headerCtrl,
                                  decoration: const InputDecoration(
                                    labelText: 'Header Text',
                                    hintText: 'HEADER EMPTY',
                                  ),
                                ),
                                const SizedBox(height: 8),
                                TextFormField(
                                  controller: _footerCtrl,
                                  decoration: const InputDecoration(
                                    labelText: 'Footer Text',
                                    hintText: 'FOOTER EMPTY',
                                  ),
                                ),
                                const SizedBox(height: 8),
                                TextFormField(
                                  controller: _watermarkCtrl,
                                  decoration: const InputDecoration(
                                    labelText: 'Watermark Text',
                                    hintText: 'Empty',
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),

                      // Replace Rules Category
                      ExpansionTile(
                        leading: const Icon(
                          Icons.find_replace_rounded,
                          color: Colors.cyan,
                        ),
                        title: Text(t.get('rules_replace_title')),
                        subtitle: Text(
                          t.get(
                            'rules_replace_summary',
                            params: {'count': '${_replacements.length}'},
                          ),
                        ),
                        children: [
                          if (_replacements.isEmpty)
                            Padding(
                              padding: const EdgeInsets.all(12),
                              child: Text(
                                t.get('rules_replace_empty'),
                                style: Theme.of(context).textTheme.bodySmall,
                              ),
                            ),
                          for (var i = 0; i < _replacements.length; i++)
                            ListTile(
                              dense: true,
                              leading: const Icon(Icons.swap_horiz, size: 18),
                              title: Text(
                                _replacements[i].key,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                              subtitle: Text(
                                _replacements[i].value.isEmpty
                                    ? t.get('rules_replace_remove')
                                    : _replacements[i].value,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                              trailing: Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  if (i > 0)
                                    IconButton(
                                      icon: const Icon(
                                        Icons.arrow_upward,
                                        size: 18,
                                      ),
                                      tooltip: t.get('rules_replace_move_up'),
                                      onPressed:
                                          () => setState(() {
                                            final tmp = _replacements[i - 1];
                                            _replacements[i - 1] =
                                                _replacements[i];
                                            _replacements[i] = tmp;
                                          }),
                                    ),
                                  IconButton(
                                    icon: const Icon(
                                      Icons.delete_outline,
                                      size: 18,
                                    ),
                                    tooltip: t.get('action_delete'),
                                    onPressed:
                                        () => setState(
                                          () => _replacements.removeAt(i),
                                        ),
                                  ),
                                ],
                              ),
                            ),
                          Padding(
                            padding: const EdgeInsets.all(8),
                            child: OutlinedButton.icon(
                              icon: const Icon(Icons.add, size: 18),
                              label: Text(t.get('rules_replace_add')),
                              onPressed: () => _addReplacementDialog(t),
                            ),
                          ),
                        ],
                      ),

                      // AI Selection (per-task AI config binding)
                      ExpansionTile(
                        leading: const Icon(
                          Icons.auto_awesome_rounded,
                          color: Colors.deepPurple,
                        ),
                        title: Text(t.get('rules_ai_pick_title')),
                        subtitle: Text(
                          _selectedAiConfigId.isEmpty
                              ? t.get('rules_ai_pick_none')
                              : _aiConfigLabel(_selectedAiConfigId),
                        ),
                        children: [
                          RadioListTile<String>(
                            dense: true,
                            value: '',
                            groupValue: _selectedAiConfigId,
                            onChanged:
                                (v) => setState(() => _selectedAiConfigId = ''),
                            title: Text(t.get('rules_ai_pick_none')),
                          ),
                          ..._aiConfigs.map(
                            (c) => RadioListTile<String>(
                              dense: true,
                              value: c.id,
                              groupValue: _selectedAiConfigId,
                              onChanged:
                                  (v) => setState(
                                    () => _selectedAiConfigId = v ?? '',
                                  ),
                              title: Text(_aiConfigLabel(c.id)),
                              subtitle: Text(
                                '${c.provider}${c.model.isNotEmpty ? ' · ${c.model}' : ''}',
                              ),
                            ),
                          ),
                          if (_aiConfigs.isEmpty)
                            Padding(
                              padding: const EdgeInsets.all(12),
                              child: Text(
                                t.get('rules_ai_pick_empty'),
                                style: Theme.of(context).textTheme.bodySmall,
                              ),
                            ),
                        ],
                      ),

                      // AI Features Category
                      ExpansionTile(
                        leading: const Icon(
                          Icons.auto_awesome_rounded,
                          color: Colors.indigo,
                        ),
                        title: Text(t.get('rules_sec_ai')),
                        subtitle: Text(t.get('rules_sec_ai_sub')),
                        children: [
                          SwitchListTile(
                            title: Text(t.get('rules_ai_ocr')),
                            subtitle: const Text(
                              'Optical character recognition via vision AI',
                            ),
                            value: _ocrImageAi,
                            onChanged: (v) => setState(() => _ocrImageAi = v),
                          ),
                          SwitchListTile(
                            title: Text(t.get('rules_ai_rewrite')),
                            subtitle: const Text(
                              'LLM intelligent content rephrasing',
                            ),
                            value: _smartAiRewrite,
                            onChanged:
                                (v) => setState(() => _smartAiRewrite = v),
                          ),
                          SwitchListTile(
                            title: Text(t.get('rules_ai_digest')),
                            subtitle: const Text(
                              'Automated news summary bullets',
                            ),
                            value: _newsDigest,
                            onChanged: (v) => setState(() => _newsDigest = v),
                          ),
                        ],
                      ),

                      // PAID ADDON Catalogue (Screen 4)
                      ExpansionTile(
                        leading: const Icon(
                          Icons.workspace_premium_rounded,
                          color: Colors.amber,
                        ),
                        title: Text(t.get('rules_sec_paid')),
                        subtitle: Text(t.get('rules_sec_paid_sub')),
                        children: [
                          ...[
                            'channel_comments',
                            'every_n',
                            'kw_confirm',
                            'kw_reply',
                            'auto_del',
                            'webhook',
                            'crypto',
                            'affiliate',
                            'link_buttons',
                            'watermark',
                          ].map((key) {
                            final val = _paidAddons[key] ?? false;
                            return SwitchListTile(
                              dense: true,
                              title: Text(
                                key.replaceAll('_', ' ').toUpperCase(),
                              ),
                              value: val,
                              onChanged:
                                  (v) => setState(() => _paidAddons[key] = v),
                            );
                          }),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 24),

              ElevatedButton(
                onPressed: _saving ? null : _save,
                child:
                    _saving
                        ? const SizedBox(
                          width: 20,
                          height: 20,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Colors.white,
                          ),
                        )
                        : Text(t.get('action_save')),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
