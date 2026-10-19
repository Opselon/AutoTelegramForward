"""Release engineering and manifest validation."""

from __future__ import annotations

import shutil
import tempfile
import zipapp
from pathlib import Path

from . import _shared as sh

NAME = "release"
HELP = "Manage release versions, checksum manifests, and integrity checks"


def _version(ctx, args) -> None:
    current = ctx.repo.version
    if args.bump:
        new_ver = args.bump
        ctx.repo.path("VERSION").write_text(new_ver.strip() + "\n", encoding="utf-8")
        ctx.out.ok(f"Bumped version {current} -> {new_ver}")
    else:
        ctx.out.line(current)


def _manifest(ctx, args) -> int:
    ctx.out.step("Generating release manifest...")
    res = ctx.repo.run_streaming([ctx.repo.python(), "scripts/release_manifest.py"], check=False)
    if res != 0:
        ctx.out.fail("Failed to generate manifest")
        return 1
    ctx.out.ok("Release manifest created")
    return 0


def _verify(ctx, args) -> int:
    ctx.out.step("Verifying release manifest...")
    res = ctx.repo.run_streaming([ctx.repo.python(), "scripts/verify_manifest.py"], check=False)
    if res != 0:
        ctx.out.fail("Manifest verification failed")
        return 1
    ctx.out.ok("Manifest verified")
    return 0


PORTABLE_DEFAULT = "dist/atf.pyz"
#: The whole CLI is stdlib-only, so one zipapp file runs anywhere a supported
#: python (3.11-3.13, see atfcli.platforms) is installed — no pip, no venv, no
#: checkout. That is the point of this artifact.
PORTABLE_ENTRY = """import sys

from atfcli.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
"""


def _portable(ctx, args) -> int:
    target = Path(args.output or PORTABLE_DEFAULT)
    if not target.is_absolute():
        target = ctx.repo.path(str(target))
    if ctx.repo.dry_run:
        ctx.out.info(f"[dry-run] would build the portable single-file CLI at {target}")
        return 0

    package = ctx.repo.path("atfcli")
    if not package.is_dir():
        ctx.out.fail(f"atfcli package not found at {package}")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="atf-portable-") as tmp:
        stage = Path(tmp)
        shutil.copytree(package, stage / "atfcli", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stage / "__main__.py").write_text(PORTABLE_ENTRY, encoding="utf-8")
        zipapp.create_archive(stage, target=str(target), interpreter="/usr/bin/env python3", compressed=True)
    target.chmod(0o755)
    size = human_size(target.stat().st_size)
    ctx.out.ok(f"built {target} ({size}) — single file, stdlib only")

    if args.verify:
        ran = ctx.repo.run([ctx.repo.python(), str(target), "--version"], check=False, quiet=True)
        if ran.ok:
            ctx.out.ok(f"verified: {ran.stdout.strip() or 'ok'} (python {ctx.repo.python()})")
        else:
            ctx.out.fail(f"the built file did not run: {ran.text.strip()[:200]}")
            return 1
    ctx.out.line()
    ctx.out.kv(
        [
            ("run it", f"python3 {target.name} --help"),
            ("wizard", f"python3 {target.name} setup"),
            ("hosts", "copy this file to any machine with python 3.11-3.13 (Linux, macOS, Windows)"),
            ("needs", "docker only for the default docker mode — source and portable modes need none"),
        ]
    )
    return 0


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} MB"


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("version", help="view or bump system version", parents=parents)
    p.add_argument("--bump", default="", help="new version string")
    p.set_defaults(func=_version)

    p = sub.add_parser("manifest", help="generate cryptographic release manifest", parents=parents)
    p.set_defaults(func=_manifest)

    p = sub.add_parser("verify", help="verify release manifest", parents=parents)
    p.set_defaults(func=_verify)

    p = sub.add_parser(
        "portable",
        help="build the single-file, no-install CLI (dist/atf.pyz)",
        parents=parents,
    )
    p.add_argument("--output", default="", help=f"where to write it (default {PORTABLE_DEFAULT})")
    p.add_argument("--no-verify", dest="verify", action="store_false", help="skip running the built file")
    p.set_defaults(func=_portable, verify=True)
