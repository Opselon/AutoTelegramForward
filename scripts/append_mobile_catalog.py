#!/usr/bin/env python3
"""Append a batch of keys to the Flutter locale catalogs with parity validation.

The bot catalog has `scripts/append_catalog.py`; this is its Dart counterpart.
Both clients must carry the same languages, so a key added to one client must be
added to *every* locale of that client in the same change.

Usage:
    python scripts/append_mobile_catalog.py scripts/catalog_batch_system_keys.json

Input shape (one entry per key, every locale present):

    {
      "sys_title": {
        "en": "System", "fa": "سیستم", "pt": "Sistema", "...": "..."
      }
    }

Guarantees, enforced before anything is written:
  * the locale set of the batch matches the locale set of the Dart catalogs;
  * no value is empty or whitespace;
  * no RTL character in an English value;
  * `{placeholder}` sets match English exactly in every locale;
  * keys already present are refused (this only appends new keys).

Exit codes: 0 written, 1 validation error, 2 usage error.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
L10N = ROOT / "mobile/lib/l10n"
REFERENCE = L10N / "app_messages.dart"  # holds the English map

RTL = re.compile(r"[\u0590-\u05ff\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
PLACEHOLDER = re.compile(r"\{([^}]+)\}")

# file suffix -> locale code, for the per-locale catalogs
LOCALE_FILES = {
    "fa": "app_fa.dart",
    "pt": "app_pt.dart",
    "uz": "app_uz.dart",
    "ru": "app_ru.dart",
    "id": "app_id.dart",
    "es": "app_es.dart",
    "fr": "app_fr.dart",
    "it": "app_it.dart",
    "zh": "app_zh.dart",
}
_ENTRY = re.compile(r"^(\s*)'((?:[^'\\]|\\.)*)':(\s*)'((?:[^'\\]|\\.)*)',?\s*$")


def _escape(value: str) -> str:
    """Escape a value for a single-quoted Dart literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("$", "\\$")


def map_keys(text: str) -> set[str]:
    """Keys declared in the `_map` block of a catalog file."""
    keys = set()
    for line in text.splitlines():
        m = _ENTRY.match(line)
        if m:
            keys.add(m.group(2))
    return keys


def append_to(path: Path, entries: dict[str, str], banner: str) -> int:
    """Insert `entries` at the end of the file's `_map` block."""
    text = path.read_text(encoding="utf-8")
    closing = text.rfind("\n  };")
    if closing == -1:
        print(f"  ✗ {path.name}: no map terminator found", file=sys.stderr)
        return 1
    lines = [f"    // ---- {banner} ----"]
    for key, value in entries.items():
        lines.append(f"    '{key}': '{_escape(value)}',")
    head, tail = text[:closing], text[closing:]
    head = head.rstrip("\n")
    path.write_text(f"{head}\n" + "\n".join(lines) + tail, encoding="utf-8")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    batch = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    if not batch:
        print("empty batch")
        return 2

    catalogs = {"en": REFERENCE, **{loc: L10N / name for loc, name in LOCALE_FILES.items()}}
    expected_locales = set(catalogs)
    errors: list[str] = []

    for key, translations in batch.items():
        rtl_ok = bool(translations.get("_rtl_ok"))
        translations = {k: v for k, v in translations.items() if not k.startswith("_")}
        extra = set(translations) - expected_locales
        missing = expected_locales - set(translations)
        if extra:
            errors.append(f"{key}: unknown locale(s) {sorted(extra)}")
        if missing:
            errors.append(f"{key}: missing locale(s) {sorted(missing)}")
        for loc, value in translations.items():
            if not str(value).strip():
                errors.append(f"{key}: empty in {loc}")
        en_value = str(translations.get("en", ""))
        # A language's own name is the one legitimate RTL value in the English
        # catalog — showing "فارسی" to an English speaker is the correct label.
        # Such keys opt in explicitly via "_rtl_ok": true rather than the rule
        # being relaxed for everything.
        if RTL.search(en_value) and not rtl_ok:
            errors.append(f"{key}: RTL characters in the English value")
        en_ph = set(PLACEHOLDER.findall(en_value))
        for loc, value in translations.items():
            if set(PLACEHOLDER.findall(str(value))) != en_ph:
                errors.append(
                    f"{key}: placeholder mismatch en={sorted(en_ph)} {loc}="
                    f"{sorted(PLACEHOLDER.findall(str(value)))}"
                )

    # Refuse to shadow an existing key: an accidental duplicate means the batch
    # was already applied, and silently appending would leave two definitions.
    for loc, path in catalogs.items():
        existing = map_keys(path.read_text(encoding="utf-8"))
        for key in batch:
            if key in existing:
                errors.append(f"{key}: already present in {path.name}")

    if errors:
        for err in errors:
            print(f"  ✗ {err}")
        print(f"FAILED: {len(errors)} validation error(s) — nothing written")
        return 1

    written = 0
    for loc, path in catalogs.items():
        entries = {key: str(batch[key][loc]) for key in batch}
        if append_to(path, entries, "system surface (appended)") == 0:
            written += 1
    print(f"PASS: appended {len(batch)} key(s) to {written} catalog(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
