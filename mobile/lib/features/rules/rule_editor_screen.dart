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

  late TextEditingController _sourceIdCtrl;
  late TextEditingController _sourceTitleCtrl;
  late TextEditingController _targetIdCtrl;
  late TextEditingController _targetTitleCtrl;
  late TextEditingController _intermediateCtrl;
  late TextEditingController _filtersCtrl;
  late TextEditingController _delayCtrl;
  late TextEditingController _headerCtrl;
  late TextEditingController _footerCtrl;

  String? _selectedSessionId;
  String _filterMode = 'all';
  String _mediaMode = 'all';
  bool _useVipRouting = false;
  bool _isEnabled = true;
  bool _blockVoice = false;
  bool _blockStickers = false;
  bool _removeEmojis = false;
  bool _syncDeletes = false;
  bool _ignoreEdits = false;
  String _albumMode = 'album';

  List<TelegramSession> _sessions = const [];
  bool _saving = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final r = widget.rule;
    _sourceIdCtrl = TextEditingController(text: r?.sourceChatId ?? '');
    _sourceTitleCtrl = TextEditingController(text: r?.sourceChatTitle ?? '');
    _targetIdCtrl = TextEditingController(text: r?.targetChatId ?? '');
    _targetTitleCtrl = TextEditingController(text: r?.targetChatTitle ?? '');
    _intermediateCtrl = TextEditingController(
      text: r?.intermediateChannelId ?? '',
    );
    _filtersCtrl = TextEditingController(text: r?.filters.join(', ') ?? '');
    _delayCtrl = TextEditingController(text: '${r?.delaySeconds ?? 0}');
    _headerCtrl = TextEditingController(text: r?.headerTemplate ?? '');
    _footerCtrl = TextEditingController(text: r?.footerTemplate ?? '');

    _selectedSessionId = r?.sessionId;
    _filterMode = r?.filterMode ?? 'all';
    _mediaMode = r?.mediaMode ?? 'all';
    _useVipRouting =
        r?.useVipRouting ?? (r?.intermediateChannelId.isNotEmpty ?? false);
    _isEnabled = r?.isEnabled ?? true;
    _blockVoice = r?.blockVoice ?? false;
    _blockStickers = r?.blockStickers ?? false;
    _removeEmojis = r?.removeEmojis ?? false;
    _syncDeletes = r?.syncDeletes ?? false;
    _ignoreEdits = r?.ignoreEdits ?? false;
    _albumMode = r?.albumMode ?? 'album';

    _loadSessions();
  }

  @override
  void dispose() {
    _sourceIdCtrl.dispose();
    _sourceTitleCtrl.dispose();
    _targetIdCtrl.dispose();
    _targetTitleCtrl.dispose();
    _intermediateCtrl.dispose();
    _filtersCtrl.dispose();
    _delayCtrl.dispose();
    _headerCtrl.dispose();
    _footerCtrl.dispose();
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
