#!/usr/bin/env python3
"""Generate the dashboard's setting labels for all ten locales from the bot catalog.

The per-task settings schema is language-neutral on purpose: the Python core
serves `{key, kind, default, group, label, help, ...}` and the Go API and the
REST clients pass it through verbatim. That keeps one registry, but it also
means the dashboard's settings panel showed `Block text` / `Header text` to a
Persian user while the bot, reading the same registry, showed `متن هدر`.

The translations already exist — the bot's registry panel needs them — so this
tool copies them into a typed module instead of hand-writing a second list that
would drift the first time somebody adds a setting:

  * `SETTING_LABELS`  <- `ui_reg_set_<key>`   (every key in SETTING_SPECS)
  * `SETTING_HELP`    <- `ui_reg_help_<key>`  (only the settings that have help)
  * `SETTING_GROUPS`  <- `ui_reg_group_<group>`

A missing label is an error, not a fallback: the dashboard would silently show
English, and the whole point of this file is that it does not. Every locale the
product ships is required — a panel row that reads in English for a Chinese
user is the same defect as one that reads in English for a Persian user.

Usage:
    python3 scripts/gen_web_setting_labels.py            # write the module
    python3 scripts/gen_web_setting_labels.py --check    # exit 1 if it drifted

`core/tests/test_web_setting_labels_generated.py` runs the check in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "core" / "infrastructure" / "telegram" / "locales" / "translations.json"
OUT = ROOT / "web" / "frontend" / "src" / "settingLabels.generated.ts"

sys.path.insert(0, str(ROOT))

from core.domain.rule_settings import SETTING_SPECS  # noqa: E402

# The locales the dashboard ships, in the order they appear in every row.
# `fa` stays first because the generated-module test reads the row shape, and
# `en` stays second because it is the reference locale.
LANGS = ["fa", "en", "pt", "uz", "ru", "id", "es", "fr", "it", "zh"]


def _load() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _entries(cat: dict, prefix: str, names) -> list[tuple[str, dict[str, str]]]:
    """(name, {lang: text}) for every name that has a catalog pair in every locale."""
    missing: dict[str, list[str]] = {}
    for lang in LANGS:
        table = cat.get(lang, {})
        absent = [n for n in names if f"{prefix}{n}" not in table]
        if absent:
            missing[lang] = absent
    if missing:
        detail = "; ".join(f"{lang}: {', '.join(sorted(ns))}" for lang, ns in missing.items())
        raise SystemExit(
            f"{prefix}* is missing for: {detail}\n"
            "Add the key to every locale (the bot's registry panel needs it too)."
        )
    return [(n, {lang: cat[lang][f"{prefix}{n}"] for lang in LANGS}) for n in names]


def _ts_literal(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def build() -> str:
    cat = _load()
    keys = sorted(SETTING_SPECS)
    groups = sorted({spec.group for spec in SETTING_SPECS.values()})
    # Only the settings that actually carry help; an empty help stays absent so
    # the panel keeps rendering nothing instead of an empty line.
    help_keys = sorted(k for k, spec in SETTING_SPECS.items() if spec.help)

    labels = _entries(cat, "ui_reg_set_", keys)
    helps = _entries(cat, "ui_reg_help_", help_keys)
    group_rows = _entries(cat, "ui_reg_group_", groups)

    def block(rows, indent="  ") -> str:
        return "\n".join(
            f"{indent}{_ts_literal(name)}: {{ "
            + ", ".join(f"{lang}: {_ts_literal(texts[lang])}" for lang in LANGS)
            + " },"
            for name, texts in rows
        )

    lang_fields = "\n".join(f"  {lang}: string" for lang in LANGS)

    return (
        "/**\n"
        " * GENERATED FILE - do not edit by hand.\n"
        " * Regenerate: python3 scripts/gen_web_setting_labels.py\n"
        " *\n"
        " * Source: core/domain/rule_settings.py (the key set) and\n"
        " * core/infrastructure/telegram/locales/translations.json (the strings).\n"
        " * The bot and the dashboard read the same registry, so they read the same\n"
        " * translations too; a setting with no label in every locale fails the\n"
        " * generator.\n"
        " */\n\n"
        "export interface LocalizedText {\n"
        f"{lang_fields}\n"
        "}\n\n"
        "/** `ui_reg_set_<key>` - the settings panel's row label. */\n"
        "export const SETTING_LABELS: Record<string, LocalizedText> = {\n"
        f"{block(labels)}\n"
        "}\n\n"
        "/** `ui_reg_help_<key>` - the description under the row, when there is one. */\n"
        "export const SETTING_HELP: Record<string, LocalizedText> = {\n"
        f"{block(helps)}\n"
        "}\n\n"
        "/** `ui_reg_group_<group>` - the section headings. */\n"
        "export const SETTING_GROUPS: Record<string, LocalizedText> = {\n"
        f"{block(group_rows)}\n"
        "}\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] != "--check"):
        print(__doc__)
        return 2
    wanted = build()
    if len(argv) == 2:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != wanted:
            print(f"{OUT.relative_to(ROOT)} is stale - run: python3 scripts/gen_web_setting_labels.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is in sync")
        return 0
    OUT.write_text(wanted, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
