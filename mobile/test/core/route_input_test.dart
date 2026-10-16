/// The route parser, the validator and the model's endpoint lists.
///
/// One task carries a source LIST and a target LIST, so 1->1, N->1, 1->N and
/// M->N are the same shape. Everything the user types goes through
/// `splitChatIdList` / `parseChatIdList`, and whether a route may be saved is
/// decided only by `validateRoute` — this locks both down.
library;

import 'package:autoforward_mobile/core/models.dart';
import 'package:autoforward_mobile/core/route_input.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('splitChatIdList', () {
    test('splits on the ASCII and the Persian comma, trims, drops empties', () {
      expect(splitChatIdList('-1001, -1002،@sales ,, t.me/news,'), const [
        '-1001',
        '-1002',
        '@sales',
        't.me/news',
      ]);
    });

    test('keeps order and repeats, because dedup is a separate decision', () {
      expect(splitChatIdList('B, A, B'), const ['B', 'A', 'B']);
    });

    test('nothing typed means no entries', () {
      expect(splitChatIdList('   '), isEmpty);
      expect(splitChatIdList(','), isEmpty);
    });

    test('never resolves a chat: ids, @usernames and links pass through', () {
      expect(
        splitChatIdList('-1001234567890, @news, https://t.me/news'),
        const ['-1001234567890', '@news', 'https://t.me/news'],
      );
    });
  });

  group('parseChatIdList', () {
    test('de-duplicates, first occurrence winning', () {
      expect(parseChatIdList('-1001, -1002, -1001'), const ['-1001', '-1002']);
    });

    test('a repeat never displaces the primary (first) endpoint', () {
      expect(parseChatIdList('-1002, -1001, -1002').first, '-1002');
    });
  });

  group('chatIdKey', () {
    test('one chat has one key however it is spelled', () {
      final key = chatIdKey('@Sales');
      expect(chatIdKey('t.me/sales'), key);
      expect(chatIdKey('https://t.me/sales/'), key);
      expect(key, 'sales');
    });

    test('a numeric id is its own key', () {
      expect(chatIdKey(' -1001234567890 '), '-1001234567890');
    });
  });

  group('validateRoute', () {
    test('accepts M -> N', () {
      expect(validateRoute(sourcesText: 'A, B', targetsText: 'X, Y'), isNull);
    });

    test('requires at least one source and one target', () {
      expect(
        validateRoute(sourcesText: '', targetsText: 'X')!.kind,
        RouteIssueKind.sourcesEmpty,
      );
      expect(
        validateRoute(sourcesText: 'A', targetsText: ' ')!.kind,
        RouteIssueKind.targetsEmpty,
      );
    });

    test('rejects a repeat inside a list and names the offending id', () {
      final issue = validateRoute(
        sourcesText: '-1001, -1002, -1001',
        targetsText: '-1003',
      )!;
      expect(issue.kind, RouteIssueKind.duplicateSource);
      expect(issue.id, '-1001');
      expect(issue.messageKey, 'rules_route_duplicate_id');

      final targetIssue = validateRoute(
        sourcesText: '-1001',
        targetsText: '-1003, -1003',
      )!;
      expect(targetIssue.kind, RouteIssueKind.duplicateTarget);
      expect(targetIssue.id, '-1003');
    });

    test('rejects a chat that is both a source and a target', () {
      final issue = validateRoute(
        sourcesText: '-1001, @news',
        targetsText: 'https://t.me/news',
      )!;
      expect(issue.kind, RouteIssueKind.overlap);
      expect(issue.id, '@news');
      expect(issue.messageKey, 'rules_route_overlap_id');
    });

    test('a live preview may skip the emptiness rule the save enforces', () {
      expect(
        validateRoute(
          sourcesText: '',
          targetsText: '',
          requireSources: false,
          requireTargets: false,
        ),
        isNull,
      );
    });
  });

  group('verifyRouteEntries', () {
    test('marks each entry accepted, duplicated or overlapping', () {
      final entries = verifyRouteEntries('A, A, B', peerRaw: 'B');
      expect(entries.map((e) => e.state).toList(), const [
        RouteEntryState.accepted,
        RouteEntryState.duplicate,
        RouteEntryState.overlap,
      ]);
      expect(entries.map((e) => e.value).toList(), const ['A', 'A', 'B']);
      expect(entries.first.isOk, isTrue);
      expect(entries.first.messageKey, 'rules_route_chip_ok');
      expect(entries[1].messageKey, 'rules_route_chip_duplicate');
      expect(entries[2].messageKey, 'rules_route_chip_overlap');
    });
  });

  group('ForwardRule endpoint lists', () {
    test('a legacy payload with only the singular fields parses', () {
      final rule = ForwardRule.fromJson(const {
        'id': 'r-legacy',
        'source_chat_id': '-1001',
        'target_chat_id': '-1002',
        'is_enabled': true,
      });

      expect(rule.sourceIds, const ['-1001']);
      expect(rule.targetIds, const ['-1002']);
      expect(rule.sourceChatId, '-1001');
      expect(rule.targetChatId, '-1002');
      expect(rule.isFanOut, isFalse);
    });

    test(
      'the canonical lists win and the singulars mirror their first entry',
      () {
        final rule = ForwardRule.fromJson(const {
          'id': 'r-multi',
          'source_chat_id': '-1001',
          'source_chat_ids': ['-1001', '-1002'],
          'target_chat_id': '-1003',
          'target_chat_ids': ['-1003', '-1004'],
          'is_enabled': true,
        });

        expect(rule.sourceIds, const ['-1001', '-1002']);
        expect(rule.targetIds, const ['-1003', '-1004']);
        expect(rule.sourceChatId, '-1001');
        expect(rule.targetChatId, '-1003');
        expect(rule.isFanOut, isTrue);
        expect(rule.allTargets, const ['-1003', '-1004']);
      },
    );

    test('a comma-separated string in the list field is parsed, not kept', () {
      final rule = ForwardRule.fromJson(const {
        'id': 'r-string',
        'source_chat_ids': '-1001, -1002',
        'target_chat_ids': '-1003،-1004',
        'is_enabled': true,
      });

      expect(rule.sourceIds, const ['-1001', '-1002']);
      expect(rule.targetIds, const ['-1003', '-1004']);
      expect(rule.sourceChatId, '-1001');
    });

    test('the create payload carries both lists and the legacy singulars', () {
      const rule = ForwardRule(
        id: '',
        sourceChatId: '-1001',
        sourceChatIds: ['-1001', '-1002'],
        targetChatId: '-1003',
        targetChatIds: ['-1003', '-1004'],
        isEnabled: true,
      );
      final json = rule.toCreateJson();

      expect(json['source_chat_ids'], const ['-1001', '-1002']);
      expect(json['target_chat_ids'], const ['-1003', '-1004']);
      expect(json['source_chat_id'], '-1001');
      expect(json['target_chat_id'], '-1003');
    });

    test('the update payload carries the lists too', () {
      const rule = ForwardRule(
        id: 'r-1',
        sourceChatId: '-1001',
        sourceChatIds: ['-1001', '-1002'],
        targetChatId: '-1003',
        targetChatIds: ['-1003', '-1004'],
        isEnabled: true,
        version: 4,
      );
      final json = rule.toUpdateJson();

      expect(json['source_chat_ids'], const ['-1001', '-1002']);
      expect(json['target_chat_ids'], const ['-1003', '-1004']);
      expect(json['expected_version'], 4);
    });

    test('copyWith keeps the singular consistent with the new list', () {
      const rule = ForwardRule(
        id: 'r-1',
        sourceChatId: '-1001',
        targetChatId: '-1003',
        isEnabled: true,
      );
      final updated = rule.copyWith(
        sourceChatIds: const ['-2001', '-2002'],
        targetChatIds: const ['-3003'],
      );

      expect(updated.sourceIds, const ['-2001', '-2002']);
      expect(updated.sourceChatId, '-2001');
      expect(updated.targetChatId, '-3003');
    });

    test('the fan-out list is primary-first and de-duplicated', () {
      const rule = ForwardRule(
        id: 'r-1',
        sourceChatId: '-1001',
        targetChatId: '-1003',
        targetChatIds: ['-1004', '-1003'],
        isEnabled: true,
      );

      expect(rule.allTargets, const ['-1003', '-1004']);
    });

    test('the task id is readable in its short form', () {
      const rule = ForwardRule(
        id: 'abcdefgh-1111-2222-3333',
        sourceChatId: '-1001',
        targetChatId: '-1002',
        isEnabled: true,
      );
      expect(rule.shortId, 'abcdefgh');

      const short = ForwardRule(
        id: 'r-9',
        sourceChatId: '-1001',
        targetChatId: '-1002',
        isEnabled: true,
      );
      expect(short.shortId, 'r-9');
    });
  });
}
