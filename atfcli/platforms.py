"""Host detection and per-OS guidance.

The wizard has to work for someone on Windows, macOS, a Debian box, or a
container, so "docker is missing" must turn into the exact command that fixes
it on *that* machine — and a documented way to continue without docker at all.
"""

from __future__ import annotations

import os
import platform as _platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Docker's official convenience script; the one command that works on any
#: Debian/Ubuntu-family host.
GET_DOCKER = "curl -fsSL https://get.docker.com | sh"

_DISTRO_HINTS: dict[str, str] = {
    "ubuntu": "sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2",
    "debian": "sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin",
    "raspbian": "sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin",
    "linuxmint": "sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2",
    "pop": "sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2",
    "fedora": "sudo dnf install -y docker docker-compose-plugin && sudo systemctl enable --now docker",
    "rhel": "sudo dnf install -y docker docker-compose-plugin && sudo systemctl enable --now docker",
    "centos": "sudo yum install -y docker docker-compose-plugin && sudo systemctl enable --now docker",
    "rocky": "sudo dnf install -y docker docker-compose-plugin && sudo systemctl enable --now docker",
    "almalinux": "sudo dnf install -y docker docker-compose-plugin && sudo systemctl enable --now docker",
    "arch": "sudo pacman -S --noconfirm docker docker-compose && sudo systemctl enable --now docker",
    "manjaro": "sudo pacman -S --noconfirm docker docker-compose && sudo systemctl enable --now docker",
    "alpine": "sudo apk add docker docker-cli-compose && sudo rc-update add docker default && sudo service docker start",
    "opensuse-leap": "sudo zypper install -y docker docker-compose && sudo systemctl enable --now docker",
    "opensuse-tumbleweed": "sudo zypper install -y docker docker-compose && sudo systemctl enable --now docker",
}


@dataclass
class Host:
    os: str  # linux | macos | windows | unknown
    distro: str = ""
    pretty: str = ""
    arch: str = ""
    is_wsl: bool = False
    in_container: bool = False
    python: str = ""
    shell: str = ""

    @property
    def label(self) -> str:
        bits = [self.pretty or self.os.title()]
        if self.is_wsl:
            bits.append("WSL")
        if self.in_container:
            bits.append("container")
        if self.arch:
            bits.append(self.arch)
        return " · ".join(bits)

    @property
    def systemd(self) -> bool:
        """True when `systemctl` is usable (boot-service installable)."""
        if self.os != "linux" or self.in_container or self.is_wsl:
            return False
        return Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return values


def detect_host() -> Host:
    system = _platform.system().lower()
    kind = {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(system, "unknown")
    release = _platform.uname().release.lower()
    is_wsl = kind == "linux" and ("microsoft" in release or "wsl" in release or bool(os.environ.get("WSL_DISTRO_NAME")))
    in_container = Path("/.dockerenv").exists() or "docker" in (os.environ.get("HOSTNAME", "") + os.environ.get("container", ""))
    rel = _os_release()
    return Host(
        os=kind,
        distro=(rel.get("ID") or "").lower(),
        pretty=rel.get("PRETTY_NAME") or (kind.title() if kind != "unknown" else _platform.platform()),
        arch=_platform.machine(),
        is_wsl=is_wsl,
        in_container=in_container,
        python=sys.executable,
        shell=os.environ.get("SHELL") or os.environ.get("COMSPEC", ""),
    )


@dataclass
class Docker:
    present: bool = False
    client: str = ""
    daemon: bool = False
    server: str = ""
    compose: str = ""  # "plugin" | "v1" | ""
    compose_version: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.present and self.daemon and bool(self.compose)


def _read(argv: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    """Run a *read-only* probe (docker --version, docker info, docker compose version).

    Deliberately not `repo.run`: these calls inspect the host and change nothing,
    so they must also work under `--dry-run` — otherwise the wizard cannot tell
    whether docker is usable and would wrongly offer "source mode".
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return proc.returncode == 0, (proc.stdout or "").strip()


def detect_docker(ctx) -> Docker:
    """Inspect the host's docker install: client, daemon, compose."""
    info = Docker()
    path = shutil.which("docker")
    if not path:
        info.detail = "docker is not on PATH"
        return info
    info.present = True
    ok, version = _read([path, "--version"])
    info.client = version if ok else ""
    ok, server = _read([path, "info", "--format", "{{.ServerVersion}}"])
    if ok and server and "error" not in server.lower():
        info.daemon = True
        info.server = server
    else:
        info.detail = "the docker daemon is not reachable (is the service running?)"
    ok, compose = _read([path, "compose", "version", "--short"])
    if ok and compose:
        info.compose = "plugin"
        info.compose_version = compose
    else:
        legacy = shutil.which("docker-compose")
        if legacy:
            legacy_ok, legacy_version = _read([legacy, "--version"])
            info.compose = "v1" if legacy_ok else ""
            info.compose_version = legacy_version
        else:
            info.detail = "docker compose v2 plugin not found (install docker-compose-plugin)"
    for call in ([path, "info", "--format", "{{.ServerVersion}}"], [path, "compose", "version", "--short"]):
        ctx.out.debug("$ " + " ".join(call))
    return info


def install_guidance(host: Host) -> list[tuple[str, str]]:
    """(what, exact command) pairs for this specific machine."""
    if host.os == "macos":
        return [
            ("Docker Desktop (GUI, recommended)", "brew install --cask docker"),
            ("Colima (CLI-only alternative)", "brew install colima docker docker-compose && colima start"),
            ("manual", "https://docs.docker.com/desktop/install/mac-install/"),
        ]
    if host.os == "windows":
        return [
            ("Docker Desktop + WSL2 backend", "winget install --id Docker.DockerDesktop -e"),
            ("manual", "https://docs.docker.com/desktop/install/windows-install/"),
        ]
    if host.is_wsl:
        return [
            ("enable Docker Desktop WSL integration (Settings → Resources → WSL)", "docker info"),
            ("or install docker inside this distro", _DISTRO_HINTS.get(host.distro, GET_DOCKER)),
        ]
    if host.in_container:
        return [
            ("mount the host socket and rebuild this container with docker installed", "docker info"),
            ("or run the wizard on the host", "atf setup"),
        ]
    if host.os == "linux":
        hint = _DISTRO_HINTS.get(host.distro)
        rows = []
        if hint:
            rows.append((f"install for {host.pretty}", hint))
        rows.append(("official convenience script (any distro)", GET_DOCKER))
        rows.append(("then allow your user", "sudo usermod -aG docker $USER && newgrp docker"))
        return rows
    return [("install docker", "https://docs.docker.com/engine/install/")]


#: Ways to run ATF, cheapest-first for the operator.
RUN_MODES: tuple[tuple[str, str, str], ...] = (
    (
        "docker",
        "docker compose",
        "recommended: builds all four services and runs them together, repairs networks automatically",
    ),
    (
        "source",
        "bare metal (python 3.11+)",
        "no docker: installs into a venv and supervises local processes (atf setup --source-wizard)",
    ),
    (
        "portable",
        "single file (atf.pyz)",
        "one file, no install: python3 atf.pyz setup — build it with `atf release portable`",
    ),
)


def require_docker(ctx, docker: Docker) -> bool:
    """Explain — precisely — why we cannot continue without docker."""
    ctx.out.fail(docker.detail or "docker is not usable on this host")
    ctx.out.line()
    ctx.out.line("  install it for this machine:")
    for what, command in install_guidance(detect_host()):
        ctx.out.line(f"    {ctx.out.paint('•', 'cyan')} {what}")
        ctx.out.line(f"      {ctx.out.paint(command, 'grey')}")
    ctx.out.line()
    ctx.out.info("no docker? run the same wizard in source mode:  atf setup --source-wizard")
    ctx.out.info("or go portable:  atf release portable  →  python3 atf.pyz setup")
    return False
