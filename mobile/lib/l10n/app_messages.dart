/// English (source) strings. See `app_fa.dart` for the Persian mirror.
///
/// Every user-visible message in the app goes through [AppMessages] so that
/// a missing translation key fails loudly (`fallback` asserts) rather than
/// rendering an empty widget.
library;

import 'package:flutter/widgets.dart';

import '../core/api_client.dart';
import 'app_es.dart';
import 'app_fa.dart';
import 'app_fr.dart';
import 'app_id.dart';
import 'app_it.dart';
import 'app_pt.dart';
import 'app_ru.dart';
import 'app_uz.dart';
import 'app_zh.dart';

class AppMessagesEn {
  static const Map<String, String> _map = {
    // app
    'app_name': 'AutoTelegramForward',
    'app_tagline': 'Production Telegram message forwarding engine',

    // generic
    'action_retry': 'Retry',
    'action_cancel': 'Cancel',
    'action_save': 'Save',
    'action_delete': 'Delete',
    'action_edit': 'Edit',
    'action_confirm': 'Confirm',
    'action_close': 'Close',
    'action_refresh': 'Refresh',
    'action_continue': 'Continue',
    'action_back': 'Back',
    'action_yes': 'Yes',
    'action_no': 'No',
    'state_loading': 'Loading…',
    'state_empty': 'Nothing here yet',
    'state_error': 'Something went wrong',
    'state_offline': 'No connection to the server',

    // auth
    'login_title': 'Sign in',
    'login_username': 'Username',
    'login_password': 'Password',
    'login_submit': 'Sign in',
    'login_register': 'Create account',
    'login_error_unauthorized': 'Wrong username or password',
    'login_error_network': 'Cannot reach the server',
    'login_session_expired': 'Your session expired. Please sign in again.',
    'login_server_label': 'Server',
    'login_server_change': 'Change server',
    'login_server_invalid': 'Enter a valid https:// server address',
    'login_no_account': 'No account yet?',
    'login_have_account': 'Already have an account?',

    // server connection
    'server_probe_test': 'Test connection',
    'server_probe_testing': 'Testing connection…',
    'server_probe_reachable': 'Server reachable and compatible',
    'server_probe_unreachable':
        'Cannot reach the server. Check the address and your connection.',
    'server_probe_bad_response':
        'The server answered but returned an unexpected response.',
    'server_probe_no_version':
        'This server does not expose a version endpoint. Check the address or update the server.',
    'server_probe_tls_error':
        'Secure connection failed. Check the server certificate and use https://.',
    'server_restore_default': 'Restore official default',

    // nav
    'nav_dashboard': 'Dashboard',
    'nav_rules': 'Rules',
    'nav_sessions': 'Sessions',
    'nav_filters': 'Filters',
    'nav_ai': 'AI',
    'nav_settings': 'Settings',
    'nav_logs': 'Logs',

    // dashboard
    'dash_title': 'Dashboard',
    'dash_total_rules': 'Rules',
    'dash_active_rules': 'Active rules',
    'dash_sessions': 'Sessions',
    'dash_active_sessions': 'Connected',
    'dash_core': 'Core service',
    'dash_uptime': 'Uptime',
    'dash_delivered': 'Delivered',
    'dash_failed': 'Failed',
    'dash_health': 'Service health',
    'dash_recent_errors': 'Recent errors',
    'diag_title': 'Preflight checks',
    'diag_all_ok': 'No problems found — every task is healthy.',

    // sessions
    'sessions_title': 'Telegram sessions',
    'sessions_empty':
        'No Telegram account is linked yet. Add one to start forwarding.',
    'sessions_add': 'Link a Telegram account',
    'sessions_terminate': 'Disconnect',
    'sessions_terminate_confirm':
        'Disconnect this Telegram account? Forwarding rules that use it stop running.',
    'sessions_active': 'Connected',
    'sessions_inactive': 'Offline',
    'sessions_status_live': 'live',
    'sessions_status_offline': 'offline',

    // telegram login flow
    'tg_login_title': 'Link Telegram account',
    'tg_login_phone': 'Phone number',
    'tg_login_phone_hint': '+98 912 000 0000',
    'tg_login_phone_invalid':
        'Enter the phone number in international format, e.g. +989120000000',
    'tg_login_code': 'Verification code',
    'tg_login_code_hint': 'Code from the Telegram app',
    'tg_login_code_invalid': 'Enter the digits Telegram sent you',
    'tg_login_password': 'Two-step verification password',
    'tg_login_send_code': 'Send code',
    'tg_login_resend': 'Resend code',
    'tg_login_verify': 'Verify',
    'tg_login_cancel': 'Cancel login',
    'tg_login_success': 'Telegram account linked',
    'tg_login_state_awaiting_code': 'Enter the code Telegram sent to {phone}',
    'tg_login_state_awaiting_password':
        'Enter your two-step verification password',
    'tg_login_state_completed': 'Connected',
    'tg_login_state_failed': 'Login failed',
    'tg_login_state_expired': 'This login expired. Start it again.',
    'tg_login_state_rate_limited': 'Too many attempts. Wait {seconds}s.',
    'tg_login_state_pending': 'Contacting Telegram…',
    'tg_login_state_cancelled': 'Login cancelled',
    'login_code_sent': 'Verification code sent',
    'login_password_needed': 'Two-step verification required',
    'login_invalid_phone': 'That phone number is not valid for Telegram',
    'login_invalid_code': 'That code is not correct',
    'login_code_expired': 'That code has expired. Request a new one.',
    'login_invalid_password': 'That password is not correct',
    'login_too_many_attempts': 'Too many wrong attempts. Start a new login.',
    'login_auth_expired': 'The login session expired. Start again.',
    'login_no_pending': 'No login is in progress. Start again.',
    'login_flood_wait': 'Telegram asked us to wait {seconds}s before retrying',
    'login_cooldown': 'Wait {seconds}s before requesting another code',
    'login_error': 'Login failed. Try again.',

    // rules
    'rules_title': 'Forwarding rules',
    'rules_empty':
        'No forwarding rules yet. Create one to start copying messages.',
    'rules_create': 'New rule',
    'rules_source': 'Source channel',
    'rules_target': 'Destination channel',
    'rules_session': 'Telegram session',
    'nav_stats': 'Statistics',
    'rules_cleaner': 'Cleaner',
    'rules_content_mode': 'Content mode',
    'rules_duplicate_filters': 'Duplicate filter',
    'rules_edit': 'Edit task',
    'rules_forward_mode': 'Forward mode',
    'rules_show_forward_header': 'Show header',
    'rules_status_active': 'RUNNING',
    'rules_status_paused': 'PAUSED',
    'rules_sync_edits': 'Sync edits',
    'rules_sync_replies': 'Sync replies',
    'settings_server_url': 'Server URL',
    'rules_intermediate': 'Intermediate (VIP) channel',
    'rules_sec_status': 'Forwarding Status',
    'rules_sec_debug': 'Debug Mode',
    'rules_sec_filters': 'Filters',
    'rules_sec_schedules': 'Schedules',
    'rules_sec_time_forward': 'Time Forward',
    'rules_sec_processing': 'Processing Messages',
    'rules_sec_modify': 'Modify Content',
    'rules_sec_modify_sub': 'Header, footer, watermark',
    'rules_sec_ai': 'AI Features',
    'rules_sec_ai_sub': 'Creativity & token limits',
    'rules_sec_paid': 'PAID ADDON Catalogue',
    'rules_sec_paid_sub': 'Channel comments, webhooks, TTL & more',
    'rules_filters_dup': 'Duplicate Filters',
    'rules_filters_dup_sub': 'Media & text deep fingerprint deduplication',
    'rules_filters_crypto': 'Crypto Mode',
    'rules_filters_crypto_sub':
        'Signal detection, wallet cleaner, pair parsing',
    'rules_sched_auto': 'Auto Post Scheduler',
    'rules_sched_power': 'Power Control Schedule',
    'rules_time_delay': 'Set delay (seconds)',
    'rules_time_edit_limit': 'Max edit time (seconds)',
    'rules_proc_raw': 'Process Raw Text',
    'rules_proc_btn2txt': 'Button To Text',
    'rules_ai_ocr': 'OCR Image AI',
    'rules_ai_ocr_sub': 'Optical character recognition via vision AI',
    'rules_ai_rewrite': 'Smart AI Rewrite',
    'rules_ai_rewrite_sub': 'LLM intelligent content rephrasing',
    'rules_ai_digest': 'News Digest',
    'rules_ai_digest_sub': 'Automated news summary bullets',
    'rules_replace_title': 'Replace Rules',
    'rules_replace_summary': '{count} replacement(s) applied in order',
    'rules_replace_empty':
        'No replace rules yet. Add one to rewrite message text.',
    'rules_replace_add': 'Add replacement',
    'rules_replace_find': 'Find text',
    'rules_replace_find_required': 'Text to find is required',
    'rules_replace_with': 'Replace with',
    'rules_replace_with_hint': 'Leave empty to remove the matched text',
    'rules_replace_remove': '(removed)',
    'rules_replace_move_up': 'Move up',
    'rules_ai_pick_title': 'AI Configuration',
    'rules_ai_pick_none': 'No AI (direct forwarding)',
    'rules_ai_pick_empty':
        'No AI configurations yet. Create one from the AI screen.',
    'rules_filters': 'Filters',
    'rules_filter_mode': 'Filter mode',
    'rules_media_mode': 'Media handling',
    'rules_delay': 'Delay (seconds)',
    'rules_header': 'Header template',
    'rules_footer': 'Footer template',
    'rules_ai_config': 'AI rewriting',
    'rules_enabled': 'Enabled',
    'rules_disabled': 'Disabled',
    'rules_toggle': 'Enable / disable',
    'rules_delete_confirm': 'Delete this forwarding rule?',
    'rules_concurrent_edit':
        'This rule was changed elsewhere. Reload it and try again.',
    'rules_validation_failed':
        'The server rejected the form. Check the highlighted fields.',
    'rules_source_required': 'Pick a source channel',
    'rules_target_required': 'Pick a destination channel',
    'rules_session_required': 'Pick a Telegram session first',
    'rules_media_toggles_title': 'Media & Sync Settings (bot/web parity)',
    'rules_block_voice': 'Block Voice',
    'rules_block_stickers': 'Block Stickers',
    'rules_remove_emojis': 'Strip Emojis',
    'rules_sync_deletes': 'Sync Deletes',
    'rules_ignore_edits': 'Ignore Edits',
    'rules_album_mode': 'Album Mode',
    'rules_album_full': 'Full Album',
    'rules_album_first': 'First Media Only',
    'rules_album_split': 'Split Messages',

    // filters
    'filters_title': 'Filters',
    'filters_empty': 'No filters yet.',
    'filters_pattern': 'Pattern',
    'filters_kind': 'Type',
    'filters_kind_blacklist': 'Blacklist',
    'filters_kind_whitelist': 'Whitelist',
    'filters_kind_link': 'Links',
    'filters_delete_confirm': 'Delete this filter?',

    // ai
    'ai_title': 'AI rewriting',
    'ai_empty': 'No AI configuration yet.',
    'ai_provider': 'Provider',
    'ai_model': 'Model',
    'ai_language': 'Target language',
    'ai_prompt': 'Prompt',
    'ai_base_url': 'Provider base URL (optional)',
    'ai_note':
        'API keys are configured from the bot or the web dashboard. The mobile app never holds provider keys.',

    // logs
    'logs_title': 'Recent errors',
    'logs_empty': 'No errors recorded.',
    'logs_level': 'Level',

    // settings
    'settings_title': 'Settings',
    'settings_server': 'Server',
    'settings_server_current': 'Current server',
    'settings_language': 'Language',
    'settings_theme': 'Theme',
    'settings_theme_system': 'Follow system',
    'settings_theme_light': 'Light',
    'settings_theme_dark': 'Dark',
    'settings_change_password': 'Change password',
    'settings_logout': 'Sign out',
    'settings_logout_confirm': 'Sign out of this server?',
    'settings_docs': 'Documentation',
    'settings_repository': 'GitHub repository',
    'settings_about': 'About',
    'settings_version': 'Version',

    // server compat
    'incompatible_server':
        'This server is running an older AutoTelegramForward release. Update it to {version} or newer.',
  };

  const AppMessagesEn();

  String get(String key, {Map<String, String>? params}) {
    final value = _map[key];
    if (value == null) {
      // A missing key is a programming error; fail loudly in debug builds.
      assert(false, 'missing message key: $key');
      return key;
    }
    if (params == null || params.isEmpty) {
      return value;
    }
    var out = value;
    params.forEach((name, replacement) {
      out = out.replaceAll('{$name}', replacement);
    });
    return out;
  }
}

/// Localisation entry point used by the widgets.
class AppMessages {
  const AppMessages(this.locale);

  final Locale locale;

  static const AppMessagesEn _en = AppMessagesEn();

  static AppMessages of(BuildContext context) {
    final locale = Localizations.maybeLocaleOf(context) ?? const Locale('en');
    return AppMessages(locale);
  }

  static const LocalizationsDelegate<AppMessages> delegate =
      _AppMessagesDelegate();

  static const supportedLocales = <Locale>[
    Locale('en'),
    Locale('fa'),
    Locale('pt'),
    Locale('uz'),
    Locale('ru'),
    Locale('id'),
    Locale('es'),
    Locale('fr'),
    Locale('it'),
    Locale('zh'),
  ];

  static const en = _en;

  bool get isRtl => locale.languageCode == 'fa';

  String get(String key, {Map<String, String>? params}) {
    switch (locale.languageCode) {
      case 'fa':
        return AppMessagesFa.instance.get(key, params: params);
      case 'pt':
        return AppMessagesPt.instance.get(key, params: params);
      case 'uz':
        return AppMessagesUz.instance.get(key, params: params);
      case 'ru':
        return AppMessagesRu.instance.get(key, params: params);
      case 'id':
        return AppMessagesId.instance.get(key, params: params);
      case 'es':
        return AppMessagesEs.instance.get(key, params: params);
      case 'fr':
        return AppMessagesFr.instance.get(key, params: params);
      case 'it':
        return AppMessagesIt.instance.get(key, params: params);
      case 'zh':
        return AppMessagesZh.instance.get(key, params: params);
      default:
        return _en.get(key, params: params);
    }
  }

  /// Translates a [LoginChallenge] state into a localised sentence.
  String loginState(LoginChallenge challenge, String phoneDisplay) {
    switch (challenge.state) {
      case LoginChallengeState.awaitingCode:
        return get(
          'tg_login_state_awaiting_code',
          params: {'phone': phoneDisplay},
        );
      case LoginChallengeState.awaitingPassword:
        return get('tg_login_state_awaiting_password');
      case LoginChallengeState.completed:
        return get('tg_login_state_completed');
      case LoginChallengeState.failed:
        return get('tg_login_state_failed');
      case LoginChallengeState.expired:
        return get('tg_login_state_expired');
      case LoginChallengeState.rateLimited:
        return get(
          'tg_login_state_rate_limited',
          params: {'seconds': '${challenge.retryAfterSeconds}'},
        );
      case LoginChallengeState.cancelled:
        return get('tg_login_state_cancelled');
      default:
        return get('tg_login_state_pending');
    }
  }

  /// Translates the message keys returned by the Python login flow.
  String loginMessage(String message) {
    final key = loginMessageKey(message);
    if (key == 'login_flood_wait') {
      final seconds = message.split(':').elementAt(1);
      return get('login_flood_wait', params: {'seconds': seconds});
    }
    if (key == 'login_cooldown') {
      final seconds = message.split(':').elementAt(1);
      return get('login_cooldown', params: {'seconds': seconds});
    }
    if (key == 'login_error') {
      return get('login_error');
    }
    return get(key);
  }
}

class _AppMessagesDelegate extends LocalizationsDelegate<AppMessages> {
  const _AppMessagesDelegate();

  @override
  bool isSupported(Locale locale) => ['en', 'fa'].contains(locale.languageCode);

  @override
  Future<AppMessages> load(Locale locale) async => AppMessages(locale);

  @override
  bool shouldReload(_AppMessagesDelegate old) => false;
}
