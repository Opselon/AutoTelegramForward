# Agent Guidelines & Engineering Rules

This document defines mandatory engineering standards and rules for AI coding agents (and human contributors) working on **AutoTelegramForward**. Any agent generating or modifying code in this repository must strictly adhere to these rules before committing, pushing, or opening PRs.

---

## 🌐 1. Mandatory Multilingual UI Architecture (Zero Hardcoded Strings)

**Core Rule:** Every user-facing UI element across Telegram bot interfaces, web dashboards, and mobile applications **must** support the multi-language mechanism. Hardcoded string literals in UI rendering code are strictly prohibited.

### 1.1 Strict 1:1 Catalog Parity Across All 10 Supported Locales
- **Supported Locales:**
  1. `en` (English - Reference locale)
  2. `fa` (Persian)
  3. `ru` (Russian)
  4. `zh` (Simplified Chinese)
  5. `pt` (Portuguese)
  6. `uz` (Uzbek)
  7. `id` (Indonesian)
  8. `es` (Spanish)
  9. `fr` (French)
  10. `it` (Italian)

- **Catalog Location:** `core/infrastructure/telegram/locales/translations.json` (and `mobile/lib/l10n/` for mobile).
- **Zero Key Gaps:** When adding, renaming, or refactoring a UI message, button label, modal prompt, or alert, the key **must** be added to all 10 locales simultaneously. A key missing in even one locale fails CI.
- **Exact Placeholder Parity:** Placeholders (e.g., `{name}`, `{topic}`, `{secs}`, `{reply}`, `{model}`, `{grp}`, `{status}`) must be identical across all 10 language dictionaries.
- **No RTL Leaks in English:** The `en` dictionary must never contain Persian/Arabic (RTL) characters. English values must be in clean, natural English.
- **Fail-Closed Fallback to English:** Fallback resolution must **always** be `en`, never `fa` or any other locale. A default to anything other than the reference language is considered a leak.

### 1.2 Absolute Prohibition of Hardcoded UI Literals
- **No Inline String Literals:** Never write hardcoded natural language strings in:
  - Menu text and descriptive cards (`text = f"..."`)
  - Inline keyboard buttons (`InlineKeyboardButton("...", ...)`)
  - Ephemeral toasts or popup alerts (`await cq.answer("...", show_alert=True)`)
  - Conversational FSM step prompts (`await message.reply_text("...")`)
  - Error messages, validations, and confirmation dialogues
- **Dynamic Catalog Lookup:** All UI text must be fetched dynamically using `self._t(key, user_id=...)` or `self._t(key, lang=...)`.
- **No Hardcoded Language Branching:** Never use ternary or conditional branching on language codes for UI text (e.g., `is_fa ? "بله" : "Yes"` or `if self._resolve_lang(uid) == "fa": ...`). All variants must be resolved through catalog keys.
- **Allowed Source Non-English Strings:** The only permitted RTL/Persian characters in source files are:
  - User input command aliases in parsers (e.g., `if text in ("/cancel", "انصراف", "لغو"):`)
  - Language picker native display endonyms (e.g., `"fa": "فارسی 🇮🇷"`)
  - Explicit multi-lingual unit tests

### 1.3 Per-User & Owner Language Resolution
- **Telegram Numeric User ID:** Always resolve language using the immutable numeric Telegram User ID (`uid = cq.from_user.id`), never usernames or global state.
- **Resolution Precedence:**
  1. Explicitly passed `lang` override
  2. In-memory user language cache (`self._user_languages[uid]`)
  3. Persisted database preference (`users.get_language(uid)`)
  4. Telegram client language code (`cq.from_user.language_code`)
  5. Default fallback: `"en"`
- **List & Menu Signatures:** Methods rendering lists or interactive menus (e.g., `_rules_menu(rules, page, user_id=...)`) must accept `user_id` so buttons (such as "Edit Rule ✏️", "Delete", "Add") resolve in the interacting user's language.
- **Submenu Owner Context:** For rule-specific submenus (Cleaners, Filters, Replacements, AI settings), resolve against `rule.owner_user_id` when rendering rule properties or status badges.

### 1.4 Parenthesized Multiline String Concatenation Syntax
Inside parenthesized multiline strings `(...)` in Python:
- **Do not mix** explicit `+` operators with adjacent literal strings.
- **Correct Pattern:**
  ```python
  text = (
      self._t("ui_header_key") + "\n\n"
      + f"📡 {self._t('ui_source')}: `{rule.source_chat_name}`\n"
      + f"🎯 {self._t('ui_target')}: `{rule.target_chat_name}`\n\n"
      + self._t("ui_footer_key")
  )
  ```
- **Forbidden Pattern (Causes SyntaxError):**
  ```python
  text = (
      self._t("ui_header_key") + "\n\n"
      f"📡 {self._t('ui_source')}: `{rule.source_chat_name}`\n"  # SyntaxError: invalid syntax
  )
  ```

---

## 🔒 2. Multi-Tenant Isolation & Security Rules

1. **Strict Fail-Closed Isolation:**
   - No user may view, list, edit, or delete another user's forwarding rules, sessions, filters, AI configurations, or logs.
   - All database queries for user resources must include `owner_user_id == uid`.
   - Admin access must be verified strictly by checking numeric ID against `ADMIN_IDS`.
2. **Zero Plaintext Secrets:**
   - Telegram MTProto session strings, bot tokens, and AI API keys must be encrypted with AES-256-GCM.
   - Redact all tokens and keys (`[REDACTED]`) before logging or formatting error traces.

---

## 🧪 3. Mandatory Pre-Commit Quality & Audit Gates

Before pushing changes or submitting PRs, agents must run and pass the following suites locally:

1. **Cross-Language Audit:**
   ```bash
   python scripts/audit_cross_language.py
   ```
   *Validates 1:1 catalog parity across all 10 locales, placeholder consistency, zero RTL in English, and verifies the Persian source baseline.*

2. **Telegram UI Literal Guard:**
   ```bash
   python scripts/audit_telegram_i18n.py
   ```
   *Scans git diff against base branch (`origin/master` or `origin/main`) for any newly introduced hardcoded string literals in UI code.*

3. **Core & Logger Pytest Suites:**
   ```bash
   pytest -q core/tests scripts/tests
   ```

4. **Go API Test Suite:**
   ```bash
   cd api && go test ./...
   ```

5. **Flutter Formatting & Analysis (if modifying `mobile/`):**
   ```bash
   cd mobile && dart format --output=none --set-exit-if-changed lib test && flutter test
   ```

---

## 🚀 4. Deployment & Service Recreate Protocol

1. **Docker Compose Rebuilds:**
   - Rebuild individual services with `docker compose build <service>`.
   - Update containers with `docker restart <container>` or recreate via `docker run`.
2. **Preserve Network Aliases:**
   - Manual container recreates (`docker rm -f && docker run`) lose Docker Compose network aliases.
   - Always connect with explicit network alias:
     `--network autotelegramforward_atf-net --network-alias core` (or `api`, `web`, `logger`).
3. **Verify Healthz:**
   - Always verify `/api/v1/health/services` returns status `"healthy"`.
   - Always verify `https://api.legoten.com/healthz` returns 200 OK.
