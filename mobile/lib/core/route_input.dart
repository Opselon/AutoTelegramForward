/// Multi-endpoint route input: the one parser and the one validator.
///
/// A routing task is a single rule carrying a SOURCE LIST and a TARGET LIST, so
/// 1->1, N->1, 1->N and M->N are all the same shape. The user types those lists
/// as comma-separated text; this file is the only place that turns that text
/// into the two lists the API expects, and the only place that decides whether
/// the pair of lists may be saved.
///
/// The parser matches the one every other surface applies (the bot splits on
/// ASCII ',' and Persian '،' — see `pro_bot_ui` keyword parsing), so a list
/// pasted from a Persian keyboard behaves exactly the same here.
///
/// Nothing in this file resolves a chat: `-1001234567890` ids, `@usernames` and
/// `t.me/<name>` links are all passed through untouched because the server owns
/// name resolution.
library;

import 'package:flutter/foundation.dart';

/// Splits a comma-separated endpoint string into entries, keeping duplicates.
///
/// * ASCII ',' and Persian '،' both separate entries;
/// * every entry is trimmed;
/// * empty entries are dropped, so `"a,,b,"` is `a`, `b`.
///
/// Order is preserved (the first source is the primary one), never sorted.
/// Use [parseChatIdList] for the payload, which additionally drops repeats.
List<String> splitChatIdList(String raw) {
  final out = <String>[];
  for (final part in raw.split(RegExp(r'[,\u060C]'))) {
    final value = part.trim();
    if (value.isEmpty) continue;
    out.add(value);
  }
  return List<String>.unmodifiable(out);
}

/// [splitChatIdList] plus de-duplication, first occurrence winning.
///
/// This is what goes on the wire: `source_chat_ids` / `target_chat_ids` are
/// canonical lists, and a repeat would otherwise make the engine fan a message
/// out to the same chat twice.
List<String> parseChatIdList(String raw) {
  final seen = <String>{};
  final out = <String>[];
  for (final value in splitChatIdList(raw)) {
    if (seen.add(value)) out.add(value);
  }
  return List<String>.unmodifiable(out);
}

/// Comparison key for two spellings of the same chat.
///
/// `@Channel`, `t.me/Channel` and `https://t.me/Channel` all address one chat,
/// so duplicate and self-loop checks compare these keys rather than the raw
/// text. Numeric ids are already canonical and only lose their surrounding
/// whitespace and case.
String chatIdKey(String value) {
  var key = value.trim().toLowerCase();
  key = key.replaceFirst(RegExp(r'^https?://'), '');
  key = key.replaceFirst(RegExp(r'^(www\.)?(t\.me|telegram\.me)/'), '');
  key = key.replaceFirst(RegExp(r'^@'), '');
  key = key.replaceFirst(RegExp(r'/+$'), '');
  return key;
}

/// Verification verdict for one entered endpoint.
enum RouteEntryState {
  /// Non-empty, unique in its own list, and not in the opposite list.
  accepted,

  /// The same chat appears twice in this list.
  duplicate,

  /// The chat is listed on both sides, which would make the task forward to
  /// itself.
  overlap,
}

/// One entered endpoint and how it verified.
@immutable
class RouteEntry {
  const RouteEntry({required this.value, required this.state});

  /// The entry exactly as the user typed it.
  final String value;

  final RouteEntryState state;

  bool get isOk => state == RouteEntryState.accepted;

  /// Message key describing this verdict (localised by the widget that
  /// renders the chip).
  String get messageKey => switch (state) {
    RouteEntryState.accepted => 'rules_route_chip_ok',
    RouteEntryState.duplicate => 'rules_route_chip_duplicate',
    RouteEntryState.overlap => 'rules_route_chip_overlap',
  };
}

/// Verifies every entry of one side of the route.
///
/// [peerRaw] is the raw text of the opposite side: an entry that appears there
/// too is the self-loop case. Repeats are detected on [chatIdKey], so a channel
/// named once as `@sales` and once as `https://t.me/sales` is caught.
List<RouteEntry> verifyRouteEntries(String raw, {String peerRaw = ''}) {
  final peerKeys = splitChatIdList(peerRaw).map(chatIdKey).toSet();
  final seen = <String>{};
  final out = <RouteEntry>[];
  for (final value in splitChatIdList(raw)) {
    final key = chatIdKey(value);
    final RouteEntryState state;
    if (peerKeys.contains(key)) {
      state = RouteEntryState.overlap;
    } else if (seen.add(key)) {
      state = RouteEntryState.accepted;
    } else {
      state = RouteEntryState.duplicate;
    }
    out.add(RouteEntry(value: value, state: state));
  }
  return List<RouteEntry>.unmodifiable(out);
}

/// Which rule the route broke.
enum RouteIssueKind {
  /// No source at all.
  sourcesEmpty,

  /// No target at all.
  targetsEmpty,

  /// A chat appears twice among the sources.
  duplicateSource,

  /// A chat appears twice among the targets.
  duplicateTarget,

  /// A chat is used as a source and as a target.
  overlap,
}

/// A rejected route, with the offending entry when the rule is about one.
@immutable
class RouteIssue {
  const RouteIssue(this.kind, {this.id = ''});

  final RouteIssueKind kind;

  /// The entry that broke the rule, as the user typed it. Empty for the
  /// "nothing entered" cases, where there is no id to name.
  final String id;

  bool get isMissingEndpoint =>
      kind == RouteIssueKind.sourcesEmpty ||
      kind == RouteIssueKind.targetsEmpty;

  /// Message key for this issue. The duplicate/overlap messages carry `{id}`,
  /// so the user is told which channel is wrong instead of just "invalid".
  String get messageKey => switch (kind) {
    RouteIssueKind.sourcesEmpty => 'rules_source_required',
    RouteIssueKind.targetsEmpty => 'rules_target_required',
    RouteIssueKind.duplicateSource ||
    RouteIssueKind.duplicateTarget => 'rules_route_duplicate_id',
    RouteIssueKind.overlap => 'rules_route_overlap_id',
  };
}

/// Validates one route. Returns null when it may be saved.
///
/// Rules, in the order the user hits them:
///   * at least one source and one target ([requireSources] /
///     [requireTargets] let a live preview ignore the emptiness that is normal
///     while the form is still being filled in, while save blocks on it);
///   * no repeats inside a list;
///   * no chat in both lists.
RouteIssue? validateRoute({
  required String sourcesText,
  required String targetsText,
  bool requireSources = true,
  bool requireTargets = true,
}) {
  final sources = splitChatIdList(sourcesText);
  final targets = splitChatIdList(targetsText);

  if (requireSources && sources.isEmpty) {
    return const RouteIssue(RouteIssueKind.sourcesEmpty);
  }
  if (requireTargets && targets.isEmpty) {
    return const RouteIssue(RouteIssueKind.targetsEmpty);
  }

  final sourceKeys = <String>{};
  for (final source in sources) {
    if (!sourceKeys.add(chatIdKey(source))) {
      return RouteIssue(RouteIssueKind.duplicateSource, id: source);
    }
  }
  final targetKeys = <String>{};
  for (final target in targets) {
    if (!targetKeys.add(chatIdKey(target))) {
      return RouteIssue(RouteIssueKind.duplicateTarget, id: target);
    }
  }
  for (final source in sources) {
    if (targetKeys.contains(chatIdKey(source))) {
      return RouteIssue(RouteIssueKind.overlap, id: source);
    }
  }
  return null;
}
