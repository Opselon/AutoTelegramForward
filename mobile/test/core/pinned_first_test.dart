// The ordering rule every chat picker on the app shares with the bot and the
// dashboard: the chats a user pinned deliberately lead, because those are the
// ones a task is usually built around.
//
// These tests run the pure function rather than pumping the sheet: the sheet
// composes the rule with filtering, paging and service state, so a failure
// there could be any of those, while a failure here names exactly the
// behaviour that was lost.
import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/models.dart';

ChatSummary _chat(String id, {bool pinned = false, String type = 'channel'}) =>
    ChatSummary(chatId: id, chatType: type, pinned: pinned);

void main() {
  group('pinnedFirst', () {
    test('a list with nothing pinned is returned in the server order', () {
      final chats = [_chat('a'), _chat('b'), _chat('c')];
      final ordered = pinnedFirst(chats);
      expect(ordered.map((c) => c.chatId), ['a', 'b', 'c']);
    });

    test('a pinned chat leads; the rest keep the server order', () {
      final ordered = pinnedFirst([
        _chat('a'),
        _chat('b'),
        _chat('c', pinned: true),
        _chat('d'),
      ]);
      expect(ordered.map((c) => c.chatId), ['c', 'a', 'b', 'd']);
    });

    test('several pinned chats keep their own arrival order', () {
      final ordered = pinnedFirst([
        _chat('x'),
        _chat('p2', pinned: true),
        _chat('x2'),
        _chat('p1', pinned: true),
      ]);
      expect(ordered.map((c) => c.chatId), ['p2', 'p1', 'x', 'x2']);
    });

    test('absent and false both parse as not pinned (older servers)', () {
      final ordered = pinnedFirst([
        _chat('no-flag'),
        _chat('explicit-false', pinned: false),
        _chat('explicit-true', pinned: true),
      ]);
      expect(ordered.map((c) => c.chatId), [
        'explicit-true',
        'no-flag',
        'explicit-false',
      ]);
    });

    test('an empty list stays empty', () {
      expect(pinnedFirst(const <ChatSummary>[]), isEmpty);
    });

    test(
      'the result is unmodifiable: a picker cannot mutate its source list',
      () {
        final ordered = pinnedFirst([_chat('a', pinned: true), _chat('b')]);
        expect(() => ordered.add(_chat('c')), throwsUnsupportedError);
      },
    );

    test('the input order is untouched (the server list is read-only)', () {
      final source = [_chat('a'), _chat('b', pinned: true)];
      pinnedFirst(source);
      expect(source.map((c) => c.chatId), ['a', 'b']);
    });
  });

  group('ChatSummary.pinned parsing', () {
    test('a payload without the key parses as not pinned', () {
      final chat = ChatSummary.fromJson({
        'chat_id': '-100123',
        'chat_type': 'channel',
      });
      expect(chat.pinned, isFalse);
    });

    test('the key survives the round trip when true', () {
      final chat = ChatSummary.fromJson({
        'chat_id': '-100123',
        'chat_type': 'channel',
        'pinned': true,
      });
      expect(chat.pinned, isTrue);
    });
  });
}
