// The filter editor is the only screen that writes a FilterRule, and the model
// is the only place the app maps what a user typed to what the engine stores.
//
// The model used to carry a single `pattern` string, which the API has no
// field for — a blacklist typed in the app was silently dropped server-side,
// so the filter looked saved while the engine never saw it. These tests pin
// the contract the fix restored:
//
// * toJson emits the proto's real lists (whitelist_keywords,
//   blacklist_keywords, regex_patterns) and never a dead `pattern`/`kind`;
// * a multi-word phrase survives a round trip as ONE entry — the bug from the
//   bot ("Whale Update on a VIP call" cut into fragments that match nothing)
//   must not come back on this surface;
// * splitKeywordList cuts only on the separators the engine's split_word_list
//   accepts (comma, Persian/Arabic comma, newline), never on a space.
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/models.dart';

void main() {
  group('splitKeywordList', () {
    test('a comma separates entries', () {
      expect(splitKeywordList('spam, scam'), ['spam', 'scam']);
      expect(splitKeywordList('spam,scam ,  promo'), ['spam', 'scam', 'promo']);
    });

    test('a newline separates entries', () {
      expect(splitKeywordList('spam\nscam'), ['spam', 'scam']);
      expect(splitKeywordList('spam\r\nscam'), ['spam', 'scam']);
    });

    test('the Persian/Arabic comma separates entries', () {
      // A Persian keyboard offers \u060c directly, not the ASCII comma.
      expect(splitKeywordList('اسپم، کلاهبرداری'), ['اسپم', 'کلاهبرداری']);
    });

    test('a space never separates an entry — a phrase stays whole', () {
      expect(splitKeywordList('Whale Update on a VIP call'), [
        'Whale Update on a VIP call',
      ]);
      // The exact reported defect: the phrase typed next to a short keyword.
      expect(splitKeywordList('5x, 10x\nWhale Update on a VIP call'), [
        '5x',
        '10x',
        'Whale Update on a VIP call',
      ]);
    });

    test('entries are trimmed, empties dropped, duplicates removed', () {
      expect(splitKeywordList('  , spam ,,, spam,'), ['spam']);
      expect(splitKeywordList(''), <String>[]);
    });
  });

  group('FilterRule', () {
    test('toJson emits the fields the API actually carries', () {
      final json = FilterRule(
        id: 'f1',
        name: 'Anti-spam',
        blacklistKeywords: ['scam', 'Whale Update on a VIP call'],
      ).toJson();
      expect(json['blacklist_keywords'], [
        'scam',
        'Whale Update on a VIP call',
      ]);
      expect(json['whitelist_keywords'], <String>[]);
      expect(json['name'], 'Anti-spam');
      // The dead keys the API silently dropped must not come back.
      expect(json.containsKey('pattern'), isFalse);
      expect(json.containsKey('kind'), isFalse);
    });

    test('a phrase survives a JSON round trip as one entry', () {
      final rule = FilterRule.fromJson({
        'id': 'f1',
        'name': 'vip',
        'whitelist_keywords': ['5x', 'Whale Update on a VIP call'],
        'blacklist_keywords': <String>[],
      });
      expect(rule.whitelistKeywords, ['5x', 'Whale Update on a VIP call']);
      expect(rule.keywordEntries, ['5x', 'Whale Update on a VIP call']);
      expect(rule.isBlacklist, isFalse);
      // What the list screen shows: the phrase, not fragments of it.
      expect(rule.keywordEntries.join(', '), '5x, Whale Update on a VIP call');
    });

    test('a blacklist rule reports itself as a blacklist', () {
      final rule = FilterRule.fromJson({
        'id': 'f2',
        'name': 'block',
        'blacklist_keywords': ['SCAM'],
      });
      expect(rule.isBlacklist, isTrue);
      expect(rule.keywordEntries, ['SCAM']);
    });

    test('copyWith keeps the untouched list intact', () {
      final rule = FilterRule(
        id: 'f1',
        name: 'vip',
        blacklistKeywords: ['scam'],
        whitelistKeywords: ['5x'],
      );
      final updated = rule.copyWith(blacklistKeywords: ['scam', 'promo']);
      expect(updated.blacklistKeywords, ['scam', 'promo']);
      // An edit to one list must not wipe the other.
      expect(updated.whitelistKeywords, ['5x']);
      expect(updated.name, 'vip');
    });

    test('a missing or malformed list degrades to empty, never a throw', () {
      final rule = FilterRule.fromJson({'id': 'f3', 'name': 'x'});
      expect(rule.whitelistKeywords, <String>[]);
      expect(rule.blacklistKeywords, <String>[]);
      expect(rule.keywordEntries, <String>[]);
    });
  });
}
