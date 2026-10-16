#!/usr/bin/env python3
"""Rebuild every Flutter catalog from a clean base plus the authored batches.

Why this exists: an earlier incremental sync read the catalog source text and
re-escaped it, so every value containing an apostrophe (`O'chirish`,
`Nom d'utilisateur`) came back double-escaped as `O\\'chirish`. Incremental
patching cannot be trusted to undo escaping damage, so this rebuilds from the
last known-good revision:

    base      = every value as it stands in the given git revision (HEAD)
    overrides = the authored batches, which are the source of truth for any key
                they mention

It then validates parity/placeholders/emptiness/RTL and only writes when the
result is clean. Run it once to repair, and keep it around: it is also how the
catalogs are regenerated when a batch is edited after the fact.

Usage:
    python scripts/rebuild_mobile_catalogs.py --revision HEAD \
        scripts/catalog_batch_system_keys.json \
        scripts/catalog_batch_system_keys_v2.json \
        scripts/catalog_sync_mobile.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mobile_catalog import catalog_paths, escape, parse_values, validate, write_values  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def revision_values(revision: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for loc, path in catalog_paths().items():
        rel = path.relative_to(ROOT)
        source = subprocess.run(
            ["git", "show", f"{revision}:{rel}"],
            capture_output=True, text=True, cwd=ROOT, check=True,
        ).stdout
        out[loc] = parse_values(source)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revision", default="HEAD")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("batches", nargs="+")
    args = ap.parse_args()

    base = revision_values(args.revision)
    locales = set(base)
    print(f"base: {args.revision} -> " + ", ".join(f"{loc}:{len(v)}" for loc, v in sorted(base.items())))

    target_keys = set(base["en"])
    for values in base.values():
        target_keys |= set(values)

    overrides: dict[str, dict[str, str]] = {loc: {} for loc in locales}
    for batch_path in args.batches:
        raw = json.loads(Path(batch_path).read_text(encoding="utf-8"))
        for key, trans in raw.items():
            for loc, value in trans.items():
                if loc.startswith("_"):
                    continue
                if loc not in locales:
                    raise SystemExit(f"{batch_path}: unknown locale {loc!r} for {key!r}")
                overrides[loc][key] = value
                target_keys.add(key)

    merged: dict[str, dict[str, str]] = {}
    for loc in locales:
        values = dict(base[loc])
        values.update(overrides[loc])
        merged[loc] = {key: values.get(key, "") for key in target_keys}

    errors = validate(merged)
    # Escaping sanity: a literal backslash-apostrophe in a parsed value means
    # somebody escaped twice; it would render as `O\'chirish` on screen.
    for loc, values in merged.items():
        for key, value in values.items():
            if "\\'" in value or "\\\\" in value:
                errors.append(f"{loc}/{key}: double-escaped value {value!r}")

    if errors:
        for err in errors:
            print(f"  ✗ {err}")
        print(f"FAILED: {len(errors)} error(s) - nothing written")
        return 1

    print(f"clean: {len(target_keys)} keys x {len(locales)} catalogs ({len(target_keys) * len(locales)} strings)")
    if args.dry_run:
        print("dry run: not writing")
        return 0

    for loc, path in catalog_paths().items():
        write_values(path, merged[loc])
    print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
