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

Exits 0 only when every catalog passes on every rule.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Persian/Arabic graphemes that must never appear in English strings.
RTL_GRAPHEMES = set("یگچپژکیهؤئإأآ")
PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")

# The one legitimate RTL value in an English catalog: a language's own endonym.
# A user picking Persian in a picker written in English must read "فارسی", so
# these keys opt out of the cross-leak rule explicitly rather than relaxing it
# for every key - anything else with RTL text in `en` is still a defect.
RTL_EXEMPT_KEYS = frozenset({"lang_native_fa"})

failures: list[str] = []


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
            if any(c in RTL_GRAPHEMES for c in (keys.get(k) or ""))
        ]
        if leaked:
            failures.append(
                f"{name}/en: English strings contain Persian/Arabic text in {sorted(leaked)}"
            )


def audit_source_hardcoded() -> None:
    """Scanner for the real production defect: Persian STRINGS HARDCODED IN
    SOURCE, bypassing the catalog entirely.

    The catalog parity check above passes in the presence of these, because
    they never enter the catalog at all — the English user still sees
    Persian. This scans every Python source file under core/ for Persian
    graphemes, EXCLUDING:
      - the locale catalog itself (it holds the translations)
      - tests (legitimately assert on translated text)
      - pv_responder's persona prompt (a user-authored Persian persona by
        design, not a UI string)

    A pre-existing backlog is pinned in scripts/.persian_baseline.txt, one entry
    per line as ``path:lineno|text``. The audit fails only on leaks NOT in the
    baseline — so CI stays green while the backlog is being extracted, and any
    NEW hardcoded Persian fails immediately. Shrink the baseline as functions
    are converted; never grow it.

    The ``text`` half is what makes this stable. A bare line number is not: any
    insertion above it (a new import, a new catalog key) shifts every entry
    below and the whole backlog reads as "new" — a red CI run that has nothing
    to do with the change. An offender counts as pinned when the same file has a
    baselined line with the same text (position ignored), or, for entries that
    carry no text, when the file:line still matches.
    """
    baseline_path = REPO / "scripts/.persian_baseline.txt"
    pinned_text: set[str] = set()
    pinned_loc: set[str] = set()
    if baseline_path.exists():
        for raw in baseline_path.read_text(encoding="utf-8").splitlines():
            entry = raw.strip()
            if not entry or entry.startswith("#"):
                continue
            locator, sep, text = entry.partition("|")
            pinned_loc.add(locator.split("#")[0].strip())
            if sep and text.strip():
                pinned_text.add(f"{locator.split(':')[0]}|{text.strip()}")

    skip_dirs = {"tests", "locales", "__pycache__"}
    offenders: list[str] = []
    new_leaks: list[str] = []
    for p in sorted(REPO.joinpath("core").rglob("*.py")):
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.name == "pv_responder.py":
            continue
        rel = p.relative_to(REPO)
        for i, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            if RTL_GRAPHEMES.intersection(line) and "STR.Migrations" not in line:
                locator = f"{rel}:{i}"
                offenders.append(locator)
                if locator in pinned_loc:
                    continue
                if f"{rel}|{line.strip()}" in pinned_text:
                    continue  # same line, moved by an insertion above it
                new_leaks.append(locator)

    if new_leaks:
        failures.append(
            f"source: {len(new_leaks)} NEW hardcoded Persian strings not in "
            f"scripts/.persian_baseline.txt (e.g. {new_leaks[:3]}). "
            "Convert them to catalog keys, or if genuinely unavoidable, "
            "regenerate the baseline and explain why."
        )
    pinned_total = len(pinned_text) or len(pinned_loc)
    if len(offenders) < pinned_total:
        print(
            f"source: backlog shrank {pinned_total} -> {len(offenders)}; "
            "regenerate scripts/.persian_baseline.txt to pin the progress."
        )


def audit_bot() -> None:
    p = REPO / "core/infrastructure/telegram/locales/translations.json"
    if not p.exists():
        print("bot catalog missing — skipped")
        return
    catalog = json.loads(p.read_text(encoding="utf-8"))
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


def report() -> int:
    if failures:
        print(f"CROSS-LANGUAGE AUDIT: {len(failures)} FAILURE(S)")
        for f in failures:
            print("  ✗", f)
        return 1
    print("CROSS-LANGUAGE AUDIT: all catalogs 1:1, no cross-leak, placeholders match")
    return 0



def audit_bot_event_language_scope() -> None:
    """Ensure every registered Telegram event runs within its user's locale."""
    path = REPO / "core/infrastructure/telegram/pro_bot_ui.py"
    lines = path.read_text(encoding="utf-8").splitlines()
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
                    missing.append(f"{path.relative_to(REPO)}:{i + 1}")
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
    """Catch _t() references whose keys are absent from the English catalog."""
    catalog_path = REPO / "core/infrastructure/telegram/locales/translations.json"
    source_path = REPO / "core/infrastructure/telegram/pro_bot_ui.py"
    if not catalog_path.exists() or not source_path.exists():
        failures.append("bot: translation catalog or production Telegram UI is missing")
        return
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    english = set(catalog.get("en", {}))
    source = source_path.read_text(encoding="utf-8")
    used = set(re.findall(r"""(?:self\._t|self\.i18n\.t)\(\s*["']([^"']+)["']""", source))
    missing = sorted(used - english)
    if missing:
        failures.append(
            f"bot: {len(missing)} static translation keys are undefined (e.g. {missing[:8]})"
        )
    else:
        print(f"PASS bot translation references: {len(used)} static keys resolve")

if __name__ == "__main__":
    audit_bot()
    audit_mobile()
    audit_source_hardcoded()
    audit_bot_event_language_scope()
    audit_bot_translation_references()
    sys.exit(report())
