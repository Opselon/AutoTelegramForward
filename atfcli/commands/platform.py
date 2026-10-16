"""`atf platform` — what this machine is, and how to make it able to run ATF.

This is the one command that works with no checkout, no docker and no venv:

    python3 atf.pyz platform

It reports the host, whether docker is usable, the exact install commands for
*this* distribution, and the three supported ways to run the stack. That makes
it the right first command for a new operator, and the right diagnostic command
when `atf setup` says docker is unavailable.
"""

from __future__ import annotations

from .. import platforms as plat
from .. import ui

NAME = "platform"
HELP = "Detect this host and print the exact steps to run ATF here"


def _platform(ctx, args) -> int:
    host = plat.detect_host()
    docker = plat.detect_docker(ctx)

    if args.json:
        ctx.out.json(
            {
                "host": {
                    "os": host.os,
                    "distro": host.distro,
                    "pretty": host.pretty,
                    "arch": host.arch,
                    "is_wsl": host.is_wsl,
                    "in_container": host.in_container,
                    "python": host.python,
                    "systemd": host.systemd,
                    "shell": host.shell,
                },
                "docker": {
                    "present": docker.present,
                    "client": docker.client,
                    "daemon": docker.daemon,
                    "server": docker.server,
                    "compose": docker.compose,
                    "compose_version": docker.compose_version,
                    "usable": docker.usable,
                    "detail": docker.detail,
                },
                "modes": [{"name": name, "how": how, "why": why} for name, how, why in plat.RUN_MODES],
                "install": [{"what": what, "command": command} for what, command in plat.install_guidance(host)],
                "ready": docker.usable,
            }
        )
        return 0 if docker.usable else 1

    ui.banner(ctx.out, "AutoTelegramForward · platform", "detect this machine, then start the stack")
    ui.box(
        ctx.out,
        [
            ("host", host.label),
            ("os", f"{host.os}{' (' + host.distro + ')' if host.distro else ''}"),
            ("python", host.python),
            ("shell", host.shell or "—"),
            ("systemd", "yes" if host.systemd else "no"),
            ("workdir", str(ctx.repo.root)),
        ],
        title="host",
    )
    ui.box(
        ctx.out,
        [
            ("docker", docker.client or "not installed"),
            ("daemon", (docker.server and f"up (server {docker.server})") or ("up" if docker.daemon else "not reachable")),
            ("compose", docker.compose_version or "not found"),
            ("verdict", "usable" if docker.usable else (docker.detail or "not usable")),
        ],
        title="docker",
    )

    ctx.out.line()
    if docker.usable:
        ctx.out.ok("this machine can run the docker stack")
    else:
        ctx.out.warn("docker is not usable here — install it, or use a no-docker mode below")
        ctx.out.line()
        ctx.out.line("  " + ctx.out.paint("install docker on this machine:", "bold"))
        for what, command in plat.install_guidance(host):
            ctx.out.line(f"    {ctx.out.paint('•', 'cyan')} {what}")
            ctx.out.line(f"      {ctx.out.paint(command, 'grey')}")

    ctx.out.line()
    ctx.out.line("  " + ctx.out.paint("three ways to run ATF:", "bold"))
    for name, how, why in plat.RUN_MODES:
        ctx.out.line(f"    {ctx.out.paint(name.ljust(9), 'bold')} {how}")
        ctx.out.line(f"      {ctx.out.paint(why, 'grey')}")

    ctx.out.line()
    ctx.out.info("next: atf setup            (guided wizard: asks, validates, builds, then proves it works)")
    ctx.out.info("      atf setup --check    (read-only readiness report)")
    ctx.out.info("      atf platform --json  (the same data, for scripts)")
    return 0 if docker.usable else 1


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=_platform)
