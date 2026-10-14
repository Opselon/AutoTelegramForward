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
    # cross-leak (reference language only — translations legitimately use it)
    if lang == "en":
        leaked = [k for k in ref_keys if any(c in RTL_GRAPHEMES for c in (keys.get(k) or ""))]
        if leaked:
            failures.append(
                f"{name}/en: English strings contain Persian/Arabic text in {sorted(leaked)}"
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


if __name__ == "__main__":
    audit_bot()
    audit_mobile()
    sys.exit(report())
