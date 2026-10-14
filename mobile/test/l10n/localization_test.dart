import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/l10n/app_fa.dart';
import 'package:autoforward_mobile/l10n/app_messages.dart';

void main() {
  group('Localization dictionaries', () {
    test('English dictionary has essential keys', () {
      final en = AppMessages(const Locale('en'));
      expect(en.get('app_name'), contains('AutoTelegramForward'));
      expect(en.get('login_title'), isNotEmpty);
      expect(en.get('sessions_title'), isNotEmpty);
      expect(en.get('rules_title'), isNotEmpty);
      expect(en.get('action_save'), isNotEmpty);
      expect(en.isRtl, isFalse);
    });

    test('Persian dictionary mirrors essential keys and enables RTL', () {
      final fa = AppMessages(const Locale('fa'));
      expect(fa.get('app_name'), contains('اتوتلگرام فوروارد'));
      expect(fa.get('login_title'), isNotEmpty);
      expect(fa.get('sessions_title'), isNotEmpty);
      expect(fa.isRtl, isTrue);
    });

    test('AppMessagesFa instance has translations', () {
      final fa = AppMessagesFa.instance;
      expect(fa.get('app_name'), contains('اتوتلگرام فوروارد'));
      expect(fa.get('sessions_add'), isNotEmpty);
    });

    test('Persian translations exist for a representative key sample', () {
      final fa = AppMessagesFa.instance;
      const keys = [
        'login_title',
        'login_username',
        'login_password',
        'sessions_title',
        'rules_title',
        'action_retry',
      ];
      for (final key in keys) {
        expect(
          fa.get(key),
          isNot(key),
          reason: 'Persian dictionary should translate "$key"',
        );
      }
    });

    test('Login challenge state translations work in both locales', () {
      final en = AppMessages(const Locale('en'));
      final fa = AppMessages(const Locale('fa'));

      const challenge = LoginChallenge(
        id: 'c-1',
        state: LoginChallengeState.awaitingCode,
        message: 'code sent',
      );

      expect(
        en.loginState(challenge, '+989121234567'),
        contains('+989121234567'),
      );
      expect(
        fa.loginState(challenge, '+989121234567'),
        contains('+989121234567'),
      );
    });


    // ---- 10-language parity guard --------------------------------------
    // Every supported catalog must expose the complete key set, including
    // the per-task config keys (replace rules, AI picker, add-ons).
    test('every supported locale mirrors the full English key set', () {
      const codes = [
        'fa',
        'es',
        'fr',
        'id',
        'it',
        'pt',
        'ru',
        'uz',
        'zh',
      ];
      const probeKeys = <String>[
        'rules_title',
        'rules_replace_title',
        'rules_replace_find',
        'rules_replace_with',
        'rules_replace_add',
        'rules_replace_summary',
        'rules_ai_pick_title',
        'rules_ai_pick_none',
        'rules_ai_pick_empty',
        'rules_sec_processing',
        'rules_sec_modify',
        'rules_sec_ai',
        'rules_sec_paid',
        'action_save',
        'action_cancel',
        'action_delete',
        'state_error',
        'state_empty',
      ];
      for (final code in codes) {
        final msgs = AppMessages(Locale(code));
        for (final key in probeKeys) {
          expect(
            msgs.get(key),
            isNot(key),
            reason: '$code is missing translation key $key',
          );
        }
      }
      expect(
        AppMessages.supportedLocales.map((l) => l.languageCode),
        containsAll(const [
          'en',
          'fa',
          'es',
          'fr',
          'id',
          'it',
          'pt',
          'ru',
          'uz',
          'zh',
        ]),
      );
    });
  });
}
