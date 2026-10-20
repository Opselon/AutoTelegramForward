// The auto-reply endpoints are only as good as the bodies they send.
//
// These tests drive the real [ApiClient] over a canned transport and assert on
// the bytes that leave the app: the path, the method and the JSON body. They
// exist because the client models double as request bodies — a field the model
// renames, or a server-owned field it starts echoing back, is invisible in a
// widget test but rejected (or worse, silently ignored) by the Go handler.
library;

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/core/auto_reply_models.dart';
import 'package:autoforward_mobile/core/storage.dart';

typedef _Call = ({String method, String path, Map<String, dynamic> body});

/// A transport that records every call and answers with [response] (or 200 OK
/// with an empty object when the caller does not care about the answer).
_Transport _recorder(List<_Call> log, {String? response}) {
  return (
    Uri url, {
    required String method,
    Map<String, String>? headers,
    Object? body,
    required Duration timeout,
  }) async {
    final decoded = body == null
        ? <String, dynamic>{}
        : body is String && body.isNotEmpty
        ? (jsonDecode(body) as Map).cast<String, dynamic>()
        : body is List
        ? {'prompts': body}
        : (body as Map).cast<String, dynamic>();
    log.add((method: method, path: url.path, body: decoded));
    return http.Response(
      response ?? '{"settings":{}}',
      200,
      headers: const {'content-type': 'application/json'},
    );
  };
}

typedef _Transport = Future<http.Response> Function(
  Uri url, {
  required String method,
  Map<String, String>? headers,
  Object? body,
  required Duration timeout,
});

ApiClient _client(List<_Call> log, {String? response}) => ApiClient(
  baseUrl: 'https://atf.test/',
  tokenStore: MemoryTokenStore(),
  transport: _recorder(log, response: response),
);

void main() {
  group('prompt requests', () {
    test('create posts the writable prompt body, never an id', () async {
      final log = <_Call>[];
      final client = _client(
        log,
        response: '{"prompt":{"id":"srv-1","name":"Welcome","user_prompt_template":"{text}"}}',
      );

      final created = await client.createAutoReplyPrompt(
        const AutoReplyPrompt(
          id: 'client-side-id-must-not-be-sent',
          name: 'Welcome',
          description: 'First contact',
          systemPrompt: 'Be brief.',
          userPromptTemplate: 'Answer: {text}',
          tags: ['intro'],
          category: 'general',
          isDefault: true,
        ),
      );

      expect(log, hasLength(1));
      final call = log.single;
      expect(call.method, 'POST');
      expect(call.path, '/api/auto-reply/prompts');
      expect(call.body['name'], 'Welcome');
      expect(call.body['system_prompt'], 'Be brief.');
      expect(call.body['user_prompt_template'], 'Answer: {text}');
      expect(call.body['tags'], ['intro']);
      expect(call.body['is_default'], isTrue);
      expect(call.body.containsKey('id'), isFalse);
      expect(call.body.containsKey('usage_count'), isFalse);
      // The response is the server's truth: it assigns the id.
      expect(created.id, 'srv-1');
    });

    test(
      'update puts the same writable body to the encoded prompt path',
      () async {
        final log = <_Call>[];
        final client = _client(
          log,
          response: '{"prompt":{"id":"p 1/2","name":"Renamed"}}',
        );

        await client.updateAutoReplyPrompt(
          'p 1/2',
          const AutoReplyPrompt(id: 'p 1/2', name: 'Renamed'),
        );

        final call = log.single;
        expect(call.method, 'PUT');
        expect(call.path, '/api/auto-reply/prompts/p%201%2F2');
        expect(call.body['name'], 'Renamed');
        expect(call.body.containsKey('id'), isFalse);
      },
    );

    test('duplicate posts only the requested name', () async {
      final log = <_Call>[];
      final client = _client(
        log,
        response: '{"prompt":{"id":"p-2","name":"Welcome copy"}}',
      );

      final copy = await client.duplicateAutoReplyPrompt(
        'p-1',
        name: '  Welcome copy  ',
      );

      final call = log.single;
      expect(call.method, 'POST');
      expect(call.path, '/api/auto-reply/prompts/p-1/duplicate');
      expect(call.body, {'name': 'Welcome copy'});
      expect(copy.name, 'Welcome copy');
    });

    test(
      'duplicate without a name sends an empty object, not a blank name',
      () async {
        final log = <_Call>[];
        final client = _client(log, response: '{"prompt":{"id":"p-2"}}');

        await client.duplicateAutoReplyPrompt('p-1', name: '   ');

        expect(log.single.body, isEmpty);
        expect(log.single.path, '/api/auto-reply/prompts/p-1/duplicate');
      },
    );

    test('delete hits the prompt id and tolerates {"success":true}', () async {
      final log = <_Call>[];
      final client = _client(log, response: '{"success":true}');

      await client.deleteAutoReplyPrompt('p-9');

      expect(log.single.method, 'DELETE');
      expect(log.single.path, '/api/auto-reply/prompts/p-9');
    });

    test('import posts a schema version and the prompt list', () async {
      final log = <_Call>[];
      final client = _client(
        log,
        response: '{"imported":1,"skipped":0,"prompts":[]}',
      );

      final result = await client.importAutoReplyPrompts(const [
        AutoReplyPrompt(id: 'p-1', name: 'A', userPromptTemplate: '{text}'),
      ]);

      final call = log.single;
      expect(call.path, '/api/auto-reply/prompts/import');
      expect(call.body['schema_version'], 1);
      expect(call.body['prompts'], hasLength(1));
      expect(result.imported, 1);
    });
  });

  group('settings requests', () {
    test(
      'a masked save sends update_mask with exactly the named field',
      () async {
        final log = <_Call>[];
        final client = _client(
          log,
          response: '{"settings":{"enabled":true,"version":8}}',
        );

        final saved = await client.saveAutoReplySettings(
          const AutoReplySettings(version: 7).copyWith(enabled: true),
          fields: const ['enabled'],
        );

        final call = log.single;
        expect(call.method, 'PUT');
        expect(call.path, '/api/auto-reply/settings');
        expect(call.body['update_mask'], orderedEquals(const ['enabled']));
        expect(call.body['enabled'], isTrue);
        expect(saved.enabled, isTrue);
        expect(saved.version, 8);
      },
    );

    test('GET unwraps the settings envelope', () async {
      final log = <_Call>[];
      final client = _client(
        log,
        response:
            '{"settings":{"enabled":true,"tone":"direct","max_tokens":77}}',
      );

      final settings = await client.autoReplySettings();

      expect(log.single.method, 'GET');
      expect(log.single.path, '/api/auto-reply/settings');
      expect(settings.enabled, isTrue);
      expect(settings.tone, 'direct');
      expect(settings.maxTokens, 77);
    });
  });

  group('target requests', () {
    test('bulk delete sends the chat ids in the body', () async {
      final log = <_Call>[];
      final client = _client(log, response: '{"success":true,"removed":2}');

      final removed = await client.deleteAutoReplyTargets(const ['1', '2']);

      final call = log.single;
      expect(call.method, 'DELETE');
      expect(call.path, '/api/auto-reply/targets');
      expect(call.body['chat_ids'], ['1', '2']);
      expect(removed, 2);
    });

    test('resolve posts the session and the trimmed query', () async {
      final log = <_Call>[];
      final client = _client(
        log,
        response: '{"found":true,"target":{"chat_id":"42","title":"Ada"}}',
      );

      final found = await client.resolveAutoReplyTarget(
        sessionId: 's-1',
        query: '  @ada  ',
      );

      final call = log.single;
      expect(call.path, '/api/auto-reply/targets/resolve');
      expect(call.body, {'session_id': 's-1', 'query': '@ada'});
      expect(found.found, isTrue);
      expect(found.target?.title, 'Ada');
    });
  });

  group('rule reorder', () {
    test(
      'posts the whole order, because priorities are renumbered server-side',
      () async {
        final log = <_Call>[];
        final client = _client(log, response: '{"rules":[]}');

        await client.reorderAutoReplyRules(const ['r-3', 'r-1', 'r-2']);

        final call = log.single;
        expect(call.path, '/api/auto-reply/rules/reorder');
        expect(call.body['rule_ids'], ['r-3', 'r-1', 'r-2']);
      },
    );
  });

  group('delivery jobs', () {
    test('retry and cancel post to the job sub-paths', () async {
      final log = <_Call>[];
      final client = _client(log, response: '{"success":true}');

      await client.retryAutoReplyJob('j-1');
      await client.cancelAutoReplyJob('j-2');

      expect(log[0].path, '/api/auto-reply/jobs/j-1/retry');
      expect(log[0].method, 'POST');
      expect(log[1].path, '/api/auto-reply/jobs/j-2/cancel');
      expect(log[1].method, 'POST');
    });
  });
}
