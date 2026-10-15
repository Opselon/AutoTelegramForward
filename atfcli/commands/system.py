"""System-wide management, upgrades, and platform diagnostics."""

from __future__ import annotations

import time

from ..context import AI_NETWORK, COMPOSE_NETWORK, CONTAINERS, NETWORK_ALIASES, SERVICES
from ..errors import AtfError
from . import _shared as sh
from .doctor import Report, check_aliases, check_api, check_containers, check_database

NAME = "system"
HELP = "Manage full system lifecycle, version upgrades, and host diagnostics"


def _info(ctx, args) -> None:
    facts = ctx.repo.system_facts()
    if args.json:
        ctx.out.json(facts)
        return
    ctx.out.title("Host & System Facts")
    ctx.out.kv([(k, v) for k, v in facts.items()])


def _upgrade(ctx, args) -> int:
    """Full-system automated upgrade procedure."""
    ctx.out.title("Executing Full ATF System Upgrade")
    sh.confirm_or_fail(ctx, "Proceed with full stack rebuild, container recreation, and upgrade?")

    # 1. Git pull if requested or in git repo
    if not args.no_pull:
        ctx.out.step("Step 1/5: Pulling latest code from upstream...")
        pull_res = ctx.repo.run(["git", "pull", "--ff-only"], check=False)
        if pull_res.ok:
            ctx.out.ok(f"Git updated: {pull_res.stdout.strip()}")
        else:
            ctx.out.warn(f"Git pull skipped or returned non-zero: {pull_res.stderr.strip() or pull_res.stdout.strip()}")
    else:
        ctx.out.info("Skipping git pull (--no-pull)")

    # 2. Build Docker images
    ctx.out.step("Step 2/5: Building all service container images...")
    ctx.repo.run_streaming(["docker", "compose", "build", *SERVICES])
    ctx.out.ok("Container images built")

    # 3. Stop and Recreate
    ctx.out.step("Step 3/5: Recreating containers with zero lingering state...")
    for s in SERVICES:
        ctx.repo.run(["docker", "rm", "-f", CONTAINERS[s]], check=False, quiet=True)
    ctx.repo.run(["docker", "compose", "up", "-d", "--no-deps", *SERVICES], check=True)
    ctx.out.ok("Containers successfully recreated")

    # 4. Repair network aliases & bridges
    ctx.out.step("Step 4/5: Enforcing compose network aliases and bridge links...")
    for s in SERVICES:
        alias = NETWORK_ALIASES.get(s, s)
        ctx.repo.run(
            ["docker", "network", "connect", "--alias", alias, COMPOSE_NETWORK, CONTAINERS[s]],
            check=False,
            quiet=True,
        )
    if ctx.repo.container_running("core"):
        ctx.repo.run(["docker", "network", "connect", AI_NETWORK, CONTAINERS["core"]], check=False, quiet=True)
    ctx.out.ok("Network DNS routing and external bridges connected")

    # 5. Doctor & Health validation
    ctx.out.step("Step 5/5: Running comprehensive post-upgrade doctor checks...")
    time.sleep(3.0)
    report = Report()
    check_containers(ctx, report, fix=False)
    check_aliases(ctx, report, fix=True)
    check_api(ctx, report, fix=False)
    check_database(ctx, report, fix=False)

    if report.failures:
        ctx.out.fail(f"Upgrade completed but found {len(report.failures)} degraded component(s):")
        for f in report.failures:
            ctx.out.line(f"  • {f.check}: {f.detail}")
        return 1

    ctx.out.title("System Upgrade Succeeded")
    ctx.out.ok(f"AutoTelegramForward is running cleanly at v{ctx.repo.version}")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("info", help="display host hardware, OS, and runtime facts", parents=parents)
    p.set_defaults(func=_info)

    p = sub.add_parser("upgrade", help="run full zero-downtime stack upgrade", parents=parents)
    p.add_argument("--no-pull", action="store_true", help="do not run git pull")
    p.set_defaults(func=_upgrade)
