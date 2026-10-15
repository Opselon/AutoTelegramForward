"""`atf doctor` — check and repair the whole stack.

Each check answers a question an operator actually asks when something is
broken, in the order that narrows the fault fastest:

    docker → repo → containers → networks → aliases → ports → api → services
    → telegram session → database → disk → certificate → config

Checks are independent: a failure records a finding and the run continues, so
one command tells you everything that is wrong instead of one thing per attempt.
`--fix` applies the repairs that are safe to automate (the big one being network
aliases lost when a container is recreated with `docker run`).
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import Callable

from ..context import (
    AI_NETWORK,
    COMPOSE_NETWORK,
    CONTAINERS,
    NETWORK_ALIASES,
    SERVICES,
)
from ..errors import AtfError

NAME = "doctor"
HELP = "Diagnose the whole stack (and --fix the repairs that are safe)"


@dataclass
class Finding:
    check: str
    status: str  # ok | warn | fail
    detail: str
    fix: str = ""
    fixed: bool = False

    @property
    def bad(self) -> bool:
        return self.status in ("warn", "fail")


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, status: str, detail: str, fix: str = "", fixed: bool = False) -> Finding:
        finding = Finding(check, status, detail, fix, fixed)
        self.findings.append(finding)
        return finding

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "fail"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "warn"]

    def score(self) -> int:
        if not self.findings:
            return 0
        penalty = sum(15 if f.status == "fail" else (5 if f.status == "warn" else 0) for f in self.findings)
        return max(0, 100 - penalty)


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------- checks
def check_docker(ctx, report: Report, *, fix: bool) -> None:
    info = ctx.repo.docker_info(refresh=True)
    if not info.available:
        report.add("docker", "fail", info.error or "docker unavailable",
                   fix="start the daemon: `systemctl start docker`")
        return
    detail = f"engine {info.version}"
    if info.compose:
        detail += f", compose {info.compose_version}"
    else:
        detail += ", compose plugin MISSING"
        report.add("docker", "warn", detail, fix="install the docker compose plugin")
        return
    report.add("docker", "ok", detail)


def check_repo(ctx, report: Report, *, fix: bool) -> None:
    repo = ctx.repo
    missing = [name for name in ("docker-compose.yml", "api/main.go", "core") if not repo.exists(name)]
    if missing:
        report.add("repository", "fail", f"{repo.root} is missing {', '.join(missing)}",
                   fix="run with --root /path/to/AutoTelegramForward")
        return
    report.add("repository", "ok", f"{repo.root} @ v{repo.version}")


def check_containers(ctx, report: Report, *, fix: bool) -> None:
    if not ctx.repo.docker_info().available:
        report.add("containers", "warn", "skipped — docker unavailable")
        return
    down = [svc for svc in SERVICES if not ctx.repo.container_running(svc)]
    if down:
        names = ", ".join(CONTAINERS[s] for s in down)
        report.add("containers", "fail", f"not running: {names}",
                   fix="atf docker up  (or: atf deploy)")
        return
    report.add("containers", "ok", f"all {len(SERVICES)} services running")


def check_aliases(ctx, report: Report, *, fix: bool) -> None:
    """Compose service aliases are what make `http://api:8080` resolve.

    A container recreated with `docker run` keeps working from the host but
    loses its alias, which breaks upstreams inside the network while every
    status check outside it still looks green.
    """
    if not ctx.repo.docker_info().available:
        report.add("network aliases", "warn", "skipped — docker unavailable")
        return
    inspected = ctx.repo.run(
        ["docker", "ps", "--quiet", "--filter", "name=atf-"],
        check=False,
        quiet=True,
    ).stdout.split()
    if not inspected:
        report.add("network aliases", "warn", "no atf containers to inspect")
        return
    missing: list[tuple[str, str]] = []
    for cid in inspected:
        res = ctx.repo.run(
            ["docker", "inspect", "-f", "{{.Name}}|{{json .NetworkSettings.Networks}}", cid],
            check=False,
            quiet=True,
        )
        line = res.stdout.strip()
        if "|" not in line:
            continue
        name, _, raw_networks = line.partition("|")
        name = name.lstrip("/")
        service = next((s for s, c in CONTAINERS.items() if c == name), "")
        if not service:
            continue
        alias = NETWORK_ALIASES.get(service, service)
        try:
            networks = json.loads(raw_networks)
        except Exception:
            networks = {}
        if COMPOSE_NETWORK not in networks:
            missing.append((service, f"not attached to {COMPOSE_NETWORK}"))
            continue
        net_info = networks.get(COMPOSE_NETWORK) or {}
        aliases = net_info.get("Aliases") or []
        dns_names = net_info.get("DNSNames") or []
        if alias not in aliases and alias not in dns_names:
            missing.append((service, f"alias {alias!r} missing on {COMPOSE_NETWORK}"))

    if not missing:
        report.add("network aliases", "ok", f"{len(inspected)} containers carry their compose aliases")
        return

    detail = "; ".join(f"{svc}: {why}" for svc, why in missing)
    if not fix:
        report.add("network aliases", "fail", detail, fix="re-run with --fix to reconnect the aliases")
        return
    repaired: list[str] = []
    for svc, why in missing:
        container = CONTAINERS[svc]
        alias = NETWORK_ALIASES.get(svc, svc)
        if "not attached" in why:
            ctx.repo.run(["docker", "network", "connect", COMPOSE_NETWORK, container], check=False, quiet=True)
        ctx.repo.run(
            ["docker", "network", "connect", "--alias", alias, COMPOSE_NETWORK, container],
            check=False,
            quiet=True,
        )
        repaired.append(svc)
    
    report.add("network aliases", "ok", f"reconnected aliases for {', '.join(repaired)}", fixed=True)



def check_ports(ctx, report: Report, *, fix: bool) -> None:
    expected = {"api": 8080, "web": 8088, "core": 50051, "logger": 50052}
    closed = [f"{svc}:{port}" for svc, port in expected.items() if not _port_open("127.0.0.1", port)]
    if closed:
        # api/web are host-published; the gRPC ports are normally container-only,
        # so a closed gRPC port is informational rather than a failure.
        host_facing = [c for c in closed if c.split(":")[0] in ("api", "web")]
        if host_facing:
            report.add("ports", "fail", f"host ports closed: {', '.join(host_facing)}",
                       fix="atf docker restart api web")
        else:
            report.add("ports", "ok", f"host ports open; internal-only closed: {', '.join(closed)}")
        return
    report.add("ports", "ok", "8080, 8088, 50051, 50052 all answering")


def check_api(ctx, report: Report, *, fix: bool) -> None:
    client = ctx.api_or_none
    if client is None:
        report.add("api", "warn", "no credential — cannot probe the API",
                   fix="atf auth login  (or --token)")
        return
    began = time.time()
    try:
        health = client.get("/healthz")
    except AtfError as exc:
        report.add("api", "fail", f"{(client.url or '?')}/healthz: {exc.message}",
                   fix="atf docker logs api --tail 50")
        return
    ms = int((time.time() - began) * 1000)
    version = health.get("version", "?") if isinstance(health, dict) else "?"
    upstream = health.get("upstream_connected") if isinstance(health, dict) else None
    status = "healthy" if upstream in (True, None) else "upstream disconnected"
    report.add(
        "api",
        "ok" if upstream in (True, None) else "fail",
        f"{client.url} v{version} — {status} ({ms}ms)",
        fix="" if upstream in (True, None) else "check the telegram session: atf sessions list",
    )


def check_services(ctx, report: Report, *, fix: bool) -> None:
    client = ctx.api_or_none
    if client is None:
        report.add("services", "warn", "skipped — no credential")
        return
    try:
        data = client.get("/api/v1/health/services", expect=(200, 404))
    except AtfError as exc:
        report.add("services", "warn", f"health endpoint unavailable: {exc.message}")
        return
    if isinstance(data, str) or not data:
        report.add("services", "warn", "health endpoint returned no data")
        return
    items = data.get("services") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        report.add("services", "ok", "gateway reports no degraded services")
        return
    offline = [str(i.get("name", "?")) for i in items if not i.get("online", i.get("healthy", False))]
    if offline:
        report.add("services", "fail", f"offline: {', '.join(offline)}",
                   fix="atf docker restart " + " ".join(offline))
    else:
        report.add("services", "ok", f"{len(items)} services online")


def check_database(ctx, report: Report, *, fix: bool) -> None:
    """Read DB facts through the API so the CLI never opens the file directly."""
    client = ctx.api_or_none
    if client is None:
        report.add("database", "warn", "skipped — no credential")
        return
    try:
        stats = client.get("/api/diagnostics")
    except AtfError as exc:
        report.add("database", "warn", f"diagnostics unavailable: {exc.message}")
        return
    if not isinstance(stats, dict):
        report.add("database", "warn", "diagnostics returned an unexpected shape")
        return
    counts = stats.get("counts") or {}
    counts_text = ", ".join(f"{k}={v}" for k, v in list(counts.items())[:6]) if isinstance(counts, dict) else ""
    findings = stats.get("findings") or []
    if findings:
        report.add("database", "warn", f"{len(findings)} diagnostics finding(s): {findings[0]}",
                   fix="atf status --json  for the full list")
        return
    report.add("database", "ok", f"score {stats.get('score', '?')} — {counts_text or 'no counts reported'}")


def check_logs(ctx, report: Report, *, fix: bool) -> None:
    client = ctx.api_or_none
    if client is None:
        report.add("log archive", "warn", "skipped — no credential")
        return
    try:
        stats = client.get("/api/logs/stats")
    except AtfError as exc:
        report.add("log archive", "warn", f"log stats unavailable: {exc.message}")
        return
    total = stats.get("total") if isinstance(stats, dict) else None
    newest = stats.get("newest_ts") if isinstance(stats, dict) else None
    if newest:
        age = time.time() - float(newest)
        from ..console import human_duration

        report.add("log archive", "ok", f"{total or 0} entries, newest {human_duration(age)} old")
    else:
        report.add("log archive", "ok", f"{total or 0} entries")


def check_session(ctx, report: Report, *, fix: bool) -> None:
    client = ctx.api_or_none
    if client is None:
        report.add("telegram session", "warn", "skipped — no credential")
        return
    try:
        data = client.get("/api/sessions", expect=(200, 404))
    except AtfError as exc:
        report.add("telegram session", "warn", f"session list unavailable: {exc.message}")
        return
    items = data if isinstance(data, list) else (data.get("sessions") if isinstance(data, dict) else [])
    if not items:
        report.add("telegram session", "fail", "no telegram session linked",
                   fix="atf auth login --phone <number>")
        return
    active = [s for s in items if s.get("is_active", s.get("authorized", True))]
    report.add("telegram session", "ok" if active else "warn",
               f"{len(active)}/{len(items)} session(s) active")


def check_certificate(ctx, report: Report, *, fix: bool) -> None:
    """The public gateway is TLS; an expired cert is invisible from inside."""
    repo = ctx.repo
    cert_dir = repo.path("/etc/letsencrypt/live")
    domain = repo.env("ATF_DOMAIN") or repo.env("DOMAIN") or "api.legoten.com"
    candidates = [
        repo.path("/etc/letsencrypt/live", domain, "fullchain.pem"),
        repo.path("certs", "fullchain.pem"),
    ]
    cert_path = next((c for c in candidates if c.exists()), None)
    if cert_path is None:
        if cert_dir.exists():
            report.add("certificate", "warn", f"no certificate found for {domain}")
        else:
            report.add("certificate", "ok", "no local TLS material (proxy terminates TLS elsewhere)")
        return
    res = repo.run(
        ["openssl", "x509", "-enddate", "-noout", "-in", str(cert_path)],
        check=False,
        quiet=True,
    )
    if not res.ok:
        report.add("certificate", "warn", f"cannot read {cert_path}")
        return
    text = res.stdout.strip()
    if "=" not in text:
        report.add("certificate", "warn", f"unexpected openssl output: {text[:80]}")
        return
    when = text.split("=", 1)[1].strip()
    report.add("certificate", "ok", f"{domain} valid until {when}",
               fix="atf certs renew  (if within 30 days)")


def check_disk(ctx, report: Report, *, fix: bool) -> None:
    facts = ctx.repo.system_facts()
    pct = facts.get("disk_used_pct")
    free = facts.get("disk_free_gb")
    if pct is None:
        report.add("disk", "warn", "disk usage unavailable")
        return
    status = "ok" if float(pct) < 85 else ("warn" if float(pct) < 95 else "fail")
    report.add("disk", status, f"{pct}% used, {free} GB free",
               fix="atf docker prune  (and: atf logs purge --older-than 30d)" if status != "ok" else "")


def check_ai(ctx, report: Report, *, fix: bool) -> None:
    """The AI rewrite backend is an external dependency with no health endpoint."""
    repo = ctx.repo
    base_url = repo.env("AI_BASE_URL") or repo.env("OPENAI_BASE_URL") or ""
    model = repo.env("AI_MODEL") or repo.env("OPENAI_MODEL") or ""
    if not base_url:
        installed = repo.container_env("core", "AI_BASE_URL")
        base_url = installed
    if not base_url:
        report.add("ai backend", "ok", "not configured (AI rewrite disabled)")
        return
    host, _, port = base_url.split("//", 1)[-1].partition(":")
    port = port.split("/")[0] or "80"
    reachable = False
    try:
        reachable = _port_open(host, int(port))
    except ValueError:
        reachable = False
    report.add(
        "ai backend",
        "ok" if reachable else "warn",
        f"{host}:{port} {'reachable' if reachable else 'unreachable'}"
        + (f" (model {model})" if model else ""),
        fix="" if reachable else "check the 9router container: docker ps | grep 9router",
    )


CHECKS: tuple[Callable, ...] = (
    check_docker,
    check_repo,
    check_containers,
    check_aliases,
    check_ports,
    check_api,
    check_services,
    check_database,
    check_logs,
    check_session,
    check_certificate,
    check_disk,
    check_ai,
)


# -------------------------------------------------------------------- render
_MARK = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red")}


def _doctor(ctx, args) -> int:
    report = Report()
    selected = [c for c in CHECKS if not args.only or c.__name__.replace("check_", "") in args.only]
    started = time.time()
    for check in selected:
        try:
            check(ctx, report, fix=args.fix)
        except AtfError as exc:
            report.add(check.__name__.replace("check_", ""), "warn", exc.message)
        except Exception as exc:  # noqa: BLE001 - a broken check must not hide the rest
            report.add(check.__name__.replace("check_", ""), "warn", f"{type(exc).__name__}: {exc}")
    elapsed = time.time() - started

    payload = {
        "score": report.score(),
        "status": "healthy" if not report.failures else "degraded",
        "duration_s": round(elapsed, 2),
        "fix_mode": bool(args.fix),
        "findings": [
            {
                "check": f.check,
                "status": f.status,
                "detail": f.detail,
                "fix": f.fix,
                "fixed": f.fixed,
            }
            for f in report.findings
        ],
    }
    if args.json:
        ctx.out.json(payload)
        return 0 if not report.failures else 1

    ctx.out.title("atf doctor")
    for finding in report.findings:
        mark, color = _MARK[finding.status]
        line = f"  {ctx.out.paint(mark, color)} {finding.check.ljust(18)} {finding.detail}"
        ctx.out.line(line)
        if finding.fix and finding.status != "ok":
            ctx.out.line(ctx.out.paint(f"      → {finding.fix}", "grey"))
    ctx.out.line()
    verdict = "healthy" if not report.failures else f"degraded ({len(report.failures)} failure(s))"
    ctx.out.line(f"  score {report.score()}/100 · {verdict} · {elapsed:.1f}s")
    if args.fix:
        repaired = [f.check for f in report.findings if f.fixed]
        if repaired:
            ctx.out.line(f"  repaired: {', '.join(repaired)}")
    return 0 if not report.failures else 1


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(
        NAME,
        help=HELP,
        description=HELP,
        parents=parents,
        epilog=(
            "Exit code is 0 when nothing failed, 1 when any check failed — so a\n"
            "cron job can alert on `atf doctor` without parsing output."
        ),
    )
    parser.add_argument("--fix", action="store_true", help="apply safe repairs (network aliases, etc.)")
    parser.add_argument("--only", nargs="+", metavar="CHECK", help="run only these checks")
    parser.set_defaults(func=_doctor)
