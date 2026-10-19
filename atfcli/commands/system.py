"""System-wide management, upgrades, and platform diagnostics."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..context import AI_NETWORK, COMPOSE_NETWORK, CONTAINERS, NETWORK_ALIASES, SERVICES
from .. import upgrade
from . import _shared as sh
from . import deploy, validate
from .doctor import Report, check_aliases, check_api, check_containers, check_database

NAME = "system"
HELP = "Manage full system lifecycle, version upgrades, and host diagnostics"


def _info(ctx, args) -> None:
    facts = ctx.repo.system_facts()
    if args.json:
        ctx.out.json(facts)
        return
    ctx.out.title("Host & System Facts")
    ctx.out.kv([(k, v) for k, v in facts.items()])


def _upgrade(ctx, args) -> int:
    """Full-system upgrade — the same engine as `atf upgrade`, kept for muscle memory.

    This used to be a straight five-step script. It now delegates to
    `atfcli.upgrade` so there is exactly one implementation of the sequence:
    a second copy here is how the two would drift, and the one an operator
    typed the wrong one of would be the bug.
    """
    ctx.out.title("Executing Full ATF System Upgrade")
    run = upgrade.apply(
        ctx,
        rebuild=True,
        backup=True,
        verify=True,
        assume_yes=bool(getattr(args, "yes", False)),
        fetch_remote=not getattr(args, "no_pull", False),
    )
    if getattr(args, "json", False):
        ctx.out.json(run.to_json())
        return 0 if run.ok else 1
    upgrade.render_run(ctx, run)
    return 0 if run.ok else 1


UNIT_NAME = "atf-stack.service"
UNIT_DIR = Path("/etc/systemd/system")


def _unit_text(ctx) -> str:
    """A systemd unit that owns the compose stack (start at boot, stop at shutdown)."""
    compose = ctx.repo.compose_file
    docker = shutil.which("docker") or "/usr/bin/docker"
    atf = ctx.repo.path("atf")
    return f"""[Unit]
Description=AutoTelegramForward stack (docker compose)
Documentation=https://github.com/Opselon/AutoTelegramForward
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={ctx.repo.root}
ExecStart={docker} compose -f {compose} up -d
ExecStartPost=/bin/sh -c '{ctx.repo.python()} {atf} net reconnect >/dev/null 2>&1 || true'
ExecStop={docker} compose -f {compose} stop
TimeoutStartSec=0
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
"""


def _service_install(ctx, args) -> int:
    unit = UNIT_DIR / UNIT_NAME
    text = _unit_text(ctx)
    if ctx.repo.dry_run:
        ctx.out.info(f"[dry-run] would write {unit}:")
        ctx.out.raw(text)
        ctx.out.info(f"[dry-run] would run: systemctl daemon-reload && systemctl enable --now {UNIT_NAME}")
        return 0
    try:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(text, encoding="utf-8")
    except OSError as exc:
        ctx.out.fail(f"cannot write {unit}: {exc}")
        ctx.out.hint("re-run with sudo, or use `atf up` at boot time")
        return 1
    ctx.out.ok(f"wrote {unit}")
    for argv in (
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", UNIT_NAME],
    ):
        res = ctx.repo.run(argv, check=False, quiet=True)
        if res.ok:
            ctx.out.ok(" ".join(argv))
        else:
            ctx.out.warn(f"{' '.join(argv)} failed: {res.text.strip().splitlines()[-1] if res.text.strip() else res.returncode}")
    ctx.out.line()
    return _service_status(ctx, args)


def _service_status(ctx, args) -> int:
    res = ctx.repo.run(["systemctl", "is-enabled", UNIT_NAME], check=False, quiet=True)
    enabled = res.stdout.strip() or "not installed"
    if enabled in ("not-found", "inactive"):
        enabled = "not installed"
    res2 = ctx.repo.run(["systemctl", "is-active", UNIT_NAME], check=False, quiet=True)
    active = res2.stdout.strip() or "inactive"
    running = ctx.repo.running_services()
    if args.json:
        ctx.out.json(
            {
                "unit": UNIT_NAME,
                "enabled": enabled,
                "active": active,
                "containers": running,
                "unit_file": str(UNIT_DIR / UNIT_NAME),
            }
        )
        return 0
    ctx.out.title(f"Boot service · {UNIT_NAME}")
    ctx.out.kv(
        [
            ("enabled", enabled),
            ("active", active),
            ("unit file", UNIT_DIR / UNIT_NAME),
        ]
    )
    ctx.out.line()
    ctx.out.table(
        ("SERVICE", "RUNNING", "CONTAINER"),
        [[name, "yes" if up else "no", CONTAINERS.get(name, name)] for name, up in running.items()],
    )
    return 0


def _service_uninstall(ctx, args) -> int:
    if ctx.repo.dry_run:
        ctx.out.info(f"[dry-run] would disable and remove {UNIT_NAME}")
        return 0
    ctx.repo.run(["systemctl", "disable", "--now", UNIT_NAME], check=False, quiet=True)
    unit = UNIT_DIR / UNIT_NAME
    if unit.exists():
        try:
            unit.unlink()
        except OSError as exc:
            ctx.out.fail(f"cannot remove {unit}: {exc}")
            return 1
    ctx.repo.run(["systemctl", "daemon-reload"], check=False, quiet=True)
    ctx.out.ok(f"removed {UNIT_NAME} (the stack keeps running until you `atf down`)")
    return 0


def _services(ctx, args) -> int:
    """Microservice table: state, health, reachability, endpoint, version."""
    from .. import services_probe

    rows = services_probe.probe(ctx)
    if args.json:
        ctx.out.json(services_probe.to_json(rows))
        return 0 if not services_probe.failing(rows) else 1
    services_probe.render(ctx, rows)
    bad = services_probe.failing(rows)
    if bad:
        ctx.out.line()
        ctx.out.fail(f"{len(bad)} of {len(rows)} services are not serving — see the fix lines above")
        return 1
    ctx.out.line()
    ctx.out.ok(f"all {len(rows)} services healthy")
    return 0


def _gate(ctx, args) -> bool:
    """Static validator as the start gate: fail closed unless --force."""
    if getattr(args, "no_preflight", False):
        return True
    ctx.out.step("Preflight: validating config and secrets before starting...")
    report = validate.run_checks(ctx, runtime=False)
    if not report.failures:
        return True
    ctx.out.fail(f"{len(report.failures)} blocking problem(s) — run `atf validate` for details:")
    for finding in report.failures:
        ctx.out.line(f"  \u2022 {finding.check}: {finding.detail}")
    if getattr(args, "force", False):
        ctx.out.warn("Continuing anyway (--force)")
        return True
    ctx.out.hint("  fix the items above, or re-run with --force")
    return False


def _summarise(ctx, report: Report, verb: str) -> int:
    if report.failures:
        ctx.out.fail(f"Stack {verb} with {len(report.failures)} unhealthy component(s):")
        for finding in report.failures:
            ctx.out.line(f"  \u2022 {finding.check}: {finding.detail}")
        return 1
    ctx.out.ok(f"ATF v{ctx.repo.version} is up and healthy")
    return 0


def _start(ctx, args) -> int:
    """Build + start the whole stack, gated on the static validator."""
    if not _gate(ctx, args):
        return 1
    ctx.out.title("Starting ATF stack")
    services = list(args.services or [])
    deploy.bring_up(ctx, services=services or None, build=not args.no_build)
    deploy.repair_networks(ctx, services=services or None)
    time.sleep(3.0)  # brief warm-up before the health gate
    return _summarise(ctx, deploy.verify_health(ctx), "started")


def _stop(ctx, args) -> int:
    """Stop the stack containers (images and volumes are left in place)."""
    containers = [CONTAINERS.get(s, s) for s in (args.services or SERVICES)]
    ctx.out.title("Stopping ATF stack")
    for name in containers:
        ctx.repo.run(["docker", "stop", name], check=False, quiet=True)
        ctx.out.ok(f"stopped {name}")
    return 0


def _restart(ctx, args) -> int:
    """Restart the stack containers, then re-verify health."""
    containers = [CONTAINERS.get(s, s) for s in (args.services or SERVICES)]
    ctx.out.title("Restarting ATF stack")
    for name in containers:
        ctx.repo.run(["docker", "restart", name], check=True, quiet=True)
    ctx.out.ok(f"restarted {len(containers)} container(s)")
    time.sleep(3.0)
    return _summarise(ctx, deploy.verify_health(ctx), "restarted")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("info", help="display host hardware, OS, and runtime facts", parents=parents)
    p.set_defaults(func=_info)

    p = sub.add_parser("upgrade", help="run full zero-downtime stack upgrade", parents=parents)
    p.add_argument("--no-pull", action="store_true", help="do not run git pull")
    p.set_defaults(func=_upgrade)

    p = sub.add_parser("start", help="build and start the whole stack (validator-gated)", parents=parents)
    p.add_argument("services", nargs="*", help="subset of services (default: all)")
    p.add_argument("--no-build", action="store_true", help="skip the docker image build")
    p.add_argument("--no-preflight", action="store_true", help="skip the static validator gate")
    p.add_argument("--force", action="store_true", help="start even if the validator found problems")
    p.set_defaults(func=_start)

    p = sub.add_parser("stop", help="stop the stack containers", parents=parents)
    p.add_argument("services", nargs="*", help="subset of services (default: all)")
    p.set_defaults(func=_stop)

    p = sub.add_parser("restart", help="restart the stack containers and re-verify health", parents=parents)
    p.add_argument("services", nargs="*", help="subset of services (default: all)")
    p.set_defaults(func=_restart)

    p = sub.add_parser("services", help="probe every microservice and report health", parents=parents)
    p.set_defaults(func=_services)

    # Boot persistence: one unit that owns the whole compose stack.
    service = sh.add_group(p_placeholder := sub.add_parser("service", help="install the stack as a boot service (systemd unit)", parents=parents))
    p_placeholder.set_defaults(func=sh.help_handler(p_placeholder))

    p2 = service.add_parser("install", help="write and enable the systemd unit", parents=parents)
    p2.set_defaults(func=_service_install)

    p2 = service.add_parser("status", help="show unit state and container state", parents=parents)
    p2.set_defaults(func=_service_status)

    p2 = service.add_parser("uninstall", help="disable and remove the systemd unit", parents=parents)
    p2.set_defaults(func=_service_uninstall)
