/// Typed models for the AutoTelegramForward Control API.
///
/// These mirror the JSON emitted by `api/internal/handlers/*.go`. The Go
/// handlers are the single authority: the client never re-derives rule
/// ownership, tenant scope or session state — it only renders what the server
/// returns for the authenticated caller.
library;

import 'package:flutter/foundation.dart';

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
    userId: (json['user_id'] as num).toInt(),
    username: json['username'] as String,
    isAdmin: json['is_admin'] as bool? ?? false,
    serverUrl: json['server_url'] as String,
    expiresAt:
        json['expires_at'] is String
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

  String get displayName =>
      firstName.isNotEmpty
          ? firstName
          : (username.isNotEmpty
              ? '@$username'
              : (phone.isNotEmpty ? phone : id));

  factory TelegramSession.fromJson(Map<String, dynamic> json) =>
      TelegramSession(
        id: json['id'] as String,
        isActive: json['is_active'] as bool? ?? false,
        phone: json['phone_number'] as String? ?? '',
        userId: json['user_id']?.toString() ?? '',
        username: json['username'] as String? ?? '',
        firstName: json['first_name'] as String? ?? '',
        apiCredentialId: json['api_credential_id'] as String? ?? '',
        ownerUserId: (json['owner_user_id'] as num?)?.toInt() ?? 0,
      );

  @override
  String toString() => 'TelegramSession($id, active=$isActive, $displayName)';
}

/// ---- Forwarding rules ---------------------------------------------------

@immutable
class ForwardRule {
  const ForwardRule({
    required this.id,
    required this.sourceChatId,
    required this.targetChatId,
    required this.isEnabled,
    this.sourceChatTitle = '',
    this.targetChatTitle = '',
    this.sessionId = '',
    this.intermediateChannelId = '',
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
  }) => ForwardRule(
    id: id ?? this.id,
    sessionId: sessionId ?? this.sessionId,
    sourceChatId: sourceChatId ?? this.sourceChatId,
    sourceChatTitle: sourceChatTitle ?? this.sourceChatTitle,
    targetChatId: targetChatId ?? this.targetChatId,
    targetChatTitle: targetChatTitle ?? this.targetChatTitle,
    intermediateChannelId: intermediateChannelId ?? this.intermediateChannelId,
    useVipRouting: useVipRouting ?? this.useVipRouting,
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

  Map<String, dynamic> toCreateJson() => {
    'session_id': sessionId,
    'source_chat_id': sourceChatId,
    'source_chat_title': sourceChatTitle,
    'target_chat_id': targetChatId,
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

  factory ForwardRule.fromJson(Map<String, dynamic> json) {
    final meta = json['metadata'] is Map ? (json['metadata'] as Map) : const {};
    final rawAddons =
        json['paid_addons'] is Map
            ? json['paid_addons'] as Map
            : (meta['paid_addons'] is Map
                ? meta['paid_addons'] as Map
                : const {});
    final Map<String, bool> parsedAddons = {};
    rawAddons.forEach((k, v) {
      if (k != null) parsedAddons[k.toString()] = v == true;
    });

    return ForwardRule(
      id: json['id'] as String,
      sessionId: json['session_id'] as String? ?? '',
      sourceChatId: json['source_chat_id']?.toString() ?? '',
      sourceChatTitle: json['source_chat_title'] as String? ?? '',
      targetChatId: json['target_chat_id']?.toString() ?? '',
      targetChatTitle: json['target_chat_title'] as String? ?? '',
      intermediateChannelId: json['intermediate_channel_id']?.toString() ?? '',
      useVipRouting: json['use_vip_routing'] as bool? ?? false,
      name: (json['name'] as String?) ?? (meta['name'] as String? ?? ''),
      topicId:
          (json['topic_id'] as num?)?.toInt() ??
          ((meta['topic_id'] as num?)?.toInt() ?? 0),
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
      editTimeLimitSeconds:
          (json['edit_time_limit_seconds'] as num?)?.toInt() ??
          ((meta['edit_time_limit_seconds'] as num?)?.toInt() ?? 0),
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
      replacements:
          meta['replacements'] is Map
              ? (meta['replacements'] as Map).map(
                (k, v) => MapEntry(k.toString(), v?.toString() ?? ''),
              )
              : const {},
      aiCreativity:
          (json['ai_creativity'] as num?)?.toDouble() ??
          ((meta['ai_creativity'] as num?)?.toDouble() ?? 0.7),
      aiMaxTokens:
          (json['ai_max_tokens'] as num?)?.toInt() ??
          ((meta['ai_max_tokens'] as num?)?.toInt() ?? 600),
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
      delaySeconds: (json['delay_seconds'] as num?)?.toInt() ?? 0,
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
      version: (json['version'] as num?)?.toInt() ?? 0,
      isEnabled:
          json['is_enabled'] as bool? ?? !(json['is_paused'] as bool? ?? false),
      ownerUserId: (json['owner_user_id'] as num?)?.toInt() ?? 0,
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
    version: (json['version'] as num?)?.toInt() ?? 0,
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
    version: (json['version'] as num?)?.toInt() ?? 0,
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
    final int totalRules =
        rules is Map
            ? (rules['total'] as num?)?.toInt() ??
                (json['total_rules'] as num?)?.toInt() ??
                0
            : (json['total_rules'] as num?)?.toInt() ?? 0;
    final int activeRules =
        rules is Map
            ? (rules['active'] as num?)?.toInt() ??
                (json['active_rules'] as num?)?.toInt() ??
                0
            : (json['active_rules'] as num?)?.toInt() ?? 0;
    return SystemStats(
      totalRules: totalRules,
      activeRules: activeRules,
      totalSessions:
          (json['total_sessions'] as num?)?.toInt() ??
          (json['active_sessions_count'] as num?)?.toInt() ??
          0,
      activeSessions:
          (json['active_sessions'] as num?)?.toInt() ??
          (json['active_sessions_count'] as num?)?.toInt() ??
          0,
      coreRunning: json['core_running'] as bool? ?? false,
      version: json['version'] as String? ?? '',
      uptimeSeconds: (json['uptime_seconds'] as num?)?.toInt() ?? 0,
      deliveredTotal:
          (json['delivered_total'] as num?)?.toInt() ??
          (json['total_delivered'] as num?)?.toInt() ??
          0,
      failedTotal:
          (json['failed_total'] as num?)?.toInt() ??
          (json['total_failed'] as num?)?.toInt() ??
          0,
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
    latencyMs: (json['latency_ms'] as num?)?.toInt() ?? 0,
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
  });

  final String timestamp;
  final String level;
  final String category;
  final String message;
  final String eventCode;

  factory LogEntry.fromJson(Map<String, dynamic> json) => LogEntry(
    timestamp:
        json['timestamp'] as String? ??
        json['ts'] as String? ??
        json['time'] as String? ??
        '',
    level: (json['level'] as String? ?? 'info').toLowerCase(),
    category: json['category'] as String? ?? json['source'] as String? ?? '',
    message: json['message'] as String? ?? json['event'] as String? ?? '',
    eventCode: json['event_code'] as String? ?? '',
  );
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
    final ts = json['ts_utc'] as num? ?? 0;
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
      durationMs: (json['duration_ms'] as num?)?.toInt() ?? 0,
    );
  }
}
