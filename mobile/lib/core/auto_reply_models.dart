/// Models for the AI private-message auto-reply surface.
///
/// Contract source of truth: `api/internal/handlers/auto_reply*.go`. Every
/// field name here is the snake_case key the server sends and expects, so the
/// models double as the request bodies — there is no second mapping layer that
/// could drift from the API.
///
/// Two invariants worth stating because they are easy to get wrong:
///   * no model carries an owner/user id. The server derives identity from the
///     bearer token, and a client that sent one would be claiming another
///     tenant's data.
///   * `null` and "absent" are read as the documented default, never as an
///     error: one missing field must not blank a settings form.
library;

import 'json_cast.dart';

/// The four ways targeting can decide who gets a reply.
class AutoReplyTargetMode {
  const AutoReplyTargetMode._();

  /// Every private chat that writes to the account.
  static const String everyone = 'EVERYONE';

  /// Only the chats on the target list that are enabled.
  static const String selectedOnly = 'SELECTED_ONLY';

  /// Every private chat except the targets that are switched off.
  static const String allExcept = 'ALL_EXCEPT';

  /// Only chats added by hand, regardless of the list.
  static const String manual = 'MANUAL';

  static const List<String> all = [everyone, selectedOnly, allExcept, manual];

  /// Accepts the server's value in any case; anything unknown degrades to
  /// [everyone] so a typo cannot silently disable every reply.
  static String normalize(Object? raw) {
    final value = (raw is String ? raw : '').trim().toUpperCase();
    return all.contains(value) ? value : everyone;
  }

  /// The catalog key that names [mode] in the user's language.
  static String labelKey(String mode) => switch (normalize(mode)) {
    selectedOnly => 'ar_mode_selected',
    allExcept => 'ar_mode_all_except',
    manual => 'ar_mode_manual',
    _ => 'ar_mode_everyone',
  };

  /// One line explaining what [mode] means for the list below it.
  static String hintKey(String mode) => switch (normalize(mode)) {
    selectedOnly => 'ar_mode_selected_hint',
    allExcept => 'ar_mode_all_except_hint',
    manual => 'ar_mode_manual_hint',
    _ => 'ar_mode_everyone_hint',
  };
}

/// The account-wide auto-reply configuration (`GET/PUT /api/auto-reply/settings`).
///
/// One flat object on purpose: the server accepts a partial update masked by
/// `update_mask`, and a flat model means the mask can name any field without a
/// nested-path syntax.
class AutoReplySettings {
  const AutoReplySettings({
    this.enabled = false,
    this.aiConfigId = '',
    this.promptId = '',
    this.targetMode = AutoReplyTargetMode.everyone,
    this.language = '',
    this.tone = 'friendly',
    this.responseLength = 'medium',
    this.temperature = 0.7,
    this.maxTokens = 400,
    this.contextMessages = 8,
    this.memoryEnabled = true,
    this.memoryMaxFacts = 20,
    this.extraInstructions = '',
    this.maxReplyChars = 1000,
    this.typingSimulation = true,
    this.typingDelayMin = 1,
    this.typingDelayMax = 3,
    this.cooldownSeconds = 60,
    this.dailyLimitPerChat = 50,
    this.rateLimitPerMinute = 6,
    this.maxConcurrency = 2,
    this.maxAttempts = 3,
    this.retryBackoffSeconds = 30,
    this.quietHoursStart = '',
    this.quietHoursEnd = '',
    this.timezoneOffsetMinutes = 0,
    this.activeDays = const [0, 1, 2, 3, 4, 5, 6],
    this.fallbackText = '',
    this.ignoreBots = true,
    this.version = 0,
  });

  final bool enabled;

  /// Which stored AI configuration answers. Empty means "not chosen yet" and
  /// the server refuses to run rather than picking one silently.
  final String aiConfigId;

  /// The default prompt; a target or a rule may override it.
  final String promptId;
  final String targetMode;

  /// Reply language code, empty means "match the sender".
  final String language;
  final String tone;
  final String responseLength;
  final double temperature;
  final int maxTokens;

  /// How many previous messages of the conversation the model sees.
  final int contextMessages;
  final bool memoryEnabled;
  final int memoryMaxFacts;

  /// Appended to every prompt as operator guidance.
  final String extraInstructions;
  final int maxReplyChars;
  final bool typingSimulation;
  final int typingDelayMin;
  final int typingDelayMax;
  final int cooldownSeconds;
  final int dailyLimitPerChat;
  final int rateLimitPerMinute;
  final int maxConcurrency;
  final int maxAttempts;
  final int retryBackoffSeconds;

  /// `HH:MM` in the account's own offset; empty disables quiet hours.
  final String quietHoursStart;
  final String quietHoursEnd;

  /// Minutes east of UTC, so the server never guesses a timezone.
  final int timezoneOffsetMinutes;

  /// Days replies are allowed, Monday = 0 … Sunday = 6.
  final List<int> activeDays;
  final String fallbackText;
  final bool ignoreBots;

  /// Optimistic-concurrency token echoed back on save.
  final int version;

  bool get quietHoursSet =>
      quietHoursStart.trim().isNotEmpty && quietHoursEnd.trim().isNotEmpty;

  AutoReplySettings copyWith({
    bool? enabled,
    String? aiConfigId,
    String? promptId,
    String? targetMode,
    String? language,
    String? tone,
    String? responseLength,
    double? temperature,
    int? maxTokens,
    int? contextMessages,
    bool? memoryEnabled,
    int? memoryMaxFacts,
    String? extraInstructions,
    int? maxReplyChars,
    bool? typingSimulation,
    int? typingDelayMin,
    int? typingDelayMax,
    int? cooldownSeconds,
    int? dailyLimitPerChat,
    int? rateLimitPerMinute,
    int? maxConcurrency,
    int? maxAttempts,
    int? retryBackoffSeconds,
    String? quietHoursStart,
    String? quietHoursEnd,
    int? timezoneOffsetMinutes,
    List<int>? activeDays,
    String? fallbackText,
    bool? ignoreBots,
    int? version,
  }) => AutoReplySettings(
    enabled: enabled ?? this.enabled,
    aiConfigId: aiConfigId ?? this.aiConfigId,
    promptId: promptId ?? this.promptId,
    targetMode: targetMode ?? this.targetMode,
    language: language ?? this.language,
    tone: tone ?? this.tone,
    responseLength: responseLength ?? this.responseLength,
    temperature: temperature ?? this.temperature,
    maxTokens: maxTokens ?? this.maxTokens,
    contextMessages: contextMessages ?? this.contextMessages,
    memoryEnabled: memoryEnabled ?? this.memoryEnabled,
    memoryMaxFacts: memoryMaxFacts ?? this.memoryMaxFacts,
    extraInstructions: extraInstructions ?? this.extraInstructions,
    maxReplyChars: maxReplyChars ?? this.maxReplyChars,
    typingSimulation: typingSimulation ?? this.typingSimulation,
    typingDelayMin: typingDelayMin ?? this.typingDelayMin,
    typingDelayMax: typingDelayMax ?? this.typingDelayMax,
    cooldownSeconds: cooldownSeconds ?? this.cooldownSeconds,
    dailyLimitPerChat: dailyLimitPerChat ?? this.dailyLimitPerChat,
    rateLimitPerMinute: rateLimitPerMinute ?? this.rateLimitPerMinute,
    maxConcurrency: maxConcurrency ?? this.maxConcurrency,
    maxAttempts: maxAttempts ?? this.maxAttempts,
    retryBackoffSeconds: retryBackoffSeconds ?? this.retryBackoffSeconds,
    quietHoursStart: quietHoursStart ?? this.quietHoursStart,
    quietHoursEnd: quietHoursEnd ?? this.quietHoursEnd,
    timezoneOffsetMinutes: timezoneOffsetMinutes ?? this.timezoneOffsetMinutes,
    activeDays: activeDays ?? this.activeDays,
    fallbackText: fallbackText ?? this.fallbackText,
    ignoreBots: ignoreBots ?? this.ignoreBots,
    version: version ?? this.version,
  );

  /// Every field in snake_case, exactly as the server stores it.
  Map<String, dynamic> toJson() => {
    'enabled': enabled,
    'ai_config_id': aiConfigId,
    'prompt_id': promptId,
    'target_mode': AutoReplyTargetMode.normalize(targetMode),
    'language': language,
    'tone': tone,
    'response_length': responseLength,
    'temperature': temperature,
    'max_tokens': maxTokens,
    'context_messages': contextMessages,
    'memory_enabled': memoryEnabled,
    'memory_max_facts': memoryMaxFacts,
    'extra_instructions': extraInstructions,
    'max_reply_chars': maxReplyChars,
    'typing_simulation': typingSimulation,
    'typing_delay_min': typingDelayMin,
    'typing_delay_max': typingDelayMax,
    'cooldown_seconds': cooldownSeconds,
    'daily_limit_per_chat': dailyLimitPerChat,
    'rate_limit_per_minute': rateLimitPerMinute,
    'max_concurrency': maxConcurrency,
    'max_attempts': maxAttempts,
    'retry_backoff_seconds': retryBackoffSeconds,
    'quiet_hours_start': quietHoursStart,
    'quiet_hours_end': quietHoursEnd,
    'timezone_offset_minutes': timezoneOffsetMinutes,
    'active_days': activeDays,
    'fallback_text': fallbackText,
    'ignore_bots': ignoreBots,
    'version': version,
  };

  /// The PUT body: the settings object plus the fields that actually changed.
  ///
  /// [fields] empty means "write everything" — the form's save button passes
  /// the full key set, while a lone switch passes the one name it touched so a
  /// concurrent edit elsewhere cannot be overwritten with stale values.
  Map<String, dynamic> updateBody(List<String> fields) => {
    ...toJson(),
    if (fields.isNotEmpty) 'update_mask': fields,
  };

  factory AutoReplySettings.fromJson(Map<String, dynamic> json) {
    final days = ((json['active_days'] as List?) ?? const [])
        .map((e) => jsonInt(e, -1))
        .where((d) => d >= 0 && d <= 6)
        .toList(growable: false);
    return AutoReplySettings(
      enabled: json['enabled'] as bool? ?? false,
      aiConfigId: json['ai_config_id'] as String? ?? '',
      promptId: json['prompt_id'] as String? ?? '',
      targetMode: AutoReplyTargetMode.normalize(json['target_mode']),
      language: json['language'] as String? ?? '',
      tone: json['tone'] as String? ?? 'friendly',
      responseLength: json['response_length'] as String? ?? 'medium',
      temperature: jsonDouble(json['temperature'], 0.7),
      maxTokens: jsonInt(json['max_tokens'], 400),
      contextMessages: jsonInt(json['context_messages'], 8),
      memoryEnabled: json['memory_enabled'] as bool? ?? true,
      memoryMaxFacts: jsonInt(json['memory_max_facts'], 20),
      extraInstructions: json['extra_instructions'] as String? ?? '',
      maxReplyChars: jsonInt(json['max_reply_chars'], 1000),
      typingSimulation: json['typing_simulation'] as bool? ?? true,
      typingDelayMin: jsonInt(json['typing_delay_min'], 1),
      typingDelayMax: jsonInt(json['typing_delay_max'], 3),
      cooldownSeconds: jsonInt(json['cooldown_seconds'], 60),
      dailyLimitPerChat: jsonInt(json['daily_limit_per_chat'], 50),
      rateLimitPerMinute: jsonInt(json['rate_limit_per_minute'], 6),
      maxConcurrency: jsonInt(json['max_concurrency'], 2),
      maxAttempts: jsonInt(json['max_attempts'], 3),
      retryBackoffSeconds: jsonInt(json['retry_backoff_seconds'], 30),
      quietHoursStart: json['quiet_hours_start'] as String? ?? '',
      quietHoursEnd: json['quiet_hours_end'] as String? ?? '',
      timezoneOffsetMinutes: jsonInt(json['timezone_offset_minutes']),
      activeDays: days.isEmpty ? const [0, 1, 2, 3, 4, 5, 6] : days,
      fallbackText: json['fallback_text'] as String? ?? '',
      ignoreBots: json['ignore_bots'] as bool? ?? true,
      version: jsonInt(json['version']),
    );
  }
}

/// One reusable reply instruction (`/api/auto-reply/prompts`).
class AutoReplyPrompt {
  const AutoReplyPrompt({
    required this.id,
    required this.name,
    this.description = '',
    this.systemPrompt = '',
    this.userPromptTemplate = '',
    this.language = '',
    this.tone = '',
    this.responseLength = '',
    this.tags = const [],
    this.category = '',
    this.isDefault = false,
    this.isArchived = false,
    this.usageCount = 0,
    this.createdAt = 0,
    this.updatedAt = 0,
  });

  final String id;
  final String name;
  final String description;
  final String systemPrompt;

  /// Must contain `{text}`: that is where the inbound message is substituted.
  final String userPromptTemplate;
  final String language;
  final String tone;
  final String responseLength;
  final List<String> tags;
  final String category;
  final bool isDefault;
  final bool isArchived;
  final int usageCount;
  final int createdAt;
  final int updatedAt;

  /// Read-only fields are omitted: `usage_count` is measured by the server and
  /// `id`/timestamps are not the client's to write.
  Map<String, dynamic> toCreateJson() => {
    'name': name,
    'description': description,
    'system_prompt': systemPrompt,
    'user_prompt_template': userPromptTemplate,
    'language': language,
    'tone': tone,
    'response_length': responseLength,
    'tags': tags,
    'category': category,
    'is_default': isDefault,
    'is_archived': isArchived,
  };

  Map<String, dynamic> toUpdateJson() => toCreateJson();

  AutoReplyPrompt copyWith({
    String? id,
    String? name,
    String? description,
    String? systemPrompt,
    String? userPromptTemplate,
    String? language,
    String? tone,
    String? responseLength,
    List<String>? tags,
    String? category,
    bool? isDefault,
    bool? isArchived,
    int? usageCount,
    int? createdAt,
    int? updatedAt,
  }) => AutoReplyPrompt(
    id: id ?? this.id,
    name: name ?? this.name,
    description: description ?? this.description,
    systemPrompt: systemPrompt ?? this.systemPrompt,
    userPromptTemplate: userPromptTemplate ?? this.userPromptTemplate,
    language: language ?? this.language,
    tone: tone ?? this.tone,
    responseLength: responseLength ?? this.responseLength,
    tags: tags ?? this.tags,
    category: category ?? this.category,
    isDefault: isDefault ?? this.isDefault,
    isArchived: isArchived ?? this.isArchived,
    usageCount: usageCount ?? this.usageCount,
    createdAt: createdAt ?? this.createdAt,
    updatedAt: updatedAt ?? this.updatedAt,
  );

  factory AutoReplyPrompt.fromJson(Map<String, dynamic> json) =>
      AutoReplyPrompt(
        id: json['id'] as String? ?? '',
        name: json['name'] as String? ?? '',
        description: json['description'] as String? ?? '',
        systemPrompt: json['system_prompt'] as String? ?? '',
        userPromptTemplate: json['user_prompt_template'] as String? ?? '',
        language: json['language'] as String? ?? '',
        tone: json['tone'] as String? ?? '',
        responseLength: json['response_length'] as String? ?? '',
        tags: ((json['tags'] as List?) ?? const []).whereType<String>().toList(
          growable: false,
        ),
        category: json['category'] as String? ?? '',
        isDefault: json['is_default'] as bool? ?? false,
        isArchived: json['is_archived'] as bool? ?? false,
        usageCount: jsonInt(json['usage_count']),
        createdAt: jsonInt(json['created_at']),
        updatedAt: jsonInt(json['updated_at']),
      );
}

/// The reply of `POST /api/auto-reply/prompts/import`.
class AutoReplyImportResult {
  const AutoReplyImportResult({
    this.imported = 0,
    this.skipped = 0,
    this.prompts = const [],
  });

  final int imported;
  final int skipped;
  final List<AutoReplyPrompt> prompts;

  factory AutoReplyImportResult.fromJson(Map<String, dynamic> json) =>
      AutoReplyImportResult(
        imported: jsonInt(json['imported']),
        skipped: jsonInt(json['skipped']),
        prompts: ((json['prompts'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(AutoReplyPrompt.fromJson)
            .toList(growable: false),
      );
}

/// One chat the reply engine knows about (`/api/auto-reply/targets`).
class AutoReplyTarget {
  const AutoReplyTarget({
    required this.chatId,
    this.title = '',
    this.username = '',
    this.isEnabled = true,
    this.promptId = '',
    this.note = '',
    this.priority = 0,
    this.addedAt = 0,
  });

  final String chatId;
  final String title;
  final String username;

  /// Under SELECTED_ONLY an enabled row is answered; under ALL_EXCEPT a
  /// disabled row is the exception.
  final bool isEnabled;
  final String promptId;
  final String note;
  final int priority;
  final int addedAt;

  /// The best available human label; never empty for a real chat.
  String get label => title.trim().isNotEmpty
      ? title
      : (username.isNotEmpty ? '@$username' : chatId);

  Map<String, dynamic> toAddJson() => {
    'chat_id': chatId,
    'title': title,
    'username': username,
    'is_enabled': isEnabled,
    'prompt_id': promptId,
    'note': note,
    'priority': priority,
  };

  Map<String, dynamic> toUpdateJson() => {
    'is_enabled': isEnabled,
    'title': title,
    'note': note,
    'prompt_id': promptId,
    'priority': priority,
  };

  AutoReplyTarget copyWith({
    String? chatId,
    String? title,
    String? username,
    bool? isEnabled,
    String? promptId,
    String? note,
    int? priority,
    int? addedAt,
  }) => AutoReplyTarget(
    chatId: chatId ?? this.chatId,
    title: title ?? this.title,
    username: username ?? this.username,
    isEnabled: isEnabled ?? this.isEnabled,
    promptId: promptId ?? this.promptId,
    note: note ?? this.note,
    priority: priority ?? this.priority,
    addedAt: addedAt ?? this.addedAt,
  );

  factory AutoReplyTarget.fromJson(Map<String, dynamic> json) =>
      AutoReplyTarget(
        chatId: '${json['chat_id'] ?? ''}',
        title: json['title'] as String? ?? '',
        username: json['username'] as String? ?? '',
        isEnabled: json['is_enabled'] as bool? ?? true,
        promptId: json['prompt_id'] as String? ?? '',
        note: json['note'] as String? ?? '',
        priority: jsonInt(json['priority']),
        addedAt: jsonInt(json['added_at']),
      );
}

/// One page of targets plus the two counts the header needs.
class AutoReplyTargetPage {
  const AutoReplyTargetPage({
    this.targets = const [],
    this.total = 0,
    this.enabled = 0,
  });

  final List<AutoReplyTarget> targets;
  final int total;
  final int enabled;

  factory AutoReplyTargetPage.fromJson(Map<String, dynamic> json) =>
      AutoReplyTargetPage(
        targets: ((json['targets'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(AutoReplyTarget.fromJson)
            .toList(growable: false),
        total: jsonInt(json['total']),
        enabled: jsonInt(json['enabled']),
      );
}

/// The answer to `POST /api/auto-reply/targets/resolve`.
///
/// [found] is separate from [target] because "no such chat" is a normal answer,
/// not an error, and the UI has to say exactly that.
class AutoReplyResolveResult {
  const AutoReplyResolveResult({this.found = false, this.target});

  final bool found;
  final AutoReplyTarget? target;

  factory AutoReplyResolveResult.fromJson(Map<String, dynamic> json) {
    final raw = json['target'];
    return AutoReplyResolveResult(
      found: json['found'] as bool? ?? false,
      target: raw is Map
          ? AutoReplyTarget.fromJson(raw.cast<String, dynamic>())
          : null,
    );
  }
}

/// Where a rule sends the conversation: answer it, or leave it alone.
class AutoReplyAction {
  const AutoReplyAction._();

  static const String reply = 'REPLY';
  static const String skip = 'SKIP';
  static const List<String> all = [reply, skip];

  static String normalize(Object? raw) {
    final value = (raw is String ? raw : '').trim().toUpperCase();
    return all.contains(value) ? value : reply;
  }

  static String labelKey(String action) =>
      normalize(action) == skip ? 'ar_action_skip' : 'ar_action_reply';
}

/// One targeting rule (`/api/auto-reply/rules`), evaluated in [priority] order.
class AutoReplyRule {
  const AutoReplyRule({
    required this.id,
    required this.name,
    this.isEnabled = true,
    this.priority = 0,
    this.action = AutoReplyAction.reply,
    this.conditions = const {},
    this.exclusions = const {},
    this.promptId = '',
    this.aiConfigId = '',
    this.cooldownSeconds = -1,
    this.language = '',
    this.tone = '',
    this.responseLength = '',
    this.temperature = 0,
    this.maxTokens = 0,
  });

  final String id;
  final String name;
  final bool isEnabled;

  /// Lower runs first. The list is reordered by dragging (or the arrow buttons)
  /// and persisted through `POST /rules/reorder`.
  final int priority;
  final String action;

  /// Keyword conditions, e.g. `{"keywords": ["price", "cost"]}`.
  final Map<String, dynamic> conditions;

  /// Keywords that veto the rule even when [conditions] matched.
  final Map<String, dynamic> exclusions;
  final String promptId;
  final String aiConfigId;

  /// `-1` inherits the account default — a real value, not "unset".
  final int cooldownSeconds;
  final String language;
  final String tone;
  final String responseLength;

  /// `0` means inherit; temperature is otherwise 0..2.
  final double temperature;
  final int maxTokens;

  List<String> get keywords => ruleKeywords(conditions);
  List<String> get exclusionKeywords => ruleKeywords(exclusions);

  Map<String, dynamic> toJson() => {
    'name': name,
    'is_enabled': isEnabled,
    'priority': priority,
    'action': AutoReplyAction.normalize(action),
    'conditions': conditions,
    'exclusions': exclusions,
    'prompt_id': promptId,
    'ai_config_id': aiConfigId,
    'cooldown_seconds': cooldownSeconds,
    'language': language,
    'tone': tone,
    'response_length': responseLength,
    'temperature': temperature,
    'max_tokens': maxTokens,
  };

  AutoReplyRule copyWith({
    String? id,
    String? name,
    bool? isEnabled,
    int? priority,
    String? action,
    Map<String, dynamic>? conditions,
    Map<String, dynamic>? exclusions,
    String? promptId,
    String? aiConfigId,
    int? cooldownSeconds,
    String? language,
    String? tone,
    String? responseLength,
    double? temperature,
    int? maxTokens,
  }) => AutoReplyRule(
    id: id ?? this.id,
    name: name ?? this.name,
    isEnabled: isEnabled ?? this.isEnabled,
    priority: priority ?? this.priority,
    action: action ?? this.action,
    conditions: conditions ?? this.conditions,
    exclusions: exclusions ?? this.exclusions,
    promptId: promptId ?? this.promptId,
    aiConfigId: aiConfigId ?? this.aiConfigId,
    cooldownSeconds: cooldownSeconds ?? this.cooldownSeconds,
    language: language ?? this.language,
    tone: tone ?? this.tone,
    responseLength: responseLength ?? this.responseLength,
    temperature: temperature ?? this.temperature,
    maxTokens: maxTokens ?? this.maxTokens,
  );

  factory AutoReplyRule.fromJson(Map<String, dynamic> json) => AutoReplyRule(
    id: json['id'] as String? ?? '',
    name: json['name'] as String? ?? '',
    isEnabled: json['is_enabled'] as bool? ?? true,
    priority: jsonInt(json['priority']),
    action: AutoReplyAction.normalize(json['action']),
    conditions:
        (json['conditions'] as Map?)?.cast<String, dynamic>() ?? const {},
    exclusions:
        (json['exclusions'] as Map?)?.cast<String, dynamic>() ?? const {},
    promptId: json['prompt_id'] as String? ?? '',
    aiConfigId: json['ai_config_id'] as String? ?? '',
    cooldownSeconds: jsonInt(json['cooldown_seconds'], -1),
    language: json['language'] as String? ?? '',
    tone: json['tone'] as String? ?? '',
    responseLength: json['response_length'] as String? ?? '',
    temperature: jsonDouble(json['temperature']),
    maxTokens: jsonInt(json['max_tokens']),
  );
}

/// Reads a keyword list out of a condition object.
///
/// The server stores `{"keywords": [...]}`, but an older row (or a hand-edited
/// export) may carry a single string or a comma-separated list. Reading all
/// three shapes is what keeps the editor from silently dropping conditions.
List<String> ruleKeywords(Map<String, dynamic> source) {
  final raw = source['keywords'];
  if (raw is List) {
    return raw
        .map((e) => '$e'.trim())
        .where((e) => e.isNotEmpty)
        .toList(growable: false);
  }
  if (raw is String) {
    return raw
        .split(',')
        .map((e) => e.trim())
        .where((e) => e.isNotEmpty)
        .toList(growable: false);
  }
  return const [];
}

/// The condition object for a keyword list, or an empty map for no keywords.
Map<String, dynamic> ruleConditions(List<String> keywords) => keywords.isEmpty
    ? const <String, dynamic>{}
    : <String, dynamic>{'keywords': keywords};

/// One delivery attempt (`/api/auto-reply/jobs`).
class AutoReplyJob {
  const AutoReplyJob({
    required this.id,
    this.chatId = '',
    this.chatTitle = '',
    this.status = '',
    this.attempt = 0,
    this.maxAttempts = 0,
    this.error = '',
    this.errorCode = '',
    this.inboundText = '',
    this.replyText = '',
    this.correlationId = '',
    this.latencyMs = 0,
    this.createdAt = 0,
    this.sentAt = 0,
    this.sourceMessageId = 0,
  });

  final String id;
  final String chatId;
  final String chatTitle;
  final String status;
  final int attempt;
  final int maxAttempts;
  final String error;
  final String errorCode;
  final String inboundText;
  final String replyText;
  final String correlationId;
  final int latencyMs;
  final int createdAt;
  final int sentAt;
  final int sourceMessageId;

  String get chatLabel => chatTitle.trim().isNotEmpty
      ? chatTitle
      : (chatId.isNotEmpty ? chatId : id);

  /// True while the engine may still act on the job, which is when retry and
  /// cancel are meaningful.
  bool get isActive {
    final s = status.toUpperCase();
    return s == 'PENDING' ||
        s == 'QUEUED' ||
        s == 'RETRYING' ||
        s == 'RETRY_WAIT' ||
        s == 'PROCESSING';
  }

  bool get isDeadLetter {
    final s = status.toUpperCase();
    return s == 'DEAD_LETTER' || s == 'DEAD';
  }

  factory AutoReplyJob.fromJson(Map<String, dynamic> json) => AutoReplyJob(
    id: '${json['id'] ?? ''}',
    chatId: '${json['chat_id'] ?? ''}',
    chatTitle: json['chat_title'] as String? ?? '',
    status: json['status'] as String? ?? '',
    attempt: jsonInt(json['attempt']),
    maxAttempts: jsonInt(json['max_attempts']),
    error: json['error'] as String? ?? '',
    errorCode: json['error_code'] as String? ?? '',
    inboundText: json['inbound_text'] as String? ?? '',
    replyText: json['reply_text'] as String? ?? '',
    correlationId: json['correlation_id'] as String? ?? '',
    latencyMs: jsonInt(json['latency_ms']),
    createdAt: jsonInt(json['created_at']),
    sentAt: jsonInt(json['sent_at']),
    sourceMessageId: jsonInt(json['source_message_id']),
  );
}

/// One page of jobs plus the match count.
class AutoReplyJobPage {
  const AutoReplyJobPage({this.jobs = const [], this.total = 0});

  final List<AutoReplyJob> jobs;
  final int total;

  factory AutoReplyJobPage.fromJson(Map<String, dynamic> json) =>
      AutoReplyJobPage(
        jobs: ((json['jobs'] as List?) ?? const [])
            .whereType<Map<String, dynamic>>()
            .map(AutoReplyJob.fromJson)
            .toList(growable: false),
        total: jsonInt(json['total']),
      );
}

/// The counters behind `/api/auto-reply/stats`.
class AutoReplyStats {
  const AutoReplyStats({
    this.sentToday = 0,
    this.sentTotal = 0,
    this.pending = 0,
    this.retrying = 0,
    this.deadLetter = 0,
    this.chatsEnabled = 0,
    this.prompts = 0,
    this.rules = 0,
    this.averageLatencyMs = 0,
    this.lastSentAt = 0,
    this.statuses = const {},
  });

  final int sentToday;
  final int sentTotal;
  final int pending;
  final int retrying;
  final int deadLetter;
  final int chatsEnabled;
  final int prompts;
  final int rules;
  final double averageLatencyMs;
  final int lastSentAt;
  final Map<String, int> statuses;

  factory AutoReplyStats.fromJson(Map<String, dynamic> json) => AutoReplyStats(
    sentToday: jsonInt(json['sent_today']),
    sentTotal: jsonInt(json['sent_total']),
    pending: jsonInt(json['pending']),
    retrying: jsonInt(json['retrying']),
    deadLetter: jsonInt(json['dead_letter']),
    chatsEnabled: jsonInt(json['chats_enabled']),
    prompts: jsonInt(json['prompts']),
    rules: jsonInt(json['rules']),
    averageLatencyMs: jsonDouble(json['average_latency_ms']),
    lastSentAt: jsonInt(json['last_sent_at']),
    statuses: jsonIntMap(json['statuses']),
  );
}

/// The verdict of a dry run (`POST /api/auto-reply/preview`).
///
/// `ok` says whether the model answered; [replyText] is what it would send.
/// Nothing is delivered by a preview.
class AutoReplyPreview {
  const AutoReplyPreview({
    this.ok = false,
    this.replyText = '',
    this.error = '',
    this.errorCode = '',
    this.provider = '',
    this.model = '',
    this.latencyMs = 0,
    this.tokens = 0,
  });

  final bool ok;
  final String replyText;
  final String error;
  final String errorCode;
  final String provider;
  final String model;
  final int latencyMs;
  final int tokens;

  factory AutoReplyPreview.fromJson(Map<String, dynamic> json) {
    final result = (json['result'] as Map?)?.cast<String, dynamic>() ?? json;
    return AutoReplyPreview(
      ok: result['ok'] as bool? ?? false,
      replyText: result['reply_text'] as String? ?? '',
      error: result['error'] as String? ?? '',
      errorCode: result['error_code'] as String? ?? '',
      provider: result['provider'] as String? ?? '',
      model: result['model'] as String? ?? '',
      latencyMs: jsonInt(result['latency_ms']),
      tokens: jsonInt(result['tokens']),
    );
  }
}

/// The catalog key that names a job status, or '' when the server invented one.
///
/// Returning '' keeps the screen honest: it shows the raw status as data rather
/// than pretending an unknown state is "pending".
String autoReplyStatusKey(String status) =>
    switch (status.trim().toUpperCase()) {
      'SENT' || 'DELIVERED' || 'SUCCEEDED' => 'ar_status_sent',
      'PENDING' || 'QUEUED' => 'ar_status_pending',
      'RETRYING' || 'RETRY_WAIT' => 'ar_status_retrying',
      'PROCESSING' => 'ar_status_processing',
      'DEAD_LETTER' || 'DEAD' => 'ar_status_dead',
      'FAILED' => 'ar_status_failed',
      'CANCELLED' || 'CANCELED' => 'ar_status_cancelled',
      _ => '',
    };

/// The catalog key for a tone value from the server ('' when unknown).
String autoReplyToneKey(String tone) => switch (tone.trim().toLowerCase()) {
  'friendly' => 'ar_tone_friendly',
  'casual' => 'ar_tone_casual',
  'professional' => 'ar_tone_professional',
  'formal' => 'ar_tone_formal',
  'playful' => 'ar_tone_playful',
  'empathetic' => 'ar_tone_empathetic',
  'direct' => 'ar_tone_direct',
  _ => '',
};

/// The catalog key for a response length ('' when unknown).
String autoReplyLengthKey(String length) =>
    switch (length.trim().toLowerCase()) {
      'short' => 'ar_length_short',
      'medium' => 'ar_length_medium',
      'long' => 'ar_length_long',
      _ => '',
    };

/// The catalog key for a reply language code, for the languages the catalogs
/// name. Empty means "match the sender"; unknown codes return '' so callers can
/// show the code itself instead of a raw key.
String autoReplyLanguageKey(String code) {
  final c = code.trim().toLowerCase();
  if (c.isEmpty || c == 'auto') return 'ar_lang_auto';
  const known = {
    'en',
    'fa',
    'ar',
    'ru',
    'es',
    'fr',
    'it',
    'pt',
    'id',
    'uz',
    'zh',
    'tr',
    'de',
  };
  return known.contains(c) ? 'ar_lang_$c' : '';
}

/// The targets the list shows for a mode and a search term.
///
/// This is deliberately a pure function: the screen's meaning changes with the
/// mode (SELECTED_ONLY lists what is answered, ALL_EXCEPT lists the exceptions),
/// and that rule is the one thing about targeting worth testing without a
/// widget.
List<AutoReplyTarget> filterAutoReplyTargets(
  Iterable<AutoReplyTarget> targets, {
  String search = '',
  String mode = AutoReplyTargetMode.everyone,
}) {
  // A pasted `@handle` must find the stored handle: the column itself does not
  // carry the sigil, so comparing the raw text would return nothing.
  final needle = search.trim().toLowerCase().replaceFirst(RegExp(r'^@'), '');
  final normalized = AutoReplyTargetMode.normalize(mode);
  return targets
      .where((t) {
        if (normalized == AutoReplyTargetMode.selectedOnly && !t.isEnabled) {
          return false;
        }
        if (normalized == AutoReplyTargetMode.allExcept && t.isEnabled) {
          return false;
        }
        if (needle.isEmpty) return true;
        return t.title.toLowerCase().contains(needle) ||
            t.username.toLowerCase().contains(needle) ||
            t.chatId.toLowerCase().contains(needle) ||
            t.note.toLowerCase().contains(needle);
      })
      .toList(growable: false);
}

/// Whether the engine would answer [chatId] under [mode].
///
/// Mirrors the server's gate so the targeting screen can explain its own list
/// instead of leaving the operator to infer it.
bool autoReplyModeAnswers({
  required String mode,
  required String chatId,
  required List<AutoReplyTarget> targets,
}) {
  final match = targets.where((t) => t.chatId == chatId);
  final listed = match.isNotEmpty;
  final enabled = listed && match.first.isEnabled;
  return switch (AutoReplyTargetMode.normalize(mode)) {
    AutoReplyTargetMode.selectedOnly => enabled,
    AutoReplyTargetMode.allExcept => !listed || enabled,
    AutoReplyTargetMode.manual => listed,
    _ => true,
  };
}
