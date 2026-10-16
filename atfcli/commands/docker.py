"""Docker container and image operations."""

from __future__ import annotations

from typing import Sequence

from ..context import CONTAINERS, SERVICES
from ..errors import UsageError
from . import _shared as sh

NAME = "docker"
HELP = "Manage Docker containers, images, logs, builds, and lifecycle"


def _ps(ctx, args) -> None:
    res = ctx.repo.run(["docker", "ps", "--filter", "name=atf-", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"], check=False)
    rows = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rows.append([parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""])
    if args.json:
        ctx.out.json([{"name": r[0], "status": r[1], "ports": r[2]} for r in rows])
        return
    ctx.out.table(("CONTAINER", "STATUS", "PORTS"), rows, empty="no ATF containers running")


def _up(ctx, args) -> None:
    services = args.services or list(SERVICES)
    cmd = ["docker", "compose", "up", "-d"]
    if args.build:
        cmd.append("--build")
    if args.no_deps:
        cmd.append("--no-deps")
    cmd.extend(services)
    ctx.out.step(f"Starting {', '.join(services)}...")
    ctx.repo.run_streaming(cmd)
    ctx.out.ok("Containers started")


def _down(ctx, args) -> None:
    sh.confirm_or_fail(ctx, "Stop and remove ATF containers?")
    ctx.out.step("Stopping containers...")
    ctx.repo.run_streaming(["docker", "compose", "down"])
    ctx.out.ok("Containers stopped")


def _restart(ctx, args) -> None:
    services = args.services or list(SERVICES)
    containers: list[str] = [CONTAINERS.get(s, s) for s in services]
    ctx.out.step(f"Restarting {', '.join(containers)}...")
    ctx.repo.run(["docker", "restart", *containers])
    ctx.out.ok(f"Restarted {len(containers)} container(s)")


def _logs(ctx, args) -> int:
    service = args.service
    container = CONTAINERS.get(service, service)
    cmd = ["docker", "logs"]
    if args.follow:
        cmd.append("-f")
    cmd.extend(["--tail", str(args.tail or 100), container])
    return ctx.repo.run_streaming(cmd, check=False)


def _build(ctx, args) -> None:
    services = args.services or list(SERVICES)
    cmd = ["docker", "compose", "build"]
    if args.no_cache:
        cmd.append("--no-cache")
    cmd.extend(services)
    ctx.out.step(f"Building Docker images for: {', '.join(services)}...")
    ctx.repo.run_streaming(cmd)
    ctx.out.ok("Build complete")


def _prune(ctx, args) -> None:
    sh.confirm_or_fail(ctx, "Prune unused Docker images, containers, and volumes?")
    ctx.out.step("Pruning Docker system...")
    ctx.repo.run_streaming(["docker", "system", "prune", "-f"])
    ctx.out.ok("Prune complete")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("ps", help="list running ATF containers", parents=parents)
    p.set_defaults(func=_ps)

    p = sub.add_parser("up", help="start containers with compose", parents=parents)
    p.add_argument("services", nargs="*", help="services to start (default: all)")
    p.add_argument("--build", action="store_true", help="build images before starting")
    p.add_argument("--no-deps", action="store_true", help="do not start linked services")
    p.set_defaults(func=_up)

    p = sub.add_parser("down", help="stop and remove containers", parents=parents)
    p.set_defaults(func=_down)

    p = sub.add_parser("restart", help="restart containers", parents=parents)
    p.add_argument("services", nargs="*", help="services to restart (default: all)")
    p.set_defaults(func=_restart)

    p = sub.add_parser("logs", help="view Docker container logs", parents=parents)
    p.add_argument("service", choices=list(SERVICES) + list(CONTAINERS.values()), help="target service or container")
    p.add_argument("-f", "--follow", action="store_true", help="follow log output")
    p.add_argument("-n", "--tail", type=int, default=100, help="number of lines from end")
    p.set_defaults(func=_logs)

    p = sub.add_parser("build", help="build container images", parents=parents)
    p.add_argument("services", nargs="*", help="services to build (default: all)")
    p.add_argument("--no-cache", action="store_true", help="do not use cache when building the image")
    p.set_defaults(func=_build)

    p = sub.add_parser("prune", help="prune unused Docker resources", parents=parents)
    p.set_defaults(func=_prune)
