"""Network, DNS, and bridge alias management."""

from __future__ import annotations

from ..context import AI_NETWORK, COMPOSE_NETWORK, CONTAINERS, NETWORK_ALIASES, SERVICES
from . import _shared as sh

NAME = "net"
HELP = "Manage Docker networks, DNS aliases, and inter-service connectivity"


def _inspect(ctx, args) -> None:
    res = ctx.repo.run(["docker", "network", "inspect", COMPOSE_NETWORK], check=False)
    if not res.ok:
        ctx.out.fail(f"Network {COMPOSE_NETWORK} not found")
        return
    import json
    data = json.loads(res.stdout)
    if args.json:
        ctx.out.json(data)
        return
    ctx.out.title(f"Network: {COMPOSE_NETWORK}")
    net = data[0] if isinstance(data, list) and data else {}
    containers = net.get("Containers") or {}
    rows = []
    for cid, cinfo in containers.items():
        name = cinfo.get("Name")
        ip = cinfo.get("IPv4Address", "")
        rows.append([name, ip])
    ctx.out.table(("CONTAINER", "IP ADDRESS"), rows, empty="no containers attached")


def _reconnect(ctx, args) -> None:
    ctx.out.step(f"Reconnecting network aliases on {COMPOSE_NETWORK}...")
    for s, c in CONTAINERS.items():
        if not ctx.repo.container_running(s):
            continue
        alias = NETWORK_ALIASES.get(s, s)
        ctx.repo.run(["docker", "network", "connect", "--alias", alias, COMPOSE_NETWORK, c], check=False, quiet=True)
    if ctx.repo.container_running("core"):
        ctx.repo.run(["docker", "network", "connect", AI_NETWORK, CONTAINERS["core"]], check=False, quiet=True)
    ctx.out.ok("Aliases reconnected")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("inspect", help="inspect network and attached containers", parents=parents)
    p.set_defaults(func=_inspect)

    p = sub.add_parser("reconnect", help="reconnect missing DNS aliases", parents=parents)
    p.set_defaults(func=_reconnect)
