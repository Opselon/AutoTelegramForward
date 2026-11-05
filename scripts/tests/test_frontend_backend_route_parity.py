"""Frontend ↔ backend route parity for the worker ops surface.

The bug this gate exists to catch: the Web client called
``${API_BASE}/worker/status`` while the backend registered the handler under
the ADMIN mux at ``/api/admin/worker/status``. Every poll 404'd, the DevOps
panel rendered "unavailable", and the Rust logger filled with WARN lines —
all from one missing path segment. A UI that cannot reach its backend is the
most basic integration failure, and nothing else in the suite checks it.

What it does: parse the frontend's ``api.ts`` for every ``${API_BASE}/...``
route the client can call, parse ``api/main.go`` for every route the backend
registers, and require the first set to be a subset of the second (the
backend legitimately serves routes no client calls yet).

Logs endpoints are excluded: ``/api/logs`` is user-scoped (dashMux) and its
client path is already correct.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
API_TS = REPO / "web" / "frontend" / "src" / "api.ts"
MAIN_GO = REPO / "api" / "main.go"

# The worker-ops routes are the freshly added surface; the rest of api.ts is
# long-standing and already padlocked by the OpenAPI parity gate. Match both
# spellings the client has used historically: /worker/... (the bug, missing
# the admin segment) and /admin/worker/... (the correct path).
WORKER_PREFIXES = ("/worker/", "/admin/worker/")

# adminMux routes are mounted under /admin/ (StripPrefix in main.go), so
# they answer at /api/admin/<path>; dashMux routes answer at /api/<path>.
HANDLEFUNC = re.compile(r"(\w+)Mux\.HandleFunc\(\"([A-Z]+) (/[^\"]*)\"")
HANDLE = re.compile(r"(\w+)Mux\.Handle\(\"([A-Z]+) (/[^\"]*)\"")
TEMPLATE = re.compile(r"API_BASE\}/([A-Za-z0-9_\-/{}$.]+)")


def _frontend_routes() -> set[str]:
    src = API_TS.read_text(encoding="utf-8")
    out: set[str] = set()
    for m in TEMPLATE.finditer(src):
        path = "/" + m.group(1)
        # Strip query-string and template expressions after the path core.
        path = path.split("?")[0].split("${")[0].rstrip("/")
        if any(path.startswith(p) for p in WORKER_PREFIXES):
            out.add(path)
    return out


def _backend_routes() -> set[str]:
    src = MAIN_GO.read_text(encoding="utf-8")
    admin_paths: set[str] = set()
    dash_paths: set[str] = set()
    for rx in (HANDLEFUNC, HANDLE):
        for mux, _method, path in rx.findall(src):
            if mux == "admin":
                admin_paths.add(path)
            elif mux == "dash":
                dash_paths.add(path)
    out: set[str] = set()
    for p in admin_paths:
        out.add("/admin" + p)
    for p in dash_paths:
        out.add(p)
    return out


def test_the_parsers_are_not_silently_broken() -> None:
    fe = _frontend_routes()
    be = _backend_routes()
    assert fe, "parsed no frontend worker routes — the gate is broken, not the app"
    assert "/admin/worker/status" in be, (
        "parsed no /admin/worker/status backend route — the gate is broken"
    )
    # The parser must see admin-prefixed routes: if the regex only matched
    # the historical bare /worker/ spelling, the gate would pass silently
    # while the real routes moved.
    assert "/admin/worker/status" in fe, (
        "parser sees no /admin/worker/status in the client — it cannot "
        "guard what it cannot see"
    )
    # And a deliberately-broken client route must be visible to the gate:
    # prepend the bug we fixed and confirm the gate WOULD have failed.
    broken = {p.replace("/admin/worker/", "/worker/") for p in fe}
    would_fail = sorted(p for p in broken if p not in be)
    assert would_fail, (
        "the gate cannot catch the bug it exists for: the bare /worker/ "
        "spelling parsed as if it were registered"
    )


def test_every_frontend_worker_route_has_a_backend_route() -> None:
    frontend = _frontend_routes()
    backend = _backend_routes()
    missing = sorted(p for p in frontend if p not in backend)
    assert not missing, (
        "the Web client calls these worker routes but the backend registers "
        "no handler for them (every poll would 404 and the Rust logger would "
        "WARN-spam — check the admin mux mount path):\n"
        + "\n".join(missing)
    )
