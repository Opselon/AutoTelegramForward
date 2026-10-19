"""Network, DNS, bridge alias, and host-port management."""

from __future__ import annotations

from ..context import AI_NETWORK, COMPOSE_NETWORK, CONTAINERS, NETWORK_ALIASES, SERVICES
from ..errors import AtfError
from .. import ports as portmap
from .. import validators
from . import _shared as sh

NAME = "net"
HELP = "Manage Docker networks, DNS aliases, host ports, and inter-service connectivity"


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


# ------------------------------------------------------------------ host ports


def _ports_status(ctx, args) -> int:
    """Who holds each host port, why one is closed, and where it could go."""
    services = tuple(args.services) if args.services else SERVICES
    bad = [s for s in services if s not in SERVICES]
    if bad:
        raise AtfError(
            f"unknown service(s): {', '.join(bad)}",
            hint=f"services: {validators.describe_choices(SERVICES)}",
        )
    diagnoses = portmap.diagnose(ctx, services=services)
    if args.json:
        ctx.out.json(portmap.to_json(diagnoses, firewall=portmap.firewall_findings(ctx, [d.port for d in diagnoses if d.listening])))
    else:
        portmap.render(ctx, diagnoses)
    return 0 if not portmap.failing(diagnoses) else 1


def _ports_fix(ctx, args) -> int:
    """Move a service off a port held by something else, and re-create it.

    Reversible by design: the new port is written to `.env`, so re-running with
    the old value puts it back.
    """
    service = args.service
    if service not in SERVICES:
        # `suggest` already includes the " — did you mean 'x'?" tail.
        tail = validators.suggest(service, SERVICES)
        raise AtfError(
            f"unknown service {service!r}{tail}",
            hint=f"services: {validators.describe_choices(SERVICES)}",
        )
    current = portmap.expected_ports(ctx).get(service, 0)
    target = args.port or None
    plan = portmap.rebind_plan(ctx, service, target=target)
    if not args.json:
        ctx.out.title(f"atf net port fix {service}")
        ctx.out.kv(
            [
                ("service", service),
                ("current port", str(plan.current or "unset")),
                ("holder", plan.holder or "free"),
                ("new port", str(plan.target)),
                ("env key", plan.env_key),
            ]
        )
        ctx.out.hint(f"{plan.reason}; written to .env as {plan.env_key}={plan.target}")
    if plan.current == plan.target and not args.force:
        if args.json:
            ctx.out.json({"changed": False, "service": service, "port": plan.target, "reason": plan.reason})
        else:
            ctx.out.ok(f"{service} already uses {plan.target} — nothing to change")
        return 0
    if ctx.repo.dry_run:
        if args.json:
            ctx.out.json({"changed": False, "dry_run": True, **plan.to_json()})
        else:
            ctx.out.hint("dry run: nothing was written or recreated")
        return 0
    message = portmap.apply_rebind(ctx, plan, recreate=not args.no_recreate)
    if ctx.json:
        ctx.out.json({"changed": True, "service": service, "port": plan.target, **plan.to_json()})
    else:
        ctx.out.ok(message)
        ctx.out.hint(f"the public URL for {service} changed to :{plan.target} — check your reverse proxy and firewall")
    if current and current != plan.target and not args.json:
        ctx.out.hint(f"to undo: atf net port fix {service} --port {current}")
    return 0


def _ports_free(ctx, args) -> int:
    """Which ports near the configured ones are actually free."""
    avoid = list(portmap.expected_ports(ctx).values())
    base = args.port or portmap.DEFAULT_PORTS.get(args.service, 8080)
    found = portmap.free_port(base, avoid=[p for p in avoid if p != base])
    if args.json:
        holders = {}
        for port in avoid:
            owner = portmap.listeners(ctx, port)
            holders[str(port)] = owner.label if owner else ""
        ctx.out.json({"base": base, "free": found, "holders": holders})
        return 0 if found else 1
    if found is None:
        ctx.out.fail(f"no free port found within {portmap.AUTO_PORT_SPAN} of {base}")
        return 1
    ctx.out.ok(f"{found} is free (searched from {base})")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("inspect", help="inspect network and attached containers", parents=parents)
    p.set_defaults(func=_inspect)

    p = sub.add_parser("reconnect", help="reconnect missing DNS aliases", parents=parents)
    p.set_defaults(func=_reconnect)

    port_group = sh.add_group(sub.add_parser("port", help="inspect and repair host-port bindings", parents=parents))

    p = port_group.add_parser("status", aliases=["list"], help="who holds each host port", parents=parents)
    p.add_argument("services", nargs="*", help="subset of services (default: all)")
    p.set_defaults(func=_ports_status)

    p = port_group.add_parser("fix", help="move a service onto a free port and re-create it", parents=parents)
    p.add_argument("service", type=validators.service_argparse_type(),
                   help=f"service to rebind ({validators.describe_choices(SERVICES)})")
    p.add_argument("--port", type=validators.argparse_type("port"), default=None, help="explicit port to use")
    p.add_argument("--no-recreate", action="store_true", help="write .env only; do not recreate the container")
    p.add_argument("--force", action="store_true", help="apply even when the port is already free")
    p.set_defaults(func=_ports_fix)

    p = port_group.add_parser("free", help="find a free port near a service's port", parents=parents)
    p.add_argument("service", nargs="?", default="api", help="service to search around (default: api)")
    p.add_argument("--port", type=validators.argparse_type("port"), default=None, help="start the search here")
    p.set_defaults(func=_ports_free)
