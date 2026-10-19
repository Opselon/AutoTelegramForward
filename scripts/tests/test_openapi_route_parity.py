"""`docs/openapi.yaml` must describe exactly the routes the Go API serves.

The published contract had drifted to the point of being misleading: it claimed
`info.version: 2.3.0` while the module exported `APIVersion = 1.0.0`, it carried
a corrupted description fragment (`HTT>`), and it documented only 14 of the
routes `api/main.go` registers — while advertising a dozen endpoints under
prefixes (`/api/v1/rules`, ...) the gateway serves without that prefix. A
reader, and any generated client, got 404s for most of the documented surface
and had no way to learn that `/api/logs/purge` was reachable at all.

This test parses both sides statically — no server, no network — and pins three
properties:

1. Every served route is documented, and every documented route is served
   (method + path shape, so `{id}`/`{sessionId}` naming is not significant).
2. The authorization class the document claims for a route is the class the
   server actually enforces. A route silently becoming public, or an
   administrator-only control being documented as user-scoped, fails here.
3. `info.version` equals `APIVersion` in `api/internal/handlers/version.go`.

The public allowlist is written out below on purpose: adding a public route is a
security decision, so it must be a deliberate edit to this list AND to the
document, never an accident of a regex.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_MAIN = ROOT / "api" / "main.go"
VERSION_GO = ROOT / "api" / "internal" / "handlers" / "version.go"
OPENAPI = ROOT / "docs" / "openapi.yaml"

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# Registered on the root mux: reachable with no credential, because these are
# what a client must read BEFORE it has one (how to authenticate, whether the
# box is ready).
PUBLIC_ROOT_ROUTES = {
    ("GET", "/healthz"),
    ("GET", "/health"),
    ("GET", "/readyz"),
    ("GET", "/ready"),
    ("GET", "/api/version"),
    ("GET", "/api/openapi.yaml"),
    ("GET", "/api/health/services"),
    ("GET", "/api/v1/health/services"),
    ("GET", "/api/system/setup"),
    ("GET", "/api/v1/system/setup"),
    ("GET", "/api/system/env"),
    ("GET", "/api/v1/system/env"),
}

# The credential-issuing half of /api/auth. Everything else mounted there
# validates the Bearer token itself (see authMux below in main.go).
PUBLIC_AUTH_ROUTES = {
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/exchange-bot-token"),
    ("POST", "/api/auth/tma"),
}

# Registered on dashMux (Bearer-gated) but shadowed by a more specific root
# registration, so it answers without a token today.
SHADOWED_BY_ROOT = {("GET", "/api/health/services")}

PUBLIC = PUBLIC_ROOT_ROUTES | PUBLIC_AUTH_ROUTES


def _normalize(path: str) -> str:
    """Compare route *shapes*: every `{param}` is one opaque id."""
    return re.sub(r"\{[^}]+\}", "{id}", path)


def _served_routes() -> dict[tuple[str, str], str]:
    """(method, shape) -> authorization class the server enforces."""
    source = API_MAIN.read_text(encoding="utf-8")
    registered: list[tuple[str, str, str]] = []  # method, path, mux

    for mux in ("mux", "authMux", "dashMux", "adminMux"):
        pattern = re.compile(
            r'^\s*%s\.HandleFunc\("(%s) ([^"]+)"' % (re.escape(mux), "|".join(METHODS)),
            re.M,
        )
        for method, path in pattern.findall(source):
            registered.append((method, path, mux))

    # `dashMux.Handle("POST /logs/diagnostics/start", h.RequireAdmin(...))` —
    # the administrator gate is the wrapper, not the registration call.
    for method, path in re.findall(
        r'^\s*dashMux\.Handle\("(%s) ([^"]+)", h\.RequireAdmin' % "|".join(METHODS),
        source,
        re.M,
    ):
        registered.append((method, path, "adminDash"))

    served: dict[tuple[str, str], str] = {}
    for method, path, mux in registered:
        if mux == "mux":
            full = path
        elif mux == "authMux":
            # Mounted at /api/auth/ with StripPrefix.
            full = f"/api/auth{path}"
        elif mux == "adminMux":
            # adminMux is mounted at /api/admin/ via StripPrefix.
            full = f"/api/admin{path}"
        else:  # adminDash: a dashMux route wrapped in h.RequireAdmin
            full = f"/api{path}"

        key = (method, _normalize(full))
        if mux == "mux":
            cls = "public" if (method, full) in PUBLIC else "user"
        elif mux == "authMux":
            cls = "public" if (method, full) in PUBLIC else "user"
        elif mux in ("adminMux", "adminDash"):
            cls = "admin"
        else:
            cls = "public" if (method, full) in SHADOWED_BY_ROOT else "user"

        # A root registration is more specific than the "/api/" prefix handler,
        # so it wins over a dashMux registration of the same path.
        if mux == "dashMux" and cls == "public":
            served[key] = "public"
            continue
        if key in served and served[key] == "public" and mux == "dashMux":
            continue
        served[key] = cls
    return served


def _documented_routes() -> dict[tuple[str, str], str]:
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML ships with the CI image
        raise AssertionError("PyYAML is required to validate docs/openapi.yaml")

    doc = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    paths = doc.get("paths") or {}
    found: dict[tuple[str, str], str] = {}
    for path, operations in paths.items():
        for method, operation in operations.items():
            if method.lower() not in {m.lower() for m in METHODS}:
                continue
            if not isinstance(operation, dict):
                continue
            found[(method.upper(), _normalize(path))] = operation.get(
                "x-atf-authorization", "user"
            )
    return found


def test_the_parsers_are_not_silently_broken() -> None:
    served = _served_routes()
    documented = _documented_routes()
    assert len(served) >= 80, f"parsed only {len(served)} served routes"
    assert len(documented) >= 80, f"parsed only {len(documented)} documented operations"


def test_every_served_route_is_documented() -> None:
    documented = _documented_routes()
    missing = sorted({f"{m} {p}" for m, p in _served_routes() if (m, p) not in documented})
    assert not missing, (
        "served but undocumented (clients cannot discover or generate for these):\n"
        + "\n".join(missing)
    )


def test_every_documented_route_is_served() -> None:
    served = _served_routes()
    phantom = sorted({f"{m} {p}" for m, p in _documented_routes() if (m, p) not in served})
    assert not phantom, (
        "documented but not served (a reader following this gets a 404):\n"
        + "\n".join(phantom)
    )


def test_documented_authorization_matches_the_server() -> None:
    served = _served_routes()
    mismatches = []
    for key, claimed in sorted(_documented_routes().items()):
        actual = served.get(key)
        if actual is None or actual == claimed:
            continue
        mismatches.append(f"{key[0]} {key[1]}: documented {claimed}, enforced {actual}")
    assert not mismatches, (
        "the document promises a different authorization class than the router enforces:\n"
        + "\n".join(mismatches)
    )


def test_only_known_public_routes_are_documented_as_public() -> None:
    public_documented = {k for k, v in _documented_routes().items() if v == "public"}
    unexpected = sorted(
        f"{m} {p}" for m, p in public_documented if (m, p) not in {(a, _normalize(b)) for a, b in PUBLIC}
    )
    assert not unexpected, (
        "documented as public but not on the public allowlist:\n" + "\n".join(unexpected)
    )
    assert len(public_documented) == len(PUBLIC), (
        f"{len(PUBLIC)} public routes exist, {len(public_documented)} are documented as public"
    )


def test_openapi_version_matches_the_exported_api_version() -> None:
    import yaml

    source = VERSION_GO.read_text(encoding="utf-8")
    match = re.search(r'APIVersion\s*=\s*"([^"]+)"', source)
    assert match, "APIVersion not found in version.go"
    exported = match.group(1)

    doc = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    documented = doc["info"]["version"]
    assert documented == exported, (
        f"openapi info.version is {documented!r} but the module exports APIVersion {exported!r}; "
        "a generated client would gate its compatibility check on the wrong number"
    )


README = ROOT / "README.md"


def _readme_routes() -> set[tuple[str, str]]:
    """Method/path pairs a reader can copy out of the README code blocks."""
    found: set[tuple[str, str]] = set()
    for line in README.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*(%s)\s+(/[^\s#`]*)" % "|".join(METHODS), line)
        if not match:
            continue
        path = match.group(2).split("?", 1)[0].rstrip(",.")
        found.add((match.group(1), _normalize(path)))
    return found


def test_readme_only_lists_served_routes() -> None:
    """The README advertised 18 routes under `/api/v1/...`; 2 of them existed.

    The rest 404'd, and three (`/api/v1/ai`, `/api/v1/ai/{id}/test`,
    `/api/v1/sessions/restore`) were never designed at all: the `/api/v1` prefix
    only ever existed for the four system routes. A reader who copied a curl out
    of the README got "Not Found" and no way to tell whether the box or the doc
    was wrong.
    """
    served = _served_routes()
    invented = sorted(f"{m} {p}" for m, p in _readme_routes() if (m, p) not in served)
    assert not invented, (
        "the README documents routes the API does not serve:\n" + "\n".join(invented)
    )
    assert len(_readme_routes()) >= 20, "README route parse looks broken"
