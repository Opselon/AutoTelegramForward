"""Regression tests: the shipped compose stack must install anywhere.

Defect being pinned: commit 6452342 (after v2.0.0) attached Core and the API to
the sibling 9router AI-gateway bridge network as an `external: true` entry
inside `docker-compose.yml`. Every host that does not run that stack — a fresh
install, a fork, the release pipeline's clean-install smoke test — then failed
at `docker compose up` with

    network 9router_default declared as external, but could not be found

and, because `publish` hard-requires `clean-install == success`, no release
could be published at all.

Contract these tests enforce:
  * the base file names only networks it defines itself (no `external:`), so a
    clean `docker compose up` can never depend on a foreign network;
  * every network a base service references is declared in the base file;
  * the AI-gateway attachment lives in `docker-compose.9router.yml`, which
    declares `9router_default` as external and attaches it to exactly Core and
    the API — the two services that need to resolve `9router`;
  * the overlay changes nothing else about those services (ports, volumes,
    environment, build), so `-f base -f overlay` is the same stack plus one
    network.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = REPO_ROOT / "docker-compose.yml"
OVERLAY = REPO_ROOT / "docker-compose.9router.yml"

SERVICE_KEYS = ("image", "build", "ports", "volumes", "environment",
                "depends_on", "healthcheck", "container_name", "restart")


def _load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - yaml ships with CI image
        raise AssertionError("PyYAML is required to validate compose files")
    return yaml.safe_load(text)


def _strip_comments(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if stripped:
            out.append(stripped)
    return out


def test_base_compose_declares_no_external_network():
    """A clean `docker compose up` must not require a network it cannot create."""
    lines = _strip_comments(BASE.read_text(encoding="utf-8"))
    offenders = [ln for ln in lines if re.match(r"\s*external\s*:\s*true", ln)]
    assert not offenders, (
        "docker-compose.yml must not declare external networks — a fresh "
        f"install cannot create them: {offenders}"
    )
    assert "9router_default" not in "\n".join(lines), (
        "the host-specific 9router network must not be named in the base file"
    )


def test_every_referenced_network_is_defined_in_the_base_file():
    compose = _load(BASE)
    declared = set((compose.get("networks") or {}).keys())
    assert declared == {"atf-net"}, f"unexpected base networks: {sorted(declared)}"
    for svc_name, svc in (compose.get("services") or {}).items():
        for net in svc.get("networks") or []:
            name = net if isinstance(net, str) else next(iter(net))
            assert name in declared, (
                f"service {svc_name!r} references undeclared network {name!r}; "
                "compose would refuse to start on a clean host"
            )


def test_overlay_attaches_the_ai_gateway_to_the_services_that_resolve_it():
    overlay = _load(OVERLAY)
    networks = overlay.get("networks") or {}
    ai = networks.get("ai-net")
    assert isinstance(ai, dict) and ai.get("external") is True, (
        "the overlay must own the external ai-net declaration"
    )
    assert ai.get("name") == "9router_default"

    services = overlay.get("services") or {}
    assert set(services) == {"core", "api"}, (
        "only core (rewrite calls) and api (preflight probe) need 9router; "
        f"overlay touches {sorted(services)}"
    )
    for svc_name, svc in services.items():
        assert svc.get("networks") == ["atf-net", "ai-net"], (
            f"{svc_name} must keep atf-net and add ai-net"
        )


def test_overlay_does_not_redefine_service_surfaces():
    """The overlay adds a network; it must not quietly rewrite the stack."""
    overlay = _load(OVERLAY)
    for svc_name, svc in (overlay.get("services") or {}).items():
        extra = [k for k in svc if k not in ("networks",)]
        assert not extra, f"{svc_name} overlay only adds networks, found {extra}"
        assert not any(k in svc for k in SERVICE_KEYS)


def test_overlay_network_is_named_once_in_the_repo_docs():
    """Exactly one file owns the host-specific network name.

    Comment text is stripped first: the base file *explains* the overlay in a
    comment, and that prose is the documentation for the defect these tests
    pin — only an active (uncommented) declaration counts.
    """
    skip = {".git", "node_modules", "build", "dist", ".dart_tool", "target"}
    hits = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.rglob("*.yml")
        if p.is_file()
        and not skip.intersection(p.relative_to(REPO_ROOT).parts)
        and "9router_default" in "\n".join(_strip_comments(p.read_text(encoding="utf-8")))
    ]
    assert hits == ["docker-compose.9router.yml"], (
        f"9router_default must be declared in the overlay only, found {hits}"
    )
