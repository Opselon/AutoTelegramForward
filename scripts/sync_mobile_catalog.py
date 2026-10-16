#!/usr/bin/env python3
"""Restore strict 1:1 parity across the Flutter locale catalogs.

Drift happens one screen at a time: a key gets added to the two or three
catalogs the author had in front of them, and the rest silently render the raw
key (`_map[key] ?? key`) or, worse, hit the debug assert in the reference
catalog. This tool makes English the single source of truth:

  * the target key set is English's key set plus every key any locale already
    defines (a key another locale uses is a key English is missing, not junk);
  * every (key, locale) cell must then be filled - from the catalog if it is
    already there, otherwise from the authored batch file;
  * empty cells are an error: nothing is silently back-filled with English,
    because an untranslated string is a defect, not a default;
  * a key can never be dropped: `mobile_catalog.write_values` refuses any write
    that would lose one (that guard exists because the first version of this
    tool silently deleted a wrapped entry).

Usage:
    python scripts/sync_mobile_catalog.py scripts/catalog_sync_mobile.json

Exit codes: 0 in sync, 1 gaps remain, 2 usage error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mobile_catalog import catalog_paths, read_values, validate, write_values  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__)
        return 2

    catalogs = catalog_paths()
    values_by_locale = {loc: read_values(path) for loc, path in catalogs.items()}

    batch: dict[str, dict[str, str]] = {}
    if len(argv) == 2:
        raw = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        batch = {
            key: {loc: val for loc, val in trans.items() if not loc.startswith("_")}
            for key, trans in raw.items()
        }

    target_keys = set(values_by_locale["en"])
    for values in values_by_locale.values():
        target_keys |= set(values)

    errors: list[str] = []
    merged: dict[str, dict[str, str]] = {}
    for loc, values in values_by_locale.items():
        filled = dict(values)
        for key in target_keys:
            if not filled.get(key):
                supplied = batch.get(key, {}).get(loc)
                if supplied:
                    filled[key] = supplied
                else:
                    errors.append(f"no {loc} translation for '{key}'")
        merged[loc] = {key: filled.get(key, "") for key in target_keys}

    errors.extend(validate(merged))
    if errors:
        for err in errors:
            print(f"  ✗ {err}")
        print(f"GAPS: {len(errors)} - nothing written")
        return 1

    for loc, path in catalogs.items():
        write_values(path, merged[loc])
    print(
        f"IN SYNC: {len(target_keys)} keys x {len(catalogs)} catalogs "
        f"({len(target_keys) * len(catalogs)} strings)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
