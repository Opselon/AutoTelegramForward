// Unit tests for the auto-reply wire models (`lib/core/auto_reply_models.dart`).
//
// The models double as request bodies, so the JSON round-trip is not a detail:
// a key rename in the client would silently become a field the server ignores.
// Every fixture here carries each field name exactly as the Go handler
// (`/api/auto-reply/*`) defines it.
//
// Null-safety is also contractual: a payload missing any single field must
// resolve to the documented default, never blank a settings form or throw.
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/auto_reply_models.dart';

Map<String, dynamic> get _settingsSample => const {
  'enabled': true,
  'ai_config_id': 'cfg-1',
  'prompt_id': 'p-9',
  'target_mode': 'SELECTED_ONLY',
  'language': 'fa',
  'tone': 'professional',
  'response_length': 'long',
  'temperature': 0.9,
  'max_tokens': 512,
  'context_messages': 12,
  'memory_enabled': false,
  'memory_max_facts': 8,
  'extra_instructions': 'No medical advice.',
  'max_reply_chars': 700,
  'typing_simulation': false,
  'typing_delay_min': 2,
  'typing_delay_max': 5,
  'cooldown_seconds': 120,
  'daily_limit_per_chat': 20,
  'rate_limit_per_minute': 3,
  'max_concurrency': 1,
  'max_attempts': 4,
  'retry_backoff_seconds': 45,
  'quiet_hours_start': '23:00',
  'quiet_hours_end': '07:00',
  'timezone_offset_minutes': 210,
  'active_days': [0, 2, 4],
  'fallback_text': 'Ask a human.',
  'ignore_bots': false,
  'version': 7,
};

void main() {
  group('AutoReplySettings', () {
    test('every documented snake_case field survives a JSON round-trip', () {
      final settings = AutoReplySettings.fromJson(_settingsSample);
      expect(settings.toJson(), equals(_settingsSample));
    });

    test('toJson keys match the server contract exactly', () {
      expect(
        AutoReplySettings().toJson().keys.toSet(),
        equals(_settingsSample.keys.toSet()),
      );
    });

    test('an empty payload falls back to the documented defaults', () {
      final s = AutoReplySettings.fromJson(const {});
      expect(s.enabled, isFalse);
      expect(s.aiConfigId, isEmpty);
      expect(s.targetMode, AutoReplyTargetMode.everyone);
      expect(s.tone, 'friendly');
      expect(s.responseLength, 'medium');
      expect(s.temperature, 0.7);
      expect(s.maxTokens, 400);
      expect(s.contextMessages, 8);
      expect(s.memoryEnabled, isTrue);
      expect(s.memoryMaxFacts, 20);
      expect(s.maxReplyChars, 1000);
      expect(s.typingSimulation, isTrue);
      expect(s.cooldownSeconds, 60);
      expect(s.dailyLimitPerChat, 50);
      expect(s.rateLimitPerMinute, 6);
      expect(s.maxAttempts, 3);
      expect(s.quietHoursSet, isFalse);
      expect(s.activeDays, orderedEquals(const [0, 1, 2, 3, 4, 5, 6]));
      expect(s.ignoreBots, isTrue);
      expect(s.version, 0);
    });

    test('one missing field degrades alone, not the whole form', () {
      final partial = Map<String, dynamic>.of(_settingsSample);
      partial.remove('max_reply_chars');
      final s = AutoReplySettings.fromJson(partial);
      expect(s.maxReplyChars, 1000); // default
      expect(s.tone, 'professional'); // neighbours untouched
      expect(s.version, 7);
    });

    test('a malformed active_days entry is dropped and the rest kept', () {
      final s = AutoReplySettings.fromJson({
        'active_days': [1, 'x', 9, -1, 6, null],
      });
      expect(s.activeDays, orderedEquals(const [1, 6]));
    });

    test('an empty active_days list still means every day', () {
      expect(
        AutoReplySettings.fromJson({'active_days': []}).activeDays,
        hasLength(7),
      );
    });

    test('unknown target modes normalise to EVERYONE, never throw', () {
      expect(
        AutoReplySettings.fromJson({'target_mode': 42}).targetMode,
        AutoReplyTargetMode.everyone,
      );
    });

    test('updateBody with one field carries exactly that update_mask', () {
      final body = AutoReplySettings.fromJson(_settingsSample)
          .copyWith(enabled: false)
          .updateBody(const ['enabled']);
      expect(body['update_mask'], orderedEquals(const ['enabled']));
      expect(body['enabled'], isFalse);
      expect(body['ai_config_id'], 'cfg-1'); // other fields still travel
    });

    test('updateBody with no mask lists every setting name and no mask', () {
      final body = AutoReplySettings().updateBody(const []);
      expect(body.containsKey('update_mask'), isFalse);
      expect(
        body.keys.toSet().difference({'enabled': false}.keys.toSet()),
        equals(const {
          'ai_config_id',
          'prompt_id',
          'target_mode',
          'language',
          'tone',
          'response_length',
          'temperature',
          'max_tokens',
          'context_messages',
          'memory_enabled',
          'memory_max_facts',
          'extra_instructions',
          'max_reply_chars',
          'typing_simulation',
          'typing_delay_min',
          'typing_delay_max',
          'cooldown_seconds',
          'daily_limit_per_chat',
          'rate_limit_per_minute',
          'max_concurrency',
          'max_attempts',
          'retry_backoff_seconds',
          'quiet_hours_start',
          'quiet_hours_end',
          'timezone_offset_minutes',
          'active_days',
          'fallback_text',
          'ignore_bots',
          'version',
        }),
      );
    });
  });

  group('AutoReplyPrompt', () {
    test('create body is the full writable contract and nothing else', () {
      final prompt = const AutoReplyPrompt(
        id: 'srv-assigned',
        name: 'Support replies',
        description: 'For the support room',
        systemPrompt: 'You answer support questions.',
        userPromptTemplate: 'Re: {text}',
        tags: ['support', 'vip'],
        category: 'support',
        isDefault: true,
        usageCount: 123, // server-measured
        createdAt: 1700000001, // server-owned
        updatedAt: 1700000002,
      );
      final body = prompt.toCreateJson();
      expect(body, {
        'name': 'Support replies',
        'description': 'For the support room',
        'system_prompt': 'You answer support questions.',
        'user_prompt_template': 'Re: {text}',
        'language': '',
        'tone': '',
        'response_length': '',
        'tags': ['support', 'vip'],
        'category': 'support',
        'is_default': true,
        'is_archived': false,
      });
      // A client that sends `id` to POST /prompts invents identities; the
      // server owns identity. usage_count is the server's measurement.
      expect(body.containsKey('id'), isFalse);
      expect(body.containsKey('usage_count'), isFalse);
      expect(body.containsKey('created_at'), isFalse);
      expect(body.containsKey('updated_at'), isFalse);
    });

    test('update body equals create body (masked server-side)', () {
      final prompt = const AutoReplyPrompt(
        id: 'p-1',
        name: 'n',
        userPromptTemplate: '{text}',
      );
      expect(prompt.toUpdateJson(), prompt.toCreateJson());
    });

    test('from a full payload recreates every field', () {
      final payload = {
        'id': 'p-2',
        'name': 'Welcome',
        'description': 'd',
        'system_prompt': 'sp',
        'user_prompt_template': 'pre- {text} -post',
        'language': 'en',
        'tone': 'casual',
        'response_length': 'short',
        'tags': ['a'],
        'category': 'general',
        'is_default': true,
        'is_archived': true,
        'usage_count': 5,
        'created_at': 111,
        'updated_at': 222,
      };
      final restored = AutoReplyPrompt.fromJson(payload.cast<String, dynamic>())
          .toCreateJson();
      expect(restored, {
        'name': 'Welcome',
        'description': 'd',
        'system_prompt': 'sp',
        'user_prompt_template': 'pre- {text} -post',
        'language': 'en',
        'tone': 'casual',
        'response_length': 'short',
        'tags': ['a'],
        'category': 'general',
        'is_default': true,
        'is_archived': true,
      });
    });

    test('missing fields spoil a prompt instead of throwing', () {
      final p = AutoReplyPrompt.fromJson(const {'id': 'p-3'});
      expect(p.name, isEmpty);
      expect(p.userPromptTemplate, isEmpty);
      expect(p.tags, isEmpty);
      expect(p.isDefault, isFalse);
      expect(p.usageCount, 0);
    });

    test('duplicate body only carries the name the server should give', () {
      final duplicated = AutoReplyPrompt.fromJson(const {
        'id': 'p-1',
        'name': 'Welcome',
        'usage_count': 9,
      }).copyWith(name: 'Welcome copy', usageCount: 0);
      expect(duplicated.toCreateJson()['name'], 'Welcome copy');
      // The duplicate starts fresh: it is not marked default and the server
      // recomputes usage, so the client never copies counters over.
      expect(duplicated.toCreateJson()['is_default'], isFalse);
    });
  });

  group('AutoReplyTarget', () {
    test('add body omits server-owned added_at, update body omits chat_id', () {
      const target = AutoReplyTarget(
        chatId: '123',
        title: 'Ada',
        username: 'ada',
        isEnabled: false,
        promptId: 'p-2',
        note: 'VIP',
        priority: 4,
        addedAt: 99,
      );
      expect(target.toAddJson(), {
        'chat_id': '123',
        'title': 'Ada',
        'username': 'ada',
        'is_enabled': false,
        'prompt_id': 'p-2',
        'note': 'VIP',
        'priority': 4,
      });
      expect(target.toUpdateJson(), {
        'is_enabled': false,
        'title': 'Ada',
        'note': 'VIP',
        'prompt_id': 'p-2',
        'priority': 4,
      });
    });

    test('falls back through label title → username → raw id', () {
      expect(const AutoReplyTarget(chatId: '42', title: 'Ada').label, 'Ada');
      expect(
        const AutoReplyTarget(chatId: '42', username: 'ada').label,
        '@ada',
      );
      expect(const AutoReplyTarget(chatId: '42').label, '42');
    });
  });

  group('AutoReplyRule', () {
    test('toJson is the create/update body; nothing derived leaks', () {
      const rule = AutoReplyRule(
        id: 'r-1',
        name: 'Ship canned reply',
        isEnabled: false,
        priority: 3,
        action: AutoReplyAction.skip,
        conditions: {
          'keywords': ['price'],
        },
        exclusions: {
          'keywords': ['refund'],
        },
        promptId: 'p-4',
        aiConfigId: 'cfg-2',
        cooldownSeconds: 90,
        language: 'en',
        tone: 'direct',
        responseLength: 'short',
        temperature: 1.25,
        maxTokens: 128,
      );
      expect(rule.toJson(), {
        'name': 'Ship canned reply',
        'is_enabled': false,
        'priority': 3,
        'action': 'SKIP',
        'conditions': {
          'keywords': ['price'],
        },
        'exclusions': {
          'keywords': ['refund'],
        },
        'prompt_id': 'p-4',
        'ai_config_id': 'cfg-2',
        'cooldown_seconds': 90,
        'language': 'en',
        'tone': 'direct',
        'response_length': 'short',
        'temperature': 1.25,
        'max_tokens': 128,
      });
    });

    test('keywords read through from conditions and exclusions', () {
      const rule = AutoReplyRule(
        id: 'r',
        name: 'r',
        conditions: {
          'keywords': ['price', 'cost'],
        },
        exclusions: {
          'keywords': ['refund'],
        },
      );
      expect(rule.keywords, orderedEquals(const ['price', 'cost']));
      expect(rule.exclusionKeywords, orderedEquals(const ['refund']));
    });
  });

  group('AutoReplyJob and stats', () {
    test('a job parses the delivery record the queue returns', () {
      final job = AutoReplyJob.fromJson({
        'id': 'j-1',
        'chat_id': '999',
        'chat_title': 'Ada',
        'status': 'SENT',
        'attempt': 2,
        'max_attempts': 3,
        'error': 'first try flooded',
        'error_code': 'rate_limit',
        'inbound_text': 'pricing?',
        'reply_text': 'Here it is.',
        'correlation_id': 'c-1',
        'latency_ms': 340,
        'created_at': 1700000000,
        'sent_at': 1700000001,
        'source_message_id': 555,
      });
      expect(job.status, 'SENT');
      expect(job.attempt, 2);
      expect(job.replyText, 'Here it is.');
      expect(job.sourceMessageId, 555);
    });

    test('a job row missing every optional field still renders', () {
      final job = AutoReplyJob.fromJson(const {'id': 'j-2'});
      expect(job.status, isEmpty);
      expect(job.latencyMs, 0);
      expect(job.inboundText, isEmpty);
    });

    test('stats counters come straight from the server, not derived', () {
      final stats = AutoReplyStats.fromJson({
        'sent_today': 12,
        'sent_total': 900,
        'pending': 3,
        'retrying': 1,
        'dead_letter': 2,
        'chats_enabled': 7,
        'prompts': 4,
        'rules': 2,
        'average_latency_ms': 540.5,
        'last_sent_at': 1700000002,
        'statuses': {'SENT': 900},
      });
      expect(stats.sentToday, 12);
      expect(stats.sentTotal, 900);
      expect(stats.deadLetter, 2);
      expect(stats.chatsEnabled, 7);
      expect(stats.prompts, 4);
      expect(stats.rules, 2);
      expect(stats.averageLatencyMs, 540.5);
      expect(stats.statuses['SENT'], 900);
    });

    test('an empty stats payload is all zeroes, never NaN or null', () {
      final stats = AutoReplyStats.fromJson(const {});
      expect(stats.sentToday, 0);
      expect(stats.averageLatencyMs, 0);
      expect(stats.statuses, isEmpty);
    });
  });

  group('filterAutoReplyTargets', () {
    const answered = AutoReplyTarget(
      chatId: '1',
      title: 'Ada',
      username: 'ada',
      isEnabled: true,
    );
    const silent = AutoReplyTarget(
      chatId: '2',
      title: 'Bob',
      username: 'bob',
      isEnabled: false,
      note: 'spam',
    );

    test('SELECTED_ONLY shows only chats that are answered', () {
      final visible = filterAutoReplyTargets(const [
        answered,
        silent,
      ], mode: AutoReplyTargetMode.selectedOnly);
      expect(visible.map((t) => t.chatId), orderedEquals(const ['1']));
    });

    test('ALL_EXCEPT shows only the exceptions — the silent chats', () {
      final visible = filterAutoReplyTargets(const [
        answered,
        silent,
      ], mode: AutoReplyTargetMode.allExcept);
      expect(visible.map((t) => t.chatId), orderedEquals(const ['2']));
    });

    test('EVERYONE (default) shows every chat regardless of state', () {
      final visible = filterAutoReplyTargets(const [
        answered,
        silent,
      ], mode: AutoReplyTargetMode.everyone);
      expect(visible, hasLength(2));
    });

    test('search matches title, username, id and note', () {
      expect(
        filterAutoReplyTargets(
          const [answered, silent],
          search: '@BOB',
          mode: AutoReplyTargetMode.everyone,
        ).length,
        1,
      );
      expect(
        filterAutoReplyTargets(const [
          answered,
          silent,
        ], search: 'spam').first.chatId,
        '2',
      );
      expect(filterAutoReplyTargets(const [answered], search: '42').length, 0);
    });

    test('unknown mode values behave like EVERYONE instead of hiding all', () {
      expect(
        filterAutoReplyTargets(const [answered, silent], mode: '???'),
        hasLength(2),
      );
    });
  });
}
