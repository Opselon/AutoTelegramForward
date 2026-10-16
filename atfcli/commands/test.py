"""Test runner for Python, Go, and Flutter suites."""

from __future__ import annotations

from typing import Sequence

from . import _shared as sh

NAME = "test"
HELP = "Run automated test suites across Python, Go, and Flutter"


def _run_tests(ctx, args) -> int:
    suite = args.suite or "all"
    failed = False

    if suite in ("all", "py", "python"):
        ctx.out.step("Running Python test suite...")
        # Only hand pytest the directories that exist: the Python logger engine
        # was removed with the Rust cutover (June 2026 refactor to
        # core/observability). Only core/tests and scripts/tests remain.
        dirs = [d for d in ("core/tests", "scripts/tests") if ctx.repo.exists(d)]
        if dirs:
            res = ctx.repo.run_streaming([ctx.repo.python(), "-m", "pytest", *dirs], check=False)
            if res != 0:
                failed = True
        else:
            ctx.out.warn("no Python test directories found — nothing to run")

    if suite in ("all", "go"):
        ctx.out.step("Running Go test suite...")
        res = ctx.repo.run_streaming(["go", "test", "./..."], cwd=ctx.repo.path("api"), check=False)
        if res != 0:
            failed = True

    if suite in ("all", "flutter"):
        if ctx.repo.exists("mobile"):
            ctx.out.step("Running Flutter test suite...")
            res = ctx.repo.run_streaming(["flutter", "test"], cwd=ctx.repo.path("mobile"), check=False)
            if res != 0:
                failed = True

    if failed:
        ctx.out.fail("Test suite failed")
        return 1
    ctx.out.ok("All requested test suites passed")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=_run_tests)
    parser.add_argument("suite", nargs="?", choices=["all", "python", "py", "go", "flutter"], default="all")
