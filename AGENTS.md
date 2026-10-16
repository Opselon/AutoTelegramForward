# AGENTS.md — AutoTelegramForward Repository Instructions for AI Agents

Welcome, Agent. Before modifying code, adding features, or opening pull requests in this repository, you **must** read and follow the engineering rules outlined below and detailed in [`docs/agent-rules.md`](docs/agent-rules.md).

---

## 🚨 Non-Negotiable Core Rule: Mandatory Multilingual UI (Zero Hardcoded UI Strings)

Every user-facing UI element across the Telegram bot, web dashboard, and mobile client **must** use the centralized multi-language system.

1. **Strict 1:1 Catalog Parity (10 Locales):**
   - Every translation key in `core/infrastructure/telegram/locales/translations.json` must be defined across all 10 supported locales:
     `en`, `fa`, `ru`, `zh`, `pt`, `uz`, `id`, `es`, `fr`, `it`.
   - Missing a key in any single locale will cause CI failure.
   - Placeholder variables (e.g. `{name}`, `{topic}`, `{secs}`) must match exactly in all locales.
   - The English (`en`) catalog must never contain Persian/Arabic (RTL) characters.
   - Fallback is strictly `en` (English), never `fa`.

2. **No Hardcoded UI Strings in Code:**
   - **Never** write hardcoded natural language strings in buttons, menu text, FSM step prompts, or callback alerts (`cq.answer(...)`).
   - Always resolve via `self._t("key", user_id=uid)` or `self._t("key", lang=lang)`.
   - **Never** branch on language code (e.g. `is_fa ? x : y` or `if lang == "fa":`).

3. **Per-User Resolution:**
   - Always resolve language using numeric Telegram User ID (`uid = cq.from_user.id`), falling back: User DB preference -> User Telegram `language_code` -> `"en"`.
   - Thread `user_id` into all list and menu rendering methods so action buttons ("Edit", "Delete", "Add") resolve in the user's selected language.

4. **Multiline Parentheses Concatenation Syntax:**
   - When concatenating strings and `self._t(...)` calls inside parentheses `(...)`, always use explicit `+` operators on every line. Never rely on implicit concatenation between function calls and string literals.

---

## 🔒 Security & Multi-Tenant Isolation
- Fail-closed tenant boundaries: filter all user queries by `owner_user_id == uid`.
- Zero plaintext credentials: encrypt MTProto session strings and bot tokens with AES-256-GCM.
- Redact secrets (`[REDACTED]`) before logging.

---

## 🧪 Required Local Checks Before PR / Commit
```bash
# 1. Cross-language audit (1:1 parity, placeholder validation, leak scanner)
python scripts/audit_cross_language.py

# 2. Telegram UI hardcoded literal guard (checks git diff against base branch)
python scripts/audit_telegram_i18n.py

# 3. Python unit test suites
pytest -q core/tests scripts/tests

# 4. Go API test suite
cd api && go test ./...
```

For the complete guidelines and deployment procedures, refer to [`docs/agent-rules.md`](docs/agent-rules.md).
