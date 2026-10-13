#!/usr/bin/env python3
"""Generate release/manifest.json for AutoTelegramForward releases.

Scans a directory of downloaded release artifacts, hashes every known file,
and emits a single manifest describing versions, deliverables, and which
artifacts are REQUIRED vs OPTIONAL (optional = best-effort legs such as
native-macOS builds that GitHub may cancel for runner capacity).

Usage:
    python3 scripts/release_manifest.py --help
    python3 scripts/release_manifest.py --dry-run
    python3 scripts/release_manifest.py --artifacts-dir all-assets \\
        --output release/manifest.json

The manifest schema:
    {
      "product_version": "1.5.0",          # from VERSION file
      "commit_sha": "abc123...",           # --commit-sha / $GITHUB_SHA / git
      "api_version": "1.0.0",              # api/internal/handlers/version.go
      "min_supported_client": "1.0.0",     # same source
      "generated_at": "2026-...Z",
      "server": {"images": [...], "platforms": ["linux/amd64", ...]},
      "binaries":   [{name, platform, sha256, size_bytes, required}],
      "api_binaries": [...],
      "android": {"artifacts": [{name, abi, sha256, size_bytes, required}]},
      "portable": {name, sha256, size_bytes, required} (available:false when missing),
      "ios": {"simulator": {"available": bool, ...},
              "ipa": {"available": bool, "signing": "..."}},
      "required_vs_optional": [{name, required, reason}],
      "build_status": {deliverable: "built" | "missing" | "not-run"}
    }

Generation NEVER fails on missing artifacts: gaps are recorded as
available:false / build_status:"missing" so the release notes stay honest.
Enforcement lives in scripts/verify_manifest.py, which exits non-zero when
any REQUIRED artifact is missing or any recorded sha256 mismatches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (filename regex, platform label, required?, reason)
BINARY_RULES = [
    (r"^atf-linux-x64$", "linux/x86_64", True, "primary linux deliverable"),
    (r"^atf-linux-arm64$", "linux/arm64", True, "primary linux deliverable"),
    (r"^atf-windows-x64\.exe$", "windows/x86_64", True, "primary windows deliverable"),
    (r"^atf-macos-x64$", "macos/x86_64", False, "native mac runner; may be cancelled for capacity"),
    (r"^atf-macos-arm64$", "macos/arm64", False, "native mac runner; may be cancelled for capacity"),
]

API_RULES = [
    (r"^atf-api-linux-x64$", "linux/x86_64", True, "primary linux deliverable"),
    (r"^atf-api-linux-arm64$", "linux/arm64", True, "primary linux deliverable"),
    (r"^atf-api-windows-x64\.exe$", "windows/x86_64", True, "primary windows deliverable"),
    (r"^atf-api-macos-x64$", "macos/x86_64", False, "cross-compiled darwin leg; optional"),
    (r"^atf-api-macos-arm64$", "macos/arm64", False, "cross-compiled darwin leg; optional"),
]

# (filename regex, abi label, required?)
ANDROID_RULES = [
    (r"android-arm64\.apk$", "arm64-v8a", True),
    (r"android-armv7\.apk$", "armeabi-v7a", True),
    (r"android-x86_64\.apk$", "x86_64", True),
    (r"android-bundle\.aab$", "universal (aab)", True),
]

SERVER_IMAGES = ["atf-core", "atf-logger", "atf-api", "atf-web"]
SERVER_PLATFORMS = ["linux/amd64", "linux/arm64"]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_version(version_file: Path) -> str:
    return version_file.read_text().strip()


def parse_go_constant(source: Path, name: str, default: str) -> str:
    try:
        text = source.read_text()
    except OSError:
        return default
    m = re.search(rf'{name}\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else default


def get_api_contract() -> tuple[str, str, list[str]]:
    version_go = REPO_ROOT / "api" / "internal" / "handlers" / "version.go"
    api_version = parse_go_constant(version_go, "APIVersion", "unknown")
    min_client = parse_go_constant(version_go, "MinSupportedClient", "unknown")
    features: list[str] = []
    try:
        text = version_go.read_text()
        m = re.search(r"VersionFeatures\s*=\s*\[\]string\{([^}]*)\}", text)
        if m:
            features = re.findall(r'"([^"]+)"', m.group(1))
    except OSError:
        pass
    return api_version, min_client, features


def resolve_commit_sha(explicit: str | None) -> str:
    if explicit:
        return explicit
    env_sha = os.environ.get("GITHUB_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def collect_files(artifacts_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not artifacts_dir.is_dir():
        return found
    for p in sorted(artifacts_dir.rglob("*")):
        if p.is_file() and p.name not in found:
            found[p.name] = p
    return found


def match_rules(found: dict[str, Path], rules: list) -> tuple[list[dict], dict[str, str]]:
    entries: list[dict] = []
    status: dict[str, str] = {}
    for pattern, label, *rest in rules:
        required = bool(rest[0]) if rest else True
        rx = re.compile(pattern)
        hit = next((n for n in found if rx.search(n)), None)
        if hit is None:
            key = pattern.strip("^$").replace("\\.", ".")
            status[key] = "missing"
            entries.append({"name": key, "required": required, "available": False})
            continue
        p = found[hit]
        entries.append(
            {
                "name": hit,
                "platform_or_abi": label,
                "sha256": sha256_of(p),
                "size_bytes": p.stat().st_size,
                "required": required,
                "available": True,
            }
        )
        status[hit] = "built"
    return entries, status


def build_manifest(
    *,
    product_version: str,
    commit_sha: str,
    artifacts_dir: Path | None,
    owner: str,
) -> dict:
    api_version, min_client, features = get_api_contract()
    found = collect_files(artifacts_dir) if artifacts_dir else {}

    binaries, bin_status = match_rules(found, BINARY_RULES)
    api_bins, api_status = match_rules(found, API_RULES)
    android, and_status = match_rules(found, ANDROID_RULES)
    for e in android:
        if "platform_or_abi" in e:
            e["abi"] = e.pop("platform_or_abi")

    portable = None
    portable_status = "missing"
    if found:
        tar = next((n for n in found if re.search(r"portable\.tar\.gz$", n)), None)
        if tar:
            p = found[tar]
            portable = {
                "name": tar,
                "sha256": sha256_of(p),
                "size_bytes": p.stat().st_size,
                "required": True,
                "available": True,
            }
            portable_status = "built"
    if portable is None:
        # A missing REQUIRED portable bundle must be an explicit unavailable
        # entry — never null — so verify_manifest.py fails the release on it
        # instead of silently skipping a deliverable iter_manifest_entries()
        # would never see.
        portable = {
            "name": f"autotelegramforward-{product_version}-portable.tar.gz",
            "required": True,
            "available": False,
        }

    sim_name = next(
        (n for n in found if "ios-simulator" in n.lower() and n.lower().endswith(".zip")),
        None,
    )
    ipa_name = next((n for n in found if n.lower().endswith(".ipa")), None)
    ios = {
        "simulator": {
            "available": sim_name is not None,
            "artifact": sim_name,
            "sha256": sha256_of(found[sim_name]) if sim_name else "",
            "size_bytes": found[sim_name].stat().st_size if sim_name else 0,
            "required": False,
            "signing": "none (built with --no-codesign)",
        },
        "ipa": {
            "available": ipa_name is not None,
            "artifact": ipa_name,
            "sha256": sha256_of(found[ipa_name]) if ipa_name else "",
            "size_bytes": found[ipa_name].stat().st_size if ipa_name else 0,
            "required": False,
            "signing": "Apple Developer certificate via CI secrets (see docs/mobile-release.md)"
            if ipa_name
            else "not built (signing secrets absent)",
        },
    }

    required_vs_optional: list[dict] = []
    for entry in binaries + api_bins + android:
        name = entry.get("name", "")
        reason = "required deliverable" if entry.get("required") else "best-effort leg"
        for pattern, _label, *rest in BINARY_RULES + API_RULES:
            if re.search(pattern, name):
                if len(rest) > 1 and rest[1]:
                    reason = str(rest[1])
                break
        required_vs_optional.append(
            {"name": name, "required": entry.get("required", False), "reason": reason}
        )
    # portable is never None: a missing bundle is an explicit
    # available:false REQUIRED entry (see above), so it always lands here.
    required_vs_optional.append(
        {"name": portable["name"], "required": True, "reason": "required deliverable"}
    )

    build_status: dict[str, str] = {}
    build_status.update(bin_status)
    build_status.update(api_status)
    build_status.update(and_status)
    build_status["portable"] = portable_status
    build_status["ios-simulator"] = "built" if ios["simulator"]["available"] else "missing"
    build_status["ios-ipa"] = "built" if (ipa_name is not None) else "not-run"

    normalized_owner = owner.lower()
    images = [
        f"ghcr.io/{normalized_owner}/{img}:{product_version}" for img in SERVER_IMAGES
    ] + [f"ghcr.io/{normalized_owner}/{img}:latest" for img in SERVER_IMAGES]

    return {
        "product_version": product_version,
        "commit_sha": commit_sha,
        "api_version": api_version,
        "min_supported_client": min_client,
        "features": features,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": {"images": images, "platforms": SERVER_PLATFORMS},
        "binaries": binaries,
        "api_binaries": api_bins,
        "android": {"artifacts": android},
        "portable": portable,
        "ios": ios,
        "required_vs_optional": required_vs_optional,
        "build_status": build_status,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate release/manifest.json")
    ap.add_argument("--artifacts-dir", default="all-assets",
                    help="directory holding downloaded release artifacts")
    ap.add_argument("--output", default="release/manifest.json",
                    help="where to write the manifest")
    ap.add_argument("--version-file", default="VERSION", help="file holding product version")
    ap.add_argument("--commit-sha", default=None, help="commit SHA (default: $GITHUB_SHA or git)")
    ap.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "OWNER"),
                    help="GHCR owner for server image names")
    ap.add_argument("--dry-run", action="store_true",
                    help="emit a manifest with no artifacts scanned (for local validation)")
    args = ap.parse_args(argv)

    version_file = Path(args.version_file)
    if not version_file.is_file():
        version_file = REPO_ROOT / args.version_file
    try:
        product_version = read_version(version_file)
    except OSError as exc:
        print(f"error: cannot read version file: {exc}", file=sys.stderr)
        return 2
    if not product_version:
        print("error: version file is empty", file=sys.stderr)
        return 2

    artifacts_dir = None if args.dry_run else Path(args.artifacts_dir)
    manifest = build_manifest(
        product_version=product_version,
        commit_sha=resolve_commit_sha(args.commit_sha),
        artifacts_dir=artifacts_dir,
        owner=args.owner,
    )

    out = Path(args.output)
    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    built = sum(1 for v in manifest["build_status"].values() if v == "built")
    total = len(manifest["build_status"])
    print(f"wrote {out} (product {product_version}, {built}/{total} deliverables present)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
