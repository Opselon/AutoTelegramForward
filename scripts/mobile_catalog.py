#!/usr/bin/env python3
"""Reading and writing the Flutter locale catalogs, safely.

The catalogs are Dart source, not JSON, and `dart format` wraps long entries:

    'incompatible_server':
        'This server is running an older release. Update it to {version} or newer.',

A line-oriented regex therefore sees a key with no value and drops the entry on
rewrite - silent, catastrophic data loss in a translation file. This module
parses the map block as text: every `'key': 'value'` pair, whatever whitespace
sits between the two literals, with escapes honoured on both directions.

Everything that edits a catalog should go through here so the parse can never
disagree with the write.
"""

from __future__ import annotations

import re
from pathlib import Path

L10N = Path(__file__).resolve().parent.parent / "mobile/lib/l10n"

# locale code -> catalog file (English lives in app_messages.dart)
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
REFERENCE_NAME = "app_messages.dart"

MAP_OPEN = "  static const Map<String, String> _map = {"
MAP_CLOSE = "\n  };"

# A pair of Dart string literals separated by a colon, across any whitespace
# (including the newline `dart format` inserts before a wrapped value).
PAIR = re.compile(r"'((?:[^'\\]|\\.)*)'\s*:\s*'((?:[^'\\]|\\.)*)'")

# Language endonyms are RTL in the English catalog by design.
RTL_OK_PREFIX = "lang_native_"
RTL = re.compile(r"[\u0590-\u05ff\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
PLACEHOLDER = re.compile(r"\{([^}]+)\}")


def catalog_paths() -> dict[str, Path]:
    return {"en": L10N / REFERENCE_NAME, **{loc: L10N / name for loc, name in LOCALE_FILES.items()}}


def _unescape(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("$", "\\$")


def map_body(text: str) -> tuple[str, str, str]:
    """Split a catalog file into (head, body, tail) around the `_map` block."""
    start = text.find(MAP_OPEN)
    if start == -1:
        raise SystemExit("catalog has no _map block")
    end = text.find(MAP_CLOSE, start)
    if end == -1:
        raise SystemExit("catalog _map block is not terminated")
    return text[: start + len(MAP_OPEN)], text[start + len(MAP_OPEN) : end], text[end:]


def parse_values(text: str) -> dict[str, str]:
    """Every key -> unescaped value in a catalog file."""
    _head, body, _tail = map_body(text)
    return {key: _unescape(value) for key, value in PAIR.findall(body)}


def read_values(path: Path) -> dict[str, str]:
    return parse_values(path.read_text(encoding="utf-8"))


def write_values(path: Path, values: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    head, body, tail = map_body(text)
    before = set(parse_values(text))
    lost = before - set(values)
    if lost:
        # The whole point of this module: never let a rewrite drop a key.
        raise SystemExit(f"{path.name}: refusing to write, would lose {sorted(lost)}")

    lines = "\n".join(f"    '{key}': '{escape(values[key])}'," for key in sorted(values))
    path.write_text(f"{head}\n{lines}{tail}", encoding="utf-8")


def validate(values_by_locale: dict[str, dict[str, str]]) -> list[str]:
    """Cross-catalog checks: parity, emptiness, placeholders, RTL in English."""
    errors: list[str] = []
    reference = values_by_locale["en"]
    for loc, values in values_by_locale.items():
        missing = set(reference) - set(values)
        extra = set(values) - set(reference)
        if missing:
            errors.append(f"{loc}: missing {len(missing)} key(s): {sorted(missing)[:8]}")
        if extra:
            errors.append(f"{loc}: extra {len(extra)} key(s): {sorted(extra)[:8]}")
    for key, en_value in reference.items():
        if not en_value.strip():
            errors.append(f"{key}: empty English value")
        if RTL.search(en_value) and not key.startswith(RTL_OK_PREFIX):
            errors.append(f"{key}: RTL characters in the English value")
        for loc, values in values_by_locale.items():
            value = values.get(key, "")
            if not value.strip():
                errors.append(f"{key}: empty in {loc}")
            if set(PLACEHOLDER.findall(value)) != set(PLACEHOLDER.findall(en_value)):
                errors.append(
                    f"{key}: placeholder mismatch in {loc} "
                    f"(en={sorted(PLACEHOLDER.findall(en_value))} "
                    f"{loc}={sorted(PLACEHOLDER.findall(value))})"
                )
    return errors
