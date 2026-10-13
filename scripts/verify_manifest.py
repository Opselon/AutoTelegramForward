#!/usr/bin/env python3
"""Verify release artifacts against release/manifest.json.

Fails (exit != 0) when:
  * any REQUIRED artifact listed in the manifest is missing from the
    artifacts directory, or
  * any present artifact's sha256 does not match the recorded value.

Optional artifacts that are absent are reported as SKIP, not failure.
Artifacts present on disk but absent from the manifest are reported as
UNTRACKED (warning only, exit code unaffected).

Usage:
    python3 scripts/verify_manifest.py --help
    python3 scripts/verify_manifest.py --manifest release/manifest.json \\
        --artifacts-dir release
    python3 scripts/verify_manifest.py --manifest release/manifest.json \\
        --artifacts-dir all-assets --sample 3   # post-publish spot check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_manifest_entries(manifest: dict) -> list[dict]:
    entries: list[dict] = []
    for e in manifest.get("binaries", []) + manifest.get("api_binaries", []):
        entries.append(e)
    for e in manifest.get("android", {}).get("artifacts", []):
        entries.append(e)
    portable = manifest.get("portable")
    if isinstance(portable, dict):
        entries.append(portable)
    ios = manifest.get("ios", {})
    for key in ("simulator", "ipa"):
        node = ios.get(key, {})
        if isinstance(node, dict) and node.get("artifact"):
            entries.append(
                {
                    "name": node["artifact"],
                    "sha256": node.get("sha256", ""),
                    "required": node.get("required", False),
                    "available": node.get("available", True),
                }
            )
    return entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify release artifacts against manifest.json")
    ap.add_argument("--manifest", default="release/manifest.json")
    ap.add_argument("--artifacts-dir", default="release",
                    help="directory holding the artifact files to check")
    ap.add_argument("--sample", type=int, default=0,
                    help="verify only N randomly chosen artifacts (post-publish spot check; 0 = all)")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for --sample")
    ap.add_argument("--list-sample", type=int, default=0, metavar="N",
                    help="print the N sampled artifact names (one per line) and exit; "
                         "lets CI download exactly the entries it will verify")
    ap.add_argument("--skip-missing-optional", action="store_true", default=True,
                    help="(default: true) absent optional artifacts are SKIP, not failure")
    args = ap.parse_args(argv)

    manifest_path = Path(args.manifest)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot read manifest {manifest_path}: {exc}")
        return 2

    artifacts_dir = Path(args.artifacts_dir)
    on_disk = {}
    if artifacts_dir.is_dir():
        for p in artifacts_dir.rglob("*"):
            if p.is_file() and p.name != "manifest.json":
                on_disk.setdefault(p.name, p)

    entries = iter_manifest_entries(manifest)
    # Only available entries with a recorded sha256 can be meaningfully
    # sampled: unavailable/missing entries are already covered by the full
    # manifest self-verify job, and sampling them here would fail for the
    # wrong reason (not downloaded, not corrupt).
    verifiable = [e for e in entries
                  if e.get("available", True) and e.get("sha256")]
    if args.list_sample > 0:
        rng = random.Random(args.seed)
        for e in rng.sample(verifiable, min(args.list_sample, len(verifiable))):
            print(e.get("name", "?"))
        return 0
    if args.sample > 0:
        rng = random.Random(args.seed)
        entries = rng.sample(verifiable, min(args.sample, len(verifiable)))
        print(f"spot-checking {len(entries)} of {len(verifiable)} verifiable manifest entries")

    failures: list[str] = []
    skips: list[str] = []
    checked = 0
    for e in entries:
        name = e.get("name", "?")
        required = bool(e.get("required", False))
        want = e.get("sha256", "")
        if not e.get("available", True):
            (failures if required else skips).append(f"{'[REQUIRED] ' if required else ''}{name}: listed unavailable")
            continue
        target = on_disk.get(name)
        if target is None:
            msg = f"{name}: file missing in {artifacts_dir}"
            (failures if required else skips).append(
                ("[REQUIRED] " if required else "") + msg
            )
            continue
        if not want:
            skips.append(f"{name}: no recorded sha256, presence only")
            continue
        got = sha256_of(target)
        if got != want:
            failures.append(f"[MISMATCH] {name}: expected {want}, got {got}")
        else:
            checked += 1
            print(f"OK  {name}  sha256={got[:16]}...")

    manifest_names = {e.get("name") for e in iter_manifest_entries(manifest)}
    for name in sorted(set(on_disk) - manifest_names):
        if name in ("SHA256SUMS.txt", "SHA256SUMS-mobile.txt", "NOTES.md"):
            continue
        print(f"UNTRACKED (warning only): {name}")

    for s in skips:
        print(f"SKIP  {s}")
    for f in failures:
        print(f"FAIL  {f}")

    print(f"verified={checked} skipped={len(skips)} failed={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
