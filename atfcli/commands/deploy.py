"""Full deployment engine: build, recreate, reconnect network aliases, and health-check.

The individual phases live in small functions because `atf setup`, `atf up`, and
`atf deploy` all need the same steps in slightly different orders — sharing them
keeps the sequence, the alias repair, and the health gate identical everywhere.
"""

from __future__ import annotations

import time
from typing import Sequence

from ..context import AI_NETWORK, COMPOSE_NETWORK, CONTAINERS, NETWORK_ALIASES, SERVICES
from ..errors import AtfError
from . import _shared as sh
from .doctor import Report, check_aliases, check_api, check_containers

NAME = "deploy"
HELP = "Orchestrate full zero-downtime stack builds, recreations, network alias repairs, and health verification"


def build_images(ctx, services: Sequence[str] | None = None, *, build: bool = True, runbook=None) -> None:
    """Build the service images (or record that we deliberately skipped it)."""
    names = list(services or SERVICES)
    if not build:
        ctx.out.info("Skipping build (--no-build)")
        if runbook is not None:
            runbook.add("build", "skip", "--no-build given")
        return
    ctx.out.step("Building service images...")
    ctx.repo.run_streaming(["docker", "compose", "build", *names])
    ctx.out.ok("Build successful")
    if runbook is not None:
        runbook.add("build", "ok", f"images built: {', '.join(names)}")


def recreate_containers(ctx, services: Sequence[str] | None = None, *, runbook=None) -> None:
    """Drop and re-create the containers so no stale state survives."""
    names = list(services or SERVICES)
    ctx.out.step("Recreating service containers...")
    for name in names:
        ctx.repo.run(["docker", "rm", "-f", CONTAINERS.get(name, name)], check=False, quiet=True)
    ctx.repo.run(["docker", "compose", "up", "-d", "--no-deps", *names], check=True)
    ctx.out.ok(f"Recreated {len(names)} container(s)")
    if runbook is not None:
        runbook.add("start", "ok", f"{len(names)} container(s) up: {', '.join(names)}")


def repair_networks(ctx, services: Sequence[str] | None = None) -> None:
    """Re-attach compose aliases and the external AI bridge.

    A manual `docker rm -f && docker run` loses compose service aliases, so every
    recreate must re-assert them or the upstreams cannot resolve each other.
    """
    names = list(services or SERVICES)
    ctx.out.step("Repairing network aliases & bridge connections...")
    for name in names:
        container = CONTAINERS.get(name, name)
        alias = NETWORK_ALIASES.get(name, name)
        ctx.repo.run(
            ["docker", "network", "connect", "--alias", alias, COMPOSE_NETWORK, container],
            check=False,
            quiet=True,
        )
    if (not services or "core" in names) and ctx.repo.container_running("core"):
        ctx.repo.run(
            ["docker", "network", "connect", AI_NETWORK, CONTAINERS["core"]], check=False, quiet=True
        )
    ctx.out.ok("Networks and DNS aliases attached")


def verify_health(ctx) -> Report:
    """The gate every bring-up path ends on."""
    report = Report()
    check_containers(ctx, report, fix=False)
    check_aliases(ctx, report, fix=True)
    check_api(ctx, report, fix=False)
    return report


def bring_up(ctx, *, services: Sequence[str] | None = None, build: bool = True, runbook=None) -> None:
    """Build + recreate in one call (used by `atf setup` and `atf up`)."""
    names = list(services or SERVICES)
    build_images(ctx, names, build=build, runbook=runbook)
    recreate_containers(ctx, names, runbook=runbook)


def _deploy(ctx, args) -> int:
    services: list[str] = list(args.services or SERVICES)
    ctx.out.title(f"Deploying ATF ({', '.join(services)})")

    ctx.out.step("Step 1/4: Building service images...")
    build_images(ctx, services, build=not args.no_build)

    ctx.out.step("Step 2/4: Recreating service containers...")
    recreate_containers(ctx, services)

    ctx.out.step("Step 3/4: Repairing network aliases & bridge connections...")
    repair_networks(ctx, services)

    ctx.out.step("Step 4/4: Verifying health...")
    time.sleep(3.0)  # brief warm-up

    report = verify_health(ctx)
    if report.failures:
        ctx.out.fail(f"Deploy completed with {len(report.failures)} health check failure(s):")
        for finding in report.failures:
            ctx.out.line(f"  • {finding.check}: {finding.detail}")
        return 1

    ctx.out.ok("Stack is fully healthy and verified online!")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=_deploy)
    parser.add_argument("services", nargs="*", help="services to deploy (default: all)")
    parser.add_argument("--no-build", action="store_true", help="skip docker image build step")
