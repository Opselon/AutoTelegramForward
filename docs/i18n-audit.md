# Bot i18n audit

One deterministic pass over the Telegram surface, run from the repository root:

```bash
python3 scripts/i18n_audit.py                 # human report
python3 scripts/i18n_audit.py --json out.json # machine report
python3 scripts/i18n_audit.py --fail-on major # what CI runs
python3 scripts/i18n_audit.py --update-baseline
```

Exit codes: `0` clean (or only baselined findings), `1` a new finding at or
above the threshold, `2` the audit itself could not run (broken catalog).

## Why it exists

The repository already had four copy gates. Each answers one question, and the
questions they leave out are the ones users see:

| gate | question | blind spot |
|---|---|---|
| `audit_cross_language.py` | are the catalogs 1:1, placeholders equal? | knows nothing about render paths |
| `audit_telegram_i18n.py` | did *this diff* add a visible literal? | change-scoped; keyboard tuples never enter it |
| `audit_mobile_ui_copy.py` | Flutter widgets | not the bot |
| `audit_web_ui_copy.js` | JSX copy | not the bot |

The audit closes the gap between "the key exists" and "the user sees it".

## Finding classes

| class | severity | what it means |
|---|---|---|
| `CATALOG_PARITY` | critical | a locale's key set differs from English |
| `PLACEHOLDER_MISMATCH` | critical | placeholder names, conversions or format specs differ |
| `MISSING_KEY` | critical | a literal key used in code that no catalog defines |
| `HARDCODED_USER_TEXT` | critical | prose literal reaching `reply_text`/`answer`/`InlineKeyboardButton`/`_kbd` tuples, including the constant part of an f-string |
| `DYNAMIC_KEY_UNVERIFIED` | major | a key assembled from a prefix at runtime; the family must be enumerable |
| `GLOBAL_LANGUAGE_STATE` | critical | `i18n.current` read on a render path |
| `UNWRAPPED_HANDLER` | critical | an update handler renders without the user's language |
| `CALLBACK_DATA_FROM_TEXT` | critical | callback data built from a translation |
| `VALUE_LEAK` | major | a Latin locale holding Persian/Cyrillic/CJK text |
| `UNTRANSLATED_VALUE` | major | a locale's value is the English prose verbatim |
| `EMPTY_VALUE`, `INVALID_CHARACTER` | major / critical | empty string, or U+FFFD from a broken encode |
| `UNUSED_KEY` | minor | a key nothing in the repository mentions |
| `PARSE_ERROR` | critical | a UI module the audit could not parse |

Brand and protocol tokens (`VIP Pro`, `Telegram`, `API`, `JSON`, `2FA`, bank and
exchange names) are in `NEUTRAL_TOKENS`: a value made only of them is not an
untranslated string, and pinning them in the baseline instead would keep a real
leak next to them green.

## What it proved on this tree

The first full run, before any fix:

```
27 UI modules, 269 handlers (265 locale-bound), 1241/1561 keys referenced, 10 locales
UNTRANSLATED_VALUE 5 · UNUSED_KEY 320 · UNWRAPPED_HANDLER 4
```

The four unwrapped handlers were real user-visible defects in
`core/infrastructure/telegram/bot_manager.py`: `/sessions`, `/rules`, `/ai` and
`/stats` localized the panel header and then rendered every list row through
the process-wide default, so a Persian user got a Persian heading over English
rows. `_login_reply` had the same shape for login errors, and answered an
unknown result code with the code itself.

After the fixes: `269/269` handlers locale-bound, zero critical and zero major
findings, and 278 unused keys reported as minor debt (they are kept: the
catalog is also the dashboard's generated setting-label source, and deleting a
key one generator still reads is a runtime regression in a different surface).

## Baseline

`scripts/i18n_baseline.json` is content-keyed (`kind|path|detail|evidence`,
16-char hash), so moving a line does not re-red the gate and rewriting a
finding's evidence does not silently keep it green. `--update-baseline`
re-pins everything; it is meant for a reviewed commit that fixes part of the
backlog, and the diff of the baseline file is the record of what was accepted.

The default threshold is `critical`; CI runs `--fail-on major`, because an
untranslated value is a defect a user sees even though no code path is broken.

## Tests

* `core/tests/test_i18n_audit_engine.py` — 21 mutation tests. Each defect class
  is injected into a throwaway copy of the tree (real catalog, synthetic
  module) and the engine must report exactly that class; the false-positive
  tests pin that a numeric keypad, a URL button, callback-data identifiers and
  a handler that resolves `lang` itself stay silent. Two tests run the engine
  over this repository: no critical findings, ever.
* `core/tests/test_bot_locale_matrix.py` — renders 16 real panels in all ten
  locales (325 assertions): no panel is byte-identical to English, Persian
  contains Persian, Russian Cyrillic and Chinese CJK, no raw key or
  unsubstituted placeholder reaches the screen, switching language changes the
  next render, concurrent renders do not share a language, and a render with no
  update context falls back to English — never to the last picked language.

## In CI

`.github/workflows/ci.yml` runs `python scripts/i18n_audit.py --fail-on major`
next to the existing guards. A hardcoded sentence, a missing key, a translated
callback payload or a handler that forgets the user's language fails the job;
dead keys are printed in the report but do not fail it.
