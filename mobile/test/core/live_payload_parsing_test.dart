/// The client must parse what the real server actually sends.
///
/// Every fixture in `test/fixtures/live_api/` is a byte-for-byte capture from
/// the running stack, kept because it broke the shipped client: the rule carries
/// `topic_id: ""` and the session carries `user_id: "5094837833"`, both of which
/// the client read with `as num?`. That threw
/// `type 'String' is not a subtype of type 'num?' in type cast`, which surfaced
/// as the red banner on Forwarding rules and as a Chats page that never
/// finished loading.
///
/// These tests drive the real [ApiClient] over those payloads, so the day the
/// server stringifies another number the client degrades instead of blanking a
/// screen.
library;

import 'dart:convert';
import 'dart:io';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

const _dir = 'test/fixtures/live_api';

String _fixture(String name) => File('$_dir/$name').readAsStringSync();

/// Serves the captured payload for whichever path the client asks for.
HttpTransport _liveTransport({Map<String, int> status = const {}}) {
  return (
    Uri url, {
    required String method,
    Map<String, String>? headers,
    Object? body,
    required Duration timeout,
  }) async {
    final override = status[url.path];
    if (override != null) {
      return http.Response(
        '{"error":"boom"}',
        override,
        headers: const {'content-type': 'application/json'},
      );
    }
    final name = switch (url.path) {
      '/api/rules' => 'rules.json',
      '/api/sessions' => 'sessions.json',
      '/api/stats' => 'stats.json',
      '/api/ai-configs' => 'ai_configs.json',
      '/api/filters' => 'filters.json',
      '/api/logs' => 'logs.json',
      _ when url.path.endsWith('/chats') => 'chats.json',
      _ => null,
    };
    if (name == null) {
      return http.Response(
        '{"error":"unexpected ${url.path}"}',
        404,
        headers: const {'content-type': 'application/json'},
      );
    }
    return http.Response(
      _fixture(name),
      200,
      headers: const {'content-type': 'application/json; charset=utf-8'},
    );
  };
}

ApiClient _client({Map<String, int> status = const {}}) => ApiClient(
  baseUrl: 'https://atf.example.com/',
  tokenStore: MemoryTokenStore(),
  transport: _liveTransport(status: status),
);

void main() {
  group('the captured payloads parse', () {
    test('a rule whose topic_id is the empty string parses as "no topic"', () async {
      final rules = await _client().listRules();

      expect(rules, hasLength(1));
      final rule = rules.single;
      // The value that crashed the client: storage's default for topic_id.
      expect(jsonDecode(_fixture('rules.json'))['rules'][0]['topic_id'], '');
      expect(rule.topicId, 0);
      expect(rule.id, 'ea565d5c-ccce-4f1c-b5f5-79eca2911642');
      expect(rule.isEnabled, isTrue);
      expect(rule.sessionId, '1d12518c-039c-437a-aebd-7b9b7c720a18');
      // The multi-source contract: both lists, with the legacy scalar mirroring
      // the first entry.
      expect(rule.sourceIds, contains('-1004400615270'));
      expect(rule.targetIds, contains('-1003915313919'));
      expect(rule.sourceChatId, rule.sourceIds.first);
      expect(rule.targetChatId, rule.targetIds.first);
      expect(rule.ownerUserId, 5094837833);
    });

    test('a session whose user_id is a string parses', () async {
      final sessions = await _client().listSessions();

      expect(
        jsonDecode(_fixture('sessions.json'))['sessions'][0]['user_id'],
        '5094837833',
      );
      expect(sessions, hasLength(1));
      expect(sessions.single.username, 'CAPXI');
      expect(sessions.single.isActive, isTrue);
      expect(sessions.single.ownerUserId, 5094837833);
    });

    test('the chat list keeps every row and its member count', () async {
      final page = await _client().listChats(
        '1d12518c-039c-437a-aebd-7b9b7c720a18',
      );

      expect(page.sessionId, '1d12518c-039c-437a-aebd-7b9b7c720a18');
      expect(page.chats, hasLength(6));
      expect(page.total, 1000);
      final biggest = page.chats.firstWhere(
        (c) => c.chatId == '-1001957768371',
      );
      expect(biggest.members, 183028);
      expect(biggest.chatType, 'channel');
      expect(biggest.displayTitle, isNotEmpty);
      expect(page.chats.map((c) => c.chatId), contains('-1004400615270'));
    });

    test('stats, logs, filters and AI configs parse', () async {
      final client = _client();

      final stats = await client.stats();
      expect(stats.activeRules, 1);
      expect(stats.activeSessions, 1);
      expect(stats.coreRunning, isTrue);
      expect(stats.uptimeSeconds, 1070);
      expect(stats.version, '1.2.2');

      final page = await client.queryLogsPage(limit: 5);
      expect(page.logs, isNotEmpty);
      expect(page.total, greaterThanOrEqualTo(page.logs.length));

      expect(await client.listFilters(), isEmpty);
      expect(await client.listAiConfigs(), isNotEmpty);
    });
  });

  group('a stringified number never takes a screen down', () {
    test(
      'every endpoint survives its numeric fields being sent as strings',
      () async {
        // Rewrite every number the fixtures hold as a numeric string — the shape a
        // future server change would produce — and require the same parses to
        // succeed. This is the general form of the defect, not just topic_id.
        String stringified(String name) {
          Object? walk(Object? node) {
            if (node is Map) {
              return node.map((k, v) => MapEntry(k.toString(), walk(v)));
            }
            if (node is List) return node.map(walk).toList();
            if (node is num) return '$node';
            return node;
          }

          return jsonEncode(walk(jsonDecode(_fixture(name))));
        }

        Future<http.Response> transport(
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async {
          final name = switch (url.path) {
            '/api/rules' => 'rules.json',
            '/api/sessions' => 'sessions.json',
            '/api/stats' => 'stats.json',
            '/api/ai-configs' => 'ai_configs.json',
            '/api/filters' => 'filters.json',
            '/api/logs' => 'logs.json',
            _ when url.path.endsWith('/chats') => 'chats.json',
            _ => null,
          };
          if (name == null) {
            return http.Response('{"error":"unexpected"}', 404);
          }
          return http.Response(
            stringified(name),
            200,
            headers: const {'content-type': 'application/json'},
          );
        }

        final client = ApiClient(
          baseUrl: 'https://atf.example.com/',
          tokenStore: MemoryTokenStore(),
          transport: transport,
        );

        final rules = await client.listRules();
        expect(rules.single.topicId, 0);
        expect(rules.single.ownerUserId, 5094837833);
        expect(rules.single.aiMaxTokens, 600);

        final sessions = await client.listSessions();
        expect(sessions.single.ownerUserId, 5094837833);

        final chats = await client.listChats('s-1');
        expect(chats.chats.first.members, 183028);

        final stats = await client.stats();
        expect(stats.activeRules, 1);
        expect(stats.uptimeSeconds, 1070);

        final logs = await client.queryLogsPage(limit: 5);
        expect(logs.logs, isNotEmpty);

        expect(await client.listAiConfigs(), isNotEmpty);
      },
    );

    test(
      'a rule with a non-numeric topic_id still reads as no topic',
      () async {
        Future<http.Response> transport(
          Uri url, {
          required String method,
          Map<String, String>? headers,
          Object? body,
          required Duration timeout,
        }) async {
          final payload =
              jsonDecode(_fixture('rules.json')) as Map<String, dynamic>;
          (payload['rules'] as List)[0] = {
            ...(payload['rules'] as List)[0] as Map<String, dynamic>,
            'topic_id': 'not-a-number',
            'edit_time_limit_seconds': '',
            'ai_creativity': 'warm',
            'ai_max_tokens': null,
            'version': 'v18',
          };
          return http.Response(
            jsonEncode(payload),
            200,
            headers: const {'content-type': 'application/json'},
          );
        }

        final client = ApiClient(
          baseUrl: 'https://atf.example.com/',
          tokenStore: MemoryTokenStore(),
          transport: transport,
        );

        final rule = (await client.listRules()).single;
        expect(rule.topicId, 0);
        expect(rule.editTimeLimitSeconds, 0);
        expect(rule.aiCreativity, 0.7);
        expect(rule.aiMaxTokens, 600);
        expect(rule.version, 0);
      },
    );
  });
}
