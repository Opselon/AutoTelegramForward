/// The Chats page: the account's own chat list, and what routes through it.
///
/// This is the read side of configuration. Every other screen starts from a
/// task; this one starts from the chats a user actually has, shows which of
/// their tasks send from or deliver to each one, and lets them act on it —
/// "use as a source", "use as a target", or start a new task from it.
///
/// Why a page and not just a picker inside the editor: the question "which of my
/// channels are not routed anywhere yet?" is a list question, and answering it
/// from inside a single task's editor is impossible.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../core/api_client.dart';
import '../../core/app_state.dart';
import '../../core/models.dart';
import '../../core/theme.dart';
import '../../l10n/app_messages.dart';
import '../../widgets/app_ui.dart';
import '../../widgets/loading_list.dart';
import '../rules/rule_editor_screen.dart';
import 'chat_picker_sheet.dart' show kChatPageSize;

class ChatsScreen extends StatefulWidget {
  const ChatsScreen({super.key});

  @override
  State<ChatsScreen> createState() => _ChatsScreenState();
}

class _ChatsScreenState extends State<ChatsScreen> {
  final TextEditingController _searchCtrl = TextEditingController();

  List<TelegramSession> _sessions = const [];
  List<ForwardRule> _rules = const [];
  List<ChatSummary> _chats = const [];

  String _sessionId = '';
  String? _loadError;
  bool _loading = true;
  bool _bootstrapped = false;
  bool _truncated = false;
  int _total = 0;
  Timer? _debounce;

  /// `all` | `channels` | `groups` | `users`.
  String _typeFilter = 'all';

  @override
  void initState() {
    super.initState();
    _searchCtrl.addListener(_onSearchChanged);
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _searchCtrl.removeListener(_onSearchChanged);
    _searchCtrl.dispose();
    super.dispose();
  }

  /// Bootstrap belongs in `didChangeDependencies`: `AppMessages.of(context)` and
  /// `context.read<AppState>()` are inherited-widget lookups and throw when
  /// called from `initState`. The guard matters because this fires again on
  /// every theme or locale change.
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_bootstrapped) return;
    _bootstrapped = true;
    _bootstrap();
  }

  void _onSearchChanged() {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 300), () {
      if (mounted) _loadChats();
    });
  }

  ApiClient _client() => context.read<AppState>().buildApiClient();

  Future<void> _bootstrap() async {
    setState(() {
      _loading = true;
      _loadError = null;
    });
    final client = _client();
    try {
      // Rules are loaded alongside the sessions so the usage count can be
      // computed locally: one request instead of one per chat. They are a
      // secondary read — a badge on each row — so a failure there must never
      // take the chat list down with it.
      final results = await Future.wait<Object>([
        client.listSessions(),
        _rulesOrEmpty(client),
      ]);
      if (!mounted) return;
      final sessions = results[0] as List<TelegramSession>;
      final rules = results[1] as List<ForwardRule>;
      setState(() {
        _sessions = sessions;
        _rules = rules;
        _sessionId = _pickSession(sessions);
      });
      await _loadChats();
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _loadError = _failureText(AppMessages.of(context), exc);
      });
    } catch (exc) {
      // Anything else — a payload this client cannot parse, most of all — is
      // still an answer the user is owed. Leaving `_loading` set would show a
      // spinner forever with no way to tell that nothing was ever going to
      // arrive.
      if (!mounted) return;
      setState(() {
        _loading = false;
        _loadError = '${AppMessages.of(context).get('state_error')} ($exc)';
      });
    }
  }

  /// The rule list only badges each chat with its usage count, so an
  /// unreachable or unparseable one yields no badges instead of no page.
  Future<List<ForwardRule>> _rulesOrEmpty(ApiClient client) async {
    try {
      return await client.listRules();
    } catch (_) {
      return const [];
    }
  }

  /// Prefer a session that can actually produce a chat list: an active one.
  /// Falling back to any session keeps the page usable (the chat request itself
  /// will explain why it is empty).
  static String _pickSession(List<TelegramSession> sessions) {
    for (final s in sessions) {
      if (s.isActive) return s.id;
    }
    return sessions.isEmpty ? '' : sessions.first.id;
  }

  static String _failureText(AppMessages t, ApiException exc) {
    switch (exc.error.kind) {
      case ErrorKind.unauthorized:
      case ErrorKind.forbidden:
        return t.get('chats_picker_forbidden');
      case ErrorKind.notFound:
        return t.get('chats_unavailable');
      default:
        return exc.error.message.isEmpty
            ? t.get('chats_unavailable')
            : '${t.get('chats_unavailable')}: ${exc.error.message}';
    }
  }

  Future<void> _loadChats() async {
    if (_sessionId.isEmpty) {
      setState(() {
        _loading = false;
        _chats = const [];
        _loadError = _sessions.isEmpty ? null : _loadError;
      });
      return;
    }
    setState(() {
      _loading = true;
      _loadError = null;
    });
    try {
      final page = await _client().listChats(
        _sessionId,
        query: _searchCtrl.text,
        limit: kChatPageSize,
      );
      if (!mounted) return;
      setState(() {
        _chats = page.chats;
        _total = page.total;
        _truncated = page.truncated;
        _loading = false;
      });
    } on ApiException catch (exc) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _chats = const [];
        _loadError = _failureText(AppMessages.of(context), exc);
      });
    }
  }

  /// Which of the user's tasks reference this chat, and on which side.
  (List<ForwardRule>, List<ForwardRule>) _usage(String chatId) {
    final asSource = <ForwardRule>[];
    final asTarget = <ForwardRule>[];
    for (final rule in _rules) {
      if (rule.sourceIds.contains(chatId)) asSource.add(rule);
      if (rule.targetIds.contains(chatId)) asTarget.add(rule);
    }
    return (asSource, asTarget);
  }

  List<ChatSummary> get _visible {
    switch (_typeFilter) {
      case 'channels':
        return _chats.where((c) => !c.isUserChat).toList(growable: false);
      case 'groups':
        return _chats
            .where((c) => c.chatType == 'group' || c.chatType == 'supergroup')
            .toList(growable: false);
      case 'users':
        return _chats.where((c) => c.isUserChat).toList(growable: false);
      default:
        return _chats;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);

    return Scaffold(
      appBar: AppBar(
        title: Text(t.get('chats_title')),
        actions: [
          IconButton(
            key: const ValueKey<String>('chats_refresh'),
            icon: const Icon(Icons.refresh),
            tooltip: t.get('chats_picker_reload'),
            onPressed: _loading ? null : () => _loadChats(),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _loadChats,
        child: ListView(
          padding: AppSpacing.screen,
          children: [
            GradientHeader(
              title: t.get('chats_title'),
              subtitle: t.get('chats_subtitle'),
              icon: Icons.forum_rounded,
            ),
            const SizedBox(height: AppSpacing.lg),
            _sessionSelector(t),
            const SizedBox(height: AppSpacing.md),
            TextField(
              key: const ValueKey<String>('chats_search'),
              controller: _searchCtrl,
              style: const TextStyle(fontSize: 14),
              decoration: InputDecoration(
                isDense: true,
                prefixIcon: const Icon(Icons.search, size: 20),
                hintText: t.get('chats_search_hint'),
                suffixIcon: _loading
                    ? const Padding(
                        padding: EdgeInsets.all(12),
                        child: SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
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
                      padding: const EdgeInsets.only(right: AppSpacing.sm),
                      child: ChoiceChip(
                        key: ValueKey<String>('chats_filter_${entry.$1}'),
                        label: Text(t.get(entry.$2)),
                        selected: _typeFilter == entry.$1,
                        onSelected: (_) =>
                            setState(() => _typeFilter = entry.$1),
                      ),
                    ),
                ],
              ),
            ),
            const SizedBox(height: AppSpacing.md),
            if (_loadError != null)
              InlineBanner(
                message: _loadError!,
                severity: 'warning',
                onRetry: _loadChats,
                retryLabel: t.get('chats_picker_reload'),
              ),
            _list(t),
          ],
        ),
      ),
    );
  }

  Widget _sessionSelector(AppMessages t) {
    if (_sessions.isEmpty) {
      return SectionCard(
        title: t.get('chats_session_label'),
        icon: Icons.send_outlined,
        child: EmptyStateView(
          message: t.get('chats_no_sessions'),
          icon: Icons.link_off_rounded,
        ),
      );
    }
    return SectionCard(
      title: t.get('chats_session_label'),
      icon: Icons.send_outlined,
      child: DropdownButtonFormField<String>(
        key: const ValueKey<String>('chats_session_dropdown'),
        initialValue: _sessionId.isEmpty ? null : _sessionId,
        isDense: true,
        items: [
          for (final s in _sessions)
            DropdownMenuItem(
              value: s.id,
              child: Text(
                // The endpoint URL is the id; showing it makes a two-session
                // account (personal + work) distinguishable without a label the
                // server does not send.
                '${s.displayName} — ${s.isActive ? t.get('chats_session_active') : t.get('chats_session_idle')}',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ),
        ],
        onChanged: (value) {
          if (value == null) return;
          setState(() => _sessionId = value);
          _loadChats();
        },
      ),
    );
  }

  Widget _list(AppMessages t) {
    if (_loading && _chats.isEmpty) {
      // Skeleton, not a spinner on a blank page: the rows land where the
      // placeholders already are, so the list does not jump.
      return const LoadingList(rows: 5, itemExtent: 76, shrinkWrap: true);
    }
    final visible = _visible;
    if (visible.isEmpty) {
      return EmptyStateView(
        message: _chats.isEmpty
            ? t.get('chats_empty')
            : t.get('chats_picker_filtered_empty'),
        icon: Icons.forum_outlined,
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (_truncated)
          Padding(
            padding: const EdgeInsets.only(bottom: AppSpacing.sm),
            child: Text(
              t.get(
                'chats_picker_total',
                params: {'shown': '${_chats.length}', 'total': '$_total'},
              ),
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ),
        for (final chat in visible)
          _ChatTile(
            chat: chat,
            usage: _usage(chat.chatId),
            onTap: () => _openActions(chat),
          ),
      ],
    );
  }

  Future<void> _openActions(ChatSummary chat) async {
    final t = AppMessages.of(context);
    final (asSource, asTarget) = _usage(chat.chatId);

    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (sheetCtx) {
        return SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(
              AppSpacing.lg,
              AppSpacing.sm,
              AppSpacing.lg,
              AppSpacing.lg,
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  chat.displayTitle,
                  style: Theme.of(sheetCtx).textTheme.titleMedium,
                ),
                Text(
                  [
                    if (chat.username.isNotEmpty) '@${chat.username}',
                    chat.chatId,
                  ].join(' • '),
                  style: Theme.of(sheetCtx).textTheme.bodySmall,
                ),
                const SizedBox(height: AppSpacing.md),
                Text(
                  asSource.isEmpty && asTarget.isEmpty
                      ? t.get('chats_usage_none')
                      : t.get(
                          'chats_usage',
                          params: {
                            'sources': '${asSource.length}',
                            'targets': '${asTarget.length}',
                          },
                        ),
                  style: Theme.of(sheetCtx).textTheme.bodySmall,
                ),
                const SizedBox(height: AppSpacing.md),
                ListTile(
                  key: const ValueKey<String>('chat_action_use_source'),
                  leading: const Icon(Icons.download_rounded),
                  title: Text(t.get('chats_use_source')),
                  subtitle: Text(
                    t.get(
                      'chats_use_source_tasks',
                      params: {'n': '${asSource.length}'},
                    ),
                  ),
                  onTap: () {
                    Navigator.of(sheetCtx).pop();
                    _pickTask(chat, asSource: true);
                  },
                ),
                ListTile(
                  key: const ValueKey<String>('chat_action_use_target'),
                  leading: const Icon(Icons.upload_rounded),
                  title: Text(t.get('chats_use_target')),
                  subtitle: Text(
                    t.get(
                      'chats_use_target_tasks',
                      params: {'n': '${asTarget.length}'},
                    ),
                  ),
                  onTap: () {
                    Navigator.of(sheetCtx).pop();
                    _pickTask(chat, asSource: false);
                  },
                ),
                ListTile(
                  key: const ValueKey<String>('chat_action_new_task'),
                  leading: const Icon(Icons.add_rounded),
                  title: Text(t.get('chats_new_task')),
                  subtitle: Text(t.get('chats_new_task_hint')),
                  onTap: () {
                    Navigator.of(sheetCtx).pop();
                    _openEditor(
                      rule: null,
                      prefillSource: chat.chatId,
                      prefillTarget: '',
                    );
                  },
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  /// Choosing the task to modify. The chat is *prefilled* into the editor and
  /// saved by the user, never written straight to the rule: a single tap here
  /// must not silently re-route a running task.
  Future<void> _pickTask(ChatSummary chat, {required bool asSource}) async {
    final t = AppMessages.of(context);
    if (_rules.isEmpty) {
      // Nothing to attach to: go straight to a new task with the chat filled in.
      _openEditor(
        rule: null,
        prefillSource: asSource ? chat.chatId : '',
        prefillTarget: asSource ? '' : chat.chatId,
      );
      return;
    }
    final selected = await showModalBottomSheet<ForwardRule>(
      context: context,
      useSafeArea: true,
      builder: (sheetCtx) => ListView(
        padding: const EdgeInsets.symmetric(vertical: AppSpacing.sm),
        shrinkWrap: true,
        children: [
          Padding(
            padding: const EdgeInsets.all(AppSpacing.lg),
            child: Text(
              asSource
                  ? t.get('chats_choose_source_task')
                  : t.get('chats_choose_target_task'),
              style: Theme.of(sheetCtx).textTheme.titleMedium,
            ),
          ),
          for (final rule in _rules)
            ListTile(
              key: ValueKey<String>('chat_task_${rule.id}'),
              leading: const Icon(Icons.alt_route_rounded),
              title: Text(
                rule.name.isNotEmpty ? rule.name : rule.shortId,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
              subtitle: Text(
                rule.sourceIds.isEmpty
                    ? t.get('chats_use_source_none')
                    : rule.sourceIds.join(', '),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
              onTap: () => Navigator.of(sheetCtx).pop(rule),
            ),
        ],
      ),
    );
    if (selected == null || !mounted) return;
    _openEditor(
      rule: selected,
      prefillSource: asSource ? chat.chatId : '',
      prefillTarget: asSource ? '' : chat.chatId,
    );
  }

  Future<void> _openEditor({
    required ForwardRule? rule,
    required String prefillSource,
    required String prefillTarget,
  }) async {
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => RuleEditorScreen(
          rule: rule,
          initialSessionId: _sessionId,
          prefillSourceIds: prefillSource.isEmpty
              ? const []
              : <String>[prefillSource],
          prefillTargetIds: prefillTarget.isEmpty
              ? const []
              : <String>[prefillTarget],
        ),
      ),
    );
    // A save can change which tasks use this chat, so the usage counts and the
    // rule list are refreshed rather than left stale behind the page.
    if (changed == true && mounted) await _bootstrap();
  }
}

/// One chat row: what it is, and how it is used today.
class _ChatTile extends StatelessWidget {
  const _ChatTile({
    required this.chat,
    required this.usage,
    required this.onTap,
  });

  final ChatSummary chat;
  final (List<ForwardRule>, List<ForwardRule>) usage;
  final VoidCallback onTap;

  static IconData _icon(ChatSummary chat) {
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

  @override
  Widget build(BuildContext context) {
    final t = AppMessages.of(context);
    final theme = Theme.of(context);
    final (asSource, asTarget) = usage;
    final parts = <String>[
      if (chat.username.isNotEmpty) '@${chat.username}',
      if (chat.members > 0)
        t.get('chats_members', params: {'n': '${chat.members}'}),
      chat.chatId,
    ];

    return Card(
      margin: const EdgeInsets.only(bottom: AppSpacing.sm),
      child: ListTile(
        key: ValueKey<String>('chats_row_${chat.chatId}'),
        leading: CircleAvatar(
          backgroundColor: theme.colorScheme.primary.withValues(alpha: 0.14),
          child: Icon(_icon(chat), size: 18, color: theme.colorScheme.primary),
        ),
        title: Text(
          chat.displayTitle,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontWeight: FontWeight.w600),
        ),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              parts.join(' • '),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 2),
            Wrap(
              spacing: AppSpacing.sm,
              children: [
                if (asSource.isNotEmpty)
                  StatusPill(
                    label: t.get(
                      'chats_used_source',
                      params: {'n': '${asSource.length}'},
                    ),
                    color: AppTheme.info,
                    icon: Icons.download_rounded,
                    dense: true,
                  ),
                if (asTarget.isNotEmpty)
                  StatusPill(
                    label: t.get(
                      'chats_used_target',
                      params: {'n': '${asTarget.length}'},
                    ),
                    color: AppTheme.success,
                    icon: Icons.upload_rounded,
                    dense: true,
                  ),
                if (asSource.isEmpty && asTarget.isEmpty)
                  StatusPill(
                    label: t.get('chats_unused'),
                    color: const Color(0xFF94A3B8),
                    dense: true,
                  ),
              ],
            ),
          ],
        ),
        trailing: const Icon(Icons.chevron_right),
        onTap: onTap,
      ),
    );
  }
}
