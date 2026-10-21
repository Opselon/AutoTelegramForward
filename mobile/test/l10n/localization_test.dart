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
import 'package:autoforward_mobile/features/rules/rule_settings_sheet.dart';

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
      // Every key the system surface renders: the System screen, the dashboard
      // readiness hero and the settings language picker.
      const keys = <String>[
        'sys_title',
        'sys_subtitle',
        'sys_ready',
        'sys_not_ready',
        'sys_deployment',
        'sys_product_version',
        'sys_api_version',
        'sys_commit',
        'sys_features',
        'sys_services_title',
        'sys_contract',
        'sys_level_required',
        'sys_level_recommended',
        'sys_level_optional',
        'sys_check_set',
        'sys_check_unset',
        'sys_missing_required',
        'sys_next_steps',
        'sys_latency',
        'sys_probed_at',
        'sys_open',
        'sys_services_online',
        'sys_unavailable',
        'status_online',
        'status_offline',
        'settings_language_hint',
        'lang_label',
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

    // ---- the branding hop (A → C → B) ------------------------------------
    // The editor's section title, its route summary and the settings-sheet
    // group header are catalog-driven, so all three must resolve to real text
    // in every shipped locale — a group header that fell back to the raw
    // server identifier would be the classic untranslated surface.
    test('the branding-hop surface resolves in all ten locales', () {
      const keys = <String>[
        'rules_intermediate_title',
        'rules_intermediate_hint',
        'rules_intermediate_summary_all',
        'rules_intermediate_summary_triggers',
        'rules_intermediate_cond_origins',
        'rules_intermediate_cond_ids',
        'rules_intermediate_cond_keywords',
        'rules_intermediate_cond_forward_only',
        'rules_intermediate_match_any',
        'rules_intermediate_match_all',
        'rules_settings_group_branding',
        'rules_settings_group_route',
      ];
      for (final locale in AppMessages.supportedLocales) {
        final msgs = AppMessages(locale);
        for (final key in keys) {
          final value = msgs.get(key);
          expect(
            value,
            isNot(key),
            reason: '${locale.languageCode} rendered the raw key for $key',
          );
          expect(
            value.trim(),
            isNotEmpty,
            reason: '${locale.languageCode} left $key empty',
          );
        }
      }
      // The hop stays readable: every locale keeps the A → C → B shape.
      for (final locale in AppMessages.supportedLocales) {
        expect(
          AppMessages(locale).get('rules_intermediate_summary_all'),
          startsWith('A → C → B'),
          reason: '${locale.languageCode} lost the readable route',
        );
      }
    });

    // The settings sheet groups settings by the server's registry group name.
    // Those names are identifiers (`branding`), so a translated group must use
    // its catalog label and an unknown group must never render as a raw key.
    test('settings group headers are translated, never raw identifiers', () {
      const groups = ['branding', 'route'];
      // Per group, per locale: a single flat map silently keeps only the last
      // group's label, which is how this test would stop checking the first one.
      final labels = <String, Map<String, String>>{};
      for (final group in groups) {
        final perLocale = <String, String>{};
        for (final locale in AppMessages.supportedLocales) {
          final msgs = AppMessages(locale);
          final label = ruleSettingsGroupTitle(msgs, group);
          expect(label, isNot(contains('rules_settings_group_')));
          expect(label.trim(), isNotEmpty);
          expect(
            label,
            isNot(group.toUpperCase()),
            reason: 'raw identifier leaked for $group',
          );
          perLocale[locale.languageCode] = label;
        }
        labels[group] = perLocale;
      }
      // English is the reference: no locale falls back to it, and English is
      // itself the authored label rather than a title-cased identifier.
      expect(labels['branding']!['en'], 'Branding hop (A → C → B)');
      expect(labels['route']!['en'], 'Delivery route');
      for (final code in const [
        'fa',
        'ru',
        'zh',
        'es',
        'fr',
        'it',
        'pt',
        'uz',
        'id',
      ]) {
        for (final group in groups) {
          expect(
            labels[group]![code],
            isNot(labels[group]!['en']),
            reason: '$code kept the English label for the $group group',
          );
        }
      }
      // A group no catalog names is title-cased, not rendered as its raw key.
      final en = AppMessages(const Locale('en'));
      expect(ruleSettingsGroupTitle(en, 'content_cleanup'), 'CONTENT CLEANUP');
      expect(ruleSettingsGroupTitle(en, ''), 'GENERAL');
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
        // Language endonyms are the one legitimate RTL value in English: a user
        // choosing Persian must see "فارسی", not a transliteration.
        if (key.startsWith('lang_native_')) continue;
        expect(
          rtl.hasMatch(en.get(key)),
          isFalse,
          reason: 'English value for $key contains RTL characters',
        );
      }
    });
  });

  group('Locale resolution', () {
    // Eight of the ten catalogs were unreachable from the UI: the settings
    // picker listed only English and Persian, and the delegate only claimed to
    // support those two - so choosing Italian silently rendered English.
    test('the delegate supports every shipped locale', () {
      for (final locale in AppMessages.supportedLocales) {
        expect(
          AppMessages.delegate.isSupported(locale),
          isTrue,
          reason: '${locale.languageCode} must be loadable',
        );
      }
      expect(AppMessages.delegate.isSupported(const Locale('nl')), isFalse);
    });

    test('the picker has a native name for every supported locale', () {
      final msgs = AppMessages(const Locale('en'));
      for (final locale in AppMessages.supportedLocales) {
        final label = msgs.get('lang_native_${locale.languageCode}');
        expect(
          label,
          isNot('lang_native_${locale.languageCode}'),
          reason: 'no endonym for ${locale.languageCode}',
        );
        expect(label.trim(), isNotEmpty);
      }
    });

    test('every English key resolves to real text in all ten locales', () {
      // The resolver asserts (debug) on a key no catalog defines, and a
      // translation gap used to render the raw key. Walking the English catalog
      // against every locale is the cross-locale completeness check that
      // catches both.
      for (final locale in AppMessages.supportedLocales) {
        final msgs = AppMessages(locale);
        for (final key in AppMessagesEn.knownKeys) {
          final value = msgs.get(key);
          expect(
            value,
            isNot(key),
            reason: '${locale.languageCode} rendered the raw key for $key',
          );
          expect(
            value.trim(),
            isNotEmpty,
            reason: '${locale.languageCode} left $key empty',
          );
        }
      }
    });

    test('the new system surface resolves in all ten locales', () {
      const keys = <String>[
        'sys_title',
        'sys_subtitle',
        'sys_ready',
        'sys_not_ready',
        'sys_deployment',
        'sys_product_version',
        'sys_api_version',
        'sys_commit',
        'sys_features',
        'sys_services_title',
        'sys_contract',
        'sys_level_required',
        'sys_level_recommended',
        'sys_level_optional',
        'sys_check_set',
        'sys_check_unset',
        'sys_missing_required',
        'sys_next_steps',
        'sys_latency',
        'sys_probed_at',
        'sys_open',
        'sys_services_online',
        'sys_unavailable',
        'status_online',
        'status_offline',
        'settings_language_hint',
        'lang_label',
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

    test('placeholders interpolate in every locale', () {
      const cases = <String, Map<String, String>>{
        'sys_services_online': {'online': '3', 'total': '4'},
        'sys_probed_at': {'time': '12:00'},
        'sys_missing_required': {'count': '2'},
        'sys_latency': {'ms': '17'},
      };
      for (final locale in AppMessages.supportedLocales) {
        final msgs = AppMessages(locale);
        cases.forEach((key, params) {
          final value = msgs.get(key, params: params);
          params.forEach((name, expected) {
            expect(
              value,
              contains(expected),
              reason: '${locale.languageCode} lost {$name} in $key',
            );
            expect(
              value,
              isNot(contains('{$name}')),
              reason: '${locale.languageCode} left {$name} literal in $key',
            );
          });
        });
      }
    });

    // ---- the replace-rule manager surface --------------------------------
    // Every key the Replace screen renders must exist in all ten catalogs, and
    // the placeholders must survive the translation: a row that renders
    // "Page {page} of {pages}" literally is worse than an English label,
    // because it looks like a bug in the data.
    test('the replace manager resolves in every locale', () {
      const keys = <String>[
        'nav_replace',
        'rp_title',
        'rp_counts',
        'rp_search_hint',
        'rp_filter_all',
        'rp_filter_enabled',
        'rp_filter_disabled',
        'rp_task_filter',
        'rp_new',
        'rp_empty_title',
        'rp_empty_filtered',
        'rp_prev',
        'rp_next',
        'rp_page_of',
        'rp_status_on',
        'rp_status_off',
        'rp_enable',
        'rp_disable',
        'rp_duplicate',
        'rp_test',
        'rp_create_title',
        'rp_edit_title',
        'rp_delete_title',
        'rp_delete_confirm',
        'rp_mode_label',
        'rp_mode_literal',
        'rp_mode_regex',
        'rp_find_label',
        'rp_replace_label',
        'rp_priority_label',
        'rp_tasks_label',
        'rp_test_sample_label',
        'rp_test_input',
        'rp_test_output',
        'rp_test_unchanged',
        'rp_no_tasks',
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

    test(
      'replace placeholders interpolate and the arrow follows the script',
      () {
        const cases = <String, Map<String, String>>{
          'rp_counts': {'total': '9', 'on': '6', 'off': '3'},
          'rp_page_of': {'page': '2', 'pages': '5'},
          'rp_priority_short': {'priority': '300'},
          'rp_runs_on': {'n': '2', 'names': 'News, Deals'},
        };
        for (final locale in AppMessages.supportedLocales) {
          final msgs = AppMessages(locale);
          cases.forEach((key, params) {
            final value = msgs.get(key, params: params);
            params.forEach((name, expected) {
              expect(
                value,
                contains(expected),
                reason: '${locale.languageCode} lost {$name} in $key',
              );
              expect(
                value,
                isNot(contains('{$name}')),
                reason: '${locale.languageCode} left {$name} literal in $key',
              );
            });
          });
        }

        // RTL points left, so the source -> replacement line must not hardcode
        // "->" and leave a Persian reader following it backwards.
        final fa = AppMessages(const Locale('fa'));
        final en = AppMessages(const Locale('en'));
        final faArrow = fa.get('rp_arrow_to', params: {'text': 'سلام'});
        final enArrow = en.get('rp_arrow_to', params: {'text': 'hi'});
        expect(faArrow, contains('\u2190'));
        expect(enArrow, contains('\u2192'));
        expect(faArrow, isNot(contains('{text}')));
        expect(enArrow, isNot(contains('{text}')));
      },
    );

    test('the arrows differ by direction, not by accident', () {
      // A locale that translated the string but kept the LTR arrow would read
      // correctly to nobody; only Persian is RTL today, and it is the one that
      // must flip.
      for (final locale in AppMessages.supportedLocales) {
        final arrow = AppMessages(locale)
            .get('rp_arrow_to', params: {'text': 'x'});
        if (locale.languageCode == 'fa') {
          expect(arrow, contains('\u2190'), reason: 'fa must point left');
        } else {
          expect(
            arrow,
            contains('\u2192'),
            reason: '${locale.languageCode} points right',
          );
        }
      }
    });
  });
}
