/// The chat picker: choose sources/targets out of the account's own chat list.
///
/// Routing used to be an id-entry problem — the user had to know that a channel
/// is `-1001234567890`. This sheet turns it into selection: the server lists the
/// chats the connected account can see (`GET /api/sessions/{id}/chats`), the
/// user searches and ticks them, and the picked ids are handed back as the same
/// strings a rule stores.
///
/// Three deliberate properties:
///   * **Manual entry survives everything.** If the endpoint is missing (an older
///     server), the session has no live client, or the network is down, the id
///     field still works and the failure is stated instead of hidden. The sheet
///     never becomes a dead end.
///   * **Selecting is not saving.** The sheet returns ids; the caller merges them
///     into the field it owns and the existing save path validates the route. So
///     a pick can never write a half-configured task.
///   * **Filtering is honest.** Type chips filter what was loaded, the search box
///     asks the server (so it can match chats beyond the first page), and the
///     "N of M" line states that the list is a window, not the whole account.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/route_input.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_ui.dart';

/// Opens the picker and resolves to the chosen entries, or null when the user
/// backed out. [alreadySelected] entries are shown as ticked and start in the
/// selection, so re-opening the sheet is a way to review the current route.
Future<List<String>?> showChatPickerSheet(
  BuildContext context, {
  required String title,
  required String sessionId,
  Set<String> alreadySelected = const {},
}) {
  return showModalBottomSheet<List<String>>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (_) => ChatPickerSheet(
      title: title,
      sessionId: sessionId,
      alreadySelected: alreadySelected,
    ),
  );
}

/// How many chats one request asks for. The server clamps this to its own
/// maximum; asking for a page (rather than everything) is what keeps the first
/// paint fast on an account with a thousand dialogs.
const int kChatPageSize = 100;

class ChatPickerSheet extends StatefulWidget {
  const ChatPickerSheet({
    super.key,
    required this.title,
    required this.sessionId,
    this.alreadySelected = const {},
  });

  final String title;
  final String sessionId;
  final Set<String> alreadySelected;

  @override
  State<ChatPickerSheet> createState() => _ChatPickerSheetState();
}

class _ChatPickerSheetState extends State<ChatPickerSheet> {
  final TextEditingController _searchCtrl = TextEditingController();
  final TextEditingController _manualCtrl = TextEditingController();

  /// Case-folding keys (see [chatIdKey]) so `@Foo`, `t.me/foo` and `-100…` are
  /// never both selected, and a repeat added by hand is refused.
  final Map<String, String> _selected = <String, String>{};

  List<ChatSummary> _chats = const [];
  int _total = 0;
  bool _truncated = false;
  bool _loading = true;
  String? _error;
  String? _manualError;
  Timer? _debounce;

  /// `all` | `channels` | `groups` | `users`.
  String _typeFilter = 'all';

  /// Page cursor: how many rows of the *current* query have been requested.
  int _loaded = 0;

  @override
  void initState() {
    super.initState();
    for (final entry in widget.alreadySelected) {
      _selected[chatIdKey(entry)] = entry;
    }
    _searchCtrl.addListener(_onSearchChanged);
    // Deferred: `context.read` is not available while initState is running.
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _searchCtrl.removeListener(_onSearchChanged);
    _searchCtrl.dispose();
    _manualCtrl.dispose();
    super.dispose();
  }

  void _onSearchChanged() {
    _debounce?.cancel();
    // The server owns the search (it can match chats past the first page), so a
    // keystroke is debounced into one request instead of one per character.
    _debounce = Timer(const Duration(milliseconds: 300), () {
      if (mounted) _load();
    });
  }

  ApiClient _client() => context.read<AppState>().buildApiClient();

  Future<void> _load({bool append = false}) async {
    if (widget.sessionId.isEmpty) {
      setState(() {
        _loading = false;
        _error = AppMessages.of(context).get('chats_picker_no_session');
      });
      return;
    }
    setState(() {
      _loading = true;
      if (!append) _error = null;
    });
    final offset = append ? _loaded : 0;
    try {
      final page = await _client().listChats(
        widget.sessionId,
        query: _searchCtrl.text,
        limit: kChatPageSize,
        offset: offset,
      );
      if (!mounted) return;
      setState(() {
        _chats = append ? <ChatSummary>[..._chats, ...page.chats] : page.chats;
        _loaded = _chats.length;
        _total = page.total;
        _truncated = page.truncated;
        _loading = false;
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        // The list is advisory; the id field below stays usable, so this is a
        // banner and not an error screen.
        _error = _listFailureText(AppMessages.of(context), exc);
      });
    }
  }

  /// Distinguishes "this server has no chat list" from a real failure: the first
  /// is permanent and read-only, the second is worth retrying.
  static String _listFailureText(AppMessages t, ApiException exc) {
    switch (exc.error.kind) {
      case ErrorKind.unauthorized:
      case ErrorKind.forbidden:
        return t.get('chats_picker_forbidden');
      case ErrorKind.notFound:
        return t.get('chats_picker_unavailable');
      case ErrorKind.network:
      case ErrorKind.timeout:
        return t.get('chats_picker_load_failed');
      default:
        return exc.error.message.isEmpty
            ? t.get('chats_picker_load_failed')
            : '${t.get('chats_picker_load_failed')}: ${exc.error.message}';
    }
  }

  List<ChatSummary> get _visible {
    switch (_typeFilter) {
      case 'channels':
        // Pinned first *inside* the filter: a filter is a narrowing, not a
        // re-sort, so the same row leads whether or not a chip is active.
        return pinnedFirst(_chats.where((c) => !c.isUserChat));
      case 'groups':
        return pinnedFirst(
          _chats.where(
            (c) => c.chatType == 'group' || c.chatType == 'supergroup',
          ),
        );
      case 'users':
        return pinnedFirst(_chats.where((c) => c.isUserChat));
      default:
        return pinnedFirst(_chats);
    }
  }

  void _toggle(ChatSummary chat) {
    final key = chatIdKey(chat.chatId);
    setState(() {
      if (_selected.containsKey(key)) {
        _selected.remove(key);
        return;
      }
      // Store the id as the rule will carry it (not the display title), because
      // that is what the engine and the duplicate check compare.
      _selected[key] = chat.chatId;
    });
  }

  void _addManual() {
    final t = AppMessages.of(context);
    final raw = _manualCtrl.text.trim();
    if (raw.isEmpty) return;
    final key = chatIdKey(raw);
    if (_selected.containsKey(key)) {
      setState(() => _manualError = t.get('chats_picker_duplicate'));
      return;
    }
    setState(() {
      _selected[key] = raw;
      _manualError = null;
      _manualCtrl.clear();
    });
  }

  void _selectAllVisible() {
    setState(() {
      for (final chat in _visible) {
        _selected[chatIdKey(chat.chatId)] = chat.chatId;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final theme = Theme.of(context);
    final visible = _visible;
    final height = MediaQuery.sizeOf(context).height;

    return ConstrainedBox(
      constraints: BoxConstraints(maxHeight: height * 0.92),
      child: Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.of(context).viewInsets.bottom,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _header(t, theme),
            const Divider(height: 1),
            Padding(
              padding: const EdgeInsetsDirectional.fromSTEB(
                AppSpacing.lg,
                AppSpacing.md,
                AppSpacing.lg,
                0,
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  TextField(
                    key: const ValueKey<String>('chat_picker_search'),
                    controller: _searchCtrl,
                    style: const TextStyle(fontSize: 14),
                    decoration: InputDecoration(
                      isDense: true,
                      prefixIcon: const Icon(Icons.search, size: 20),
                      hintText: t.get('chats_picker_search_hint'),
                      suffixIcon: _loading
                          ? const Padding(
                              padding: EdgeInsets.all(12),
                              child: SizedBox(
                                width: 16,
                                height: 16,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              ),
                            )
                          : null,
                    ),
                  ),
                  const SizedBox(height: AppSpacing.sm),
                  SingleChildScrollView(
                    scrollDirection: Axis.horizontal,
                    child: Row(
                      children: [
                        for (final entry in <(String, String)>[
                          ('all', 'chats_picker_filter_all'),
                          ('channels', 'chats_picker_filter_channels'),
                          ('groups', 'chats_picker_filter_groups'),
                          ('users', 'chats_picker_filter_users'),
                        ])
                          Padding(
                            padding: const EdgeInsetsDirectional.only(
                              end: AppSpacing.sm,
                            ),
                            child: ChoiceChip(
                              key: ValueKey<String>(
                                'chat_picker_filter_${entry.$1}',
                              ),
                              label: Text(t.get(entry.$2)),
                              selected: _typeFilter == entry.$1,
                              onSelected: (_) =>
                                  setState(() => _typeFilter = entry.$1),
                            ),
                          ),
                      ],
                    ),
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: AppSpacing.sm),
                    InlineBanner(
                      message: _error!,
                      severity: 'warning',
                      onRetry: () => _load(),
                      retryLabel: t.get('chats_picker_reload'),
                    ),
                  ],
                ],
              ),
            ),
            Expanded(child: _body(t, theme, visible)),
            _offPageSelected(t),
            _manualRow(t),
            const Divider(height: 1),
            _actions(t),
          ],
        ),
      ),
    );
  }

  Widget _header(AppMessages t, ThemeData theme) {
    return Padding(
      padding: const EdgeInsetsDirectional.fromSTEB(
        AppSpacing.lg,
        0,
        AppSpacing.sm,
        0,
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  widget.title,
                  style: theme.textTheme.titleMedium,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
                Text(
                  _selected.isEmpty
                      ? t.get('chats_picker_subtitle')
                      : t.get(
                          'chats_picker_selected_count',
                          params: {'n': '${_selected.length}'},
                        ),
                  style: theme.textTheme.bodySmall,
                ),
              ],
            ),
          ),
          if (_chats.isNotEmpty)
            TextButton(
              key: const ValueKey<String>('chat_picker_select_all'),
              onPressed: _selectAllVisible,
              child: Text(t.get('chats_picker_select_all')),
            ),
          IconButton(
            key: const ValueKey<String>('chat_picker_refresh'),
            icon: const Icon(Icons.refresh),
            tooltip: t.get('chats_picker_reload'),
            onPressed: _loading ? null : () => _load(),
          ),
        ],
      ),
    );
  }

  Widget _body(AppMessages t, ThemeData theme, List<ChatSummary> visible) {
    if (_chats.isEmpty && _loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (visible.isEmpty) {
      return EmptyStateView(
        message: _chats.isEmpty
            ? (_error != null
                  ? t.get('chats_picker_manual_hint')
                  : t.get('chats_picker_empty'))
            : t.get('chats_picker_filtered_empty'),
        icon: Icons.forum_outlined,
      );
    }
    return ListView.builder(
      key: const ValueKey<String>('chat_picker_list'),
      padding: const EdgeInsets.symmetric(horizontal: AppSpacing.sm),
      itemCount: visible.length + (_canLoadMore ? 1 : 0),
      itemBuilder: (ctx, index) {
        if (index >= visible.length) {
          return Padding(
            padding: const EdgeInsets.all(AppSpacing.md),
            child: OutlinedButton.icon(
              key: const ValueKey<String>('chat_picker_load_more'),
              icon: const Icon(Icons.expand_more, size: 18),
              label: Text(
                t.get(
                  'chats_picker_total',
                  params: {'shown': '${_chats.length}', 'total': '$_total'},
                ),
              ),
              onPressed: () => _load(append: true),
            ),
          );
        }
        final chat = visible[index];
        final key = chatIdKey(chat.chatId);
        final selected = _selected.containsKey(key);
        return CheckboxListTile(
          key: ValueKey<String>('chat_row_${chat.chatId}'),
          dense: true,
          value: selected,
          onChanged: (_) => _toggle(chat),
          secondary: Icon(_iconFor(chat)),
          title: Text(
            // 📌 is the same marker the bot's pickers use, so one glance says
            // why this row is above the rest.
            chat.pinned ? '📌 ${chat.displayTitle}' : chat.displayTitle,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
          ),
          subtitle: Text(
            _subtitle(t, chat),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 11),
          ),
          controlAffinity: ListTileControlAffinity.trailing,
        );
      },
    );
  }

  bool get _canLoadMore => _truncated && _loaded < _total;

  static IconData _iconFor(ChatSummary chat) {
    switch (chat.chatType) {
      case 'channel':
        return Icons.campaign_outlined;
      case 'supergroup':
      case 'group':
        return Icons.groups_outlined;
      case 'bot':
        return Icons.smart_toy_outlined;
      default:
        return Icons.person_outline;
    }
  }

  /// One muted line: @username, member count and the id the rule will store —
  /// the id matters because it is what the user sees in the bot and on the web.
  String _subtitle(AppMessages t, ChatSummary chat) {
    final parts = <String>[];
    if (chat.username.isNotEmpty) parts.add('@${chat.username}');
    if (chat.members > 0) {
      parts.add(t.get('chats_members', params: {'n': '${chat.members}'}));
    }
    parts.add(chat.chatId);
    if (chat.isForum) parts.add(t.get('chats_forum'));
    return parts.join(' • ');
  }

  /// The selected ids that the loaded page does not contain, as removable chips.
  ///
  /// The chat list is a page, not the account: a task's route can name a chat
  /// the current page does not include. Without this strip those ids are counted
  /// in "N selected" but never shown, so re-opening the sheet to review a route
  /// would show a total the user cannot inspect or undo. Ticking stays the only
  /// way to *add*; this is only how something already in the route is shown and
  /// removed.
  Widget _offPageSelected(AppMessages t) {
    final loaded = {for (final chat in _chats) chatIdKey(chat.chatId)};
    final extras = _selected.entries
        .where((entry) => !loaded.contains(entry.key))
        .toList(growable: false);
    if (extras.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsetsDirectional.fromSTEB(
        AppSpacing.lg,
        AppSpacing.sm,
        AppSpacing.lg,
        0,
      ),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(
          children: [
            for (final entry in extras)
              Padding(
                padding: const EdgeInsetsDirectional.only(end: AppSpacing.sm),
                child: InputChip(
                  key: ValueKey<String>('chat_picker_extra_${entry.value}'),
                  avatar: const Icon(Icons.tag, size: 16),
                  label: Text(entry.value),
                  onDeleted: () => setState(() => _selected.remove(entry.key)),
                  deleteButtonTooltipMessage: t.get('action_delete'),
                ),
              ),
          ],
        ),
      ),
    );
  }

  /// The escape hatch. Always present, so a missing/not-connected chat list never
  /// blocks configuring a task.
  Widget _manualRow(AppMessages t) {
    return Padding(
      padding: const EdgeInsetsDirectional.fromSTEB(
        AppSpacing.lg,
        AppSpacing.sm,
        AppSpacing.lg,
        AppSpacing.sm,
      ),
      child: Row(
        children: [
          Expanded(
            child: TextField(
              key: const ValueKey<String>('chat_picker_manual'),
              controller: _manualCtrl,
              style: const TextStyle(fontSize: 13),
              onSubmitted: (_) => _addManual(),
              decoration: InputDecoration(
                isDense: true,
                hintText: t.get('chats_picker_manual_hint'),
                errorText: _manualError,
                prefixIcon: const Icon(Icons.tag, size: 18),
                helperText: t.get('chats_picker_manual_help'),
              ),
            ),
          ),
          const SizedBox(width: AppSpacing.sm),
          IconButton.filledTonal(
            key: const ValueKey<String>('chat_picker_manual_add'),
            icon: const Icon(Icons.add),
            tooltip: t.get('chats_picker_manual_add'),
            onPressed: _addManual,
          ),
        ],
      ),
    );
  }

  Widget _actions(AppMessages t) {
    return Padding(
      padding: const EdgeInsetsDirectional.fromSTEB(
        AppSpacing.lg,
        AppSpacing.sm,
        AppSpacing.lg,
        AppSpacing.md,
      ),
      child: Row(
        children: [
          if (_selected.isNotEmpty)
            TextButton(
              key: const ValueKey<String>('chat_picker_clear'),
              onPressed: () => setState(_selected.clear),
              child: Text(t.get('chats_picker_clear')),
            ),
          const Spacer(),
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: Text(t.get('action_cancel')),
          ),
          const SizedBox(width: AppSpacing.sm),
          FilledButton.icon(
            key: const ValueKey<String>('chat_picker_confirm'),
            icon: const Icon(Icons.check, size: 18),
            label: Text(
              _selected.isEmpty
                  ? t.get('chats_picker_add_none')
                  : t.get(
                      'chats_picker_add',
                      params: {'n': '${_selected.length}'},
                    ),
            ),
            // Closing with an empty selection is a legitimate "make this side
            // empty", which the route validator then reports on save.
            onPressed: () =>
                Navigator.of(context)
                    .pop(_selected.values.toList(growable: false)),
          ),
        ],
      ),
    );
  }
}
