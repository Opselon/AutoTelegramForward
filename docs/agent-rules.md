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
  - Language picker native display endonyms (e.g., `"fa": "فارسی 🇮🇷"`)
  - Explicit multi-lingual unit tests
- **User Input Aliases Are Catalog Keys, Not Literals:** a word a user may *type*
  (cancel, clear, default, "back") is UI contract, not a technical token: the panel
  offers that word in the user's language, so the parser must accept it in that
  language too. Put the words in a comma-separated catalog key
  (`ui_input_cancel_aliases`, `ui_input_clear_aliases`, `ui_input_default_aliases`)
  and match with the shared helper (`cancel_aliases()`, `_input_aliases(key)`) — never
  a per-site tuple. A tuple covered only `en`/`fa` (so 8 locales could not cancel
  a flow by typing) and 21 call sites drifted into three different word lists; the
  audit now fails on any new literal, escaped (`"\u0627…"`) or not.

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

### 1.5 Button Styles Live in One Layer
- **Where:** `core/infrastructure/telegram/ui_styles.py` owns the style vocabulary —
  the canonical leading glyph and the layer-225 background colour of a semantic
  action class (`primary`, `success`, `danger`, `warning`, `nav`, `refresh`, `info`,
  `open`, `neutral`).
- **Never hand-roll a glyph.** A prefix like `"❌ " + self._t(key)` rendered as
  `"❌ ❌ Cancel"` once the catalog value carried its own glyph, and drifted between
  `"✖"`, `"✗"` and `"❌"`; `_btn("key")` applies the canonical glyph exactly once,
  after the catalog lookup, in every locale.
- **Style derives from the catalog key, never from the language** — there is no
  `lang == "fa"` branching anywhere in the style layer, and a test enforces that.
- **Colour is semantic, not decorative.** Only destructive/positive/primary classes
  are painted; `_kbd()` derives the colour from the rendered glyph and an explicit
  third tuple element `(label, data, "danger")` always wins.
- **Decoration belongs to the key, not to the locale** — every locale must wear the
  same leading glyph for the same key, catalog-wide (not only on buttons), and no
  value may carry the same glyph twice or contain U+FFFD from a bad encoding
  round-trip. A greeting emoji moved to the end of the line (`"… kelibsiz! 👋"`)
  leaves that language with an undecorated title and is a defect.
- **The glyph class is published, not copied.** `ui_styles.GLYPH_TOKEN` is the one
  character class; the cross-language audit matches the same set. Two private copies
  had drifted and both missed `ℹ` (U+2139), so `"ℹ️ Settings"` read as undecorated
  copy and got a second glyph glued in front. Box drawing (`━━━`) is deliberately
  excluded — a separator rule is layout, not decoration.
- **Decoration preserves whitespace.** A value may open with a newline
  (`"\n⚠️ *Reply … will not work*"`); the glyph is inserted *after* the leading
  whitespace, never in front of it, and the body is never stripped.
- **Two style layers exist and must not be confused:** `button_style.py` is the raw
  `keyboardButtonStyle` wire encoder; `ui_styles.py` is the vocabulary on top of it.

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
   *Validates, in order: (1) 1:1 catalog parity across all 10 locales, (2) placeholder
   parity, (3) no cross-language leak in the reference locale, (4) no hardcoded
   copy in **any** non-reference script (Persian, Cyrillic, Han, Kana, Hangul,
   Hebrew, Thai, Devanagari) beyond the pinned baseline, (5) no language-code
   branching, (6) every button label resolves through the catalog, (7) button
   decoration parity — one canonical glyph, identical in all 10 locales, for every
   catalog key and not just buttons, with no doubled glyph and no U+FFFD — (8) no
   literal callback/toast copy, (9) locale isolation on every registered handler,
   (10) every static `_t()`/`_btn()` key exists. `--report` prints the button-label
   inventory per file (labels / catalog-resolved / hardcoded).*

2. **Telegram UI Literal Guard:**
   ```bash
   python scripts/audit_telegram_i18n.py
   ```
   *Scans git diff against base branch (`origin/master` or `origin/main`) for any newly introduced hardcoded string literals in UI code.*

3. **Web Dashboard UI-Copy Gate (JSX-aware):**
   ```bash
   node scripts/audit_web_ui_copy.js
   ```
   *Protects the dashboard's copy. Parses `web/frontend/src/**` with the bundled
   `typescript` compiler (no regex guessing) and fails on user-visible copy that
   never enters a translation catalog: JSX text nodes, `placeholder` / `title` /
   `aria-label` / `aria-description` / `aria-valuetext` / `alt` / `label` /
   `tooltip` string attributes, string branches of `cond ? 'A' : 'B'`,
   `flag && 'A'` and template literals that land in a copy position, and
   copy-named object properties (`{ label: 'Active' }`). It is not fooled by
   `{t('key')}`, `<Trans>` children, or a catalog lookup in one branch of a
   ternary — and it still flags the old `t(fa, en)` positional helper, which is
   `isRtl ? fa : en` wearing a function name. Documented exemptions: the brand
   `AutoTelegramForward`, URLs/routes/MIME types, CSS values, identifiers and
   field names (`filter_rule_id`), log levels and env constants (`ERROR`),
   service names (`core`, `api`, `logger`, `web`), shell commands, format
   examples (`{source_title}`), and the catalog/generated tables themselves
   (`src/locales/**`, `*.generated.ts`). Existing debt is pinned by
   **content**, not line, in `scripts/.web_ui_copy_baseline.txt`
   (`path|normalized text`, `#` comments allowed), so inserting a line above an
   offender cannot redden CI. `--report` lists every offender;
   `--update-baseline` rewrites the pin and prints the new count; a backlog that
   shrank prints a *backlog shrank — regenerate the baseline* notice. Requires
   `web/frontend/node_modules/typescript` (`npm ci`), and exits 2 rather than
   reporting a pass when it cannot find it.*

4. **Mobile UI-Copy Gate (Dart):**
   ```bash
   python scripts/audit_mobile_ui_copy.py
   ```
   *Protects the Flutter client's copy, with the same content-keyed baseline
   (`scripts/.mobile_ui_copy_baseline.txt`). Scans `mobile/lib/**/*.dart`
   excluding `mobile/lib/l10n/` for display literals: `Text('…')`,
   `const Text('…')`, and string values of `labelText`, `hintText`, `tooltip`,
   `label`, `title`, `semanticLabel`, `helperText`, `errorText`, `emptyTitle`,
   `emptySubtitle`, `message`, `description`, … — including interpolated
   strings, whose static part is what the user reads (`'Sender: ${r.senderType}'`).
   Catalog lookups (`Text(t.get('key'))`) and technical tokens (URLs, paths,
   timestamps, `AutoTelegramForward`, `{source_title}`, `filter_rule_id`,
   `ERROR`, `application/json`) are tolerated by documented rules, and the
   matcher reads Dart's quoting (`'`/`"`, triple quotes, `r'…'`,
   `${t.get('k')}`) rather than guessing with a regex. Stdlib-only, so it runs
   in the python CI job, which has no Dart SDK — `flutter analyze` cannot catch
   a hardcoded label. Same `--report` / `--update-baseline` / shrink notice.*

5. **Core & Logger Pytest Suites:**
   ```bash
   pytest -q core/tests scripts/tests
   ```

6. **Go API Test Suite:**
   ```bash
   cd api && go test ./...
   ```

7. **Flutter Formatting & Analysis (if modifying `mobile/`):**
   ```bash
   cd mobile && dart format --output=none --set-exit-if-changed lib test && flutter test
   ```

---

## 🎨 3.5 UI/UX & Performance Contract (binding for every UI change)

[`docs/ui-ux-system.md`](ui-ux-system.md) is the design contract for the dashboard,
the mobile client and the bot: design tokens, breakpoints, the six shared
primitives, accessibility thresholds and the performance budgets. A screen that
renders but ignores a token, a breakpoint or a budget is not done.

1. **No magic values in UI code.** Colours come from role tokens, spacing/radius/
   type/motion from the token scale. A new hex literal or an ad-hoc `margin: 13px`
   in a component is a review failure.
2. **Every data screen has four states** — loading (skeleton), empty (with a call
   to action), error (with retry) and success. Blank screens and raw
   `Error.toString()` never ship.
3. **Accessibility is part of the definition of done:** 44 px / 48 dp targets, a
   visible focus ring, `aria-label`/`Semantics` on icon-only controls, contrast
   ≥ 4.5:1 in both themes, reduced-motion respected, text scaling 0.8–1.3 without
   clipping, and logical (`*-inline`) properties so RTL flips correctly.
4. **Measure, don't assert.** Any change to a screen on a hot path, to the web
   build or to a bot render path must show numbers:
   ```bash
   python3 scripts/bench/ui_perf_bench.py --label before
   # ...change...
   python3 scripts/bench/ui_perf_bench.py --label after
   python3 scripts/bench/ui_perf_compare.py docs/benchmarks/ui-perf-before.json \
       docs/benchmarks/ui-perf-after.json
   ```
   The compare gate fails on +15% latency, +5% bundle growth, a vanished endpoint
   or a collapsed code-split chunk. Paste the output in the PR.
5. **Feature reachability is tracked, not assumed:**
   ```bash
   python3 scripts/audit_feature_ui_coverage.py --gaps
   ```
   A feature counts as reachable on a surface only when a control exists, it is
   labelled in all ten locales, and a test exercises it — see
   [`docs/feature-ui-coverage.md`](feature-ui-coverage.md).

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
