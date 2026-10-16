"""Live service status board: what is running, what is healthy, what is exposed."""

from __future__ import annotations

import json
import time

from ..context import CONTAINERS, SERVICES
from ..errors import AtfError
from . import _shared as sh

NAME = "status"
HELP = "Show service, port and health status for the whole stack"


def _board(ctx, args) -> int:
    repo = ctx.repo
    docker = repo.docker_info()
    services: list[dict] = []
    if docker.available:
        ps = repo.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
            check=False,
            quiet=True,
        )
        seen: dict[str, dict] = {}
        for line in ps.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                seen[parts[0]] = {"status": parts[1], "ports": parts[2] if len(parts) > 2 else ""}
        for svc in SERVICES:
            name = CONTAINERS[svc]
            info = seen.get(name)
            services.append(
                {
                    "service": svc,
                    "container": name,
                    "running": bool(info),
                    "status": info["status"] if info else "not running",
                    "ports": info["ports"] if info else "",
                }
            )
    else:
        services = [
            {"service": s, "container": CONTAINERS[s], "running": False, "status": docker.error, "ports": ""}
            for s in SERVICES
        ]

    health = _health(ctx)
    payload = {
        "version": repo.version,
        "docker": {"available": docker.available, "version": docker.version, "error": docker.error},
        "services": services,
        "health": health,
        "urls": _urls(ctx),
    }

    if args.json:
        ctx.out.json(payload)
        return 0

    ctx.out.title(f"AutoTelegramForward {repo.version}")
    ctx.out.table(
        ("SERVICE", "CONTAINER", "RUNNING", "STATUS", "PORTS"),
        [
            [
                s["service"],
                s["container"],
                "yes" if s["running"] else "no",
                sh.truncate(s["status"], 28),
                sh.truncate(s["ports"], 30),
            ]
            for s in services
        ],
    )
    ctx.out.line()
    gateway = health.get("gateway") or {}
    if gateway:
        ok = gateway.get("status") == "healthy"
        ctx.out.banner("gateway", f"{gateway.get('status', '?')} (v{gateway.get('version', '?')})", good=ok)
        ctx.out.banner(
            "upstream",
            "connected" if gateway.get("upstream_connected") else "DISCONNECTED",
            good=bool(gateway.get("upstream_connected")),
        )
    for name, state in (health.get("services") or {}).items():
        ctx.out.banner(name, state, good=state in ("online", "healthy", "up"))
    urls = payload["urls"]
    ctx.out.line()
    ctx.out.kv([(k, v) for k, v in urls.items() if v])
    return 0


def _health(ctx) -> dict:
    """Probe the gateway + the service health endpoint. Never raises."""
    result: dict = {"gateway": {}, "services": {}, "error": ""}
    try:
        client = ctx.api_or_none
    except AtfError:
        client = None
    if client is None:
        result["error"] = "no credential available for health probes"
        return result
    for path, key in (("/healthz", "gateway"), ("/api/v1/health/services", "services")):
        try:
            data = client.get(path, expect=(200, 204, 500, 502, 503))
        except AtfError as exc:
            result.setdefault("errors", {})[path] = exc.message
            continue
        if key == "gateway" and isinstance(data, dict):
            result["gateway"] = data
        elif isinstance(data, dict):
            items = data.get("services") or data.get("components") or []
            for item in items if isinstance(items, list) else []:
                name = item.get("name") or item.get("service") or "?"
                online = item.get("online", item.get("healthy"))
                result["services"][name] = "online" if online else "offline"
    return result


def _urls(ctx) -> dict:
    repo = ctx.repo
    base = ctx.api_or_none
    return {
        "dashboard": repo.env("ATF_WEB_URL") or "http://127.0.0.1:8088",
        "api": (base.url if base else "") or "http://127.0.0.1:8080",
        "grpc_core": "core:50051",
        "grpc_logger": "logger:50052",
    }


def _watch(ctx, args) -> int:
    """Repaint the board every `--interval` seconds until interrupted."""
    rounds = args.iterations or 0
    count = 0
    while True:
        if not ctx.out.json_mode and ctx.out.is_tty:
            # Move the cursor home instead of scrolling a new board per tick.
            ctx.out.raw("\033[H\033[J")
        _board(ctx, args)
        count += 1
        if rounds and count >= rounds:
            return 0
        if not ctx.out.json_mode:
            ctx.out.line()
            ctx.out.line(f"  refreshed {time.strftime('%H:%M:%S')} · ctrl-c to stop")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def _entry(ctx, args) -> int:
    return _watch(ctx, args) if getattr(args, "watch", False) else _board(ctx, args)


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.add_argument("--watch", action="store_true", help="refresh continuously")
    parser.add_argument("--interval", type=float, default=3.0, help="refresh seconds (with --watch)")
    parser.add_argument("--iterations", type=int, default=0, help="stop after N refreshes")
    parser.set_defaults(func=_entry)
