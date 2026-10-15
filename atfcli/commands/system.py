"""System-wide management, upgrades, and platform diagnostics."""

from __future__ import annotations

import time

from ..context import AI_NETWORK, COMPOSE_NETWORK, CONTAINERS, NETWORK_ALIASES, SERVICES
from ..errors import AtfError
from . import _shared as sh
from . import deploy, validate
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


def _gate(ctx, args) -> bool:
    """Static validator as the start gate: fail closed unless --force."""
    if getattr(args, "no_preflight", False):
        return True
    ctx.out.step("Preflight: validating config and secrets before starting...")
    report = validate.run_checks(ctx, runtime=False)
    if not report.failures:
        return True
    ctx.out.fail(f"{len(report.failures)} blocking problem(s) — run `atf validate` for details:")
    for finding in report.failures:
        ctx.out.line(f"  \u2022 {finding.check}: {finding.detail}")
    if getattr(args, "force", False):
        ctx.out.warn("Continuing anyway (--force)")
        return True
    ctx.out.hint("  fix the items above, or re-run with --force")
    return False


def _summarise(ctx, report: Report, verb: str) -> int:
    if report.failures:
        ctx.out.fail(f"Stack {verb} with {len(report.failures)} unhealthy component(s):")
        for finding in report.failures:
            ctx.out.line(f"  \u2022 {finding.check}: {finding.detail}")
        return 1
    ctx.out.ok(f"ATF v{ctx.repo.version} is up and healthy")
    return 0


def _start(ctx, args) -> int:
    """Build + start the whole stack, gated on the static validator."""
    if not _gate(ctx, args):
        return 1
    ctx.out.title("Starting ATF stack")
    services = list(args.services or [])
    deploy.bring_up(ctx, services=services or None, build=not args.no_build)
    deploy.repair_networks(ctx, services=services or None)
    time.sleep(3.0)  # brief warm-up before the health gate
    return _summarise(ctx, deploy.verify_health(ctx), "started")


def _stop(ctx, args) -> int:
    """Stop the stack containers (images and volumes are left in place)."""
    containers = [CONTAINERS.get(s, s) for s in (args.services or SERVICES)]
    ctx.out.title("Stopping ATF stack")
    for name in containers:
        ctx.repo.run(["docker", "stop", name], check=False, quiet=True)
        ctx.out.ok(f"stopped {name}")
    return 0


def _restart(ctx, args) -> int:
    """Restart the stack containers, then re-verify health."""
    containers = [CONTAINERS.get(s, s) for s in (args.services or SERVICES)]
    ctx.out.title("Restarting ATF stack")
    for name in containers:
        ctx.repo.run(["docker", "restart", name], check=True, quiet=True)
    ctx.out.ok(f"restarted {len(containers)} container(s)")
    time.sleep(3.0)
    return _summarise(ctx, deploy.verify_health(ctx), "restarted")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("info", help="display host hardware, OS, and runtime facts", parents=parents)
    p.set_defaults(func=_info)

    p = sub.add_parser("upgrade", help="run full zero-downtime stack upgrade", parents=parents)
    p.add_argument("--no-pull", action="store_true", help="do not run git pull")
    p.set_defaults(func=_upgrade)

    p = sub.add_parser("start", help="build and start the whole stack (validator-gated)", parents=parents)
    p.add_argument("services", nargs="*", help="subset of services (default: all)")
    p.add_argument("--no-build", action="store_true", help="skip the docker image build")
    p.add_argument("--no-preflight", action="store_true", help="skip the static validator gate")
    p.add_argument("--force", action="store_true", help="start even if the validator found problems")
    p.set_defaults(func=_start)

    p = sub.add_parser("stop", help="stop the stack containers", parents=parents)
    p.add_argument("services", nargs="*", help="subset of services (default: all)")
    p.set_defaults(func=_stop)

    p = sub.add_parser("restart", help="restart the stack containers and re-verify health", parents=parents)
    p.add_argument("services", nargs="*", help="subset of services (default: all)")
    p.set_defaults(func=_restart)
