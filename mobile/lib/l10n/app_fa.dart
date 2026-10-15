/// Persian (فارسی) translations. RTL-first design.
///
/// Mirrors `app_messages.dart`. Every key in [AppMessagesEn] must exist here.
library;

class AppMessagesFa {
  static const AppMessagesFa instance = AppMessagesFa._();
  const AppMessagesFa._();

  static const Map<String, String> _map = {
    // app
    'app_name': 'اتوتلگرام فوروارد',
    'app_tagline': 'موتور پیشرفته فوروارد و مدیریت کانال‌های تلگرام',

    // generic
    'action_retry': 'تلاش مجدد',
    'action_cancel': 'انصراف',
    'action_save': 'ذخیره',
    'action_delete': 'حذف',
    'action_edit': 'ویرایش',
    'action_confirm': 'تأیید',
    'action_close': 'بستن',
    'action_refresh': 'تازه‌سازی',
    'action_continue': 'ادامه',
    'action_back': 'بازگشت',
    'action_yes': 'بله',
    'action_no': 'خیر',
    'state_loading': 'در حال بارگذاری…',
    'state_empty': 'موردی برای نمایش وجود ندارد',
    'state_error': 'خطایی رخ داد',
    'state_offline': 'عدم دسترسی به سرور',

    // auth
    'login_title': 'ورود به حساب',
    'login_username': 'نام کاربری',
    'login_password': 'رمز عبور',
    'login_submit': 'ورود',
    'login_register': 'ایجاد حساب کاربری',
    'login_error_unauthorized': 'نام کاربری یا رمز عبور اشتباه است',
    'login_error_network': 'عدم برقراری ارتباط با سرور',
    'login_session_expired': 'نشست شما منقضی شد. لطفاً دوباره وارد شوید.',
    'login_server_label': 'سرور',
    'login_server_change': 'تغییر آدرس سرور',
    'login_server_invalid': 'آدرس معتبر با https:// وارد کنید',
    'login_no_account': 'حساب کاربری ندارید؟',
    'login_have_account': 'حساب کاربری دارید؟',

    // server connection
    'server_probe_test': 'آزمایش اتصال',
    'server_probe_testing': 'در حال آزمایش اتصال…',
    'server_probe_reachable': 'سرور در دسترس و سازگار است',
    'server_probe_unreachable':
        'ارتباط با سرور برقرار نشد. آدرس و اتصال اینترنت را بررسی کنید.',
    'server_probe_bad_response':
        'سرور پاسخ داد اما پاسخ غیرمنتظره‌ای برگرداند.',
    'server_probe_no_version':
        'این سرور نقطه پایانی نسخه را ارائه نمی‌کند. آدرس را بررسی یا سرور را به‌روز کنید.',
    'server_probe_tls_error':
        'اتصال امن ناموفق بود. گواهی سرور را بررسی کنید و از https:// استفاده کنید.',
    'server_restore_default': 'بازگردانی به پیش‌فرض رسمی',

    // nav
    'nav_dashboard': 'داشبورد',
    'nav_rules': 'قوانین',
    'nav_sessions': 'سشن‌ها',
    'nav_filters': 'فیلترها',
    'nav_ai': 'هوش مصنوعی',
    'nav_settings': 'تنظیمات',
    'nav_logs': 'گزارش‌ها',

    // dashboard
    'dash_title': 'داشبورد',
    'dash_total_rules': 'کل قوانین',
    'dash_active_rules': 'قوانین فعال',
    'dash_sessions': 'سشن‌های متصل',
    'dash_active_sessions': 'آنلاین',
    'dash_core': 'وضعیت هسته',
    'dash_uptime': 'مدت زمان کارکرد',
    'dash_delivered': 'فوروارد شده',
    'dash_failed': 'ناموفق',
    'dash_health': 'سلامت سرویس‌ها',
    'dash_recent_errors': 'خطاهای اخیر',
    'diag_title': 'بررسی‌های پیش‌پرواز',
    'diag_all_ok': 'هیچ مشکلی پیدا نشد — همه تسک‌ها سالم هستند.',

    // sessions
    'sessions_title': 'سشن‌های تلگرام',
    'sessions_empty': 'هنوز اکانت تلگرامی متصل نشده است. یک اکانت متصل کنید.',
    'sessions_add': 'اتصال اکانت جدید',
    'sessions_terminate': 'قطع اتصال',
    'sessions_terminate_confirm':
        'آیا از قطع اتصال این سشن اطمینان دارید؟ قوانین مرتبط متوقف خواهند شد.',
    'sessions_active': 'متصل',
    'sessions_inactive': 'آفلاین',
    'sessions_status_live': 'فعال',
    'sessions_status_offline': 'غیرفعال',

    // telegram login flow
    'tg_login_title': 'اتصال اکانت تلگرام',
    'tg_login_phone': 'شماره موبایل',
    'tg_login_phone_hint': '۰۹۱۲۰۰۰۰۰۰۰ یا ۹۸۹۱۲۰۰۰۰۰۰۰+',
    'tg_login_phone_invalid':
        'شماره را با پیش‌شماره بین‌المللی وارد کنید، مانند ۹۸۹۱۲۰۰۰۰۰۰۰+',
    'tg_login_code': 'کد تأیید پیامک/تلگرام',
    'tg_login_code_hint': 'کد ارسال‌شده از طرف تلگرام',
    'tg_login_code_invalid': 'کد ارسالی تلگرام را وارد کنید',
    'tg_login_password': 'رمز تأیید دو مرحله‌ای (2FA)',
    'tg_login_send_code': 'ارسال کد',
    'tg_login_resend': 'ارسال مجدد کد',
    'tg_login_verify': 'تأیید و اتصال',
    'tg_login_cancel': 'انصراف',
    'tg_login_success': 'اکانت تلگرام با موفقیت متصل شد',
    'tg_login_state_awaiting_code': 'کد ارسال‌شده به {phone} را وارد کنید',
    'tg_login_state_awaiting_password':
        'رمز تأیید دو مرحله‌ای خود را وارد کنید',
    'tg_login_state_completed': 'اتصال با موفقیت برقرار شد',
    'tg_login_state_failed': 'ورود ناموفق بود',
    'tg_login_state_expired': 'فرصت این ورود تمام شد. لطفاً دوباره تلاش کنید.',
    'tg_login_state_rate_limited':
        'درخواست‌های مکرر. لطفاً {seconds} ثانیه منتظر بمانید.',
    'tg_login_state_pending': 'در حال ارتباط با سرورهای تلگرام…',
    'tg_login_state_cancelled': 'ورود لغو شد',
    'login_code_sent': 'کد تأیید ارسال شد',
    'login_password_needed': 'نیاز به رمز دو مرحله‌ای است',
    'login_invalid_phone': 'شماره موبایل برای تلگرام معتبر نیست',
    'login_invalid_code': 'کد وارد شده اشتباه است',
    'login_code_expired': 'کد تأیید منقضی شده است. کد جدید دریافت کنید.',
    'login_invalid_password': 'رمز دو مرحله‌ای اشتباه است',
    'login_too_many_attempts':
        'تعداد تلاش‌های ناموفق بیش از حد مجاز است. دوباره شروع کنید.',
    'login_auth_expired': 'سشن لاگین منقضی شد. دوباره تلاش کنید.',
    'login_no_pending': 'درخواست لاگین فعالی وجود ندارد. دوباره شروع کنید.',
    'login_flood_wait': 'محدودیت تلگرام: لطفاً {seconds} ثانیه صبر کنید',
    'login_cooldown': 'لطفاً {seconds} ثانیه قبل از درخواست مجدد کد صبر کنید',
    'login_error': 'خطایی در ورود رخ داد. دوباره امتحان کنید.',

    // rules
    'rules_title': 'قوانین فوروارد',
    'rules_empty': 'هنوز قانونی تعریف نشده است. یک قانون جدید ایجاد کنید.',
    'rules_create': 'قانون جدید',
    'rules_source': 'کانال مبدأ',
    'rules_target': 'کانال مقصد',
    'rules_session': 'سشن تلگرام',
    'nav_stats': 'آمار',
    'rules_cleaner': 'پاکسازی',
    'rules_content_mode': 'حالت محتوا',
    'rules_duplicate_filters': 'فیلتر تکراری',
    'rules_edit': 'ویرایش تسک',
    'rules_forward_mode': 'حالت فوروارد',
    'rules_show_forward_header': 'نمایش سربرگ',
    'rules_status_active': 'در حال اجرا',
    'rules_status_paused': 'متوقف شده',
    'rules_sync_edits': 'همگام‌سازی ویرایش‌ها',
    'rules_sync_replies': 'همگام‌سازی پاسخ‌ها',
    'settings_server_url': 'آدرس سرور',
    'rules_intermediate': 'کانال واسط (VIP)',
    'rules_sec_status': 'وضعیت فوروارد',
    'rules_sec_debug': 'حالت دیباگ',
    'rules_sec_filters': 'فیلترها',
    'rules_sec_schedules': 'زمان‌بندی‌ها',
    'rules_sec_time_forward': 'فوروارد زمانی',
    'rules_sec_processing': 'پردازش پیام‌ها',
    'rules_sec_modify': 'ویرایش محتوا',
    'rules_sec_modify_sub': 'سربرگ، پاصفحه، واترمارک',
    'rules_sec_ai': 'قابلیت‌های هوش مصنوعی',
    'rules_sec_ai_sub': 'خلاقیت و محدودیت توکن',
    'rules_sec_paid': 'کتالوگ افزونه‌های پولی',
    'rules_sec_paid_sub': 'کامنت کانال، وب‌هوک، TTL و بیشتر',
    'rules_filters_dup': 'فیلترهای تکراری',
    'rules_filters_dup_sub': 'حذف تکراری عمقی رسانه و متن',
    'rules_filters_crypto': 'حالت کریپتو',
    'rules_filters_crypto_sub': 'تشخیص سیگنال، پاکسازی کیف پول، تجزیه جفت',
    'rules_sched_auto': 'زمان‌بند ارسال خودکار',
    'rules_sched_power': 'زمان‌بند روشن/خاموش',
    'rules_time_delay': 'تنظیم تأخیر (ثانیه)',
    'rules_time_edit_limit': 'حداکثر زمان ویرایش (ثانیه)',
    'rules_proc_raw': 'پردازش متن خام',
    'rules_proc_btn2txt': 'دکمه به متن',
    'rules_ai_ocr': 'OCR تصویر هوش مصنوعی',
    'rules_ai_ocr_sub': 'تشخیص نوری کاراکتر با هوش مصنوعی بینایی',
    'rules_ai_rewrite': 'بازنویسی هوشمند هوش مصنوعی',
    'rules_ai_rewrite_sub': 'بازنویسی هوشمند محتوا',
    'rules_ai_digest': 'خلاصه اخبار',
    'rules_ai_digest_sub': 'خلاصه خودکار اخبار',
    'rules_replace_title': 'قوانین جایگزینی',
    'rules_replace_summary': '{count} جایگزینی به ترتیب اعمال می‌شود',
    'rules_replace_empty':
        'هنوز قانون جایگزینی نیست. برای بازنویسی متن پیام یکی اضافه کنید.',
    'rules_replace_add': 'افزودن جایگزینی',
    'rules_replace_find': 'متن یافتن',
    'rules_replace_find_required': 'متن جستجو الزامی است',
    'rules_replace_with': 'جایگزین با',
    'rules_replace_with_hint': 'برای حذف متن یافت‌شده خالی بگذارید',
    'rules_replace_remove': '(حذف شد)',
    'rules_replace_move_up': 'انتقال به بالا',
    'rules_ai_pick_title': 'پیکربندی هوش مصنوعی',
    'rules_ai_pick_none': 'بدون هوش مصنوعی (فوروارد مستقیم)',
    'rules_ai_pick_empty':
        'هنوز پیکربندی هوش مصنوعی نیست. از بخش هوش مصنوعی بسازید.',
    'rules_filters': 'فیلترها',
    'rules_filter_mode': 'نوع اعمال فیلتر',
    'rules_media_mode': 'نوع رسانه‌ها',
    'rules_delay': 'تأخیر (ثانیه)',
    'rules_header': 'قالب هدر',
    'rules_footer': 'قالب فوتر',
    'rules_ai_config': 'بازنویسی هوش مصنوعی',
    'rules_enabled': 'فعال',
    'rules_disabled': 'غیرفعال',
    'rules_toggle': 'فعال/غیرفعال کردن',
    'rules_delete_confirm': 'آیا این قانون فوروارد حذف شود؟',
    'rules_concurrent_edit':
        'این قانون همزمان در جای دیگری تغییر کرده است. صفحه را بازخوانی کنید.',
    'rules_validation_failed':
        'سرور مقادیر ورودی را نپذیرفت. خطاهای مشخص‌شده را بررسی کنید.',
    'rules_source_required': 'کانال مبدا را انتخاب کنید',
    'rules_target_required': 'کانال مقصد را انتخاب کنید',
    'rules_session_required': 'ابتدا یک نشست تلگرام انتخاب کنید',
    'rules_media_toggles_title': 'تنظیمات رسانه و سینک (مشابه ربات و وب)',
    'rules_block_voice': 'مسدودسازی ویس',
    'rules_block_stickers': 'مسدودسازی استیکر',
    'rules_remove_emojis': 'پاکسازی ایموجی‌ها',
    'rules_sync_deletes': 'همگام‌سازی حذف پیام‌ها',
    'rules_ignore_edits': 'نادیده گرفتن ویرایش‌ها',
    'rules_album_mode': 'حالت آلبوم',
    'rules_album_full': 'آلبوم کامل',
    'rules_album_first': 'فقط اولین رسانه',
    'rules_album_split': 'تفکیک پیام‌ها',

    // filters
    'filters_title': 'فیلترها',
    'filters_empty': 'فیلتری تعریف نشده است.',
    'filters_pattern': 'الگو / کلمات کلیدی',
    'filters_kind': 'نوع فیلتر',
    'filters_kind_blacklist': 'لیست سیاه',
    'filters_kind_whitelist': 'لیست سفید',
    'filters_kind_link': 'لینک‌ها',
    'filters_delete_confirm': 'آیا این فیلتر حذف شود؟',

    // ai
    'ai_title': 'هوش مصنوعی',
    'ai_empty': 'پیکربندی هوش مصنوعی یافت نشد.',
    'ai_provider': 'سرویس‌دهنده',
    'ai_model': 'مدل',
    'ai_language': 'زبان مقصد',
    'ai_prompt': 'پرامپت اختصاصی',
    'ai_base_url': 'آدرس سرور اختصاصی (اختیاری)',
    'ai_note':
        'کلیدهای API از طریق ربات یا پنل وب تنظیم می‌شوند. اپلیکیشن موبایل کلیدها را روی دستگاه نگه نمی‌دارد.',

    // logs
    'logs_title': 'خطاهای اخیر',
    'logs_empty': 'خطایی ثبت نشده است.',
    'logs_level': 'سطح خطا',

    // settings
    'settings_title': 'تنظیمات',
    'settings_server': 'سرور',
    'settings_server_current': 'سرور فعلی',
    'settings_language': 'زبان / Language',
    'settings_theme': 'پوسته',
    'settings_theme_system': 'هماهنگ با سیستم',
    'settings_theme_light': 'روشن',
    'settings_theme_dark': 'تاریک',
    'settings_change_password': 'تغییر رمز عبور',
    'settings_logout': 'خروج از حساب',
    'settings_logout_confirm': 'آیا از این سرور خارج می‌شوید؟',
    'settings_docs': 'مستندات',
    'settings_repository': 'مخزن گیت‌هاب',
    'settings_about': 'درباره',
    'settings_version': 'نسخه',

    // server compat
    'incompatible_server':
        'سرور از نسخه قدیمی اتوتلگرام استفاده می‌کند. سرور را به نسخه {version} یا جدیدتر ارتقا دهید.',
  };

  String get(String key, {Map<String, String>? params}) {
    final value = _map[key];
    if (value == null) {
      // In Persian fallback to English key rather than crashing so the UI works.
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
