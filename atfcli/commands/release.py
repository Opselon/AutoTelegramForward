"""Release engineering and manifest validation."""

from __future__ import annotations

from pathlib import Path

from ..errors import UsageError
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
