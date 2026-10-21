/// Models for the text-replacement manager (`/api/replace-rules*`).
///
/// Contract source of truth: `api/internal/handlers/replace.go`. Field names are
/// the snake_case keys the server sends and expects, so the models double as the
/// request bodies — there is no second mapping layer that could drift.
///
/// Two invariants, both easy to get wrong:
///
///   * **A rule is one object, however many tasks run it.** The core stores one
///     copy per task and groups them by rule id, reporting `task_ids` and
///     `task_names`. Listing the raw copies would show the same rule once per
///     task and let an edit hit only one of them.
///   * **No model carries an owner id.** Identity is the bearer token; a client
///     that sent one would be claiming another tenant's rules.
library;

import 'json_cast.dart';

/// One replace rule, as every surface sees it.
class ReplaceRule {
  const ReplaceRule({
    required this.id,
    this.name = '',
    this.source = '',
    this.to = '',
    this.mode = ReplaceMode.literal,
    this.flags = '',
    this.wholeWord = false,
    this.enabled = true,
    this.priority = 100,
    this.taskIds = const [],
    this.taskNames = const [],
    this.version = 0,
    this.updatedAt = 0,
    this.badge = '',
  });

  final String id;
  final String name;

  /// The text or pattern to find.
  final String source;

  /// The replacement. **Empty is a deletion**, not "unset".
  final String to;

  /// [ReplaceMode.literal] or [ReplaceMode.regex].
  final String mode;
  final String flags;
  final bool wholeWord;
  final bool enabled;

  /// 0–1000; a higher number runs first.
  final int priority;
  final List<String> taskIds;
  final List<String> taskNames;
  final int version;

  /// Unix seconds, `0` when the server did not send one.
  final int updatedAt;

  /// The pattern as a person would write it: `promo` or `/promo/i`.
  final String badge;

  bool get isRegex => ReplaceMode.normalize(mode) == ReplaceMode.regex;

  /// What the list shows as the rule's title: the name, else the pattern.
  String get title => name.trim().isNotEmpty ? name.trim() : source;

  Map<String, dynamic> toJson() => {
    'name': name,
    'source': source,
    'to': to,
    'mode': ReplaceMode.normalize(mode),
    'flags': flags,
    'whole_word': wholeWord,
    'enabled': enabled,
    'priority': priority,
    'task_ids': taskIds,
  };

  ReplaceRule copyWith({
    String? id,
    String? name,
    String? source,
    String? to,
    String? mode,
    String? flags,
    bool? wholeWord,
    bool? enabled,
    int? priority,
    List<String>? taskIds,
    List<String>? taskNames,
    int? version,
    int? updatedAt,
    String? badge,
  }) => ReplaceRule(
    id: id ?? this.id,
    name: name ?? this.name,
    source: source ?? this.source,
    to: to ?? this.to,
    mode: mode ?? this.mode,
    flags: flags ?? this.flags,
    wholeWord: wholeWord ?? this.wholeWord,
    enabled: enabled ?? this.enabled,
    priority: priority ?? this.priority,
    taskIds: taskIds ?? this.taskIds,
    taskNames: taskNames ?? this.taskNames,
    version: version ?? this.version,
    updatedAt: updatedAt ?? this.updatedAt,
    badge: badge ?? this.badge,
  );

  factory ReplaceRule.fromJson(Map<String, dynamic> json) => ReplaceRule(
    id: json['id'] as String? ?? '',
    name: json['name'] as String? ?? '',
    source: json['source'] as String? ?? '',
    to: json['to'] as String? ?? '',
    mode: ReplaceMode.normalize(json['mode']),
    flags: json['flags'] as String? ?? '',
    wholeWord: json['whole_word'] as bool? ?? false,
    enabled: json['enabled'] as bool? ?? true,
    priority: jsonInt(json['priority'], 100),
    taskIds: jsonStringList(json['task_ids']),
    taskNames: jsonStringList(json['task_names']),
    version: jsonInt(json['version']),
    updatedAt: jsonInt(json['updated_at']),
    badge: json['badge'] as String? ?? '',
  );
}

/// The two match modes, with the same normalisation the server applies: an
/// unknown value degrades to exact text rather than being rendered raw.
class ReplaceMode {
  const ReplaceMode._();

  static const String literal = 'literal';
  static const String regex = 'regex';

  static String normalize(Object? raw) {
    final value = (raw is String ? raw : '').trim().toLowerCase();
    return value == regex ? regex : literal;
  }

  static String labelKey(String mode) =>
      normalize(mode) == regex ? 'rp_mode_regex' : 'rp_mode_literal';
}

/// Totals over the *filtered* set, so a chip never counts a rule the current
/// task filter excludes.
class ReplaceRuleCounts {
  const ReplaceRuleCounts({
    this.total = 0,
    this.enabled = 0,
    this.disabled = 0,
  });

  final int total;
  final int enabled;
  final int disabled;

  factory ReplaceRuleCounts.fromJson(Map<String, dynamic> json) =>
      ReplaceRuleCounts(
        total: jsonInt(json['total']),
        enabled: jsonInt(json['enabled']),
        disabled: jsonInt(json['disabled']),
      );
}

/// A forwarding task a rule can be assigned to.
class ReplaceTaskOption {
  const ReplaceTaskOption({required this.id, this.name = ''});

  final String id;
  final String name;

  String get label => name.trim().isNotEmpty ? name.trim() : id;

  factory ReplaceTaskOption.fromJson(Map<String, dynamic> json) =>
      ReplaceTaskOption(
        id: json['id'] as String? ?? '',
        name: json['name'] as String? ?? '',
      );
}

/// One page of rules: the rows plus the counts, the pager and the task options
/// the editor's picker needs (so opening the editor costs no second request).
class ReplaceRulePage {
  const ReplaceRulePage({
    this.rules = const [],
    this.tasks = const [],
    this.counts = const ReplaceRuleCounts(),
    this.total = 0,
    this.page = 1,
    this.pages = 1,
    this.perPage = 0,
    this.query = '',
    this.status = 'all',
    this.taskId = '',
  });

  final List<ReplaceRule> rules;
  final List<ReplaceTaskOption> tasks;
  final ReplaceRuleCounts counts;
  final int total;
  final int page;
  final int pages;
  final int perPage;
  final String query;
  final String status;
  final String taskId;

  bool get isEmpty => rules.isEmpty;

  factory ReplaceRulePage.fromJson(Map<String, dynamic> json) =>
      ReplaceRulePage(
        rules: (json['rules'] as List? ?? const [])
            .whereType<Map>()
            .map((e) => ReplaceRule.fromJson(e.cast<String, dynamic>()))
            .toList(growable: false),
        tasks: (json['tasks'] as List? ?? const [])
            .whereType<Map>()
            .map((e) => ReplaceTaskOption.fromJson(e.cast<String, dynamic>()))
            .toList(growable: false),
        counts: ReplaceRuleCounts.fromJson(
          (json['counts'] as Map?)?.cast<String, dynamic>() ?? const {},
        ),
        total: jsonInt(json['total']),
        page: jsonInt(json['page'], 1),
        pages: jsonInt(json['pages'], 1),
        perPage: jsonInt(json['per_page']),
        query: json['query'] as String? ?? '',
        status: json['status'] as String? ?? 'all',
        taskId: json['task_id'] as String? ?? '',
      );
}

/// The test preview: what a rule does to a sample, without storing anything.
class ReplaceRuleTestResult {
  const ReplaceRuleTestResult({
    this.ok = false,
    this.input = '',
    this.output = '',
    this.matches = 0,
    this.changed = false,
  });

  final bool ok;
  final String input;
  final String output;
  final int matches;

  /// False when nothing matched — the message would be forwarded untouched.
  final bool changed;

  factory ReplaceRuleTestResult.fromJson(Map<String, dynamic> json) =>
      ReplaceRuleTestResult(
        ok: json['ok'] as bool? ?? false,
        input: json['input'] as String? ?? '',
        output: json['output'] as String? ?? '',
        matches: jsonInt(json['matches']),
        changed: json['changed'] as bool? ?? false,
      );
}
