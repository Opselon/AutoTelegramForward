/// The branding hop (A → C → B) surface on the client.
///
/// The hop itself is two first-class rule fields (`use_intermediate` /
/// `intermediate_channel_id`); what is new here is the *trigger policy* the
/// server keeps in the rule metadata under `branding_hop`, and the one-line
/// summary the editor renders from it. Two things must never happen:
///
///   * a rule whose metadata is absent or malformed must render the
///     unconditional route instead of throwing inside a build, and
///   * the summary must describe exactly what the server's `should_hop` reads:
///     forward-origin channel names/ids, keywords and the any/all mode.
library;

import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/models.dart';
import 'package:autoforward_mobile/features/rules/rule_editor_screen.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

const _en = AppMessages(Locale('en'));

/// A rule payload the way the API returns it, optionally carrying the branding
/// metadata blob.
ForwardRule _rule(Object? brandingHop) =>
    ForwardRule.fromJson(<String, dynamic>{
      'id': 'r-1',
      'source_chat_id': '-1001111111111',
      'target_chat_id': '-1002222222222',
      'intermediate_channel_id': '-1009999999999',
      'use_vip_routing': true,
      'is_enabled': true,
      if (brandingHop != null)
        'metadata': <String, dynamic>{'branding_hop': brandingHop},
    });

void main() {
  group('BrandingHopConfig.fromValue', () {
    test('absent metadata is the unconditional hop', () {
      final cfg = _rule(null).brandingHop;
      expect(cfg.triggersEnabled, isFalse);
      expect(cfg.originIds, isEmpty);
      expect(cfg.originTitles, isEmpty);
      expect(cfg.keywords, isEmpty);
      expect(cfg.matchMode, 'any');
      expect(cfg.requireForward, isFalse);
      expect(cfg.forceForward, isTrue);
      expect(cfg.hasTriggers, isFalse);
    });

    test('malformed metadata never throws and degrades to unconditional', () {
      for (final junk in <Object?>[
        'not-a-map',
        42,
        const <Object?>[1, 2, 3],
        const <String, Object?>{
          'triggers_enabled': 'yes',
          'origin_titles': 'nope',
        },
        const <String, Object?>{
          'keywords': <Object?>[
            <String>['nested'],
            null,
            ' ok ',
          ],
        },
      ]) {
        final cfg = _rule(junk).brandingHop;
        expect(cfg.hasTriggers, isFalse, reason: 'junk: $junk');
      }
    });

    test('reads every trigger key the server writes', () {
      final cfg = _rule(const <String, Object?>{
        'triggers_enabled': true,
        'origin_ids': <Object?>['-1001', '-1001', '', ' @news '],
        'origin_titles': <Object?>['Sadaf News'],
        'keywords': <Object?>['تعرفه', 'tariff'],
        'match_mode': 'all',
        'require_forward': true,
        'force_forward': false,
      }).brandingHop;

      expect(cfg.triggersEnabled, isTrue);
      expect(cfg.originIds, const [
        '-1001',
        '@news',
      ], reason: 'trim + de-duplicate');
      expect(cfg.originTitles, const ['Sadaf News']);
      expect(cfg.keywords, const ['تعرفه', 'tariff']);
      expect(cfg.matchMode, 'all');
      expect(cfg.requireForward, isTrue);
      expect(cfg.forceForward, isFalse);
      expect(cfg.hasTriggers, isTrue);
    });

    test('an unknown match mode normalises to any', () {
      expect(_rule(const {'match_mode': 'SOME'}).brandingHop.matchMode, 'any');
      expect(_rule(const {'match_mode': 'ALL'}).brandingHop.matchMode, 'all');
    });

    test(
      'switched-on triggers with nothing configured mean "every message"',
      () {
        final cfg = _rule(const {'triggers_enabled': true}).brandingHop;
        expect(cfg.triggersEnabled, isTrue);
        expect(cfg.hasTriggers, isFalse);
      },
    );

    test('the branding blob is never sent back: metadata has one writer', () {
      // The branding blob is server-owned; editing a task must not invent a
      // second write path for it.
      final rule = _rule(const {
        'triggers_enabled': true,
        'keywords': ['x'],
      });
      expect(rule.toCreateJson().containsKey('metadata'), isFalse);
      expect(rule.toCreateJson().containsKey('branding_hop'), isFalse);
      expect(rule.toUpdateJson().containsKey('metadata'), isFalse);
    });
  });

  group('brandingHopSummary', () {
    test('without triggers it states the unconditional route', () {
      expect(
        brandingHopSummary(_en, _rule(null).brandingHop),
        'A → C → B · every message',
      );
    });

    test('names the configured origin channels', () {
      final cfg = _rule(const {
        'triggers_enabled': true,
        'origin_titles': ['Sadaf News', 'Market Feed'],
      }).brandingHop;
      expect(
        brandingHopSummary(_en, cfg),
        'A → C → B · forwarded from Sadaf News, Market Feed',
      );
    });

    test('counts keywords and ids instead of listing them', () {
      final cfg = _rule(const {
        'triggers_enabled': true,
        'keywords': ['tariff', 'rate', 'usd'],
        'origin_ids': ['-1001', '-1002'],
      }).brandingHop;
      expect(
        brandingHopSummary(_en, cfg),
        'A → C → B · any trigger · 2 channel id(s) · 3 keyword(s)',
      );
    });

    test('the all mode is spelled out, the single-condition any is not', () {
      final single = _rule(const {
        'triggers_enabled': true,
        'match_mode': 'all',
        'keywords': ['tariff'],
      }).brandingHop;
      expect(brandingHopSummary(_en, single), 'A → C → B · 1 keyword(s)');

      final multi = _rule(const {
        'triggers_enabled': true,
        'match_mode': 'all',
        'origin_ids': ['-1001'],
        'keywords': ['tariff'],
      }).brandingHop;
      expect(
        brandingHopSummary(_en, multi),
        'A → C → B · every trigger · 1 channel id(s) · 1 keyword(s)',
      );
    });

    test('require_forward is reported as a gate', () {
      final cfg = _rule(const {
        'triggers_enabled': true,
        'require_forward': true,
      }).brandingHop;
      expect(
        brandingHopSummary(_en, cfg),
        'A → C → B · forwarded messages only',
      );
    });

    test('a torn blob renders the unconditional route, never a raw key', () {
      for (final junk in <Object?>[
        null,
        'oops',
        const <int>[1],
      ]) {
        final text = brandingHopSummary(_en, _rule(junk).brandingHop);
        expect(text, 'A → C → B · every message');
        expect(text, isNot(contains('rules_intermediate')));
      }
    });

    test('every locale renders a summary with no placeholder left behind', () {
      final cfg = _rule(const {
        'triggers_enabled': true,
        'origin_ids': ['-1001'],
        'keywords': ['tariff'],
        'require_forward': true,
      }).brandingHop;
      for (final locale in AppMessages.supportedLocales) {
        final t = AppMessages(locale);
        final text = brandingHopSummary(t, cfg);
        expect(
          text,
          isNot(contains('{')),
          reason: '${locale.languageCode}: $text',
        );
        expect(text.trim(), isNotEmpty);
        expect(
          text,
          startsWith('A → C → B'),
          reason: '${locale.languageCode} must keep the readable hop',
        );
      }
    });
  });
}
