#!/usr/bin/env python3
"""Append a batch of catalog keys to translations.json with parity validation."""
import json
import re
import sys
from pathlib import Path

CAT_PATH = Path(__file__).resolve().parent.parent / "core/infrastructure/telegram/locales/translations.json"
RTL = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")


def validate_and_append(new_keys: dict) -> int:
    cat = json.load(CAT_PATH.open(encoding="utf-8"))
    locales = list(cat.keys())

    errors = []
    for key, trans in new_keys.items():
        trans = {k: v for k, v in trans.items() if not k.startswith("_")}
        for loc in locales:
            if loc not in trans:
                errors.append(f"{key}: missing locale {loc}")
            elif not str(trans[loc]).strip():
                errors.append(f"{key}: empty in {loc}")
        if "en" in trans and RTL.search(trans["en"]):
            errors.append(f"{key}: RTL chars in English value")
        en_ph = set(re.findall(r"\{([^}]+)\}", trans.get("en", "")))
        for loc in locales:
            ph = set(re.findall(r"\{([^}]+)\}", trans.get(loc, "")))
            if ph != en_ph:
                errors.append(f"{key}: placeholder mismatch en={sorted(en_ph)} {loc}={sorted(ph)}")

    if errors:
        for e in errors:
            print("  ✗", e)
        print(f"FAILED: {len(errors)} validation error(s)")
        return 1

    for key, trans in new_keys.items():
        trans = {k: v for k, v in trans.items() if not k.startswith("_")}
        for loc in locales:
            cat[loc][key] = trans[loc]

    for loc in locales:
        cat[loc] = dict(sorted(cat[loc].items()))

    # The catalog on disk is the review surface: a batch has to be a pure
    # addition. Detect the file's own formatting (indent width, trailing
    # newline) instead of imposing one, or every append rewrites 13k lines
    # and hides the fifteen that changed.
    text = CAT_PATH.read_text(encoding="utf-8")
    lines = text.split("\n")
    indent = 4
    if len(lines) > 1:
        width = len(lines[1]) - len(lines[1].lstrip(" "))
        if width > 0:
            indent = width
    trailing = "\n" if text.endswith("\n") else ""
    CAT_PATH.write_text(
        json.dumps(cat, ensure_ascii=False, indent=indent) + trailing,
        encoding="utf-8",
    )
    print(f"PASS: appended {len(new_keys)} key(s) across {len(locales)} locales (indent={indent})")
    return 0


if __name__ == "__main__":
    sys.exit(validate_and_append(json.load(Path(sys.argv[1]).open())))  # noqa: S102
