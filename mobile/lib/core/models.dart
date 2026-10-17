/// Typed models for the AutoTelegramForward Control API.
///
/// These mirror the JSON emitted by `api/internal/handlers/*.go`. The Go
/// handlers are the single authority: the client never re-derives rule
/// ownership, tenant scope or session state — it only renders what the server
/// returns for the authenticated caller.
library;

import 'json_cast.dart';

import 'package:flutter/foundation.dart';

import 'route_input.dart';

@immutable
class AppError {
  const AppError({
    required this.kind,
    required this.message,
    this.statusCode,
    this.retryable = false,
  });

  /// Stable machine-readable code the UI switches on. See [ErrorKind].
  final String kind;

  /// Human-readable, already-localised-or-English detail from the server.
  final String message;

  final int? statusCode;

  /// True when repeating the identical call could succeed (network blip,
  /// 5xx, rate limit). Non-idempotent mutations are never retried blindly.
  final bool retryable;

  @override
  String toString() => 'AppError($kind, $statusCode): $message';

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is AppError &&
          runtimeType == other.runtimeType &&
          kind == other.kind &&
          message == other.message &&
          statusCode == other.statusCode &&
          retryable == other.retryable;

  @override
  int get hashCode => Object.hash(kind, message, statusCode, retryable);
}

/// Stable error taxonomy used across the app and in tests.
class ErrorKind {
  const ErrorKind._();

  static const String network = 'network';
  static const String timeout = 'timeout';
  static const String unauthorized = 'unauthorized';
  static const String forbidden = 'forbidden';
  static const String notFound = 'not_found';
  static const String validation = 'validation';
  static const String conflict = 'conflict';
  static const String rateLimited = 'rate_limited';
  static const String server = 'server';
  static const String cancelled = 'cancelled';
  static const String unknown = 'unknown';
  static const String incompatibleServer = 'incompatible_server';
}

/// ---- Authentication ----------------------------------------------------

@immutable
class AuthSession {
  const AuthSession({
    required this.token,
    required this.userId,
    required this.username,
    required this.isAdmin,
    required this.serverUrl,
    required this.expiresAt,
  });

  final String token;
  final int userId;
  final String username;
  final bool isAdmin;
  final String serverUrl;

  /// Wall-clock expiry. The client treats 401 as authoritative and this only
  /// as a hint for proactive re-auth prompts.
  final DateTime expiresAt;

  bool get isValid => token.isNotEmpty && DateTime.now().isBefore(expiresAt);

  Map<String, dynamic> toJson() => {
    'token': token,
    'user_id': userId,
    'username': username,
    'is_admin': isAdmin,
    'server_url': serverUrl,
    'expires_at': expiresAt.toIso8601String(),
  };

  factory AuthSession.fromJson(Map<String, dynamic> json) => AuthSession(
    token: json['token'] as String,
    userId: jsonInt(json['user_id']),
    username: json['username'] as String,
    isAdmin: json['is_admin'] as bool? ?? false,
    serverUrl: json['server_url'] as String,
    expiresAt: json['expires_at'] is String
        ? DateTime.tryParse(json['expires_at'] as String) ??
              DateTime.now().add(const Duration(days: 7))
        : DateTime.now().add(const Duration(days: 7)),
  );

  @override
  String toString() =>
      'AuthSession(user=$userId, server=$serverUrl, valid=$isValid)';
}

/// ---- Telegram sessions -------------------------------------------------

@immutable
class TelegramSession {
  const TelegramSession({
    required this.id,
    required this.isActive,
    this.phone = '',
    this.userId = '',
    this.username = '',
    this.firstName = '',
    this.apiCredentialId = '',
    this.ownerUserId = 0,
  });

  final String id;
  final bool isActive;
  final String phone;
  final String userId;
  final String username;
  final String firstName;
  final String apiCredentialId;

  /// Echoed by the server only; the client never trusts a client-side value.
  final int ownerUserId;

  String get displayName => firstName.isNotEmpty
      ? firstName
      : (username.isNotEmpty ? '@$username' : (phone.isNotEmpty ? phone : id));

  factory TelegramSession.fromJson(Map<String, dynamic> json) =>
      TelegramSession(
        id: json['id'] as String,
        isActive: json['is_active'] as bool? ?? false,
        phone: json['phone_number'] as String? ?? '',
        userId: json['user_id']?.toString() ?? '',
        username: json['username'] as String? ?? '',
        firstName: json['first_name'] as String? ?? '',
        apiCredentialId: json['api_credential_id'] as String? ?? '',
        ownerUserId: jsonInt(json['owner_user_id']),
      );

  @override
  String toString() => 'TelegramSession($id, active=$isActive, $displayName)';
}

/// ---- Forwarding rules ---------------------------------------------------

/// Trigger policy for the branding hop (A → C → B), read from the rule
/// metadata key `branding_hop`.
///
/// The hop itself is two first-class rule fields — `use_intermediate` and
/// `intermediate_channel_id`, modelled as [ForwardRule.useVipRouting] and
/// [ForwardRule.intermediateChannelId]. This class only describes *which*
/// messages earn the hop, mirroring `core/domain/branding.py` on the server so
/// the app can describe the effective route without re-implementing the
/// decision.
///
/// It is deliberately read-only on the client: the metadata blob has exactly
/// one server-side writer, so parsing it here can never disagree with a write
/// path, and it is never re-serialised (see [ForwardRule.toCreateJson]).
@immutable
class BrandingHopConfig {
  const BrandingHopConfig({
    this.triggersEnabled = false,
    this.originIds = const [],
    this.originTitles = const [],
    this.keywords = const [],
    this.matchMode = 'any',
    this.requireForward = false,
    this.forceForward = true,
  });

  /// Off (the server default) means the hop is unconditional.
  final bool triggersEnabled;

  /// Forward-origin chat ids / @usernames that qualify a message.
  final List<String> originIds;

  /// Forward-origin channel *names* (Telegram's `Forwarded from <channel>`
  /// header), not text inside the message body.
  final List<String> originTitles;

  /// Words that qualify a message by its text.
  final List<String> keywords;

  /// `any` (one trigger is enough) or `all` (every configured trigger must
  /// match). Anything else normalises to `any`.
  final String matchMode;

  /// A gate, not a check: when set, a message that is not itself a forward is
  /// refused regardless of [matchMode].
  final bool requireForward;

  /// Send the final step as a native forward of C, which is what makes the
  /// target's header name C. Defaults to true server-side.
  final bool forceForward;

  /// True only when the triggers actually narrow the hop down. Switched-off
  /// triggers, or switched-on triggers with nothing configured, both mean
  /// "every matched message hops".
  bool get hasTriggers =>
      triggersEnabled &&
      (originIds.isNotEmpty ||
          originTitles.isNotEmpty ||
          keywords.isNotEmpty ||
          requireForward);

  /// Parses the `branding_hop` metadata value defensively.
  ///
  /// Absent, non-map and malformed input all degrade to the unconditional
  /// configuration: a rule the app cannot read must render as "every message"
  /// rather than throwing in a build.
  factory BrandingHopConfig.fromValue(Object? raw) {
    if (raw is! Map) return const BrandingHopConfig();
    final mode = raw['match_mode']?.toString().trim().toLowerCase();
    return BrandingHopConfig(
      triggersEnabled: raw['triggers_enabled'] == true,
      originIds: _stringList(raw['origin_ids']),
      originTitles: _stringList(raw['origin_titles']),
      keywords: _stringList(raw['keywords']),
      matchMode: mode == 'all' ? 'all' : 'any',
      requireForward: raw['require_forward'] == true,
      forceForward: raw['force_forward'] is bool
          ? raw['force_forward'] as bool
          : true,
    );
  }

  /// Mirrors `_as_str_list` in `core/domain/branding.py`: scalars only,
  /// trimmed, empties dropped, de-duplicated. Nested maps/lists are junk from a
  /// hand-edited blob and are skipped instead of stringified.
  static List<String> _stringList(Object? raw) {
    final items = raw is List
        ? raw
        : (raw is String ? <Object?>[raw] : const <Object?>[]);
    final seen = <String>{};
    for (final item in items) {
      if (item is Map || item is Iterable) continue;
      final text = item?.toString().trim() ?? '';
      if (text.isEmpty || !seen.add(text)) continue;
    }
    return List<String>.unmodifiable(seen);
  }
}

@immutable
class ForwardRule {
  const ForwardRule({
    required this.id,
    required this.sourceChatId,
    required this.targetChatId,
    required this.isEnabled,
    this.sourceChatIds = const [],
    this.targetChatIds = const [],
    this.sourceChatTitle = '',
    this.targetChatTitle = '',
    this.sessionId = '',
    this.intermediateChannelId = '',
    this.brandingHop = const BrandingHopConfig(),
    this.filters = const [],
    this.filterMode = 'all',
    this.mediaMode = 'all',
    this.delaySeconds = 0,
    this.headerTemplate = '',
    this.footerTemplate = '',
    this.aiConfigId = '',
    this.promptId = '',
    this.replacements = const {},
    this.useVipRouting = false,
    this.blockVoice = false,
    this.blockStickers = false,
    this.removeEmojis = false,
    this.syncDeletes = false,
    this.ignoreEdits = false,
    this.albumMode = 'album',
    this.version = 0,
    this.ownerUserId = 0,
    this.name = '',
    this.topicId = 0,
    this.debugMode = false,
    this.senderType = 'Personal Telegram Account',
    this.featureGroup = 'all',
    this.duplicateFilters = false,
    this.cryptoMode = false,
    this.autoPosting = false,
    this.powerControl = false,
    this.editTimeLimitSeconds = 0,
    this.processRawText = false,
    this.buttonToText = false,
    this.ocrImageAi = false,
    this.smartAiRewrite = false,
    this.newsDigest = false,
    this.watermark = '',
    this.aiCreativity = 0.7,
    this.aiMaxTokens = 600,
    this.paidAddons = const {},
    // v2.0 app update fields
    this.cleanerEnabled = false,
    this.cleanerConfig = const {},
    this.showForwardHeader = false,
    this.syncReplies = true,
    this.syncEdits = true,
  });

  final String id;
  final String sessionId;
  final String sourceChatId;
  final String sourceChatTitle;
  final String targetChatId;
  final String targetChatTitle;

  /// Canonical endpoint lists. One rule carries every source and every target,
  /// so 1->1, N->1, 1->N and M->N are the same shape; the server mirrors the
  /// first entry of each list into the legacy singular fields above.
  final List<String> sourceChatIds;
  final List<String> targetChatIds;

  /// Every source of this task. Falls back to the legacy single
  /// [sourceChatId] so a payload written before multi-source routing parses
  /// into a one-entry list instead of nothing.
  List<String> get sourceIds {
    if (sourceChatIds.isNotEmpty) return sourceChatIds;
    return sourceChatId.isEmpty ? const <String>[] : <String>[sourceChatId];
  }

  /// Every target of this task, same fallback as [sourceIds].
  List<String> get targetIds {
    if (targetChatIds.isNotEmpty) return targetChatIds;
    return targetChatId.isEmpty ? const <String>[] : <String>[targetChatId];
  }

  /// The fan-out list the engine delivers to: primary first, de-duplicated.
  /// Mirrors `ForwardRule.all_targets` in the Python core so the client
  /// previews exactly what will be delivered.
  List<String> get allTargets {
    final seen = <String>{};
    final out = <String>[];
    for (final target in <String>[targetChatId, ...targetIds]) {
      if (target.isNotEmpty && seen.add(target)) out.add(target);
    }
    return List<String>.unmodifiable(out);
  }

  /// The rule id is the per-task identity. Tasks carry UUID-ish ids, so the
  /// first eight characters are what a user can read back and compare.
  String get shortId => id.length > 8 ? id.substring(0, 8) : id;

  /// True when this task fans a message out to more than one target.
  bool get isFanOut => targetIds.length > 1;

  /// Task Overview & Advanced Settings Parity Fields
  final String name;
  final int topicId;
  final bool debugMode;
  final String senderType;
  final String featureGroup;
  final bool duplicateFilters;
  final bool cryptoMode;
  final bool autoPosting;
  final bool powerControl;
  final int editTimeLimitSeconds;
  final bool processRawText;
  final bool buttonToText;
  final bool ocrImageAi;
  final bool smartAiRewrite;
  final bool newsDigest;
  final String watermark;
  final double aiCreativity;
  final int aiMaxTokens;
  final Map<String, bool> paidAddons;

  // v2.0 app update fields
  final bool cleanerEnabled;
  final Map<String, dynamic> cleanerConfig;
  final bool showForwardHeader;
  final bool syncReplies;
  final bool syncEdits;

  /// VIP / intermediate-channel routing. When the server exposes
  /// `intermediate_channel_id` the rule funnels through it first.
  final String intermediateChannelId;
  final bool useVipRouting;

  /// Which messages earn the hop (rule metadata key `branding_hop`). Read-only:
  /// it is parsed for display and never written back, so editing a task cannot
  /// invent a second writer for the same value.
  final BrandingHopConfig brandingHop;

  final List<String> filters;
  final String filterMode;
  final String mediaMode;
  final int delaySeconds;
  final String headerTemplate;
  final String footerTemplate;
  final String aiConfigId;
  final String promptId;

  /// Per-task text replace rules: find -> replace. Applied by the core
  /// pipeline in deterministic order; empty string replaces with removal.
  final Map<String, String> replacements;

  /// Bot & Web parity media toggles
  final bool blockVoice;
  final bool blockStickers;
  final bool removeEmojis;
  final bool syncDeletes;
  final bool ignoreEdits;
  final String albumMode;

  /// Server-side optimistic-concurrency version. Sent back on update so a
  /// concurrent edit is rejected instead of silently overwritten.
  final int version;
  final bool isEnabled;
  final int ownerUserId;

  ForwardRule copyWith({
    String? id,
    String? sessionId,
    String? sourceChatId,
    String? sourceChatTitle,
    String? targetChatId,
    String? targetChatTitle,
    List<String>? sourceChatIds,
    List<String>? targetChatIds,
    String? intermediateChannelId,
    bool? useVipRouting,
    List<String>? filters,
    String? filterMode,
    String? mediaMode,
    int? delaySeconds,
    String? headerTemplate,
    String? footerTemplate,
    String? aiConfigId,
    String? promptId,
    Map<String, String>? replacements,
    bool? blockVoice,
    bool? blockStickers,
    bool? removeEmojis,
    bool? syncDeletes,
    bool? ignoreEdits,
    String? albumMode,
    int? version,
    bool? isEnabled,
    String? name,
    int? topicId,
    bool? debugMode,
    String? senderType,
    String? featureGroup,
    bool? duplicateFilters,
    bool? cryptoMode,
    bool? autoPosting,
    bool? powerControl,
    int? editTimeLimitSeconds,
    bool? processRawText,
    bool? buttonToText,
    bool? ocrImageAi,
    bool? smartAiRewrite,
    bool? newsDigest,
    String? watermark,
    double? aiCreativity,
    int? aiMaxTokens,
    Map<String, bool>? paidAddons,
    bool? cleanerEnabled,
    Map<String, dynamic>? cleanerConfig,
    bool? showForwardHeader,
    bool? syncReplies,
    bool? syncEdits,
    BrandingHopConfig? brandingHop,
  }) => ForwardRule(
    id: id ?? this.id,
    sessionId: sessionId ?? this.sessionId,
    sourceChatId: sourceChatIds != null && sourceChatIds.isNotEmpty
        ? sourceChatIds.first
        : (sourceChatId ?? this.sourceChatId),
    sourceChatTitle: sourceChatTitle ?? this.sourceChatTitle,
    targetChatId: targetChatIds != null && targetChatIds.isNotEmpty
        ? targetChatIds.first
        : (targetChatId ?? this.targetChatId),
    targetChatTitle: targetChatTitle ?? this.targetChatTitle,
    sourceChatIds: sourceChatIds ?? this.sourceChatIds,
    targetChatIds: targetChatIds ?? this.targetChatIds,
    intermediateChannelId: intermediateChannelId ?? this.intermediateChannelId,
    useVipRouting: useVipRouting ?? this.useVipRouting,
    brandingHop: brandingHop ?? this.brandingHop,
    filters: filters ?? this.filters,
    filterMode: filterMode ?? this.filterMode,
    mediaMode: mediaMode ?? this.mediaMode,
    delaySeconds: delaySeconds ?? this.delaySeconds,
    headerTemplate: headerTemplate ?? this.headerTemplate,
    footerTemplate: footerTemplate ?? this.footerTemplate,
    aiConfigId: aiConfigId ?? this.aiConfigId,
    promptId: promptId ?? this.promptId,
    replacements: replacements ?? this.replacements,
    blockVoice: blockVoice ?? this.blockVoice,
    blockStickers: blockStickers ?? this.blockStickers,
    removeEmojis: removeEmojis ?? this.removeEmojis,
    syncDeletes: syncDeletes ?? this.syncDeletes,
    ignoreEdits: ignoreEdits ?? this.ignoreEdits,
    albumMode: albumMode ?? this.albumMode,
    version: version ?? this.version,
    isEnabled: isEnabled ?? this.isEnabled,
    ownerUserId: ownerUserId,
    name: name ?? this.name,
    topicId: topicId ?? this.topicId,
    debugMode: debugMode ?? this.debugMode,
    senderType: senderType ?? this.senderType,
    featureGroup: featureGroup ?? this.featureGroup,
    duplicateFilters: duplicateFilters ?? this.duplicateFilters,
    cryptoMode: cryptoMode ?? this.cryptoMode,
    autoPosting: autoPosting ?? this.autoPosting,
    powerControl: powerControl ?? this.powerControl,
    editTimeLimitSeconds: editTimeLimitSeconds ?? this.editTimeLimitSeconds,
    processRawText: processRawText ?? this.processRawText,
    buttonToText: buttonToText ?? this.buttonToText,
    ocrImageAi: ocrImageAi ?? this.ocrImageAi,
    smartAiRewrite: smartAiRewrite ?? this.smartAiRewrite,
    newsDigest: newsDigest ?? this.newsDigest,
    watermark: watermark ?? this.watermark,
    aiCreativity: aiCreativity ?? this.aiCreativity,
    aiMaxTokens: aiMaxTokens ?? this.aiMaxTokens,
    paidAddons: paidAddons ?? this.paidAddons,
    cleanerEnabled: cleanerEnabled ?? this.cleanerEnabled,
    cleanerConfig: cleanerConfig ?? this.cleanerConfig,
    showForwardHeader: showForwardHeader ?? this.showForwardHeader,
    syncReplies: syncReplies ?? this.syncReplies,
    syncEdits: syncEdits ?? this.syncEdits,
  );

  /// Create payload.
  ///
  /// Both endpoint lists and the legacy singular fields go on the wire: the
  /// lists are canonical (`source_chat_ids` / `target_chat_ids`), and the
  /// singulars carry the first entry because a server predating multi-source
  /// routing only understands those. The server resolves @usernames and links
  /// inside every list entry.
  Map<String, dynamic> toCreateJson() => {
    'session_id': sessionId,
    'source_chat_id': sourceChatId,
    'source_chat_ids': sourceIds,
    'source_chat_title': sourceChatTitle,
    'target_chat_id': targetChatId,
    'target_chat_ids': targetIds,
    'target_chat_title': targetChatTitle,
    'name': name,
    'topic_id': topicId,
    'debug_mode': debugMode,
    'sender_type': senderType,
    'feature_group': featureGroup,
    'duplicate_filters': duplicateFilters,
    'crypto_mode': cryptoMode,
    'auto_posting': autoPosting,
    'power_control': powerControl,
    'edit_time_limit_seconds': editTimeLimitSeconds,
    'process_raw_text': processRawText,
    'button_to_text': buttonToText,
    'ocr_image_ai': ocrImageAi,
    'smart_ai_rewrite': smartAiRewrite,
    'news_digest': newsDigest,
    'watermark': watermark,
    'replacements': replacements,
    'ai_creativity': aiCreativity,
    'ai_max_tokens': aiMaxTokens,
    'paid_addons': paidAddons,
    'cleaner_enabled': cleanerEnabled,
    'cleaner_config': cleanerConfig,
    'show_forward_header': showForwardHeader,
    'sync_replies': syncReplies,
    'sync_edits': syncEdits,
    if (intermediateChannelId.isNotEmpty)
      'intermediate_channel_id': intermediateChannelId,
    'use_vip_routing': useVipRouting,
    'filters': filters,
    'filter_mode': filterMode,
    'media_mode': mediaMode,
    'delay_seconds': delaySeconds,
    'header_template': headerTemplate,
    'footer_template': footerTemplate,
    if (aiConfigId.isNotEmpty) 'ai_config_id': aiConfigId,
    if (promptId.isNotEmpty) 'prompt_id': promptId,
    'block_voice': blockVoice,
    'block_stickers': blockStickers,
    'remove_emojis': removeEmojis,
    'sync_deletes': syncDeletes,
    'ignore_edits': ignoreEdits,
    'album_mode': albumMode,
  };

  Map<String, dynamic> toUpdateJson() => {
    ...toCreateJson(),
    'expected_version': version,
    'is_enabled': isEnabled,
  };

  /// Reads one endpoint list from a payload.
  ///
  /// The canonical form is a JSON array; a comma-separated string is accepted
  /// too (ASCII ',' or Persian '،') because every surface lets the user type
  /// one. Repeated entries are collapsed, first occurrence winning, so the
  /// primary endpoint can never be pushed out of first place by a duplicate.
  static List<String> _chatIdList(Object? raw) {
    if (raw is String) return parseChatIdList(raw);
    if (raw is! List) return const <String>[];
    final seen = <String>{};
    final out = <String>[];
    for (final entry in raw) {
      final value = entry?.toString().trim() ?? '';
      if (value.isEmpty || !seen.add(value)) continue;
      out.add(value);
    }
    return List<String>.unmodifiable(out);
  }

  /// Resolves one side of the route from a payload, preferring the canonical
  /// list and falling back to the legacy singular field, then to the
  /// server-computed fan-out list. Returns the endpoints plus the mirror value
  /// for the singular field — always the first endpoint, which is exactly what
  /// the server mirrors back, so a round-trip cannot reorder the route.
  static (List<String>, String) _resolveEndpoints(
    Map<String, dynamic> json,
    String listKey,
    String legacyKey, {
    String fanOutKey = '',
  }) {
    final legacy = json[legacyKey]?.toString().trim() ?? '';
    var endpoints = _chatIdList(json[listKey]);
    if (endpoints.isEmpty && fanOutKey.isNotEmpty) {
      endpoints = _chatIdList(json[fanOutKey]);
    }
    if (endpoints.isEmpty && legacy.isNotEmpty) {
      endpoints = List<String>.unmodifiable(<String>[legacy]);
    }
    return (endpoints, endpoints.isEmpty ? '' : endpoints.first);
  }

  factory ForwardRule.fromJson(Map<String, dynamic> json) {
    final meta = json['metadata'] is Map ? (json['metadata'] as Map) : const {};
    final (sources, sourcePrimary) = _resolveEndpoints(
      json,
      'source_chat_ids',
      'source_chat_id',
    );
    final (targets, targetPrimary) = _resolveEndpoints(
      json,
      'target_chat_ids',
      'target_chat_id',
      fanOutKey: 'all_targets',
    );
    final rawAddons = json['paid_addons'] is Map
        ? json['paid_addons'] as Map
        : (meta['paid_addons'] is Map ? meta['paid_addons'] as Map : const {});
    final Map<String, bool> parsedAddons = {};
    rawAddons.forEach((k, v) {
      if (k != null) parsedAddons[k.toString()] = v == true;
    });

    return ForwardRule(
      id: json['id'] as String,
      sessionId: json['session_id'] as String? ?? '',
      sourceChatId: sourcePrimary,
      sourceChatTitle: json['source_chat_title'] as String? ?? '',
      targetChatId: targetPrimary,
      targetChatTitle: json['target_chat_title'] as String? ?? '',
      sourceChatIds: sources,
      targetChatIds: targets,
      intermediateChannelId: json['intermediate_channel_id']?.toString() ?? '',
      useVipRouting: json['use_vip_routing'] as bool? ?? false,
      // The trigger policy lives in the raw metadata blob, which only the
      // server writes; parse it defensively and never send it back.
      brandingHop: BrandingHopConfig.fromValue(meta['branding_hop']),
      name: (json['name'] as String?) ?? (meta['name'] as String? ?? ''),
      topicId: jsonInt(json['topic_id'], jsonInt(meta['topic_id'])),
      debugMode:
          (json['debug_mode'] as bool?) ??
          (meta['debug_mode'] as bool? ?? false),
      senderType:
          (json['sender_type'] as String?) ??
          (meta['sender_type'] as String? ?? 'Personal Telegram Account'),
      featureGroup:
          (json['feature_group'] as String?) ??
          (meta['feature_group'] as String? ?? 'all'),
      duplicateFilters:
          (json['duplicate_filters'] as bool?) ??
          (meta['duplicate_filters'] as bool? ?? false),
      cryptoMode:
          (json['crypto_mode'] as bool?) ??
          (meta['crypto_mode'] as bool? ?? false),
      autoPosting:
          (json['auto_posting'] as bool?) ??
          (meta['auto_posting'] as bool? ?? false),
      powerControl:
          (json['power_control'] as bool?) ??
          (meta['power_control'] as bool? ?? false),
      editTimeLimitSeconds: jsonInt(
        json['edit_time_limit_seconds'],
        jsonInt(meta['edit_time_limit_seconds']),
      ),
      processRawText:
          (json['process_raw_text'] as bool?) ??
          (meta['process_raw_text'] as bool? ?? false),
      buttonToText:
          (json['button_to_text'] as bool?) ??
          (meta['button_to_text'] as bool? ?? false),
      ocrImageAi:
          (json['ocr_image_ai'] as bool?) ??
          (meta['ocr_image_ai'] as bool? ?? false),
      smartAiRewrite:
          (json['smart_ai_rewrite'] as bool?) ??
          (meta['smart_ai_rewrite'] as bool? ?? false),
      newsDigest:
          (json['news_digest'] as bool?) ??
          (meta['news_digest'] as bool? ?? false),
      watermark:
          (json['watermark'] as String?) ??
          (meta['watermark'] as String? ?? ''),
      replacements: meta['replacements'] is Map
          ? (meta['replacements'] as Map).map(
              (k, v) => MapEntry(k.toString(), v?.toString() ?? ''),
            )
          : const {},
      aiCreativity: jsonDouble(
        json['ai_creativity'],
        jsonDouble(meta['ai_creativity'], 0.7),
      ),
      aiMaxTokens: jsonInt(
        json['ai_max_tokens'],
        jsonInt(meta['ai_max_tokens'], 600),
      ),
      paidAddons: parsedAddons,
      cleanerEnabled:
          (json['cleaner_enabled'] as bool?) ??
          (meta['cleaner_enabled'] as bool? ?? false),
      cleanerConfig:
          (json['cleaner_config'] as Map<String, dynamic>?) ??
          ((meta['cleaner_config'] as Map<String, dynamic>?) ?? const {}),
      showForwardHeader:
          (json['show_forward_header'] as bool?) ??
          (meta['show_forward_header'] as bool? ?? false),
      syncReplies:
          (json['sync_replies'] as bool?) ??
          (meta['sync_replies'] as bool? ?? true),
      syncEdits:
          (json['sync_edits'] as bool?) ??
          (meta['sync_edits'] as bool? ?? true),
      filters:
          (json['filters'] as List<dynamic>?)
              ?.map((e) => e.toString())
              .toList(growable: false) ??
          const [],
      filterMode: json['filter_mode'] as String? ?? 'all',
      mediaMode: json['media_mode'] as String? ?? 'all',
      delaySeconds: jsonInt(json['delay_seconds']),
      headerTemplate: json['header_template'] as String? ?? '',
      footerTemplate: json['footer_template'] as String? ?? '',
      aiConfigId: json['ai_config_id'] as String? ?? '',
      promptId:
          json['prompt_id'] as String? ?? (meta['prompt_id'] as String? ?? ''),
      blockVoice:
          (json['block_voice'] as bool?) ??
          (meta['block_voice'] as bool? ?? false),
      blockStickers:
          (json['block_stickers'] as bool?) ??
          (meta['block_stickers'] as bool? ?? false),
      removeEmojis:
          (json['remove_emojis'] as bool?) ??
          (meta['remove_emojis'] as bool? ?? false),
      syncDeletes:
          (json['sync_deletes'] as bool?) ??
          (meta['sync_deletes'] as bool? ?? false),
      ignoreEdits:
          (json['ignore_edits'] as bool?) ??
          (meta['ignore_edits'] as bool? ?? false),
      albumMode:
          (json['album_mode'] as String?) ??
          (meta['album_mode'] as String? ?? 'album'),
      version: jsonInt(json['version']),
      isEnabled:
          json['is_enabled'] as bool? ?? !(json['is_paused'] as bool? ?? false),
      ownerUserId: jsonInt(json['owner_user_id']),
    );
  }

  @override
  String toString() =>
      'ForwardRule($id, $sourceChatTitle -> $targetChatTitle, v$version)';
}

/// ---- Filters ------------------------------------------------------------

@immutable
class FilterRule {
  const FilterRule({
    required this.id,
    required this.pattern,
    required this.kind,
    this.isEnabled = true,
    this.version = 0,
  });

  final String id;
  final String pattern;
  final String kind;
  final bool isEnabled;
  final int version;

  FilterRule copyWith({
    String? id,
    String? pattern,
    String? kind,
    bool? isEnabled,
    int? version,
  }) => FilterRule(
    id: id ?? this.id,
    pattern: pattern ?? this.pattern,
    kind: kind ?? this.kind,
    isEnabled: isEnabled ?? this.isEnabled,
    version: version ?? this.version,
  );

  Map<String, dynamic> toJson() => {
    'pattern': pattern,
    'kind': kind,
    'is_enabled': isEnabled,
    'expected_version': version,
  };

  factory FilterRule.fromJson(Map<String, dynamic> json) => FilterRule(
    id: json['id'] as String,
    pattern: json['pattern'] as String? ?? '',
    kind: json['kind'] as String? ?? 'blacklist',
    isEnabled: json['is_enabled'] as bool? ?? true,
    version: jsonInt(json['version']),
  );
}

/// ---- AI configuration ---------------------------------------------------

@immutable
class AiConfig {
  const AiConfig({
    required this.id,
    required this.provider,
    required this.isEnabled,
    this.model = '',
    this.targetLanguage = '',
    this.prompt = '',
    this.baseUrl = '',
    this.version = 0,
  });

  final String id;
  final String provider;
  final String model;
  final String targetLanguage;
  final String prompt;

  /// OpenAI-compatible providers may point at a self-hosted gateway. The API
  /// key itself is never returned by the server and never stored on-device.
  final String baseUrl;
  final bool isEnabled;
  final int version;

  AiConfig copyWith({
    String? id,
    String? provider,
    String? model,
    String? targetLanguage,
    String? prompt,
    String? baseUrl,
    bool? isEnabled,
    int? version,
  }) => AiConfig(
    id: id ?? this.id,
    provider: provider ?? this.provider,
    model: model ?? this.model,
    targetLanguage: targetLanguage ?? this.targetLanguage,
    prompt: prompt ?? this.prompt,
    baseUrl: baseUrl ?? this.baseUrl,
    isEnabled: isEnabled ?? this.isEnabled,
    version: version ?? this.version,
  );

  /// Sent to the server. `api_key` is deliberately absent: the mobile client
  /// never holds provider keys; the existing bot/web flows own that.
  Map<String, dynamic> toJson() => {
    'provider': provider,
    'model': model,
    'target_language': targetLanguage,
    'prompt': prompt,
    if (baseUrl.isNotEmpty) 'base_url': baseUrl,
    'is_enabled': isEnabled,
    'expected_version': version,
  };

  factory AiConfig.fromJson(Map<String, dynamic> json) => AiConfig(
    id: json['id'] as String,
    provider: json['provider'] as String? ?? '',
    model: json['model'] as String? ?? '',
    targetLanguage: json['target_language'] as String? ?? '',
    prompt: json['prompt'] as String? ?? '',
    baseUrl: json['base_url'] as String? ?? '',
    isEnabled: json['is_enabled'] as bool? ?? false,
    version: jsonInt(json['version']),
  );
}

/// ---- Dashboard / operations --------------------------------------------

@immutable
class SystemStats {
  const SystemStats({
    required this.totalRules,
    required this.activeRules,
    required this.totalSessions,
    required this.activeSessions,
    required this.coreRunning,
    required this.version,
    this.uptimeSeconds = 0,
    this.deliveredTotal = 0,
    this.failedTotal = 0,
  });

  final int totalRules;
  final int activeRules;
  final int totalSessions;
  final int activeSessions;
  final bool coreRunning;
  final String version;
  final int uptimeSeconds;
  final int deliveredTotal;
  final int failedTotal;

  factory SystemStats.fromJson(Map<String, dynamic> json) {
    final dynamic rules = json['rules'];
    final int totalRules = rules is Map
        ? jsonInt(rules['total'], jsonInt(json['total_rules']))
        : jsonInt(json['total_rules']);
    final int activeRules = rules is Map
        ? jsonInt(rules['active'], jsonInt(json['active_rules']))
        : jsonInt(json['active_rules']);
    return SystemStats(
      totalRules: totalRules,
      activeRules: activeRules,
      totalSessions: jsonInt(
        json['total_sessions'],
        jsonInt(json['active_sessions_count']),
      ),
      activeSessions: jsonInt(
        json['active_sessions'],
        jsonInt(json['active_sessions_count']),
      ),
      coreRunning: json['core_running'] as bool? ?? false,
      version: json['version'] as String? ?? '',
      uptimeSeconds: jsonInt(json['uptime_seconds']),
      deliveredTotal: jsonInt(
        json['delivered_total'],
        jsonInt(json['total_delivered']),
      ),
      failedTotal: jsonInt(json['failed_total'], jsonInt(json['total_failed'])),
    );
  }
}

@immutable
class ServiceHealth {
  const ServiceHealth({
    required this.name,
    required this.online,
    required this.detail,
    required this.latencyMs,
  });

  final String name;
  final bool online;
  final String detail;
  final int latencyMs;

  factory ServiceHealth.fromJson(Map<String, dynamic> json) => ServiceHealth(
    name: json['name'] as String? ?? '',
    online: json['online'] as bool? ?? false,
    detail: json['detail'] as String? ?? '',
    latencyMs: jsonInt(json['latency_ms']),
  );
}

/// One entry of the server's configuration contract.
///
/// `set` is a boolean **by design**: `/api/system/env` reports whether a key is
/// configured and never its value, so a token can never be read back out of the
/// API by a client (or by a screenshot of this screen).
@immutable
class EnvCheck {
  const EnvCheck({
    required this.key,
    required this.level,
    required this.set,
    this.reason = '',
    this.hint = '',
  });

  final String key;

  /// `required`, `recommended` or `optional`.
  final String level;
  final bool set;
  final String reason;
  final String hint;

  bool get isRequired => level == 'required';

  factory EnvCheck.fromJson(Map<String, dynamic> json) => EnvCheck(
    key: json['key'] as String? ?? '',
    level: json['level'] as String? ?? 'optional',
    set: json['set'] as bool? ?? false,
    reason: json['reason'] as String? ?? '',
    hint: json['hint'] as String? ?? '',
  );
}

/// The readiness report: `GET /api/system/setup`.
///
/// This is the machine-readable answer to "is this deployment actually set up?"
/// that the CLI wizard, the web dashboard and the mobile client all read, so a
/// new operator sees the same verdict on every surface.
///
/// Parsing is deliberately lenient: every optional section defaults to empty, so
/// an older server that omits `features` or `checks` degrades to a smaller panel
/// instead of throwing a `TypeError` and blanking the screen.
@immutable
class SetupReport {
  const SetupReport({
    required this.ready,
    required this.time,
    this.deployment = '',
    this.productVersion = '',
    this.apiVersion = '',
    this.commitSha = '',
    this.features = const [],
    this.checks = const [],
    this.services = const [],
    this.missingRequired = const [],
    this.missingRecommended = const [],
    this.nextSteps = const [],
  });

  final bool ready;
  final String time;
  final String deployment;
  final String productVersion;
  final String apiVersion;
  final String commitSha;
  final List<String> features;
  final List<EnvCheck> checks;
  final List<ServiceHealth> services;
  final List<String> missingRequired;
  final List<String> missingRecommended;
  final List<String> nextSteps;

  int get onlineServices => services.where((s) => s.online).length;

  bool get hasContractGaps => missingRequired.isNotEmpty;

  factory SetupReport.fromJson(Map<String, dynamic> json) => SetupReport(
    ready: json['ready'] as bool? ?? false,
    time: json['time'] as String? ?? '',
    deployment: json['deployment'] as String? ?? '',
    productVersion: json['product_version'] as String? ?? '',
    apiVersion: json['api_version'] as String? ?? '',
    commitSha: json['commit_sha'] as String? ?? '',
    features:
        (json['features'] as List?)?.whereType<String>().toList() ?? const [],
    checks:
        (json['checks'] as List?)
            ?.whereType<Map>()
            .map((e) => EnvCheck.fromJson(e.cast<String, dynamic>()))
            .toList() ??
        const [],
    services:
        (json['services'] as List?)
            ?.whereType<Map>()
            .map((e) => ServiceHealth.fromJson(e.cast<String, dynamic>()))
            .toList() ??
        const [],
    missingRequired:
        (json['missing_required'] as List?)?.whereType<String>().toList() ??
        const [],
    missingRecommended:
        (json['missing_recommended'] as List?)?.whereType<String>().toList() ??
        const [],
    nextSteps:
        (json['next_steps'] as List?)?.whereType<String>().toList() ?? const [],
  );
}

@immutable
class LogEntry {
  const LogEntry({
    required this.timestamp,
    required this.level,
    required this.category,
    required this.message,
    this.eventCode = '',
    this.tsSeconds = 0,
    this.service = '',
    this.detail = '',
    this.ruleId = '',
    this.userId = 0,
    this.correlationId = '',
  });

  final String timestamp;
  final String level;
  final String category;
  final String message;
  final String eventCode;

  /// Unix seconds. The API's canonical field; [timestamp] is kept for the
  /// legacy string form and for display.
  final int tsSeconds;

  /// Emitting microservice (`core`, `api`, `logger`, `web`).
  final String service;

  /// Structured payload. Search matches this too, which is why the console can
  /// find an event by a value that only ever existed inside the JSON blob.
  final String detail;

  /// Which forwarding task produced this event. Empty means system-wide.
  final String ruleId;

  /// Whose action produced this event. Zero means no human actor.
  final int userId;

  /// Ties every event of one request/forwarding hop together.
  final String correlationId;

  bool get isError => level == 'error';
  bool get isWarning => level == 'warning' || level == 'warn';

  DateTime? get when {
    if (tsSeconds > 0) {
      return DateTime.fromMillisecondsSinceEpoch(tsSeconds * 1000);
    }
    return DateTime.tryParse(timestamp);
  }

  String get levelUpper => level.toUpperCase();

  factory LogEntry.fromJson(Map<String, dynamic> json) {
    final rawTs = json['ts'] ?? json['timestamp'] ?? json['time'];
    final seconds = rawTs is int
        ? rawTs
        : rawTs is num
        ? rawTs.toInt()
        : int.tryParse('$rawTs') ?? 0;
    return LogEntry(
      timestamp: rawTs is String ? rawTs : (seconds > 0 ? '$seconds' : ''),
      tsSeconds: seconds,
      level: (json['level'] as String? ?? 'info').toLowerCase(),
      category: json['category'] as String? ?? json['source'] as String? ?? '',
      message: json['message'] as String? ?? json['event'] as String? ?? '',
      eventCode: json['event_code'] as String? ?? '',
      service: json['service'] as String? ?? '',
      detail: json['detail'] as String? ?? '',
      ruleId: json['rule_id'] as String? ?? '',
      userId: jsonInt(json['user_id']),
      correlationId: json['correlation_id'] as String? ?? '',
    );
  }
}

/// One page of archived log history plus the real match count.
///
/// [total] is the number of events matching the filter across the whole
/// archive — not the size of this page — so the client can render
/// "1–50 of 12,431" and keep walking backwards until [hasMore] is false.
@immutable
class LogPage {
  const LogPage({
    required this.logs,
    required this.total,
    required this.hasMore,
    this.offset = 0,
    this.limit = 0,
  });

  final List<LogEntry> logs;
  final int total;
  final bool hasMore;
  final int offset;
  final int limit;

  bool get isEmpty => logs.isEmpty;

  factory LogPage.fromJson(Map<String, dynamic> json) {
    final raw = json['logs'];
    final logs = raw is List
        ? raw
              .whereType<Map<String, dynamic>>()
              .map(LogEntry.fromJson)
              .toList(growable: false)
        : const <LogEntry>[];
    final total = jsonInt(json['total'], logs.length);
    return LogPage(
      logs: logs,
      total: total,
      hasMore: json['has_more'] as bool? ?? false,
      offset: jsonInt(json['offset']),
      limit: jsonInt(json['limit'], logs.length),
    );
  }
}

/// Aggregate view of the archive for the current filter
/// (`GET /api/logs/stats`).
@immutable
class LogStats {
  const LogStats({
    this.total = 0,
    this.errors = 0,
    this.warnings = 0,
    this.byLevel = const {},
    this.byRule = const {},
    this.byService = const {},
    this.oldestTs = 0,
    this.newestTs = 0,
  });

  final int total;
  final int errors;
  final int warnings;
  final Map<String, int> byLevel;
  final Map<String, int> byRule;
  final Map<String, int> byService;
  final int oldestTs;
  final int newestTs;

  static Map<String, int> _counts(Object? raw) => jsonIntMap(raw);

  factory LogStats.fromJson(Map<String, dynamic> json) => LogStats(
    total: jsonInt(json['total']),
    errors: jsonInt(json['errors']),
    warnings: jsonInt(json['warnings']),
    byLevel: _counts(json['by_level']),
    byRule: _counts(json['by_rule']),
    byService: _counts(json['by_service']),
    oldestTs: jsonInt(json['oldest_ts']),
    newestTs: jsonInt(json['newest_ts']),
  );
}

/// One controllable per-task setting, described by the server's registry
/// (`GET /api/rules/settings/schema`).
///
/// The client never hardcodes setting names or their bounds: the server owns
/// validation, and this mirrors it so the editor can render the right control
/// and refuse an out-of-range value before a round-trip.
@immutable
class RuleSettingSpec {
  const RuleSettingSpec({
    required this.key,
    required this.kind,
    required this.group,
    required this.label,
    this.help = '',
    this.defaultValue,
    this.choices = const [],
    this.minimum,
    this.maximum,
    this.nullable = false,
    this.itemKind = 'any',
  });

  final String key;

  /// `bool` | `int` | `float` | `str` | `list` | `dict`.
  final String kind;
  final String group;
  final String label;
  final String help;
  final Object? defaultValue;
  final List<String> choices;
  final num? minimum;
  final num? maximum;
  final bool nullable;
  final String itemKind;

  bool get isBool => kind == 'bool';
  bool get isNumber => kind == 'int' || kind == 'float';
  bool get isList => kind == 'list';
  bool get isDict => kind == 'dict';

  factory RuleSettingSpec.fromJson(Map<String, dynamic> json) {
    final rawChoices = json['choices'];
    return RuleSettingSpec(
      key: json['key'] as String? ?? '',
      kind: json['kind'] as String? ?? 'str',
      group: json['group'] as String? ?? 'general',
      label: json['label'] as String? ?? '',
      help: json['help'] as String? ?? '',
      defaultValue: json['default'],
      choices: rawChoices is List
          ? rawChoices.map((e) => '$e').toList(growable: false)
          : const [],
      minimum: jsonNum(json['minimum']),
      maximum: jsonNum(json['maximum']),
      nullable: json['nullable'] as bool? ?? false,
      itemKind: json['item_kind'] as String? ?? 'any',
    );
  }
}

/// The full settings registry, plus the group ordering the server chose so the
/// UI presents groups in a deliberate order rather than alphabetically.
@immutable
class RuleSettingsSchema {
  const RuleSettingsSchema({
    required this.settings,
    required this.groups,
    this.version = 0,
  });

  final List<RuleSettingSpec> settings;
  final List<String> groups;
  final int version;

  Map<String, List<RuleSettingSpec>> get byGroup {
    final out = <String, List<RuleSettingSpec>>{};
    for (final spec in settings) {
      out.putIfAbsent(spec.group, () => <RuleSettingSpec>[]).add(spec);
    }
    return out;
  }

  RuleSettingSpec? specFor(String key) {
    for (final spec in settings) {
      if (spec.key == key) return spec;
    }
    return null;
  }

  factory RuleSettingsSchema.fromJson(Map<String, dynamic> json) {
    final raw = json['settings'];
    return RuleSettingsSchema(
      version: jsonInt(json['version']),
      groups: json['groups'] is List
          ? (json['groups'] as List).map((e) => '$e').toList(growable: false)
          : const [],
      settings: raw is List
          ? raw
                .whereType<Map<String, dynamic>>()
                .map(RuleSettingSpec.fromJson)
                .toList(growable: false)
          : const [],
    );
  }
}

/// Compatibility metadata advertised by `GET /api/v1/health`.
@immutable
class ServerMeta {
  const ServerMeta({required this.version, required this.apiContract});

  final String version;
  final String apiContract;

  factory ServerMeta.fromJson(Map<String, dynamic> json) => ServerMeta(
    version: json['version'] as String? ?? '',
    apiContract:
        json['api_contract'] as String? ?? json['api_version'] as String? ?? '',
  );
}

/// One centralized audit event from the observability platform's durable
/// PostgreSQL store (`GET /api/audit/events`).
///
/// Mirrors the server's AuditEventEntry proto: outcome uses the lifecycle
/// vocabulary (RECEIVED / ACCEPTED / IN_PROGRESS / SUCCEEDED / FAILED /
/// OUTCOME_UNCONFIRMED) — `OUTCOME_UNCONFIRMED` means the final result is
/// genuinely unknown (e.g. a lost downstream response), never success.
@immutable
class AuditEvent {
  const AuditEvent({
    required this.eventId,
    required this.eventType,
    required this.timestamp,
    this.outcome = '',
    this.service = '',
    this.severity = 'INFO',
    this.correlationId = '',
    this.ruleId = '',
    this.featureId = '',
    this.reasonCode = '',
    this.resultSummary = '',
    this.errorCode = '',
    this.durationMs = 0,
  });

  final String eventId;
  final String eventType;
  final String timestamp;
  final String outcome;
  final String service;
  final String severity;
  final String correlationId;
  final String ruleId;
  final String featureId;
  final String reasonCode;
  final String resultSummary;
  final String errorCode;
  final int durationMs;

  factory AuditEvent.fromJson(Map<String, dynamic> json) {
    final ts = jsonInt(json['ts_utc']);
    String timeStr;
    if (ts > 0) {
      final dt = DateTime.fromMillisecondsSinceEpoch(ts.toInt() * 1000);
      timeStr =
          '${dt.hour.toString().padLeft(2, '0')}:'
          '${dt.minute.toString().padLeft(2, '0')}:'
          '${dt.second.toString().padLeft(2, '0')}';
    } else {
      timeStr = '';
    }
    return AuditEvent(
      eventId: json['event_id'] as String? ?? '',
      eventType: json['event_type'] as String? ?? '',
      timestamp: timeStr,
      outcome: (json['outcome'] as String? ?? '').toUpperCase(),
      service: json['service'] as String? ?? '',
      severity: (json['severity'] as String? ?? 'INFO').toUpperCase(),
      correlationId: json['correlation_id'] as String? ?? '',
      ruleId: json['rule_id'] as String? ?? '',
      featureId: json['feature_id'] as String? ?? '',
      reasonCode: json['reason_code'] as String? ?? '',
      resultSummary: json['result_summary'] as String? ?? '',
      errorCode: json['error_code'] as String? ?? '',
      durationMs: jsonInt(json['duration_ms']),
    );
  }
}

/// One finding from the server's preflight diagnostics pass.
///
/// Mirrors a single entry of `GET /api/diagnostics`'s `findings` array: a
/// severity, a human sentence, and — critically — a remediation hint, so the
/// client can tell the user what to *do* rather than only what is wrong.
@immutable
class DiagnosticFinding {
  const DiagnosticFinding({
    required this.id,
    required this.severity,
    required this.scope,
    required this.title,
    required this.detail,
    required this.remediation,
  });

  final String id;

  /// error | warning | info
  final String severity;

  /// service | session | ai | rule
  final String scope;
  final String title;
  final String detail;
  final String remediation;

  bool get isError => severity == 'error';
  bool get isWarning => severity == 'warning';

  factory DiagnosticFinding.fromJson(Map<String, dynamic> json) =>
      DiagnosticFinding(
        id: json['id'] as String? ?? '',
        severity: (json['severity'] as String? ?? 'info').toLowerCase(),
        scope: json['scope'] as String? ?? '',
        title: json['title'] as String? ?? '',
        detail: json['detail'] as String? ?? '',
        remediation: json['remediation'] as String? ?? '',
      );
}

/// Full preflight report: an overall grade plus the ranked findings behind it.
@immutable
class DiagnosticsReport {
  const DiagnosticsReport({
    required this.status,
    required this.score,
    required this.errorCount,
    required this.warningCount,
    required this.infoCount,
    required this.services,
    required this.rulesScanned,
    required this.aiConfigsScanned,
    required this.findings,
  });

  /// healthy | warning | critical
  final String status;

  /// 0-100; the server floors this at zero.
  final int score;
  final int errorCount;
  final int warningCount;
  final int infoCount;
  final List<ServiceHealth> services;
  final int rulesScanned;
  final int aiConfigsScanned;
  final List<DiagnosticFinding> findings;

  bool get isHealthy => status == 'healthy';

  factory DiagnosticsReport.fromJson(Map<String, dynamic> json) {
    final counts = (json['counts'] as Map?)?.cast<String, dynamic>() ?? {};
    final scanned = (json['scanned'] as Map?)?.cast<String, dynamic>() ?? {};
    final rawServices = (json['services'] as List?) ?? const [];
    final rawFindings = (json['findings'] as List?) ?? const [];

    return DiagnosticsReport(
      status: (json['status'] as String? ?? 'healthy').toLowerCase(),
      score: jsonInt(json['score']),
      errorCount: jsonInt(counts['error']),
      warningCount: jsonInt(counts['warning']),
      infoCount: jsonInt(counts['info']),
      services: rawServices
          .whereType<Map>()
          .map((e) => ServiceHealth.fromJson(e.cast<String, dynamic>()))
          .toList(growable: false),
      rulesScanned: jsonInt(scanned['rules']),
      aiConfigsScanned: jsonInt(scanned['ai_configs']),
      findings: rawFindings
          .whereType<Map>()
          .map((e) => DiagnosticFinding.fromJson(e.cast<String, dynamic>()))
          .toList(growable: false),
    );
  }
}

/// One chat the connected Telegram account can see.
///
/// This is the read side of routing: a user picks sources and targets out of
/// their own chat list instead of remembering `-100…` ids. The id here is the
/// same string a rule stores in `source_chat_ids` / `target_chat_ids`, so a
/// picked chat round-trips without a second resolution step.
///
/// Parsing is deliberately lenient: the endpoint is newer than some servers,
/// and a row missing an optional field must render as a plain title, never
/// throw and blank the list.
@immutable
class ChatSummary {
  const ChatSummary({
    required this.chatId,
    this.title = '',
    this.username = '',
    this.chatType = '',
    this.members = 0,
    this.isForum = false,
  });

  final String chatId;
  final String title;
  final String username;

  /// `channel` | `supergroup` | `group` | `private` | `bot` | `` (unknown).
  final String chatType;
  final int members;

  /// A forum (topic-enabled supergroup): a task can target one topic, so the
  /// distinction is worth surfacing rather than flattening into "group".
  final bool isForum;

  /// What to show on a row: the chat's name, else its @username, else the id.
  String get displayTitle {
    if (title.trim().isNotEmpty) return title.trim();
    if (username.trim().isNotEmpty) return '@${username.trim()}';
    return chatId;
  }

  /// True for a person/bot rather than a broadcast or group chat. Drives the
  /// icon and the type filter, so it is derived once here.
  bool get isUserChat => chatType == 'private' || chatType == 'bot';

  /// A `t.me` link for a public chat, or '' when the chat is private. Used for
  /// the "open in Telegram" affordance; never fabricated from an id, because a
  /// numeric id does not address a public link.
  String get publicLink =>
      username.trim().isEmpty ? '' : 'https://t.me/${username.trim()}';

  factory ChatSummary.fromJson(Map<String, dynamic> json) => ChatSummary(
    chatId:
        json['chat_id']?.toString() ??
        json['id']?.toString() ??
        json['peer_id']?.toString() ??
        '',
    title: json['title']?.toString() ?? '',
    username: json['username']?.toString() ?? '',
    chatType: (json['chat_type']?.toString() ?? '').toLowerCase(),
    members: jsonInt(json['members']),
    isForum: json['is_forum'] as bool? ?? false,
  );

  @override
  bool operator ==(Object other) =>
      other is ChatSummary && other.chatId == chatId;

  @override
  int get hashCode => chatId.hashCode;

  @override
  String toString() => 'ChatSummary($chatId, $chatType)';
}

/// One page of a session's chat list (`GET /api/sessions/{id}/chats`).
///
/// [total] is the server's count of matches for the current query, so a client
/// can say "showing 100 of 431" instead of implying the list is complete.
@immutable
class ChatListPage {
  const ChatListPage({
    this.sessionId = '',
    this.chats = const [],
    this.total = 0,
    this.truncated = false,
  });

  final String sessionId;
  final List<ChatSummary> chats;
  final int total;
  final bool truncated;

  factory ChatListPage.fromJson(Map<String, dynamic> json) {
    final raw = (json['chats'] as List?) ?? const [];
    final chats = <ChatSummary>[];
    final seen = <String>{};
    for (final entry in raw) {
      if (entry is! Map) continue;
      final chat = ChatSummary.fromJson(entry.cast<String, dynamic>());
      // A repeated id would render twice and make "select all" ambiguous.
      if (chat.chatId.isEmpty || !seen.add(chat.chatId)) continue;
      chats.add(chat);
    }
    return ChatListPage(
      sessionId: json['session_id']?.toString() ?? '',
      chats: List<ChatSummary>.unmodifiable(chats),
      total: jsonInt(json['total'], chats.length),
      truncated: json['truncated'] as bool? ?? false,
    );
  }
}

/// The verdict of a dry run of one task against a sample message
/// (`POST /api/simulate`).
///
/// This is the server's own routing decision — the same engine, the same
/// per-task settings — so a difference between this and what actually happens
/// is a bug in the report, not a difference of opinion. The rendered text is
/// included on purpose: it is how a user checks that "remove emojis" or "strip
/// links" does to their message what they meant, before enabling the task.
@immutable
class RuleSimulation {
  const RuleSimulation({
    this.matched = false,
    this.matchReason = '',
    this.ruleName = '',
    this.messageCategory = '',
    this.isVip = false,
    this.vipReason = '',
    this.loopDetected = false,
    this.loopReason = '',
    this.forwardMode = '',
    this.fallbackMode = '',
    this.renderedText = '',
    this.renderedHeader = '',
    this.renderedFooter = '',
    this.deliveryTargets = const [],
    this.intermediateChannelId = '',
    this.useIntermediate = false,
  });

  final bool matched;
  final String matchReason;
  final String ruleName;
  final String messageCategory;
  final bool isVip;
  final String vipReason;
  final bool loopDetected;
  final String loopReason;
  final String forwardMode;
  final String fallbackMode;
  final String renderedText;
  final String renderedHeader;
  final String renderedFooter;
  final List<String> deliveryTargets;
  final String intermediateChannelId;
  final bool useIntermediate;

  factory RuleSimulation.fromJson(Map<String, dynamic> json) => RuleSimulation(
    matched: json['matched'] as bool? ?? false,
    matchReason: json['match_reason'] as String? ?? '',
    ruleName: json['rule_name'] as String? ?? '',
    messageCategory: json['message_category'] as String? ?? '',
    isVip: json['is_vip'] as bool? ?? false,
    vipReason: json['vip_reason'] as String? ?? '',
    loopDetected: json['loop_detected'] as bool? ?? false,
    loopReason: json['loop_reason'] as String? ?? '',
    forwardMode: json['forward_mode'] as String? ?? '',
    fallbackMode: json['fallback_mode'] as String? ?? '',
    renderedText: json['rendered_text'] as String? ?? '',
    renderedHeader: json['rendered_header'] as String? ?? '',
    renderedFooter: json['rendered_footer'] as String? ?? '',
    deliveryTargets: ((json['delivery_targets'] as List?) ?? const [])
        .map((e) => '$e')
        .toList(growable: false),
    intermediateChannelId: json['intermediate_channel_id'] as String? ?? '',
    useIntermediate: json['use_intermediate'] as bool? ?? false,
  );
}

/// One task's live delivery counters (`GET /api/queue`).
///
/// `errors` counts failures without saying where they went, which is why the
/// screen shows [lastError] beside it: a task with errors and no message is a
/// number the operator cannot act on.
@immutable
class RuleLiveStat {
  const RuleLiveStat({
    required this.ruleId,
    this.ruleName = '',
    this.isActive = false,
    this.isPaused = false,
    this.forwarded = 0,
    this.filtered = 0,
    this.errors = 0,
    this.lastError = '',
  });

  final String ruleId;
  final String ruleName;
  final bool isActive;
  final bool isPaused;
  final int forwarded;
  final int filtered;
  final int errors;
  final String lastError;

  factory RuleLiveStat.fromJson(Map<String, dynamic> json) => RuleLiveStat(
    ruleId: json['rule_id'] as String? ?? '',
    ruleName: json['rule_name'] as String? ?? '',
    isActive: json['is_active'] as bool? ?? false,
    isPaused: json['is_paused'] as bool? ?? false,
    forwarded: jsonInt(json['forwarded']),
    filtered: jsonInt(json['filtered']),
    errors: jsonInt(json['errors']),
    lastError: json['last_error'] as String? ?? '',
  );
}

/// Aggregate delivery counters for the whole account (`GET /api/queue`).
@immutable
class DeliveryStats {
  const DeliveryStats({
    this.processedTotal = 0,
    this.forwardedTotal = 0,
    this.failedTotal = 0,
    this.dedupSkippedTotal = 0,
    this.filteredTotal = 0,
    this.inQueue = 0,
    this.deadLetteredTotal = 0,
    this.retryTotal = 0,
    this.rules = const [],
    this.errors = const [],
  });

  final int processedTotal;
  final int forwardedTotal;
  final int failedTotal;
  final int dedupSkippedTotal;
  final int filteredTotal;
  final int inQueue;
  final int deadLetteredTotal;
  final int retryTotal;
  final List<RuleLiveStat> rules;
  final List<RecentDeliveryError> errors;

  factory DeliveryStats.fromJson(Map<String, dynamic> json) {
    int num_(String key) => jsonInt(json[key]);
    return DeliveryStats(
      processedTotal: num_('processed_total'),
      forwardedTotal: num_('forwarded_total'),
      failedTotal: num_('failed_total'),
      dedupSkippedTotal: num_('dedup_skipped_total'),
      filteredTotal: num_('filtered_total'),
      inQueue: num_('in_queue'),
      deadLetteredTotal: num_('dead_lettered_total'),
      retryTotal: num_('retry_total'),
      rules: ((json['rules'] as List?) ?? const [])
          .whereType<Map<String, dynamic>>()
          .map(RuleLiveStat.fromJson)
          .toList(growable: false),
      errors: ((json['errors'] as List?) ?? const [])
          .whereType<Map<String, dynamic>>()
          .map(RecentDeliveryError.fromJson)
          .toList(growable: false),
    );
  }
}

/// A delivery failure as the queue reports it: severity and category, so the
/// screen can separate "Telegram said no" from "we could not reach Telegram".
@immutable
class RecentDeliveryError {
  const RecentDeliveryError({
    required this.ruleId,
    this.errorName = '',
    this.severity = '',
    this.detail = '',
    this.category = '',
  });

  final String ruleId;
  final String errorName;
  final String severity;
  final String detail;
  final String category;

  factory RecentDeliveryError.fromJson(Map<String, dynamic> json) =>
      RecentDeliveryError(
        ruleId: json['rule_id'] as String? ?? '',
        errorName: json['error_name'] as String? ?? '',
        severity: json['severity'] as String? ?? '',
        detail: json['detail'] as String? ?? '',
        category: json['category'] as String? ?? '',
      );
}

/// One job in the dead-letter queue (`GET /api/dlq`).
///
/// Carries the source and target it failed between, because "job 4711 failed"
/// is not actionable: which route, and how many times, is.
@immutable
class DeadLetterJob {
  const DeadLetterJob({
    required this.id,
    this.ruleId = '',
    this.sourceChatId = '',
    this.sourceMessageId = 0,
    this.targetChatId = '',
    this.attempts = 0,
    this.lastError = '',
    this.errorCategory = '',
    this.deadLetteredAt = 0,
  });

  final String id;
  final String ruleId;
  final String sourceChatId;
  final int sourceMessageId;
  final String targetChatId;
  final int attempts;
  final String lastError;
  final String errorCategory;
  final int deadLetteredAt;

  factory DeadLetterJob.fromJson(Map<String, dynamic> json) => DeadLetterJob(
    id: json['id'] as String? ?? '',
    ruleId: json['rule_id'] as String? ?? '',
    sourceChatId: json['source_chat_id'] as String? ?? '',
    sourceMessageId: jsonInt(json['source_message_id']),
    targetChatId: json['target_chat_id'] as String? ?? '',
    attempts: jsonInt(json['attempts']),
    lastError: json['last_error'] as String? ?? '',
    errorCategory: json['error_category'] as String? ?? '',
    deadLetteredAt: jsonInt(json['dead_lettered_at']),
  );
}
