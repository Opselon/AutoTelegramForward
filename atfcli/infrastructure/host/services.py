"""Live probe of the four microservices.

`atf setup` uses this to prove the stack actually came up — not that a command
returned zero — and `atf system services` exposes the same table on demand.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from ...domain.host import CONTAINERS, SERVICES

#: gRPC services (probed from inside their own container, which works the same
#: on Linux, macOS and Windows — host-side bridge IPs are not routable on the
#: latter two).
GRPC_PORTS: dict[str, int] = {"core": 50051, "logger": 50052}
#: HTTP services, reachable through their published host port.
HTTP_PORTS: dict[str, int] = {"api": 8080, "web": 8088}
#: What each service is for; shown to an operator who has never seen this stack.
PURPOSE: dict[str, str] = {
    "logger": "log ingest + query API (gRPC)",
    "core": "Telegram core: forward engine, rules, bot (gRPC)",
    "api": "public Go gateway for dashboard + mobile (HTTP)",
    "web": "Rust Axum gateway + embedded React dashboard (HTTP)",
}


def _tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_health(port: int, timeout: float = 3.0) -> dict[str, Any] | None:
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed local URL
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _inspect(ctx, container: str, fmt: str) -> str:
    res = ctx.repo.run(["docker", "inspect", "-f", fmt, container], check=False, quiet=True)
    return res.stdout.strip() if res.ok else ""


def _grpc_reachable(ctx, container: str, port: int) -> bool:
    """Prove a gRPC port accepts connections from *inside* its own container.

    It must not assume the service is written in Python: the logger is the Rust
    `atf-logger` image (debian-slim, no python3) while the core is a Python
    image. An image has either python3 or bash, so try both in turn and stop at
    the first that succeeds (a missing interpreter fails fast, exit 126/127).
    """
    probes = (
        ["python3", "-c", f"import socket;socket.create_connection(('127.0.0.1',{port}),2)"],
        ["timeout", "2", "bash", "-c", f"</dev/tcp/127.0.0.1/{port}"],
    )
    for probe in probes:
        res = ctx.repo.run(
            ["docker", "exec", container, *probe],
            check=False,
            quiet=True,
            timeout=15,
        )
        if res.ok:
            return True
    return False


def probe(ctx, *, health_url: str = "") -> list[dict[str, Any]]:
    """One row per service: container state, health, endpoint, version, purpose."""
    rows: list[dict[str, Any]] = []
    for service in SERVICES:
        container = CONTAINERS.get(service, service)
        state = _inspect(ctx, container, "{{.State.Status}}") or "absent"
        health = _inspect(ctx, container, "{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}") or "-"
        running = state == "running"

        reachable: bool | None = None
        endpoint = ""
        version = ""
        if service in GRPC_PORTS and running:
            port = GRPC_PORTS[service]
            endpoint = f"grpc :{port}"
            reachable = _grpc_reachable(ctx, container, port)
        elif service in HTTP_PORTS:
            port = HTTP_PORTS[service]
            endpoint = f"http :{port}"
            payload = _http_health(port) if running else None
            reachable = payload is not None
            version = str(payload.get("version", "")) if payload else ""

        rows.append(
            {
                "service": service,
                "container": container,
                "state": state,
                "health": health,
                "endpoint": endpoint,
                "reachable": reachable,
                "version": version,
                "purpose": PURPOSE.get(service, ""),
            }
        )
    return rows


def status_of(row: dict[str, Any]) -> str:
    """ok | warn | fail — what an operator should conclude from one row."""
    if row["state"] != "running":
        return "fail"
    if row["health"] not in ("-", "healthy"):
        return "fail"
    if row["reachable"] is False:
        return "fail"
    if row["reachable"] is None and row["state"] == "running":
        return "warn"
    return "ok"


def failing(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if status_of(row) != "ok"]


def fix_hint(row: dict[str, Any]) -> str:
    if row["state"] != "running":
        return f"docker start {row['container']}  (or: atf system restart)"
    if row["reachable"] is False:
        return f"docker logs --tail 50 {row['container']}  then: atf system restart"
    return f"docker logs --tail 50 {row['container']}"


def render(ctx, rows: list[dict[str, Any]]) -> None:
    """Coloured table + a fix line for anything not healthy."""
    from .ui import table_with_marks

    if ctx.out.json_mode:
        return
    ctx.out.title("Microservices")
    if not rows:
        ctx.out.warn("no services configured")
        return
    table = []
    for row in rows:
        status = status_of(row)
        table.append(
            [
                f"{row['service']} — {row['purpose']}",
                row["container"],
                status.upper(),
                row["state"],
                row["reachable"] is True and "yes" or (row["reachable"] is False and "no" or "?"),
                row["endpoint"],
                row["version"] or "-",
            ]
        )
    table_with_marks(ctx.out, ("SERVICE", "CONTAINER", "STATUS", "STATE", "UP", "ENDPOINT", "VER"), table, 2)
    for row in failing(rows):
        ctx.out.hint(f"{row['container']}: {fix_hint(row)}")


def to_json(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "services": rows,
        "healthy": sum(1 for row in rows if status_of(row) == "ok"),
        "total": len(rows),
        "failing": [row["service"] for row in failing(rows)],
    }
