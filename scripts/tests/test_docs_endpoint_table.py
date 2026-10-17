"""The published API table must only list routes the server actually serves.

`website/scripts/sync-repo-content.mjs` carries a hand-maintained endpoint table
that is rendered into the public documentation. It had drifted badly: every
entry in the tasks/sessions/filters/observability groups pointed at `/api/v1/...`
while the Go API serves the same resources without that prefix, so a reader who
followed the docs got a 404 on 19 of 21 routes — traffic that then showed up in
the log store as "Not Found" errors.

This test parses both sides statically (no server, no network): the `['GET',
'/api/...']` tuples in the generator and the `mux.HandleFunc("GET /path")`
registrations in `api/main.go`. Anything the docs advertise that the mux does
not register fails the build.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_GENERATOR = ROOT / "website" / "scripts" / "sync-repo-content.mjs"
API_MAIN = ROOT / "api" / "main.go"

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# Routes registered directly on the root mux (outside the /api prefix) or
# provided by the gateway rather than the Go handler table.
ROOT_LEVEL = {
    "/healthz",
    "/health",
    "/health/services",
    "/api/version",
    "/api/health/services",
    "/api/v1/health/services",
    "/api/system/setup",
    "/api/system/env",
    "/api/v1/system/setup",
    "/api/v1/system/env",
}


def _served_routes() -> set[str]:
    source = API_MAIN.read_text(encoding="utf-8")
    served: set[str] = set()
    for method, path in re.findall(r'HandleFunc\("(%s) ([^"]+)"' % "|".join(METHODS), source):
        # `mux.HandleFunc("GET /rules")` is mounted under /api by the gateway.
        served.add(path if path.startswith("/api/") or path in ROOT_LEVEL else f"/api{path}")
    return served | ROOT_LEVEL


def _documented_endpoints() -> list[tuple[str, str]]:
    source = DOCS_GENERATOR.read_text(encoding="utf-8")
    found: list[tuple[str, str]] = []
    for method, path in re.findall(r"\['(%s)', '([^']+)'" % "|".join(METHODS), source):
        if not path.startswith("/"):
            continue
        # Drop query strings and path parameters: only the shape is compared.
        shape = path.split("?", 1)[0]
        shape = re.sub(r"\{[^}]+\}", "{id}", shape)
        found.append((method, shape))
    return found


def test_every_documented_endpoint_is_served() -> None:
    served = {re.sub(r"\{[^}]+\}", "{id}", p) for p in _served_routes()}
    missing = sorted({f"{m} {p}" for m, p in _documented_endpoints() if p not in served})
    assert not missing, "documented but not served:\n" + "\n".join(missing)


def test_the_table_is_not_empty() -> None:
    # Guards against a regex/refactor silently reducing this test to a no-op.
    endpoints = _documented_endpoints()
    assert len(endpoints) >= 15, f"only parsed {len(endpoints)} documented endpoints"
    assert len(_served_routes()) >= 40, "route table parse looks broken"
