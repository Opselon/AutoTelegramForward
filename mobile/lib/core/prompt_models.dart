/// AI rewrite prompt templates (the shared `/api/prompts` contract).
///
/// The dashboard and the Telegram bot manage the same library through the
/// same endpoints; this model mirrors the wire shape the Go API proxies
/// (`PromptTemplate` from the canonical contract): a name, the system
/// instruction, the `{text}` user template, a version counter and the
/// system/owner scope. An `enabled` flag exists server-side in the
/// delivery engine but is deliberately not carried here — the wire contract
/// does not include it yet, and pretending otherwise would render a toggle
/// that cannot be saved.
library;

/// One reusable rewrite instruction ("prompt template").
class PromptTemplate {
  final String id;
  final String name;
  final String description;
  final String systemPrompt;
  final String userPromptTemplate;
  final String targetLanguage;
  final int currentVersion;
  final bool isSystem;

  const PromptTemplate({
    this.id = '',
    this.name = '',
    this.description = '',
    this.systemPrompt = '',
    this.userPromptTemplate = '{text}',
    this.targetLanguage = 'en',
    this.currentVersion = 1,
    this.isSystem = false,
  });

  factory PromptTemplate.fromJson(Map<String, dynamic> json) {
    return PromptTemplate(
      id: json['id'] as String? ?? '',
      name: json['name'] as String? ?? '',
      description: json['description'] as String? ?? '',
      systemPrompt: json['system_prompt'] as String? ?? '',
      userPromptTemplate: json['user_prompt_template'] as String? ?? '{text}',
      targetLanguage: json['target_language'] as String? ?? 'en',
      currentVersion: (json['current_version'] as num?)?.toInt() ?? 1,
      isSystem: json['is_system'] as bool? ?? false,
    );
  }

  /// The write shape: `template` + `expected_version` (the OCC guard the
  /// server checks against `current_version` when updating).
  Map<String, dynamic> toCreateJson() => {
    'template': {
      'name': name,
      'description': description,
      'system_prompt': systemPrompt,
      'user_prompt_template': userPromptTemplate,
      'target_language': targetLanguage,
      'is_system': false,
    },
    'expected_version': 0,
  };

  Map<String, dynamic> toUpdateJson() => {
    'template': {
      'id': id,
      'name': name,
      'description': description,
      'system_prompt': systemPrompt,
      'user_prompt_template': userPromptTemplate,
      'target_language': targetLanguage,
      'is_system': isSystem,
    },
    'expected_version': currentVersion,
  };

  PromptTemplate copyWith({
    String? id,
    String? name,
    String? description,
    String? systemPrompt,
    String? userPromptTemplate,
    String? targetLanguage,
    int? currentVersion,
    bool? isSystem,
  }) {
    return PromptTemplate(
      id: id ?? this.id,
      name: name ?? this.name,
      description: description ?? this.description,
      systemPrompt: systemPrompt ?? this.systemPrompt,
      userPromptTemplate: userPromptTemplate ?? this.userPromptTemplate,
      targetLanguage: targetLanguage ?? this.targetLanguage,
      currentVersion: currentVersion ?? this.currentVersion,
      isSystem: isSystem ?? this.isSystem,
    );
  }
}

/// The result of `POST /api/prompts/test`: one real provider round-trip.
class PromptTestResult {
  final bool success;
  final String transformedText;
  final String errorMessage;
  final String errorCode;
  final String modelUsed;

  const PromptTestResult({
    this.success = false,
    this.transformedText = '',
    this.errorMessage = '',
    this.errorCode = '',
    this.modelUsed = '',
  });

  factory PromptTestResult.fromJson(Map<String, dynamic> json) {
    return PromptTestResult(
      success: json['success'] as bool? ?? false,
      transformedText: json['transformed_text'] as String? ?? '',
      errorMessage: json['error_message'] as String? ?? '',
      errorCode: json['error_code'] as String? ?? '',
      modelUsed: json['model_used'] as String? ?? '',
    );
  }
}
