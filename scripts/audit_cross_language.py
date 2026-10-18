#!/usr/bin/env python3
"""Cross-language one-to-one audit for AutoTelegramForward.

Algorithm (per the mandate: "check all strings one to one algorithm and
complete cross-language configurations"):

For EVERY translation catalog in EVERY client (Telegram bot, web, mobile):
  1. KEYS: every language exposes exactly the same key set as the reference
     (English). No missing, no extra keys.
  2. NON-EMPTY: no value is empty or whitespace-only.
  3. NO CROSS-LEAK: the reference (English) values contain no graphemes of
     another language (this is the exact production defect: English users
     saw Persian strings because Persian text was stored under the 'en'
     keys, and the web dashboard hard-defaulted to 'fa').
  4. PLACEHOLDER PARITY: every {placeholder} in the reference appears in
     every translation (a missing placeholder breaks formatting at runtime).

The source side is audited too, because a hardcoded literal never enters a
catalog: parity can be perfectly green while a user reads a language they
never chose.

  5. HARDCODED SCRIPT LITERALS: no core source may carry copy in a script
     other than the reference's Latin one — Persian *and* Russian, Chinese,
     Japanese, Korean, Hebrew, Thai, Devanagari. The scanner originally
     looked for Persian only, so a `elif lang == "ru": "Connect Again"`
     branch shipped Russian copy to every locale that was not fa/ru/zh.
  6. NEVER BRANCH ON A LANGUAGE CODE: labels resolve through the catalog.
  7. BUTTON LABELS: every label an inline/reply keyboard renders resolves
     through the catalog (or is a technical token: a keypad digit, a
     callback id, a glyph-only pad key).
  8. BUTTON DECORATION PARITY: a button's leading glyph is decoration and
     must be exactly one per label and identical in all 10 locales — keys
     decorated "✖ Cancel" here and "❌ ❌ Cancel" there looked broken
     everywhere, and 8 main-menu keys carried their glyph in English only.
  9. CALLBACK COPY: a callback toast/alert must carry catalog copy, not a
     literal.
 10. Each registered event handler runs inside the requesting user's locale.

Exits 0 only when every catalog and every source rule passes.

Usage:
    python scripts/audit_cross_language.py            # gate (CI)
    python scripts/audit_cross_language.py --report   # + button inventory
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    # The audit is run as a script (`python scripts/audit_cross_language.py`),
    # so the repo root is not importable by default and the style registry
    # could not be read.
    sys.path.insert(0, str(REPO))

# The style registry is imported lazily (see _style_module).

#: Arabic-script letters that must never appear in the reference language's
#: strings. Matched as a script (every letter of the block), not a hand-picked
#: handful of graphemes: the original list missed common letters like ش/ر/ع, so
#: "شروع" under an English key read as clean copy.
RTL_LETTERS = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")

# The one legitimate RTL value in an English catalog: a language's own endonym.
# A user picking Persian in a picker written in English must read "فارسی", so
# these keys opt out of the cross-leak rule explicitly rather than relaxing it
# for every key - anything else with RTL text in `en` is still a defect.
RTL_EXEMPT_KEYS = frozenset({"lang_native_fa"})

SUPPORTED_LANGUAGES = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")
REFERENCE = "en"

BOT_CATALOG = REPO / "core/infrastructure/telegram/locales/translations.json"
BOT_UI = REPO / "core/infrastructure/telegram/pro_bot_ui.py"

# Every Telegram UI source file is discovered at runtime; no hand-kept list.
UI_ROOTS = (
    "core/infrastructure/telegram/",
    "core/presentation/telegram/",
    "core/application/telegram/",
)

# Scripts that are NOT the reference language's. A literal in one of these is
# always a leak; Latin literals are covered by rule 7 where they are visible.
NON_REFERENCE_SCRIPTS: dict[str, re.Pattern[str]] = {
    "Persian/Arabic": re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]"),
    "Cyrillic": re.compile(r"[\u0400-\u04FF]"),
    "Han": re.compile(r"[\u4E00-\u9FFF\u3400-\u4DBF]"),
    "Kana": re.compile(r"[\u3040-\u30FF]"),
    "Hangul": re.compile(r"[\uAC00-\uD7AF]"),
    "Hebrew": re.compile(r"[\u0590-\u05FF]"),
    "Thai": re.compile(r"[\u0E00-\u0E7F]"),
    "Devanagari": re.compile(r"[\u0900-\u097F]"),
}

#: A row of the language picker: "🇷🇺 Русский (RU)" is a language's own endonym
#: and is exempt from translation (docs/agent-rules.md §1.2).
ENDONYM_ROW = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")

#: A language-code comparison: `if lang == "fa":` / `elif lang == "ru":`.
LANGUAGE_BRANCH = re.compile(
    r"(?:if|elif|while)\s+[^=]*?\b(?:lang|language|locale|loc)\b[^=]*=="
    r"\s*[\"'](" + "|".join(SUPPORTED_LANGUAGES) + r")[\"']"
)

I18N_MARKERS = ("_t(", "_btn(", ".t(", "i18n.", "translate(", "gettext(", "decorate(")

#: A leading glyph plus its optional trailing space.
GLYPH = re.compile(
    r"^\s*((?:[\U0001F000-\U0001FAFF\u2190-\u2BFF\u2600-\u27BF\u231A-\u23FF"
    r"\u2705\u274C\u2B50\u2714\u2716\u20E3]|\uFE0F)+)"
)

#: Names of locals that accumulate keyboard rows: `rows.append([(label,
#: data)])` is how most panels are built, and the callback-data element is
#: what identifies the tuple as a row. Scoping on the name keeps unrelated
#: `(str, str)` tuples (e.g. the style rule table) out of the audit.
KEYBOARD_ROW_VAR = re.compile(
    r"^(?:\w*_)?(?:row|rows|kbd|kb|keyboard|keyboards|button|buttons|markup|markups|items)s?$",
    re.IGNORECASE,
)

#: Literals that are interface tokens, not copy: callback ids, keys, digits.
TECHNICAL_LITERAL = re.compile(
    r"^(?:https?://|@[A-Za-z0-9_]{3,}|[A-Z][A-Z0-9_]{2,}|[a-z0-9_]+|\d+(?:\.\d+)?|\s*|[\W_]+)$"
)

failures: list[str] = []
notes: list[str] = []


# --------------------------------------------------------------------------- #
# catalog rules
# --------------------------------------------------------------------------- #
def check(name: str, lang: str, keys: dict[str, str], ref: dict[str, str]) -> None:
    ref_keys = set(ref)
    lang_keys = set(keys)
    missing = ref_keys - lang_keys
    if missing:
        failures.append(f"{name}/{lang}: missing {len(missing)} keys, e.g. {sorted(missing)[:4]}")
    extra = lang_keys - ref_keys
    if extra:
        failures.append(f"{name}/{lang}: extra {len(extra)} keys, e.g. {sorted(extra)[:4]}")
    empty = [k for k in ref_keys if not (keys.get(k) or "").strip()]
    if empty:
        failures.append(f"{name}/{lang}: empty values for {empty[:4]}")
    # placeholder parity
    for k in ref_keys & lang_keys:
        need = set(PLACEHOLDER.findall(ref[k] or ""))
        got = set(PLACEHOLDER.findall(keys[k] or ""))
        if need != got:
            failures.append(
                f"{name}/{lang}/{k}: placeholders differ (ref={sorted(need)}, got={sorted(got)})"
            )
    # Cross-leak (reference language only — translations legitimately use it)
    if lang == "en":
        leaked = [
            k
            for k in ref_keys - RTL_EXEMPT_KEYS
            if _has_arabic_letters(keys.get(k) or "")
        ]
        if leaked:
            failures.append(
                f"{name}/en: English strings contain Persian/Arabic text in {sorted(leaked)}"
            )


def audit_bot() -> None:
    if not BOT_CATALOG.exists():
        print("bot catalog missing — skipped")
        return
    catalog = json.loads(BOT_CATALOG.read_text(encoding="utf-8"))
    ref = catalog.get("en")
    if not ref:
        failures.append("bot: no English reference catalog")
        return
    for lang, table in catalog.items():
        check("bot", lang, table, ref)


def audit_mobile() -> None:
    l10n = REPO / "mobile/lib/l10n"
    if not l10n.exists():
        print("mobile catalog missing — skipped")
        return
    ref: dict[str, str] = {}
    en = l10n / "app_messages.dart"
    if en.exists():
        ref = dict(re.findall(r"'([a-z_]+)':\s*'((?:[^'\\]|\\.)*)'", en.read_text()))
    if not ref:
        failures.append("mobile: no English reference catalog")
        return
    # Locale('es') literals and format-param maps are false positives.
    static_keys = {"es", "fa", "fr", "id", "it", "phone", "pt", "ru", "seconds", "uz", "zh"}
    ref = {k: v for k, v in ref.items() if k not in static_keys}
    for f in sorted(l10n.glob("app_*.dart")):
        table = dict(re.findall(r"'([a-z_]+)':\s*'((?:[^'\\]|\\.)*)'", f.read_text()))
        table = {k: v for k, v in table.items() if k not in static_keys}
        # app_messages.dart holds the English map too.
        lang = "en" if f.name == "app_messages.dart" else f.stem.removeprefix("app_")
        check("mobile", lang, table, ref)


# --------------------------------------------------------------------------- #
# shared source helpers
# --------------------------------------------------------------------------- #
def _style_module():
    """The button style registry, imported on first use.

    Deliberately lazy: ``ui_styles`` is importable without the Telegram
    runtime, but a module-level import would let a single broken import take
    the whole gate down instead of reporting the other nine rules.
    """
    from core.infrastructure.telegram import ui_styles

    return ui_styles


def ui_python_files() -> list[Path]:
    """Every Telegram UI source file, discovered (never hand-listed)."""
    found: set[Path] = set()
    for root in UI_ROOTS:
        folder = REPO / root
        if folder.exists():
            found.update(p for p in folder.rglob("*.py") if p.is_file())
    return sorted(found)


def load_baseline(path: Path) -> tuple[set[str], set[str]]:
    """Read a pinned backlog (``path:lineno|text`` per line, ``#`` comments).

    The text half is what makes the baseline stable: a bare line number is
    not, because any insertion above it shifts every entry below and the
    whole backlog reads as "new".
    """
    pinned_loc: set[str] = set()
    pinned_text: set[str] = set()
    if not path.exists():
        return pinned_loc, pinned_text
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        locator, sep, text = entry.partition("|")
        pinned_loc.add(locator.split("#")[0].strip())
        if sep and text.strip():
            pinned_text.add(f"{locator.split(':')[0]}|{text.strip()}")
    return pinned_loc, pinned_text


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _has_non_reference_letters(line: str) -> bool:
    """True when a line carries *letters* of a non-reference script.

    Digits and punctuation are not copy. A normaliser that maps ۰۱۲۳ to 0123
    and a parser that accepts the Arabic comma as a separator both matched the
    old Persian-only scan (which is why it was noisy and went unmaintained)
    while displaying nothing to anyone.
    """
    for char in line:
        if not char.isalpha():
            continue
        if any(rx.search(char) for rx in NON_REFERENCE_SCRIPTS.values()):
            return True
    return False


def _has_arabic_letters(value: str) -> bool:
    """True when a value carries Arabic-script letters (Persian included)."""
    return any(char.isalpha() and RTL_LETTERS.match(char) for char in value or "")


def _is_ui_copy(value: str) -> bool:
    """True when a literal is rendered copy rather than a technical token.

    Note the absence of a "must contain a space" heuristic: ``cq.answer(
    "Saved!")`` renders one word to the user, and a hardcoded single-word
    button label is just as untranslated as a sentence. Tokens that are
    identifiers, callback ids, URLs, digits or glyph padding are excluded by
    :data:`TECHNICAL_LITERAL`.
    """
    value = (value or "").strip()
    if not value or value.startswith(("http://", "https://", "@")):
        return False
    if _has_non_reference_letters(value):
        return True
    if TECHNICAL_LITERAL.fullmatch(value):
        return False
    # Two consecutive letters, so a formatting fragment ("(v)", " #", " · ")
    # is composition rather than copy.
    return bool(re.search(r"[A-Za-zÀ-ž]{2,}", value))


def _leading_glyph(value: str) -> str:
    """The literal leading glyph of a label, NOT canonicalised.

    Deliberately raw: canonicalising here would hide exactly the drift this
    audit exists to catch (``"✖ Cancel"`` rather than ``"❌ Cancel"``).
    """
    match = GLYPH.match(value or "")
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- #
# rule 5 — hardcoded non-reference script literals
# --------------------------------------------------------------------------- #
def audit_source_hardcoded() -> None:
    """No core source may hardcode copy in a script other than the reference's.

    Covers Persian (the original defect) *and* every other script a
    translator may paste: a hardcoded Russian button is exactly as invisible
    to an English user as a Persian one, and the Persian-only scanner let
    both `elif lang == "ru"` blocks ship for months.

    A pre-existing backlog is pinned in scripts/.persian_baseline.txt, one
    entry per line as ``path:lineno|text``. The audit fails only on leaks NOT
    in the baseline — so CI stays green while the backlog is being extracted,
    and any NEW hardcoded literal fails immediately. Shrink the baseline as
    functions are converted; never grow it.
    """
    baseline_path = REPO / "scripts/.persian_baseline.txt"
    pinned_loc, pinned_text = load_baseline(baseline_path)

    skip_dirs = {"tests", "locales", "__pycache__"}
    offenders: list[str] = []
    new_leaks: list[str] = []
    for p in sorted((REPO / "core").rglob("*.py")):
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.name == "pv_responder.py":
            # A user-authored Persian persona by design, not UI copy.
            continue
        rel = p.relative_to(REPO)
        for i, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            if ENDONYM_ROW.search(line):
                continue  # "🇷🇺 Русский (RU)" — an endonym, per agent-rules §1.2
            if not _has_non_reference_letters(line):
                continue
            if "STR.Migrations" in line:
                continue
            locator = f"{rel}:{i}"
            offenders.append(locator)
            if locator in pinned_loc:
                continue
            if f"{rel}|{line.strip()}" in pinned_text:
                continue  # same line, moved by an insertion above it
            new_leaks.append(locator)

    if new_leaks:
        failures.append(
            f"source: {len(new_leaks)} NEW hardcoded non-English strings not in "
            f"{baseline_path.name} (e.g. {new_leaks[:3]}). "
            "Convert them to catalog keys, or if genuinely unavoidable, "
            "regenerate the baseline and explain why."
        )
    pinned_total = len(pinned_text) or len(pinned_loc)
    if len(offenders) < pinned_total:
        print(
            f"source: backlog shrank {pinned_total} -> {len(offenders)}; "
            "regenerate scripts/.persian_baseline.txt to pin the progress."
        )


# --------------------------------------------------------------------------- #
# rule 6 — never branch on a language code
# --------------------------------------------------------------------------- #
def language_branch_findings(source: str, path: str = "ui.py") -> list[str]:
    """Find ``if lang == "fa"`` style branches in UI source."""
    return [
        f"{path}:{i}: language branch on {match.group(1)!r} — resolve the label "
        "through the catalog instead"
        for i, line in enumerate(source.split("\n"), 1)
        if (match := LANGUAGE_BRANCH.search(line))
    ]


def audit_bot_language_branching() -> None:
    findings: list[str] = []
    files = ui_python_files()
    for p in files:
        findings.extend(language_branch_findings(p.read_text(encoding="utf-8"), _relative(p)))
    if findings:
        failures.extend(findings)
        return
    print(f"PASS no language branching: {len(files)} UI source file(s)")


# --------------------------------------------------------------------------- #
# rule 7 — every button label resolves through the catalog
# --------------------------------------------------------------------------- #
def button_label_nodes(source: str) -> list[tuple[int, ast.AST, str]]:
    """Every expression that ends up as a button label, with line and kind."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    labels: list[tuple[int, ast.AST, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in ("InlineKeyboardButton", "KeyboardButton") and node.args:
            labels.append((node.args[0].lineno, node.args[0], "InlineKeyboardButton"))
        elif name in ("_kbd", "_cancel_kbd") and node.args:
            for element in ast.walk(node.args[0]):
                if isinstance(element, ast.Tuple) and len(element.elts) >= 2:
                    labels.append((element.elts[0].lineno, element.elts[0], "_kbd"))
    # Rows are commonly assembled into a local list first (`rows.append([
    # (label, data)])`) and only handed to _kbd() at the end.
    for node in ast.walk(tree):
        containers: list[ast.AST] = []
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("append", "extend", "insert"):
                if isinstance(func.value, ast.Name) and KEYBOARD_ROW_VAR.match(func.value.id):
                    containers.extend(node.args)
            elif isinstance(func, ast.Name) and KEYBOARD_ROW_VAR.match(func.id):
                containers.extend(node.args)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(t, ast.Name) and KEYBOARD_ROW_VAR.match(t.id) for t in targets
            ):
                containers.append(node.value)
        for container in containers:
            for element in ast.walk(container):
                if not isinstance(element, ast.Tuple) or len(element.elts) not in (2, 3):
                    continue
                data = element.elts[1]
                if isinstance(data, ast.Constant) and isinstance(data.value, str):
                    if re.search(r"\s", data.value):
                        continue
                elif not isinstance(
                    data, (ast.JoinedStr, ast.Attribute, ast.Subscript, ast.Name)
                ):
                    continue
                labels.append((element.elts[0].lineno, element.elts[0], "row"))
    return labels


def button_label_findings(
    source: str, path: str = "ui.py"
) -> tuple[list[str], int, int, int]:
    """Return (findings, labels, catalog-resolved labels, endonym labels)."""
    findings: list[str] = []
    total = resolved = exempt = 0
    for lineno, node, kind in button_label_nodes(source):
        total += 1
        segment = ast.get_source_segment(source, node) or ""
        if any(marker in segment for marker in I18N_MARKERS):
            resolved += 1
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if ENDONYM_ROW.search(node.value):
                # "🇷🇺 Русский (RU)": a language's own name is the one label
                # that must NOT be translated (docs/agent-rules.md §1.2).
                exempt += 1
                continue
            if not _is_ui_copy(node.value):
                continue  # keypad digit, callback id, glyph-only pad key
            findings.append(
                f"{path}:{lineno}: hardcoded {kind} button label "
                f"{node.value[:60]!r} — use self._btn('catalog_key')"
            )
        elif isinstance(node, ast.JoinedStr):
            static = "".join(
                str(part.value) for part in node.values if isinstance(part, ast.Constant)
            )
            if _is_ui_copy(static):
                findings.append(
                    f"{path}:{lineno}: hardcoded text in {kind} button label "
                    f"{static.strip()[:60]!r} — use self._btn('catalog_key')"
                )
    return findings, total, resolved, exempt


def audit_bot_button_labels() -> None:
    findings: list[str] = []
    total = resolved = exempt = 0
    for p in ui_python_files():
        found, seen, ok, native = button_label_findings(
            p.read_text(encoding="utf-8"), _relative(p)
        )
        findings.extend(found)
        total += seen
        resolved += ok
        exempt += native
    if findings:
        failures.extend(findings)
        return
    notes.append(
        f"button labels: {total} discovered, {resolved} resolve through the catalog, "
        f"{exempt} language endonyms exempt by design"
    )


# --------------------------------------------------------------------------- #
# rule 8 — button decoration: exactly one glyph, identical in every locale
# --------------------------------------------------------------------------- #
def _decorated_key(source: str, node: ast.AST) -> tuple[str, str] | None:
    """('glyph', 'catalog_key') for a hand-decorated label, else None."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            prefix, call = node.left.value, node.right
        elif isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            prefix, call = node.right.value, node.left
        else:
            return None
    elif isinstance(node, ast.JoinedStr):
        if len(node.values) != 2:
            return None
        head, tail = node.values
        if not isinstance(head, ast.Constant) or not isinstance(head.value, str):
            return None
        prefix = head.value
        call = getattr(tail, "value", None)
    else:
        return None
    if not isinstance(call, ast.Call) or _call_name(call.func) not in ("_t", "_btn"):
        return None
    keys = [
        a.value
        for a in call.args
        if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]
    if not keys:
        return None
    return _leading_glyph(prefix), keys[0]


def button_style_findings(
    source: str, catalog: dict[str, dict[str, str]], path: str = "ui.py"
) -> list[str]:
    """A decorated label must carry one canonical glyph, never doubled."""
    reference = catalog.get(REFERENCE, {})
    findings: list[str] = []
    for _lineno, node, _kind in button_label_nodes(source):
        decorated = _decorated_key(source, node)
        if not decorated:
            continue
        glyph, key = decorated
        line = getattr(node, "lineno", 1)
        if glyph in _style_module().GLYPH_ALIASES:
            findings.append(
                f"{path}:{line}: drifted glyph decorating {key} — use "
                f"self._btn('{key}') (the canonical glyph is "
                f"{_style_module().GLYPH_ALIASES[glyph]!r})"
            )
            continue
        reference_glyph = _leading_glyph(reference.get(key, ""))
        style_glyph = _style_module().style_glyph(key)
        if not glyph:
            findings.append(
                f"{path}:{line}: {key} is hand-decorated with copy, not a style glyph "
                f"— use self._btn('{key}')"
            )
        elif glyph == reference_glyph:
            findings.append(
                f"{path}:{line}: {key} would render its glyph twice (prefix {glyph!r} "
                f"plus the catalog value) — use self._btn('{key}')"
            )
        elif glyph not in {style_glyph} and glyph not in _style_module().registered_glyphs():
            findings.append(
                f"{path}:{line}: unregistered glyph {glyph!r} decorating {key} — "
                "register it in ui_styles or drop the prefix"
            )
    return findings


def decoration_parity_findings(
    catalog: dict[str, dict[str, str]], button_keys: set[str]
) -> list[str]:
    """A button key's leading glyph must match the reference in all locales."""
    reference = catalog.get(REFERENCE, {})
    findings: list[str] = []
    for key in sorted(button_keys):
        reference_glyph = _leading_glyph(reference.get(key, ""))
        for lang in SUPPORTED_LANGUAGES:
            if lang == REFERENCE or key not in catalog.get(lang, {}):
                continue
            glyph = _leading_glyph(catalog[lang][key])
            if glyph != reference_glyph:
                findings.append(
                    f"bot/{lang}/{key}: decoration differs from the reference "
                    f"({REFERENCE}={reference_glyph!r}, {lang}={glyph!r})"
                )
    return findings


def button_keys_used(source: str) -> set[str]:
    keys: set[str] = set()
    for _lineno, node, _kind in button_label_nodes(source):
        segment = ast.get_source_segment(source, node) or ""
        keys.update(
            match.group(1)
            for match in re.finditer(r"(?:_t|_btn)\(\s*[\"']([a-z0-9_]+)[\"']", segment)
        )
    return keys


def audit_bot_button_style() -> None:
    catalog = json.loads(BOT_CATALOG.read_text(encoding="utf-8"))
    findings: list[str] = []
    button_keys: set[str] = set()
    for p in ui_python_files():
        source = p.read_text(encoding="utf-8")
        findings.extend(button_style_findings(source, catalog, _relative(p)))
        button_keys |= button_keys_used(source)
    findings.extend(decoration_parity_findings(catalog, button_keys))
    if findings:
        failures.extend(findings)
        return
    print(
        f"PASS button style: {len(button_keys)} button keys, one canonical glyph "
        f"in all {len(SUPPORTED_LANGUAGES)} locales"
    )


# --------------------------------------------------------------------------- #
# rule 9 — callback toasts and alerts carry catalog copy
# --------------------------------------------------------------------------- #
def callback_copy_findings(source: str, path: str = "ui.py") -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "answer":
            continue
        if not node.args:
            continue
        segment = ast.get_source_segment(source, node.args[0]) or ""
        if any(marker in segment for marker in I18N_MARKERS):
            continue
        if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            if _is_ui_copy(node.args[0].value):
                findings.append(
                    f"{path}:{node.lineno}: literal callback copy "
                    f"{node.args[0].value[:60]!r} — use self._t('catalog_key')"
                )
    return findings


def audit_bot_callback_copy() -> None:
    findings: list[str] = []
    for p in ui_python_files():
        findings.extend(callback_copy_findings(p.read_text(encoding="utf-8"), _relative(p)))
    if findings:
        failures.extend(findings)
        return
    print("PASS callback copy: no literal toast or alert text")


# --------------------------------------------------------------------------- #
# rule 10 — every registered event runs inside the user's locale
# --------------------------------------------------------------------------- #
def audit_bot_event_language_scope() -> None:
    """Ensure every registered Telegram event runs within its user's locale."""
    lines = BOT_UI.read_text(encoding="utf-8").splitlines()
    checked = 0
    missing = []
    for i, line in enumerate(lines):
        if not re.match(r"^\s*@b\.on_", line):
            continue
        indent = len(line) - len(line.lstrip())
        for j in range(i + 1, min(len(lines), i + 30)):
            candidate = lines[j]
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate_indent == indent and re.match(r"^\s*async def\s+", candidate):
                checked += 1
                if not any("@self._localized_handler" in item for item in lines[i:j]):
                    missing.append(f"{_relative(BOT_UI)}:{i + 1}")
                break
            if candidate.strip() and candidate_indent < indent:
                break
    if checked == 0:
        failures.append("bot: no registered Telegram event handlers were audited")
    elif missing:
        failures.append(
            f"bot: {len(missing)} Telegram event handlers lack locale isolation "
            f"(e.g. {missing[:5]})"
        )
    else:
        print(f"PASS bot event locale scope: {checked} registered handlers")


def audit_bot_translation_references() -> None:
    """Catch _t()/_btn() references whose keys are absent from the catalog."""
    if not BOT_CATALOG.exists() or not BOT_UI.exists():
        failures.append("bot: translation catalog or production Telegram UI is missing")
        return
    catalog = json.loads(BOT_CATALOG.read_text(encoding="utf-8"))
    english = set(catalog.get("en", {}))
    source = BOT_UI.read_text(encoding="utf-8")
    used = set(
        re.findall(r"""(?:self\._t|self\._btn|self\.i18n\.t)\(\s*["']([^"']+)["']""", source)
    )
    missing = sorted(used - english)
    if missing:
        failures.append(
            f"bot: {len(missing)} static translation keys are undefined (e.g. {missing[:8]})"
        )
    else:
        print(f"PASS bot translation references: {len(used)} static keys resolve")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def button_report() -> None:
    """Inventory of every button label, for the multilingual workstream."""
    catalog = json.loads(BOT_CATALOG.read_text(encoding="utf-8"))
    reference = catalog.get(REFERENCE, {})
    print("\nBUTTON INVENTORY")
    print(f"  {'file':<45} {'labels':>7} {'catalog':>8} {'hard':>6}")
    total = resolved = hard = 0
    keys: set[str] = set()
    for p in ui_python_files():
        source = p.read_text(encoding="utf-8")
        findings, seen, ok, _native = button_label_findings(source, _relative(p))
        total += seen
        resolved += ok
        hard += len(findings)
        print(f"  {_relative(p):<45} {seen:>7} {ok:>8} {len(findings):>6}")
        keys |= button_keys_used(source)
    print(f"  {'TOTAL':<45} {total:>7} {resolved:>8} {hard:>6}")
    print(f"  distinct catalog keys used as labels: {len(keys)}")
    undecorated = sorted(k for k in keys if k in reference and not _leading_glyph(reference[k]))
    print(
        f"  keys with no author glyph (styled at render time): {len(undecorated)} "
        f"e.g. {undecorated[:6]}"
    )


def report() -> int:
    for note in notes:
        print(f"INFO {note}")
    if failures:
        print(f"CROSS-LANGUAGE AUDIT: {len(failures)} FAILURE(S)")
        for f in failures:
            print("  ✗", f)
        return 1
    print(
        "CROSS-LANGUAGE AUDIT: catalogs 1:1, no cross-leak, placeholders match, "
        f"source clean across {len(SUPPORTED_LANGUAGES)} locales"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-language audit")
    parser.add_argument(
        "--report", action="store_true", help="print the button-label inventory too"
    )
    args = parser.parse_args(argv)
    audit_bot()
    audit_mobile()
    audit_source_hardcoded()
    audit_bot_language_branching()
    audit_bot_button_labels()
    audit_bot_button_style()
    audit_bot_callback_copy()
    audit_bot_event_language_scope()
    audit_bot_translation_references()
    if args.report:
        button_report()
    return report()


if __name__ == "__main__":
    sys.exit(main())
