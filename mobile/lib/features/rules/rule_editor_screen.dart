/// Forwarding rule editor (create / edit).
///
/// Features:
///   * Session picker (populated from real `/api/sessions`).
///   * Source & target LISTS (M sources -> N targets, so 1->1, N->1, 1->N and
///     M->N are all the same form) with per-entry verification chips and a
///     route preview.
///   * Intermediate VIP channel routing.
///   * Filters, header/footer templates, delay.
///   * Optimistic concurrency (submits `expected_version` on edit,
///     handles 409 Conflict).
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'rule_settings_sheet.dart';
import 'rule_simulator_sheet.dart';

import '../../core/api_client.dart';
import '../../core/layout.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/route_input.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_ui.dart';
import '../../widgets/chat_id_list_field.dart';
import '../chats/chat_picker_sheet.dart';

/// One-line, plain-language description of the effective branding hop for a
/// task: which route the message takes and which messages earn it.
///
/// Reads the per-task trigger policy (`metadata["branding_hop"]`, parsed into
/// [BrandingHopConfig]) and nothing else, so what the operator reads here is
/// the same blob the engine's `should_hop` decision consumes. A rule with no
/// metadata, or with metadata the app cannot read, renders the unconditional
/// case — the hop never disappears from the UI because of a malformed value.
String brandingHopSummary(AppMessages t, BrandingHopConfig config) {
  if (!config.hasTriggers) return t.get('rules_intermediate_summary_all');

  final parts = <String>[];
  if (config.originTitles.isNotEmpty) {
    parts.add(
      t.get(
        'rules_intermediate_cond_origins',
        params: {'names': config.originTitles.join(', ')},
      ),
    );
  }
  if (config.originIds.isNotEmpty) {
    parts.add(
      t.get(
        'rules_intermediate_cond_ids',
        params: {'count': '${config.originIds.length}'},
      ),
    );
  }
  if (config.keywords.isNotEmpty) {
    parts.add(
      t.get(
        'rules_intermediate_cond_keywords',
        params: {'count': '${config.keywords.length}'},
      ),
    );
  }
  if (config.requireForward) {
    parts.add(t.get('rules_intermediate_cond_forward_only'));
  }

  final conditions = parts.join(' · ');
  // Only worth naming the match mode when more than one condition is listed:
  // with a single condition `any` and `all` decide the same thing.
  final mode = t.get(
    config.matchMode == 'all'
        ? 'rules_intermediate_match_all'
        : 'rules_intermediate_match_any',
  );
  final body = parts.length > 1 ? '$mode · $conditions' : conditions;
  return t.get(
    'rules_intermediate_summary_triggers',
    params: {'conditions': body},
  );
}

class RuleEditorScreen extends StatefulWidget {
  const RuleEditorScreen({
    super.key,
    this.rule,
    this.initialSessionId,
    this.prefillSourceIds = const [],
    this.prefillTargetIds = const [],
  });

  final ForwardRule? rule;

  /// Selected when the editor opens for a NEW task (the Chats page knows which
  /// account's chat list the user was looking at). An existing rule's own
  /// session always wins.
  final String? initialSessionId;

  /// Endpoints merged in from a chat pick: the chat the user tapped is added to
  /// the side they chose while everything already stored stays put. They land in
  /// the same text field the user types into, so there is exactly one source of
  /// truth and the normal save path validates the result.
  final List<String> prefillSourceIds;
  final List<String> prefillTargetIds;

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
  bool _blockVideo = false;
  bool _blockAudio = false;
  bool _blockText = false;
  bool _removeEmojis = false;
  bool _syncDeletes = false;
  bool _ignoreEdits = false;
  String _albumMode = 'album';

  /// Per-task text replace rules (find -> replace), edited in order.
  List<MapEntry<String, String>> _replacements = [];

  /// Focus targets for the two endpoint fields, so a rejected chip can send
  /// the caret straight back into the entry it belongs to.
  final _sourceFocus = FocusNode();
  final _targetFocus = FocusNode();

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
    _sourceIdCtrl = TextEditingController(
      text: _seedRoute(r?.sourceIds ?? const [], widget.prefillSourceIds),
    );
    _sourceTitleCtrl = TextEditingController(text: r?.sourceChatTitle ?? '');
    _targetIdCtrl = TextEditingController(
      text: _seedRoute(r?.targetIds ?? const [], widget.prefillTargetIds),
    );
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

    _selectedSessionId = r?.sessionId ?? widget.initialSessionId;
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
    _blockVideo = r?.blockVideo ?? false;
    _blockAudio = r?.blockAudio ?? false;
    _blockText = r?.blockText ?? false;
    _removeEmojis = r?.removeEmojis ?? false;
    _syncDeletes = r?.syncDeletes ?? false;
    _ignoreEdits = r?.ignoreEdits ?? false;
    _albumMode = r?.albumMode ?? 'album';

    // The chips and the route preview are derived from the two endpoint fields,
    // so they have to be recomputed as the user types.
    _sourceIdCtrl.addListener(_onRouteTextChanged);
    _targetIdCtrl.addListener(_onRouteTextChanged);

    _loadSessions();
    _loadAiConfigs();
  }

  void _onRouteTextChanged() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _sourceIdCtrl.removeListener(_onRouteTextChanged);
    _targetIdCtrl.removeListener(_onRouteTextChanged);
    _sourceFocus.dispose();
    _targetFocus.dispose();
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
      builder: (ctx) => AlertDialog(
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
                validator: (v) => (v == null || v.trim().isEmpty)
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

  /// Drops one entry from an endpoint field, leaving the other entries and
  /// their order untouched — order matters because the first entry is the
  /// primary source/target the server mirrors into the legacy field.
  void _removeEntry(TextEditingController controller, String value) {
    final entries = splitChatIdList(controller.text).toList();
    final index = entries.indexOf(value);
    if (index < 0) return;
    entries.removeAt(index);
    controller.value = TextEditingValue(
      text: entries.join(', '),
      selection: TextSelection.collapsed(offset: entries.join(', ').length),
    );
  }

  /// Stored endpoints plus anything picked from the chat list, in that order.
  ///
  /// Deduplication uses [chatIdKey] — the same comparison the route validator
  /// applies — so a chat that is already a source cannot be added twice by
  /// picking it, and `@Channel` will not sneak in beside `t.me/channel`.
  static String _seedRoute(List<String> stored, List<String> picked) {
    final out = <String>[...stored];
    final seen = stored.map(chatIdKey).toSet();
    for (final entry in picked) {
      final value = entry.trim();
      if (value.isEmpty || !seen.add(chatIdKey(value))) continue;
      out.add(value);
    }
    return out.join(', ');
  }

  /// Opens the chat-list picker for one side of the route and writes the
  /// confirmed selection back into that side's field.
  ///
  /// The picker opens with the current entries ticked, so its answer is the
  /// complete list for that side — de-selecting a chat really removes it. An
  /// entry typed by hand is carried in as a ticked entry (keyed by [chatIdKey])
  /// and comes back unchanged, so opening the picker never silently drops
  /// something the user pasted.
  ///
  /// Without a session there is no chat list to read, so the button is hidden
  /// and the field stays the manual surface it has always been.
  Future<void> _pickChats({required bool asSource}) async {
    final sessionId = _selectedSessionId ?? '';
    if (sessionId.isEmpty) return;
    final t = AppMessages.of(context);
    final controller = asSource ? _sourceIdCtrl : _targetIdCtrl;
    final picked = await showChatPickerSheet(
      context,
      title: t.get(asSource ? 'rules_pick_sources' : 'rules_pick_targets'),
      sessionId: sessionId,
      alreadySelected: splitChatIdList(controller.text).toSet(),
    );
    if (picked == null || !mounted) return;
    final merged = _seedRoute(const [], picked);
    controller.value = TextEditingValue(
      text: merged,
      selection: TextSelection.collapsed(offset: merged.length),
    );
  }

  /// Selects one entry inside its field so the user can correct just that id.
  void _focusEntry(
    TextEditingController controller,
    FocusNode focusNode,
    String value,
  ) {
    final index = controller.text.indexOf(value);
    if (index < 0) return;
    focusNode.requestFocus();
    controller.selection = TextSelection(
      baseOffset: index,
      extentOffset: index + value.length,
    );
  }

  /// Localised text for a rejected route. The duplicate/overlap messages carry
  /// the offending entry, so the user is told *which* channel is wrong.
  String _issueText(AppMessages t, RouteIssue issue) =>
      t.get(issue.messageKey, params: {'id': issue.id});

  Future<void> _save() async {
    final t = AppMessages.of(context);
    if (!_formKey.currentState!.validate()) return;

    final sources = parseChatIdList(_sourceIdCtrl.text);
    final targets = parseChatIdList(_targetIdCtrl.text);

    // Client-side route validation. The server re-checks, but a duplicate or a
    // self-loop should never cost a round-trip: the inline error names the
    // offending id and the save never happens.
    final issue = validateRoute(
      sourcesText: _sourceIdCtrl.text,
      targetsText: _targetIdCtrl.text,
    );
    if (issue != null || sources.isEmpty || targets.isEmpty) {
      setState(() {});
      return;
    }

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
    final filters = _filtersCtrl.text
        .split(',')
        .map((s) => s.trim())
        .where((s) => s.isNotEmpty)
        .toList();

    final sourceTitle = _sourceTitleCtrl.text.trim();
    final targetTitle = _targetTitleCtrl.text.trim();

    final ruleData = ForwardRule(
      id: widget.rule?.id ?? '',
      sessionId: sid,
      // Both lists are sent, and each legacy singular field carries the first
      // entry so a server predating multi-source routing still works.
      sourceChatId: sources.first,
      sourceChatIds: sources,
      sourceChatTitle: sourceTitle.isNotEmpty ? sourceTitle : sources.first,
      targetChatId: targets.first,
      targetChatIds: targets,
      targetChatTitle: targetTitle.isNotEmpty ? targetTitle : targets.first,
      intermediateChannelId: _useVipRouting
          ? _intermediateCtrl.text.trim()
          : '',
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
      blockVideo: _blockVideo,
      blockAudio: _blockAudio,
      blockText: _blockText,
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
          _error = exc.error.message.isNotEmpty
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
    final theme = Theme.of(context);
    final isEditing = widget.rule != null;

    // Everything the route section renders is derived from the two endpoint
    // fields on every build, so the chips, the preview and the inline error can
    // never drift from what a save would send.
    final sources = parseChatIdList(_sourceIdCtrl.text);
    final targets = parseChatIdList(_targetIdCtrl.text);
    final sourceEntries = verifyRouteEntries(
      _sourceIdCtrl.text,
      peerRaw: _targetIdCtrl.text,
    );
    final targetEntries = verifyRouteEntries(
      _targetIdCtrl.text,
      peerRaw: _sourceIdCtrl.text,
    );
    // Only the structural rules (duplicate / self-loop) are surfaced while the
    // user is still typing; emptiness is reported by the field validators and
    // blocks the save.
    final routeIssue = validateRoute(
      sourcesText: _sourceIdCtrl.text,
      targetsText: _targetIdCtrl.text,
      requireSources: false,
      requireTargets: false,
    );
    final routePreview = t.get(
      'rules_route_preview',
      params: {'sources': sources.join(', '), 'targets': targets.join(', ')},
    );

    return Scaffold(
      appBar: AppBar(
        title: Text(isEditing ? t.get('action_edit') : t.get('rules_create')),
        actions: [
          // The dry run is only meaningful once the server has stored the
          // settings, so it appears for an existing task and not on a form the
          // user has not saved yet.
          if (isEditing)
            IconButton(
              icon: const Icon(Icons.science_outlined),
              tooltip: t.get('rules_simulate_title'),
              onPressed: _saving
                  ? null
                  : () => showRuleSimulatorSheet(
                      context,
                      ruleId: widget.rule!.id,
                      ruleLabel: widget.rule!.name.isNotEmpty
                          ? widget.rule!.name
                          : widget.rule!.shortId,
                      initialChatId: parseChatIdList(_sourceIdCtrl.text).isEmpty
                          ? ''
                          : parseChatIdList(_sourceIdCtrl.text).first,
                    ),
            ),
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

              // The rule id is the per-task identity: an operator quoting a task
              // in a ticket or in the log console reads it back from here.
              if (isEditing)
                Padding(
                  padding: const EdgeInsets.only(bottom: AppSpacing.lg),
                  child: Row(
                    children: [
                      Icon(
                        Icons.fingerprint,
                        size: 16,
                        color: theme.colorScheme.primary,
                      ),
                      const SizedBox(width: AppSpacing.sm),
                      Text(
                        t.get('rules_task_id'),
                        style: theme.textTheme.bodySmall,
                      ),
                      const SizedBox(width: AppSpacing.sm),
                      Text(
                        '#${widget.rule!.shortId}',
                        style: theme.textTheme.bodyMedium?.copyWith(
                          fontFamily: 'monospace',
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ],
                  ),
                ),

              // Session Selector
              if (_sessions.isNotEmpty)
                DropdownButtonFormField<String>(
                  isExpanded: true,
                  initialValue: _selectedSessionId,
                  decoration: InputDecoration(
                    labelText: t.get('rules_session'),
                    prefixIcon: const Icon(Icons.send_rounded),
                  ),
                  items: _sessions.map((s) {
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
                      decoration: InputDecoration(
                        labelText: t.get('rules_task_name'),
                        hintText: 'af_route_lab_b',
                        prefixIcon: const Icon(Icons.label_outline),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    flex: 2,
                    child: TextFormField(
                      controller: _topicCtrl,
                      keyboardType: TextInputType.number,
                      decoration: InputDecoration(
                        labelText: t.get('rules_topic_label'),
                        hintText: t.get('rules_topic_hint'),
                        prefixIcon: const Icon(Icons.tag),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 12),

              // Sender Type
              DropdownButtonFormField<String>(
                initialValue: _senderType,
                decoration: InputDecoration(
                  labelText: t.get('rules_sender_type'),
                  prefixIcon: const Icon(Icons.person_outline),
                ),
                items: [
                  DropdownMenuItem(
                    value: 'Personal Telegram Account',
                    child: Text(t.get('rules_sender_account')),
                  ),
                  DropdownMenuItem(
                    value: 'Bot Account',
                    child: Text(t.get('rules_sender_bot')),
                  ),
                  DropdownMenuItem(
                    value: 'MTProto Premium Userbot',
                    child: Text(t.get('rules_sender_userbot')),
                  ),
                ],
                onChanged: (val) => setState(
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
                      subtitle: Text(
                        _isEnabled
                            ? t.get('rules_state_running')
                            : t.get('rules_state_paused'),
                      ),
                      value: _isEnabled,
                      onChanged: (v) => setState(() => _isEnabled = v),
                    ),
                  ),
                  Expanded(
                    child: SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      title: Text(t.get('rules_sec_debug')),
                      subtitle: Text(
                        _debugMode
                            ? t.get('rules_state_on')
                            : t.get('rules_state_off'),
                      ),
                      value: _debugMode,
                      onChanged: (v) => setState(() => _debugMode = v),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 16),

              // Sources: one entry, or a comma-separated list. The chips are the
              // verification result for exactly what was typed.
              ChatIdListField(
                fieldKey: const ValueKey<String>('rule_sources_field'),
                focusNode: _sourceFocus,
                controller: _sourceIdCtrl,
                label: t.get('rules_route_sources'),
                helper: t.get('rules_route_sources_hint'),
                emptyLabel: t.get('rules_route_empty'),
                entries: sourceEntries,
                onRemove: (value) => _removeEntry(_sourceIdCtrl, value),
                onFix: (value) =>
                    _focusEntry(_sourceIdCtrl, _sourceFocus, value),
                validator: (v) => parseChatIdList(v ?? '').isEmpty
                    ? t.get('rules_source_required')
                    : null,
              ),
              // The chat list is the faster way in, but only once an account is
              // selected: without one there is nothing to list, so the button is
              // absent rather than disabled-looking.
              if ((_selectedSessionId ?? '').isNotEmpty)
                Align(
                  alignment: AlignmentDirectional.centerStart,
                  child: TextButton.icon(
                    key: const ValueKey<String>('rule_pick_sources'),
                    onPressed: () => _pickChats(asSource: true),
                    icon: const Icon(Icons.forum_outlined, size: 18),
                    label: Text(t.get('rules_pick_from_chats')),
                  ),
                ),
              const SizedBox(height: AppSpacing.md),

              // Titles stay the primary endpoint's label: the fan-out list is
              // identified by ids, and the server keeps one title per side.
              TextFormField(
                controller: _sourceTitleCtrl,
                decoration: InputDecoration(
                  labelText: t.get('rules_chat_title'),
                ),
              ),
              const SizedBox(height: 16),

              // Targets: every entry here receives a copy of every forwarded
              // message (1 target = N:1, several = M:N).
              ChatIdListField(
                fieldKey: const ValueKey<String>('rule_targets_field'),
                focusNode: _targetFocus,
                controller: _targetIdCtrl,
                label: t.get('rules_route_targets'),
                helper: t.get('rules_route_targets_hint'),
                emptyLabel: t.get('rules_route_empty'),
                entries: targetEntries,
                icon: Icons.forward_to_inbox_rounded,
                onRemove: (value) => _removeEntry(_targetIdCtrl, value),
                onFix: (value) =>
                    _focusEntry(_targetIdCtrl, _targetFocus, value),
                validator: (v) => parseChatIdList(v ?? '').isEmpty
                    ? t.get('rules_target_required')
                    : null,
              ),
              if ((_selectedSessionId ?? '').isNotEmpty)
                Align(
                  alignment: AlignmentDirectional.centerStart,
                  child: TextButton.icon(
                    key: const ValueKey<String>('rule_pick_targets'),
                    onPressed: () => _pickChats(asSource: false),
                    icon: const Icon(Icons.forum_outlined, size: 18),
                    label: Text(t.get('rules_pick_from_chats')),
                  ),
                ),
              const SizedBox(height: AppSpacing.md),

              TextFormField(
                controller: _targetTitleCtrl,
                decoration: InputDecoration(
                  labelText: t.get('rules_chat_title'),
                ),
              ),

              // What is about to be saved, in one line.
              if (sources.isNotEmpty && targets.isNotEmpty)
                Container(
                  margin: const EdgeInsets.only(top: AppSpacing.lg),
                  padding: const EdgeInsets.all(AppSpacing.md),
                  decoration: BoxDecoration(
                    color: theme.colorScheme.surfaceContainerHighest.withValues(
                      alpha: 0.35,
                    ),
                    borderRadius: AppRadii.allMd,
                    border: Border.all(color: theme.dividerColor),
                  ),
                  child: Row(
                    children: [
                      Icon(
                        Icons.alt_route_rounded,
                        size: 16,
                        color: theme.colorScheme.primary,
                      ),
                      const SizedBox(width: AppSpacing.sm),
                      Text(
                        t.get('rules_route_preview_label'),
                        style: theme.textTheme.bodySmall,
                      ),
                      const SizedBox(width: AppSpacing.sm),
                      Expanded(
                        child: Text(
                          routePreview,
                          key: const ValueKey<String>('rule_route_preview'),
                          textAlign: TextAlign.end,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: theme.textTheme.bodyMedium?.copyWith(
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),

              // Rejected route: names the offending entry and blocks the save.
              if (routeIssue != null)
                Padding(
                  padding: const EdgeInsets.only(top: AppSpacing.md),
                  child: InlineBanner(
                    message: _issueText(t, routeIssue),
                    severity: 'error',
                  ),
                ),
              const SizedBox(height: 16),

              // Intermediate VIP Channel — the A → C → B branding hop. The
              // title names the hop so the section cannot be mistaken for a
              // plain relay, and the summary below the C field states the
              // effective route the engine will take for this task.
              Text(
                t.get('rules_intermediate_title'),
                style: const TextStyle(
                  fontSize: 15,
                  fontWeight: FontWeight.bold,
                ),
              ),
              Text(
                t.get('rules_intermediate_hint'),
                style: const TextStyle(fontSize: 11, color: Colors.grey),
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(t.get('rules_intermediate')),
                value: _useVipRouting,
                onChanged: (val) => setState(() => _useVipRouting = val),
              ),
              if (_useVipRouting)
                TextFormField(
                  key: const ValueKey<String>('rule_intermediate_field'),
                  controller: _intermediateCtrl,
                  decoration: InputDecoration(
                    labelText: t.get('rules_intermediate_id'),
                    hintText: '-1000000000000',
                  ),
                ),
              if (_useVipRouting)
                Padding(
                  padding: const EdgeInsets.only(top: AppSpacing.sm),
                  child: Row(
                    children: [
                      Icon(
                        Icons.alt_route_rounded,
                        size: 16,
                        color: theme.colorScheme.primary,
                      ),
                      const SizedBox(width: AppSpacing.sm),
                      Expanded(
                        child: Text(
                          // Absent metadata renders the unconditional route;
                          // a malformed blob degrades the same way instead of
                          // throwing inside a build.
                          brandingHopSummary(
                            t,
                            widget.rule?.brandingHop ??
                                const BrandingHopConfig(),
                          ),
                          key: const ValueKey<String>(
                            'rule_branding_hop_summary',
                          ),
                          maxLines: 3,
                          overflow: TextOverflow.ellipsis,
                          style: theme.textTheme.bodySmall,
                        ),
                      ),
                    ],
                  ),
                ),
              const SizedBox(height: 16),

              // Filters & Delay
              TextFormField(
                controller: _filtersCtrl,
                decoration: InputDecoration(
                  labelText: t.get('rules_filters'),
                  hintText: t.get('rules_filters_hint'),
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
                  hintText: t.get('rules_header_hint'),
                ),
              ),
              const SizedBox(height: 16),

              TextFormField(
                controller: _footerCtrl,
                maxLines: 2,
                decoration: InputDecoration(
                  labelText: t.get('rules_footer'),
                  hintText: t.get('rules_footer_hint'),
                ),
              ),
              const SizedBox(height: 16),

              // Media & Sync Settings (Bot / Web parity)
              Card(
                margin: EdgeInsets.zero,
                elevation: 0,
                color: Theme.of(context).colorScheme.surfaceContainerHighest
                    .withValues(alpha: 0.3),
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
                        secondary: const Icon(Icons.mic_none_rounded, size: 20),
                        value: _blockVoice,
                        onChanged: (v) => setState(() => _blockVoice = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_block_stickers')),
                        secondary: const Icon(
                          Icons.sticky_note_2_outlined,
                          size: 20,
                        ),
                        value: _blockStickers,
                        onChanged: (v) => setState(() => _blockStickers = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_block_video')),
                        secondary: const Icon(
                          Icons.videocam_off_outlined,
                          size: 20,
                        ),
                        value: _blockVideo,
                        onChanged: (v) => setState(() => _blockVideo = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_block_audio')),
                        secondary: const Icon(
                          Icons.audiotrack_outlined,
                          size: 20,
                        ),
                        value: _blockAudio,
                        onChanged: (v) => setState(() => _blockAudio = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_block_text')),
                        secondary: const Icon(Icons.notes_outlined, size: 20),
                        value: _blockText,
                        onChanged: (v) => setState(() => _blockText = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_remove_emojis')),
                        secondary: const Icon(
                          Icons.emoji_emotions_outlined,
                          size: 20,
                        ),
                        value: _removeEmojis,
                        onChanged: (v) => setState(() => _removeEmojis = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_sync_deletes')),
                        secondary: const Icon(Icons.delete_outline, size: 20),
                        value: _syncDeletes,
                        onChanged: (v) => setState(() => _syncDeletes = v),
                      ),
                      SwitchListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text(t.get('rules_ignore_edits')),
                        secondary: const Icon(
                          Icons.edit_off_outlined,
                          size: 20,
                        ),
                        value: _ignoreEdits,
                        onChanged: (v) => setState(() => _ignoreEdits = v),
                      ),
                      const SizedBox(height: 8),
                      DropdownButtonFormField<String>(
                        initialValue: _albumMode,
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
                      const Divider(height: AppSpacing.lg),
                      // The cleaner is a per-task SETTING (server-validated), not
                      // a rule field, so it is edited through the settings sheet
                      // — which also means every one of its ~50 switches stays in
                      // one place instead of being duplicated here.
                      ListTile(
                        key: const ValueKey<String>('rule_open_settings'),
                        contentPadding: EdgeInsets.zero,
                        dense: true,
                        leading: const Icon(
                          Icons.cleaning_services_outlined,
                          size: 20,
                        ),
                        title: Text(
                          t.get('rules_cleanup_open'),
                          style: const TextStyle(fontSize: 13),
                        ),
                        subtitle: Text(
                          t.get('rules_cleanup_hint'),
                          style: const TextStyle(fontSize: 11),
                        ),
                        trailing: Icon(AppIcons.forward(context), size: 18),
                        // A task that has not been created yet has no id, so
                        // there is nothing to attach settings to; the tile says
                        // so instead of opening an empty sheet.
                        enabled: widget.rule != null,
                        onTap: widget.rule == null
                            ? null
                            : () => showRuleSettingsSheet(
                                context,
                                ruleId: widget.rule!.id,
                                ruleLabel: widget.rule!.name.isNotEmpty
                                    ? widget.rule!.name
                                    : widget.rule!.shortId,
                              ),
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
                      Text(
                        t.get('rules_adv_categories'),
                        style: const TextStyle(
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
                          t.get(
                            'rules_filters_summary',
                            params: {
                              'duplicate': _duplicateFilters
                                  ? t.get('rules_state_on')
                                  : t.get('rules_state_off'),
                              'crypto': _cryptoMode
                                  ? t.get('rules_state_on')
                                  : t.get('rules_state_off'),
                            },
                          ),
                        ),
                        children: [
                          SwitchListTile(
                            title: Text(t.get('rules_filters_dup')),
                            subtitle: Text(t.get('rules_filters_dup_sub')),
                            value: _duplicateFilters,
                            onChanged: (v) =>
                                setState(() => _duplicateFilters = v),
                          ),
                          SwitchListTile(
                            title: Text(t.get('rules_filters_crypto')),
                            subtitle: Text(t.get('rules_filters_crypto_sub')),
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
                          t.get(
                            'rules_sched_summary',
                            params: {
                              'auto': _autoPosting
                                  ? t.get('rules_state_on')
                                  : t.get('rules_state_off'),
                              'power': _powerControl
                                  ? t.get('rules_state_on')
                                  : t.get('rules_state_off'),
                            },
                          ),
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
                        subtitle: Text(t.get('rules_time_forward_sub')),
                        children: [
                          Padding(
                            padding: const EdgeInsets.all(8),
                            child: Row(
                              children: [
                                Expanded(
                                  child: TextFormField(
                                    controller: _delayCtrl,
                                    keyboardType: TextInputType.number,
                                    decoration: InputDecoration(
                                      labelText: t.get('rules_time_delay'),
                                    ),
                                  ),
                                ),
                                const SizedBox(width: 12),
                                Expanded(
                                  child: TextFormField(
                                    controller: _editTimeCtrl,
                                    keyboardType: TextInputType.number,
                                    decoration: InputDecoration(
                                      labelText: t.get('rules_time_edit_limit'),
                                    ),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),

                      // Full control: the ~50 remaining per-task knobs, driven
                      // by the server's own registry so nothing has to be
                      // mirrored or kept in sync by hand.
                      if (widget.rule != null)
                        Container(
                          margin: const EdgeInsets.only(bottom: 12),
                          padding: const EdgeInsets.all(14),
                          decoration: BoxDecoration(
                            color: Colors.teal.withValues(alpha: 0.08),
                            borderRadius: BorderRadius.circular(12),
                            border: Border.all(
                              color: Colors.teal.withValues(alpha: 0.3),
                            ),
                          ),
                          child: Row(
                            children: [
                              const Icon(Icons.tune, color: Colors.teal),
                              const SizedBox(width: 12),
                              Expanded(
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      t.get('rules_full_control'),
                                      style: const TextStyle(
                                        fontSize: 13,
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                                    Text(
                                      t.get('rules_full_control_desc'),
                                      style: const TextStyle(
                                        fontSize: 11,
                                        color: Colors.grey,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                              FilledButton.tonal(
                                onPressed: () => showRuleSettingsSheet(
                                  context,
                                  ruleId: widget.rule!.id,
                                  ruleLabel: _nameCtrl.text.trim().isNotEmpty
                                      ? _nameCtrl.text.trim()
                                      : widget.rule!.sourceChatTitle,
                                ),
                                child: Text(t.get('rules_full_control_open')),
                              ),
                            ],
                          ),
                        ),

                      // Processing Messages Category
                      ExpansionTile(
                        leading: const Icon(
                          Icons.settings_suggest_outlined,
                          color: Colors.teal,
                        ),
                        title: Text(t.get('rules_sec_processing')),
                        subtitle: Text(
                          t.get(
                            'rules_proc_summary',
                            params: {
                              'raw': _processRawText
                                  ? t.get('rules_state_on')
                                  : t.get('rules_state_off'),
                              'btn2txt': _buttonToText
                                  ? t.get('rules_state_on')
                                  : t.get('rules_state_off'),
                            },
                          ),
                        ),
                        children: [
                          SwitchListTile(
                            title: Text(t.get('rules_proc_raw')),
                            value: _processRawText,
                            onChanged: (v) =>
                                setState(() => _processRawText = v),
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
                                  decoration: InputDecoration(
                                    labelText: t.get('rules_header'),
                                    hintText: t.get('rules_header_empty_hint'),
                                  ),
                                ),
                                const SizedBox(height: 8),
                                TextFormField(
                                  controller: _footerCtrl,
                                  decoration: InputDecoration(
                                    labelText: t.get('rules_footer'),
                                    hintText: t.get('rules_footer_empty_hint'),
                                  ),
                                ),
                                const SizedBox(height: 8),
                                TextFormField(
                                  controller: _watermarkCtrl,
                                  decoration: InputDecoration(
                                    labelText: t.get('rules_watermark_label'),
                                    hintText: t.get('rules_watermark_hint'),
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
                                      onPressed: () => setState(() {
                                        final tmp = _replacements[i - 1];
                                        _replacements[i - 1] = _replacements[i];
                                        _replacements[i] = tmp;
                                      }),
                                    ),
                                  IconButton(
                                    icon: const Icon(
                                      Icons.delete_outline,
                                      size: 18,
                                    ),
                                    tooltip: t.get('action_delete'),
                                    onPressed: () => setState(
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
                          // Both the "none" row and the configs share one
                          // selection, so they must sit under one RadioGroup:
                          // splitting them would leave the group unaware of the
                          // other options' selection state.
                          RadioGroup<String>(
                            groupValue: _selectedAiConfigId,
                            onChanged: (v) =>
                                setState(() => _selectedAiConfigId = v ?? ''),
                            child: Column(
                              children: [
                                RadioListTile<String>(
                                  dense: true,
                                  value: '',
                                  title: Text(t.get('rules_ai_pick_none')),
                                ),
                                ..._aiConfigs.map(
                                  (c) => RadioListTile<String>(
                                    dense: true,
                                    value: c.id,
                                    title: Text(_aiConfigLabel(c.id)),
                                    subtitle: Text(
                                      '${c.provider}${c.model.isNotEmpty ? ' · ${c.model}' : ''}',
                                    ),
                                  ),
                                ),
                              ],
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
                            subtitle: Text(t.get('rules_ai_ocr_sub')),
                            value: _ocrImageAi,
                            onChanged: (v) => setState(() => _ocrImageAi = v),
                          ),
                          SwitchListTile(
                            title: Text(t.get('rules_ai_rewrite')),
                            subtitle: Text(t.get('rules_ai_rewrite_sub')),
                            value: _smartAiRewrite,
                            onChanged: (v) =>
                                setState(() => _smartAiRewrite = v),
                          ),
                          SwitchListTile(
                            title: Text(t.get('rules_ai_digest')),
                            subtitle: Text(t.get('rules_ai_digest_sub')),
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
                              onChanged: (v) =>
                                  setState(() => _paidAddons[key] = v),
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
                child: _saving
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
