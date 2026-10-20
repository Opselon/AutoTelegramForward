// The auto-reply surface is 190-odd new catalog keys, and a key that exists in
// English but not in the other nine renders as English (or, worse, as the raw
// key depending on the resolver). This file is the guard the feature owns: it
// walks every `ar_*` key the English catalog defines and proves each one is
// present, non-empty and interpolating in all ten shipped locales.
//
// The family checks at the bottom are deliberate: the enumerations (days,
// tones, lengths, languages, modes, statuses, actions, tabs) are resolved
// through helper functions (`autoReplyToneKey`, `labelKey`, …), so a missing
// member is invisible to a plain `t.get('literal')` grep.
library;

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/auto_reply_models.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

/// The ten catalogs the app ships.
const _codes = ['en', 'fa', 'es', 'fr', 'it', 'pt', 'ru', 'zh', 'id', 'uz'];

/// Placeholder names in [value], e.g. `{name}` → `name`.
Set<String> _placeholders(String value) =>
    RegExp(r'\{([a-z_]+)\}').allMatches(value).map((m) => m.group(1)!).toSet();

void main() {
  final en = AppMessages(const Locale('en'));
  final arKeys = AppMessagesEn.knownKeys
      .where((key) => key.startsWith('ar_'))
      .toList(growable: false);

  group('auto-reply catalog', () {
    test('the feature defines a full vocabulary, not a handful of labels', () {
      expect(arKeys.length, greaterThan(150));
      // The five tab labels the shell renders, in order.
      for (final tab in const [
        'ar_tab_settings',
        'ar_tab_prompts',
        'ar_tab_targets',
        'ar_tab_rules',
        'ar_tab_activity',
      ]) {
        expect(arKeys, contains(tab));
      }
    });

    test('every ar_ key resolves to real text in all ten locales', () {
      for (final code in _codes) {
        final msgs = AppMessages(Locale(code));
        for (final key in arKeys) {
          final value = msgs.get(key);
          expect(
            value,
            isNot(key),
            reason: '$code rendered the raw key for $key',
          );
          expect(value.trim(), isNotEmpty, reason: '$code left $key empty');
        }
      }
    });

    test('no locale is a copy of the English text for the settings prose', () {
      // Short shared words ("Cancel", "Formal") are legitimately identical
      // across languages; the explanatory lines are not. Checking a handful of
      // prose keys catches a catalog that was padded with English.
      const proseKeys = [
        'ar_master_hint',
        'ar_context_hint',
        'ar_cooldown_hint',
        'ar_quiet_hint',
        'ar_rule_conditions_hint',
        'ar_target_query_help',
      ];
      for (final code in _codes.where((c) => c != 'en')) {
        final msgs = AppMessages(Locale(code));
        for (final key in proseKeys) {
          expect(
            msgs.get(key),
            isNot(en.get(key)),
            reason: '$code kept the English sentence for $key',
          );
        }
      }
    });

    test('placeholders survive translation and interpolate', () {
      final withPlaceholders = arKeys
          .where((key) => _placeholders(en.get(key)).isNotEmpty)
          .toList(growable: false);
      expect(withPlaceholders, isNotEmpty);

      for (final key in withPlaceholders) {
        final names = _placeholders(en.get(key));
        for (final code in _codes) {
          final msgs = AppMessages(Locale(code));
          final value = msgs.get(key);
          expect(
            _placeholders(value),
            equals(names),
            reason: '$code changed the placeholders of $key',
          );
          final params = {for (final name in names) name: 'X$name'};
          final rendered = msgs.get(key, params: params);
          for (final name in names) {
            expect(
              rendered,
              contains('X$name'),
              reason: '$code lost {$name} in $key',
            );
            expect(
              rendered,
              isNot(contains('{$name}')),
              reason: '$code left {$name} literal in $key',
            );
          }
        }
      }
    });

    test('the settings vocabulary covers every server token', () {
      // A token the server accepts but the app cannot name renders as its raw
      // value; these arrays are the contract from the Go handler.
      for (final tone in const [
        'friendly',
        'casual',
        'professional',
        'formal',
        'playful',
        'empathetic',
        'direct',
      ]) {
        expect(arKeys, contains(autoReplyToneKey(tone)), reason: tone);
      }
      for (final length in const ['short', 'medium', 'long']) {
        expect(arKeys, contains(autoReplyLengthKey(length)), reason: length);
      }
      for (final code in const [
        '',
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
      ]) {
        expect(arKeys, contains(autoReplyLanguageKey(code)), reason: code);
      }
      for (final mode in const [
        AutoReplyTargetMode.everyone,
        AutoReplyTargetMode.selectedOnly,
        AutoReplyTargetMode.allExcept,
        AutoReplyTargetMode.manual,
        'unexpected',
      ]) {
        expect(
          arKeys,
          contains(AutoReplyTargetMode.labelKey(mode)),
          reason: mode,
        );
        expect(
          arKeys,
          contains(AutoReplyTargetMode.hintKey(mode)),
          reason: mode,
        );
      }
      for (final action in const [
        AutoReplyAction.reply,
        AutoReplyAction.skip,
        'x',
      ]) {
        expect(
          arKeys,
          contains(AutoReplyAction.labelKey(action)),
          reason: action,
        );
      }
      for (var day = 0; day < 7; day++) {
        expect(arKeys, contains('ar_day_$day'));
      }
      // Job statuses arrive upper-case from the server. DEAD_LETTER is the one
      // that does not map mechanically, which is exactly why it is listed here
      // rather than derived: the catalog calls it "Dead letter", not
      // "dead_letter".
      const statusKeys = {
        'PENDING': 'ar_status_pending',
        'RETRYING': 'ar_status_retrying',
        'DEAD_LETTER': 'ar_status_dead',
        'SENT': 'ar_status_sent',
        'FAILED': 'ar_status_failed',
        'CANCELLED': 'ar_status_cancelled',
      };
      statusKeys.forEach((status, key) {
        expect(arKeys, contains(key), reason: status);
      });
    });

    test('every enum label is translated, not echoed', () {
      for (final code in _codes.where((c) => c != 'en')) {
        final msgs = AppMessages(Locale(code));
        for (var day = 0; day < 7; day++) {
          expect(msgs.get('ar_day_$day'), isNot('ar_day_$day'));
        }
        for (final tone in const [
          'friendly',
          'casual',
          'professional',
          'formal',
          'playful',
          'empathetic',
          'direct',
        ]) {
          final key = autoReplyToneKey(tone);
          expect(msgs.get(key), isNot(key), reason: '$code/$tone');
        }
      }
    });

    test('English values carry no right-to-left characters', () {
      // English is the fallback for every locale, so a Persian string pasted
      // into the English catalog would leak into left-to-right layouts.
      final rtl = RegExp(
        r'[\u0590-\u05FF\u0600-\u06FF\u0700-\u07FF\uFB1D-\uFDFD\uFE70-\uFEFF]',
      );
      for (final key in arKeys) {
        expect(
          rtl.hasMatch(en.get(key)),
          isFalse,
          reason: 'English value for $key contains RTL characters',
        );
      }
    });

    // Every `ar_` literal in the feature is a catalog key unless it is a widget
    // key. This is the check that catches a key written across two lines
    // (`t.get(` then `'ar_job_attempt',`) — a plain grep for `t.get('ar_` misses
    // those, and a missing key asserts in debug and renders raw in release.
    test('every ar_ literal in the feature exists in the catalog', () {
      final root = Directory('lib').existsSync()
          ? Directory('lib/features/auto_reply')
          : Directory('mobile/lib/features/auto_reply');
      expect(root.existsSync(), isTrue, reason: 'run from the mobile root');

      final literal = RegExp(r"'(ar_[a-z0-9_]+)'");
      final offenders = <String>[];
      for (final file in root.listSync().whereType<File>()) {
        if (!file.path.endsWith('.dart')) continue;
        final lines = file.readAsLinesSync();
        for (var i = 0; i < lines.length; i++) {
          final line = lines[i];
          // Widget keys (`ValueKey<String>('ar_master_switch')`) are identities,
          // not copy, and `keyPrefix` names a key family.
          if (line.contains('ValueKey') || line.contains('keyPrefix')) continue;
          for (final match in literal.allMatches(line)) {
            final key = match.group(1)!;
            if (!AppMessagesEn.knownKeys.contains(key)) {
              offenders.add('${file.path}:${i + 1} $key');
            }
          }
        }
      }
      expect(offenders, isEmpty, reason: 'undefined catalog keys: $offenders');
    });
  });
}
