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
    'action_back': 'Back',
    'action_cancel': 'Cancel',
    'action_close': 'Close',
    'action_confirm': 'Confirm',
    'action_continue': 'Continue',
    'action_delete': 'Delete',
    'action_edit': 'Edit',
    'action_no': 'No',
    'action_refresh': 'Refresh',
    'action_retry': 'Retry',
    'action_save': 'Save',
    'action_yes': 'Yes',
    'ai_base_url': 'Provider base URL (optional)',
    'ai_empty': 'No AI configuration yet.',
    'ai_language': 'Target language',
    'ai_model': 'Model',
    'ai_note': 'API keys are configured by the bot or the web dashboard; the mobile app never stores provider keys.',
    'ai_prompt': 'Prompt',
    'ai_provider': 'Provider',
    'ai_title': 'AI rewriting',
    'app_name': 'AutoTelegramForward',
    'app_tagline': 'Production Telegram message forwarding engine',
    'chats_choose_source_task': 'Add as source to which task?',
    'chats_choose_target_task': 'Add as target to which task?',
    'chats_empty': 'This account has no chats to show.',
    'chats_forum': 'forum',
    'chats_members': '{n} members',
    'chats_new_task': 'New task from this chat',
    'chats_new_task_hint': 'Then pick the other side of the route',
    'chats_no_sessions':
        'Connect a Telegram account first: the chat list is read from it.',
    'chats_picker_add': 'Add {n}',
    'chats_picker_add_none': 'Add',
    'chats_picker_clear': 'Clear',
    'chats_picker_duplicate': 'That chat is already in the list.',
    'chats_picker_empty': 'No chats found. Type an id or @username below.',
    'chats_picker_filter_all': 'All',
    'chats_picker_filter_channels': 'Channels',
    'chats_picker_filter_groups': 'Groups',
    'chats_picker_filter_users': 'Users',
    'chats_picker_filtered_empty': 'Nothing matches that search.',
    'chats_picker_forbidden': 'This account is not allowed to read chats.',
    'chats_picker_load_failed': 'Could not read the chat list',
    'chats_picker_manual_add': 'Add this id',
    'chats_picker_manual_help': 'Ids, @usernames and t.me links all work.',
    'chats_picker_manual_hint': '-1001234567890 or @channel',
    'chats_picker_no_session': 'Pick a Telegram account first.',
    'chats_picker_reload': 'Reload',
    'chats_picker_search_hint': 'Search chats',
    'chats_picker_select_all': 'Select all shown',
    'chats_picker_selected_count': '{n} selected',
    'chats_picker_subtitle':
        'Picking is not saving — confirm to write it into the field.',
    'chats_picker_total': 'showing {shown} of {total}',
    'chats_picker_unavailable':
        'This server does not list chats; type ids instead.',
    'chats_search_hint': 'Search by name, @username or id',
    'chats_session_active': 'connected',
    'chats_session_idle': 'not connected',
    'chats_session_label': 'Telegram account',
    'chats_subtitle':
        'Every chat this account can see, and where it is already routed',
    'chats_title': 'Chats',
    'chats_unavailable': 'This server cannot list chats',
    'chats_unused': 'unused',
    'chats_usage': '{sources} as source · {targets} as target',
    'chats_usage_none': 'Not used by any task yet',
    'chats_use_source': 'Use as source',
    'chats_use_source_none': 'no sources yet',
    'chats_use_source_tasks': 'in {n} task(s)',
    'chats_use_target': 'Use as target',
    'chats_use_target_tasks': 'in {n} task(s)',
    'chats_used_source': 'source in {n}',
    'chats_used_target': 'target in {n}',
    'dash_active_rules': 'Active rules',
    'dash_active_sessions': 'Connected',
    'dash_core': 'Core service',
    'dash_delivered': 'Delivered',
    'dash_failed': 'Failed',
    'dash_health': 'Service health',
    'dash_recent_errors': 'Recent errors',
    'dash_sessions': 'Sessions',
    'dash_title': 'Dashboard',
    'dash_total_rules': 'Rules',
    'dash_uptime': 'Uptime',
    'delivery_attempts': '{n} attempts',
    'delivery_dlq_empty': 'Nothing is stuck: no job has exhausted its retries.',
    'delivery_dlq_retry': 'Replay',
    'delivery_failed': 'Failed',
    'delivery_filtered': 'Filtered',
    'delivery_forwarded': 'Forwarded',
    'delivery_in_queue': 'In queue',
    'delivery_no_rules': 'No task has delivered anything yet.',
    'delivery_purge': 'Purge',
    'delivery_purge_confirm': 'Remove every dead-lettered job?',
    'delivery_purge_ok': 'Removed {n} job(s)',
    'delivery_retried': 'Retried',
    'delivery_retry_failed': 'Could not replay the job: {error}',
    'delivery_retry_ok': 'Replayed {n} job(s)',
    'delivery_rule_line': '{forwarded} forwarded · {errors} errors',
    'delivery_subtitle':
        'What the engine is doing right now, and what it gave up on',
    'delivery_tab_dlq': 'Dead letter',
    'delivery_tab_queue': 'Queue',
    'delivery_title': 'Delivery',
    'diag_all_ok': 'No problems found — every task is healthy.',
    'diag_title': 'Preflight checks',
    'filters_delete_confirm': 'Delete this filter?',
    'filters_empty': 'No filters yet.',
    'filters_kind': 'Type',
    'filters_kind_blacklist': 'Blacklist',
    'filters_kind_link': 'Links',
    'filters_kind_whitelist': 'Whitelist',
    'filters_pattern': 'Pattern',
    'filters_title': 'Filters',
    'incompatible_server': 'This server is running an older AutoTelegramForward release. Update it to {version} or newer.',
    'lang_label': 'Language',
    'lang_native_en': 'English',
    'lang_native_es': 'Español',
    'lang_native_fa': 'فارسی',
    'lang_native_fr': 'Français',
    'lang_native_id': 'Bahasa Indonesia',
    'lang_native_it': 'Italiano',
    'lang_native_pt': 'Português',
    'lang_native_ru': 'Русский',
    'lang_native_uz': 'Oʻzbekcha',
    'lang_native_zh': '中文',
    'login_auth_expired': 'The login session expired. Start again.',
    'login_code_expired': 'That code has expired. Request a new one.',
    'login_code_sent': 'Verification code sent',
    'login_cooldown': 'Wait {seconds}s before requesting another code',
    'login_error': 'Login failed. Try again.',
    'login_error_network': 'Cannot reach the server',
    'login_error_unauthorized': 'Wrong username or password',
    'login_flood_wait': 'Telegram asked us to wait {seconds}s before retrying',
    'login_have_account': 'Already have an account?',
    'login_invalid_code': 'That code is not correct',
    'login_invalid_password': 'That password is not correct',
    'login_invalid_phone': 'That phone number is not valid for Telegram',
    'login_no_account': 'No account yet?',
    'login_no_pending': 'No login is in progress. Start again.',
    'login_password': 'Password',
    'login_password_needed': 'Two-step verification required',
    'login_register': 'Create account',
    'login_server_change': 'Change server',
    'login_server_invalid': 'Enter a valid https:// server address',
    'login_server_label': 'Server',
    'login_session_expired': 'Your session expired. Please sign in again.',
    'login_submit': 'Sign in',
    'login_title': 'Sign in',
    'login_too_many_attempts': 'Too many wrong attempts. Start a new login.',
    'login_username': 'Username',
    'logs_all': 'All',
    'logs_all_tasks': 'All tasks',
    'logs_copy': 'Copy',
    'logs_detail_label': 'detail',
    'logs_empty': 'No errors recorded.',
    'logs_end_of_history': 'End of history',
    'logs_export_copied': 'Export copied',
    'logs_export_csv': 'Export as CSV',
    'logs_export_csv_sub': 'One row per event, spreadsheet-ready',
    'logs_export_evidence': 'Export evidence',
    'logs_export_failed': 'Export failed: {error}',
    'logs_export_json': 'Export as JSON',
    'logs_export_json_sub': 'Full structured records',
    'logs_history_from': 'history from {date}',
    'logs_level': 'Level',
    'logs_level_debug': 'Debug',
    'logs_level_errors': 'Errors',
    'logs_level_info': 'Info',
    'logs_level_warnings': 'Warnings',
    'logs_newer': 'Newer',
    'logs_older': 'Older',
    'logs_order_toggle_desc': 'Newest first — tap for oldest first',
    'logs_page_size': '{n} / page',
    'logs_policy_admin_required':
        'Only an administrator can open a verbose window.',
    'logs_policy_configured': 'Configured: {levels}',
    'logs_policy_counters': 'Emitted {emitted} · suppressed by level {by_level} · by rate {by_rate}',
    'logs_policy_counters_scope': 'Counters are {scope}',
    'logs_policy_data_age': 'Newest record {seconds}s ago',
    'logs_policy_diag_off': 'Verbose window closed',
    'logs_policy_diag_on': 'Verbose window open — {seconds}s left',
    'logs_policy_diagnostics_hint':
        'Administrators only. Opens for {seconds}s and closes by itself.',
    'logs_policy_diagnostics_title': 'Verbose collection window',
    'logs_policy_effective': 'Collecting: {levels}',
    'logs_policy_health_title': 'Pipeline health',
    'logs_policy_no_records': 'No records stored yet',
    'logs_policy_not_reported': 'Not reported',
    'logs_policy_notes_title': 'Notes',
    'logs_policy_observed': 'Observed {time}',
    'logs_policy_reason': 'Reason: {reason}',
    'logs_policy_scopes_title': 'What each process collects',
    'logs_policy_start': 'Open window',
    'logs_policy_start_failed': 'Could not open the window: {error}',
    'logs_policy_started': 'Verbose window open for {seconds}s',
    'logs_policy_status_degraded': 'Degraded',
    'logs_policy_status_healthy': 'Healthy',
    'logs_policy_status_unavailable': 'Unavailable',
    'logs_policy_status_unknown': 'Unknown',
    'logs_policy_stop': 'Close window',
    'logs_policy_stop_none': 'No verbose window was open',
    'logs_policy_stopped': 'Verbose window closed',
    'logs_policy_unavailable_hint': 'This server does not report its log policy. Update the server to use this tab.',
    'logs_scope_corr_chip': 'corr:{id}',
    'logs_scope_service': 'Service',
    'logs_scope_task': 'Task',
    'logs_scope_task_chip': 'task:{id}',
    'logs_search_hint': 'Search message + detail…',
    'logs_show_all_tasks': 'Show all tasks',
    'logs_tab_audit': 'Audit',
    'logs_tab_policy': 'Policy',
    'logs_tab_stream': 'Logs',
    'logs_title': 'Recent errors',
    'logs_truncated_display':
        '…display truncated; copying still gives the full export',
    'nav_ai': 'AI',
    'nav_chats': 'Chats',
    'nav_dashboard': 'Dashboard',
    'nav_delivery': 'Delivery',
    'nav_filters': 'Filters',
    'nav_logs': 'Logs',
    'nav_rules': 'Rules',
    'nav_sessions': 'Sessions',
    'nav_settings': 'Settings',
    'nav_stats': 'Statistics',
    'rules_adv_categories': 'Advanced settings categories',
    'rules_ai_config': 'AI rewriting',
    'rules_ai_digest': 'News Digest',
    'rules_ai_digest_sub': 'Automated news summary bullets',
    'rules_ai_ocr': 'OCR Image AI',
    'rules_ai_ocr_sub': 'Optical character recognition via vision AI',
    'rules_ai_pick_empty':
        'No AI configurations yet. Create one on the AI screen.',
    'rules_ai_pick_none': 'No AI (direct forwarding)',
    'rules_ai_pick_title': 'AI Configuration',
    'rules_ai_rewrite': 'Smart AI Rewrite',
    'rules_ai_rewrite_sub': 'LLM intelligent content rephrasing',
    'rules_album_first': 'First Media Only',
    'rules_album_full': 'Full Album',
    'rules_album_mode': 'Album Mode',
    'rules_album_split': 'Split Messages',
    'rules_block_stickers': 'Block Stickers',
    'rules_block_voice': 'Block Voice',
    'rules_chat_title': 'Title / channel name',
    'rules_cleaner': 'Cleaner',
    'rules_cleanup_hint':
        'Emojis, links, words, replacements — save the task first',
    'rules_cleanup_open': 'Content cleaner & per-task rules',
    'rules_concurrent_edit':
        'This task was changed elsewhere. Reload and try again.',
    'rules_content_mode': 'Content mode',
    'rules_create': 'New rule',
    'rules_delay': 'Delay (seconds)',
    'rules_delete_confirm': 'Delete this forwarding rule?',
    'rules_disabled': 'Disabled',
    'rules_duplicate_filters': 'Duplicate filter',
    'rules_edit': 'Edit task',
    'rules_empty': 'No tasks yet. Create one to start forwarding.',
    'rules_enabled': 'Enabled',
    'rules_filter_mode': 'Filter mode',
    'rules_filters': 'Filters',
    'rules_filters_crypto': 'Crypto Mode',
    'rules_filters_crypto_sub':
        'Signal detection, wallet cleanup, pair parsing',
    'rules_filters_dup': 'Duplicate Filters',
    'rules_filters_dup_sub': 'Media & text deep fingerprint deduplication',
    'rules_filters_summary': 'Duplicates: {duplicate} · Crypto: {crypto}',
    'rules_footer': 'Footer template',
    'rules_forward_mode': 'Forward mode',
    'rules_full_control': 'Full control',
    'rules_full_control_desc':
        'Every per-task setting the engine honours, validated on the server.',
    'rules_full_control_open': 'Open',
    'rules_header': 'Header template',
    'rules_ignore_edits': 'Ignore Edits',
    'rules_intermediate': 'Intermediate (VIP) channel',
    'rules_intermediate_id': 'VIP intermediate channel ID',
    'rules_logs_action': 'Logs for this task',
    'rules_logs_title': 'Task logs',
    'rules_media_mode': 'Media handling',
    'rules_media_toggles_title': 'Media & Sync Settings (bot/web parity)',
    'rules_pick_from_chats': 'Select from my chats',
    'rules_pick_sources': 'Select sources',
    'rules_pick_targets': 'Select targets',
    'rules_proc_btn2txt': 'Button To Text',
    'rules_proc_raw': 'Process Raw Text',
    'rules_remove_emojis': 'Strip Emojis',
    'rules_replace_add': 'Add replacement',
    'rules_replace_empty':
        'No replacements yet. Add one to rewrite message text.',
    'rules_replace_find': 'Find text',
    'rules_replace_find_required': 'Text to find is required',
    'rules_replace_move_up': 'Move up',
    'rules_replace_remove': '(removed)',
    'rules_replace_summary': '{count} replacement(s) applied in order',
    'rules_replace_title': 'Replace Rules',
    'rules_replace_with': 'Replace with',
    'rules_replace_with_hint': 'Leave empty to remove the matched text',
    'rules_route_chip_duplicate': 'Listed twice',
    'rules_route_chip_ok': 'Accepted',
    'rules_route_chip_overlap': 'Used on both sides',
    'rules_route_chip_tooltip': '{reason} — tap the chip to edit this entry.',
    'rules_route_duplicate_id':
        '{id} is listed more than once. Remove the duplicate.',
    'rules_route_empty':
        'No channels yet. Type one or more, separated by commas.',
    'rules_route_fan_out': '{count} targets',
    'rules_route_overlap_id':
        '{id} is both a source and a target. A task cannot forward to itself.',
    'rules_route_preview': '{sources} → {targets}',
    'rules_route_preview_label': 'Route preview',
    'rules_route_remove': 'Remove this entry',
    'rules_route_sources': 'Sources (comma separated)',
    'rules_route_sources_hint':
        'IDs, @usernames or t.me links, separated by commas',
    'rules_route_targets': 'Targets (comma separated)',
    'rules_route_targets_hint': 'Every message is fanned out to all of these',
    'rules_sched_auto': 'Auto Post Scheduler',
    'rules_sched_power': 'Power Control Schedule',
    'rules_sec_ai': 'AI Features',
    'rules_sec_ai_sub': 'Creativity & token limits',
    'rules_sec_debug': 'Debug Mode',
    'rules_sec_filters': 'Filters',
    'rules_sec_modify': 'Modify Content',
    'rules_sec_modify_sub': 'Header, footer, watermark',
    'rules_sec_paid': 'PAID ADDON Catalogue',
    'rules_sec_paid_sub': 'Channel comments, webhooks, TTL & more',
    'rules_sec_processing': 'Processing Messages',
    'rules_sec_schedules': 'Schedules',
    'rules_sec_status': 'Forwarding Status',
    'rules_sec_time_forward': 'Time Forward',
    'rules_session': 'Telegram session',
    'rules_session_required': 'Pick a Telegram session first',
    'rules_settings_applied': '{n} settings applied',
    'rules_settings_apply': 'Apply',
    'rules_settings_change_count': '{n} changes',
    'rules_settings_count': '{n} settings',
    'rules_settings_int_hint': 'integer',
    'rules_settings_invalid': 'invalid {kind}',
    'rules_settings_json_hint': 'JSON object',
    'rules_settings_list_hint': 'comma separated',
    'rules_settings_load_failed': 'Could not load task settings',
    'rules_settings_max': 'must be at most {max}',
    'rules_settings_min': 'must be at least {min}',
    'rules_settings_no_changes': 'No changes',
    'rules_settings_no_match': 'No setting matches "{q}".',
    'rules_settings_number_hint': 'number',
    'rules_settings_pair_add': 'Add a replacement',
    'rules_settings_pair_duplicate':
        'Two rows replace the same text — remove one.',
    'rules_settings_pair_find': 'Find',
    'rules_settings_pair_remove': 'Remove this row',
    'rules_settings_pair_with': 'Replace with',
    'rules_settings_quick_cleanup': 'Quick cleanup',
    'rules_settings_quick_cleanup_hint':
        'The switches people actually turn on for a channel.',
    'rules_settings_reload': 'Reload',
    'rules_settings_required': 'required',
    'rules_settings_save_failed': 'Could not save settings',
    'rules_settings_saving': 'Saving…',
    'rules_settings_search_hint': 'Search settings',
    'rules_settings_unavailable':
        'This server does not expose per-task settings.',
    'rules_show_forward_header': 'Show header',
    'rules_simulate_empty': 'Type a sample message first.',
    'rules_simulate_failed': 'Could not run the test: {error}',
    'rules_simulate_forward': 'This message would be forwarded.',
    'rules_simulate_hint':
        'Runs the task against a sample message. Nothing is forwarded.',
    'rules_simulate_media': 'The message has media',
    'rules_simulate_media_audio': 'audio',
    'rules_simulate_media_document': 'document',
    'rules_simulate_media_none': 'no media',
    'rules_simulate_media_photo': 'photo',
    'rules_simulate_media_type': 'Media type',
    'rules_simulate_media_video': 'video',
    'rules_simulate_output': 'Message as it would be sent',
    'rules_simulate_reason': 'Reason: {reason}',
    'rules_simulate_run': 'Run the test',
    'rules_simulate_sample': 'Sample message',
    'rules_simulate_sample_hint': 'Paste a message the source posts',
    'rules_simulate_skip': 'This message would be skipped.',
    'rules_simulate_targets': 'Targets: {targets}',
    'rules_simulate_title': 'Test this task',
    'rules_source': 'Source channel',
    'rules_source_required': 'Pick a source channel',
    'rules_state_off': 'Off',
    'rules_state_on': 'On',
    'rules_state_paused': 'Paused',
    'rules_state_running': 'Running',
    'rules_status_active': 'RUNNING',
    'rules_status_paused': 'PAUSED',
    'rules_sync_deletes': 'Sync Deletes',
    'rules_sync_edits': 'Sync edits',
    'rules_sync_replies': 'Sync replies',
    'rules_target': 'Destination channel',
    'rules_target_required': 'Pick a destination channel',
    'rules_task_id': 'Task ID',
    'rules_time_delay': 'Set delay (seconds)',
    'rules_time_edit_limit': 'Max edit time (seconds)',
    'rules_title': 'Forwarding rules',
    'rules_toggle': 'Enable / disable',
    'rules_validation_failed':
        'The server rejected the form. Check the highlighted fields.',
    'server_probe_bad_response':
        'The server answered, but with something unexpected.',
    'server_probe_no_version': 'This server does not expose a version endpoint. Check the address or update the server.',
    'server_probe_reachable': 'Server reachable and compatible',
    'server_probe_test': 'Test connection',
    'server_probe_testing': 'Testing connection…',
    'server_probe_tls_error': 'The secure connection failed. Check the server certificate and use https://.',
    'server_probe_unreachable':
        'Cannot reach the server. Check the address and your network.',
    'server_restore_default': 'Restore official default',
    'sessions_active': 'Connected',
    'sessions_add': 'Link a Telegram account',
    'sessions_empty':
        'No Telegram account connected yet. Connect one to start.',
    'sessions_inactive': 'Offline',
    'sessions_status_live': 'live',
    'sessions_status_offline': 'offline',
    'sessions_terminate': 'Disconnect',
    'sessions_terminate_confirm': 'Disconnect this Telegram account? Forwarding tasks using it will stop.',
    'sessions_title': 'Telegram sessions',
    'settings_about': 'About',
    'settings_change_password': 'Change password',
    'settings_docs': 'Documentation',
    'settings_language': 'Language',
    'settings_language_hint': 'Applies immediately to every screen',
    'settings_logout': 'Sign out',
    'settings_logout_confirm': 'Sign out of this server?',
    'settings_repository': 'GitHub repository',
    'settings_server': 'Server',
    'settings_server_current': 'Current server',
    'settings_server_url': 'Server URL',
    'settings_theme': 'Theme',
    'settings_theme_dark': 'Dark',
    'settings_theme_light': 'Light',
    'settings_theme_system': 'Follow system',
    'settings_title': 'Settings',
    'settings_version': 'Version',
    'state_empty': 'Nothing here yet',
    'state_error': 'Something went wrong',
    'state_loading': 'Loading…',
    'state_offline': 'No connection to the server',
    'status_offline': 'Offline',
    'status_online': 'Online',
    'sys_api_version': 'API version',
    'sys_check_set': 'set',
    'sys_check_unset': 'missing',
    'sys_commit': 'Build commit',
    'sys_contract': 'Configuration contract',
    'sys_deployment': 'Deployment',
    'sys_features': 'Capabilities',
    'sys_latency': '{ms} ms',
    'sys_level_optional': 'optional',
    'sys_level_recommended': 'recommended',
    'sys_level_required': 'required',
    'sys_missing_required': '{count} required setting(s) missing',
    'sys_next_steps': 'Next steps',
    'sys_not_ready': 'Not ready',
    'sys_open': 'View system status',
    'sys_probed_at': 'Probed at {time}',
    'sys_product_version': 'Product version',
    'sys_ready': 'Ready',
    'sys_services_online': '{online}/{total} services online',
    'sys_services_title': 'Microservices',
    'sys_subtitle':
        'Server readiness, service topology and the configuration contract',
    'sys_title': 'System',
    'sys_unavailable': 'This server does not expose the system status endpoint. Update the server to see readiness here.',
    'tg_login_cancel': 'Cancel login',
    'tg_login_code': 'Verification code',
    'tg_login_code_hint': 'Code from the Telegram app',
    'tg_login_code_invalid': 'Enter the digits Telegram sent you',
    'tg_login_password': 'Two-step verification password',
    'tg_login_phone': 'Phone number',
    'tg_login_phone_hint': '+98 912 000 0000',
    'tg_login_phone_invalid':
        'Enter the phone number in international format, e.g. +12025550123.',
    'tg_login_resend': 'Resend code',
    'tg_login_send_code': 'Send code',
    'tg_login_state_awaiting_code': 'Enter the code Telegram sent to {phone}',
    'tg_login_state_awaiting_password':
        'Enter your two-step verification password',
    'tg_login_state_cancelled': 'Login cancelled',
    'tg_login_state_completed': 'Connected',
    'tg_login_state_expired': 'This login expired. Start it again.',
    'tg_login_state_failed': 'Login failed',
    'tg_login_state_pending': 'Contacting Telegram…',
    'tg_login_state_rate_limited': 'Too many attempts. Wait {seconds}s.',
    'tg_login_success': 'Telegram account linked',
    'tg_login_title': 'Link Telegram account',
    'tg_login_verify': 'Verify',
  };

  /// Every key this catalog defines. Exposed so the parity test can compare
  /// the compiled maps instead of scraping source text, where a value's
  /// quoting style can hide a key from a regex.
  static Iterable<String> get knownKeys => _map.keys;

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
        return _localized(
          AppMessagesFa.instance.get,
          AppMessagesFa.knownKeys,
          key,
          params,
        );
      case 'pt':
        return _localized(
          AppMessagesPt.instance.get,
          AppMessagesPt.knownKeys,
          key,
          params,
        );
      case 'uz':
        return _localized(
          AppMessagesUz.instance.get,
          AppMessagesUz.knownKeys,
          key,
          params,
        );
      case 'ru':
        return _localized(
          AppMessagesRu.instance.get,
          AppMessagesRu.knownKeys,
          key,
          params,
        );
      case 'id':
        return _localized(
          AppMessagesId.instance.get,
          AppMessagesId.knownKeys,
          key,
          params,
        );
      case 'es':
        return _localized(
          AppMessagesEs.instance.get,
          AppMessagesEs.knownKeys,
          key,
          params,
        );
      case 'fr':
        return _localized(
          AppMessagesFr.instance.get,
          AppMessagesFr.knownKeys,
          key,
          params,
        );
      case 'it':
        return _localized(
          AppMessagesIt.instance.get,
          AppMessagesIt.knownKeys,
          key,
          params,
        );
      case 'zh':
        return _localized(
          AppMessagesZh.instance.get,
          AppMessagesZh.knownKeys,
          key,
          params,
        );
      default:
        return _en.get(key, params: params);
    }
  }

  /// Resolves a key against one locale's catalog, falling back to English.
  ///
  /// The per-locale catalogs answer with the key itself when they do not define
  /// it (`_map[key] ?? key`), which puts a literal `rules_empty` on screen - the
  /// classic way a missing translation becomes visible garbage. English is the
  /// only acceptable fallback (never Persian), so an unknown key resolves here
  /// instead of rendering as its own key name.
  static String _localized(
    String Function(String key, {Map<String, String>? params}) lookup,
    Iterable<String> keys,
    String key,
    Map<String, String>? params,
  ) {
    if (keys.contains(key)) return lookup(key, params: params);
    return _en.get(key, params: params);
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

  /// Every locale the app ships a catalog for. This used to list only `en` and
  /// `fa` while `supportedLocales` advertised ten: picking Italian or Chinese
  /// then silently rendered English, because the delegate refused to load the
  /// catalog the resolver was perfectly able to read.
  @override
  bool isSupported(Locale requested) => AppMessages.supportedLocales.any(
    (locale) => locale.languageCode == requested.languageCode,
  );

  @override
  Future<AppMessages> load(Locale locale) async => AppMessages(locale);

  @override
  bool shouldReload(_AppMessagesDelegate old) => false;
}
