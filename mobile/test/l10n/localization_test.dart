import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:autoforward_mobile/core/api_client.dart';
import 'package:autoforward_mobile/l10n/app_es.dart';
import 'package:autoforward_mobile/l10n/app_fa.dart';
import 'package:autoforward_mobile/l10n/app_fr.dart';
import 'package:autoforward_mobile/l10n/app_id.dart';
import 'package:autoforward_mobile/l10n/app_it.dart';
import 'package:autoforward_mobile/l10n/app_pt.dart';
import 'package:autoforward_mobile/l10n/app_ru.dart';
import 'package:autoforward_mobile/l10n/app_uz.dart';
import 'package:autoforward_mobile/l10n/app_zh.dart';
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
      const codes = ['fa', 'es', 'fr', 'id', 'it', 'pt', 'ru', 'uz', 'zh'];
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

    // ---- exact set parity, both directions ------------------------------
    // The probe list above can only catch a *missing* key for keys someone
    // remembered to list. This compares the compiled maps, so a key added to
    // one locale only — or to English only — fails here, and a value's quoting
    // style cannot hide it from the check.
    test('every locale defines exactly the English key set', () {
      final en = AppMessagesEn.knownKeys.toSet();
      expect(en, isNotEmpty);

      final catalogs = <String, Iterable<String>>{
        'fa': AppMessagesFa.knownKeys,
        'es': AppMessagesEs.knownKeys,
        'fr': AppMessagesFr.knownKeys,
        'id': AppMessagesId.knownKeys,
        'it': AppMessagesIt.knownKeys,
        'pt': AppMessagesPt.knownKeys,
        'ru': AppMessagesRu.knownKeys,
        'uz': AppMessagesUz.knownKeys,
        'zh': AppMessagesZh.knownKeys,
      };

      catalogs.forEach((code, keys) {
        final set = keys.toSet();
        expect(
          en.difference(set),
          isEmpty,
          reason: '$code is missing ${en.difference(set)}',
        );
        expect(
          set.difference(en),
          isEmpty,
          reason: '$code has keys English does not: ${set.difference(en)}',
        );
      });
    });

    // ---- the v18 log-control surface -------------------------------------
    test('log control and full control keys resolve in every locale', () {
      const keys = <String>[
        'logs_scope_task',
        'logs_scope_service',
        'logs_all_tasks',
        'logs_search_hint',
        'logs_order_toggle_desc',
        'logs_page_size',
        'logs_export_evidence',
        'logs_export_json',
        'logs_export_csv',
        'logs_copy',
        'logs_history_from',
        'logs_newer',
        'logs_older',
        'logs_end_of_history',
        'logs_scope_task_chip',
        'logs_scope_corr_chip',
        'logs_level_errors',
        'logs_level_warnings',
        'logs_level_info',
        'logs_level_debug',
        'rules_full_control',
        'rules_full_control_desc',
        'rules_logs_action',
        'rules_logs_title',
        'rules_settings_count',
        'rules_settings_apply',
        'rules_settings_min',
        'rules_settings_max',
        'rules_settings_invalid',
      ];
      for (final locale in AppMessages.supportedLocales) {
        final msgs = AppMessages(locale);
        for (final key in keys) {
          expect(
            msgs.get(key),
            isNot(key),
            reason: '${locale.languageCode} is missing $key',
          );
        }
      }
    });

    // English must never carry right-to-left characters: it is the fallback for
    // every other locale, so a stray Persian value would leak into a UI that
    // renders left-to-right.
    test('English values contain no right-to-left characters', () {
      final rtl = RegExp(
        r'[\u0590-\u05FF\u0600-\u06FF\u0700-\u07FF\uFB1D-\uFDFD\uFE70-\uFEFF]',
      );
      final en = AppMessages(const Locale('en'));
      for (final key in AppMessagesEn.knownKeys) {
        expect(
          rtl.hasMatch(en.get(key)),
          isFalse,
          reason: 'English value for $key contains RTL characters',
        );
      }
    });
  });
}
