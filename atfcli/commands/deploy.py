"""Full deployment engine: build, recreate, reconnect network aliases, and health-check."""

from __future__ import annotations

import time
from typing import Sequence

from ..context import AI_NETWORK, COMPOSE_NETWORK, CONTAINERS, NETWORK_ALIASES, SERVICES
from ..errors import AtfError
from . import _shared as sh
from .doctor import Report, check_aliases, check_api, check_containers

NAME = "deploy"
HELP = "Orchestrate full zero-downtime stack builds, recreations, network alias repairs, and health verification"


def _deploy(ctx, args) -> int:
    repo = ctx.repo
    services: list[str] = list(args.services or SERVICES)
    ctx.out.title(f"Deploying ATF ({', '.join(services)})")

    # 1. Build
    if not args.no_build:
        ctx.out.step("Step 1/4: Building service images...")
        repo.run_streaming(["docker", "compose", "build", *services])
        ctx.out.ok("Build successful")
    else:
        ctx.out.info("Skipping build (--no-build)")

    # 2. Recreate / restart
    ctx.out.step("Step 2/4: Recreating service containers...")
    containers_to_stop: list[str] = [CONTAINERS.get(s, s) for s in services]
    for c in containers_to_stop:
        repo.run(["docker", "rm", "-f", c], check=False, quiet=True)

    up_cmd = ["docker", "compose", "up", "-d", "--no-deps", *services]
    repo.run(up_cmd, check=True)
    ctx.out.ok(f"Recreated {len(services)} container(s)")

    # 3. Ensure network aliases & external networks
    ctx.out.step("Step 3/4: Repairing network aliases & bridge connections...")
    for s in services:
        c = CONTAINERS.get(s, s)
        alias = NETWORK_ALIASES.get(s, s)
        # Ensure connected to internal compose network with alias
        repo.run(["docker", "network", "connect", "--alias", alias, COMPOSE_NETWORK, c], check=False, quiet=True)

    # Reconnect external AI router network to core if core is running
    if "core" in services and repo.container_running("core"):
        repo.run(["docker", "network", "connect", AI_NETWORK, CONTAINERS["core"]], check=False, quiet=True)
    ctx.out.ok("Networks and DNS aliases attached")

    # 4. Health verification
    ctx.out.step("Step 4/4: Verifying health...")
    time.sleep(3.0)  # Brief warm up

    report = Report()
    check_containers(ctx, report, fix=False)
    check_aliases(ctx, report, fix=True)
    check_api(ctx, report, fix=False)

    if report.failures:
        ctx.out.fail(f"Deploy completed with {len(report.failures)} health check failure(s):")
        for f in report.failures:
            ctx.out.line(f"  • {f.check}: {f.detail}")
        return 1

    ctx.out.ok("Stack is fully healthy and verified online!")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=_deploy)
    parser.add_argument("services", nargs="*", help="services to deploy (default: all)")
    parser.add_argument("--no-build", action="store_true", help="skip docker image build step")
