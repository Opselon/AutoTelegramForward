"""Repository + runtime context for the ATF CLI.

Everything that touches the machine (files, env, docker, subprocesses) goes
through [RepoContext] so commands stay testable: a test can point the context at
a temp directory with a stub `docker` on PATH.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ...console import Console
from ...domain.errors import CommandFailed, UnreachableError
from ...domain.host import (
    AI_NETWORK,
    COMPOSE_NETWORK,
    CONTAINERS,
    NETWORK_ALIASES,
    SERVICES,
)
from ...domain.secrets import SECRET_NAME_PATTERN, is_secret_name


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")


@dataclass
class DockerInfo:
    available: bool
    version: str = ""
    compose: bool = False
    compose_version: str = ""
    error: str = ""


class RepoContext:
    """Where the code is, what is running, and how to run things."""

    def __init__(
        self,
        console: Console,
        *,
        root: Path | None = None,
        dry_run: bool = False,
        assume_yes: bool = False,
        timeout: float = 120.0,
    ) -> None:
        self.out = console
        self.root = Path(root) if root else find_repo_root()
        self.dry_run = dry_run
        self.assume_yes = assume_yes
        self.timeout = timeout
        self._docker_info: DockerInfo | None = None
        self._env: dict[str, str] | None = None
        self._env_file_values: dict[str, str] | None = None
        self.secrets: set[str] = set()

    # ------------------------------------------------------------------ paths
    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def exists(self, *parts: str) -> bool:
        return self.path(*parts).exists()

    @property
    def version(self) -> str:
        try:
            return self.path("VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"

    @property
    def compose_file(self) -> Path:
        return self.path("docker-compose.yml")

    # -------------------------------------------------------------------- env
    @property
    def env_file_values(self) -> dict[str, str]:
        """Values from `.env` (parsed, not exported)."""
        if self._env_file_values is not None:
            return self._env_file_values
        values: dict[str, str] = {}
        env_path = self.path(".env")
        if env_path.exists():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
        for key, value in values.items():
            if is_secret_name(key):
                self.out.register_secret(value)
        self._env_file_values = values
        return values

    def refresh_env(self) -> None:
        """Drop the parsed `.env` cache, after something rewrote the file.

        `atf config set` writes through the store, not through this context, so
        the cache the earlier reads filled would keep answering with the values
        it saw before the write for the rest of the invocation.
        """
        self._env_file_values = None
        self._env = None

    def env(self, key: str, default: str = "") -> str:
        """Process env wins over `.env`, matching how compose behaves."""
        if key in os.environ and os.environ[key]:
            return os.environ[key]
        return self.env_file_values.get(key, default)

    # ----------------------------------------------------------------- docker
    def docker_info(self, refresh: bool = False) -> DockerInfo:
        if self._docker_info is not None and not refresh:
            return self._docker_info
        if not shutil.which("docker"):
            self._docker_info = DockerInfo(available=False, error="docker not on PATH")
            return self._docker_info
        ver = self.run(["docker", "version", "--format", "{{.Server.Version}}"], check=False, quiet=True)
        if not ver.ok:
            self._docker_info = DockerInfo(
                available=False,
                error=(ver.stderr or ver.stdout or "docker daemon unreachable").strip(),
            )
            return self._docker_info
        comp = self.run(["docker", "compose", "version", "--short"], check=False, quiet=True)
        self._docker_info = DockerInfo(
            available=True,
            version=ver.stdout.strip(),
            compose=comp.ok,
            compose_version=comp.stdout.strip() if comp.ok else "",
        )
        return self._docker_info

    def require_docker(self) -> DockerInfo:
        info = self.docker_info()
        if not info.available:
            raise UnreachableError(
                f"docker is not usable: {info.error}",
                hint="start the docker daemon, or run this command on the host running the stack",
            )
        return info

    def compose(self, *args: str, check: bool = True, quiet: bool = False) -> CommandResult:
        return self.run(["docker", "compose", *args], check=check, quiet=quiet)

    def container_running(self, service: str) -> bool:
        name = CONTAINERS.get(service, service)
        res = self.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            check=False,
            quiet=True,
        )
        return res.ok and res.stdout.strip() == "true"

    def container_names(self) -> list[str]:
        res = self.run(["docker", "ps", "--format", "{{.Names}}"], check=False, quiet=True)
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def running_services(self) -> dict[str, bool]:
        return {svc: self.container_running(svc) for svc in SERVICES}

    def container_env(self, service: str, key: str) -> str:
        """Read one env var from a running container without printing others."""
        name = CONTAINERS.get(service, service)
        res = self.run(["docker", "exec", name, "printenv", key], check=False, quiet=True)
        return res.stdout.strip() if res.ok else ""

    # ------------------------------------------------------------------- exec
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        quiet: bool = False,
        timeout: float | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        argv = [str(a) for a in argv]
        if self.dry_run:
            # --dry-run means *nothing* is executed: a real side effect behind a
            # "dry run" banner is worse than no dry run at all. Callers that
            # need a value here get an empty success result.
            self.out.debug(f"[dry-run] {' '.join(argv)}")
            return CommandResult(argv=argv, returncode=0, stdout="", stderr="", duration=0.0)
        started = time.time()
        proc = subprocess.run(
            argv,
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
            env={**os.environ, **dict(env or {})},
            input=input_text,
        )
        result = CommandResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration=time.time() - started,
        )
        if not quiet:
            self.out.debug(f"$ {' '.join(argv)} -> {result.returncode} in {result.duration:.2f}s")
        if check and not result.ok:
            raise CommandFailed(
                f"command failed ({result.returncode}): {' '.join(argv)}",
                output=result.text.strip()[-2000:],
            )
        return result

    def run_streaming(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> int:
        """Run a command with the terminal attached (builds, deploys, tests)."""
        argv = [str(a) for a in argv]
        self.out.debug(f"$ {' '.join(argv)}")
        if self.dry_run:
            self.out.debug(f"[dry-run] {' '.join(argv)}")
            return 0
        proc = subprocess.run(
            argv,
            cwd=str(cwd or self.root),
            env={**os.environ, **dict(env or {})} if env else None,
        )
        if check and proc.returncode != 0:
            raise CommandFailed(f"command failed ({proc.returncode}): {' '.join(argv)}")
        return proc.returncode

    def python(self) -> str:
        """.venv interpreter when present, else the current one."""
        venv = self.path(".venv", "bin", "python")
        if venv.exists():
            return str(venv)
        return "python3"

    def confirm(self, question: str, *, default: bool = False) -> bool:
        """Ask before a destructive action. `--yes` and non-tty skip the prompt."""
        if self.assume_yes:
            return True
        if self.dry_run:
            return False
        if not getattr(self.out.out, "isatty", lambda: False)():
            return default
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            answer = input(f"{question} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not answer:
            return default
        return answer in ("y", "yes")

    # ------------------------------------------------------------------- info
    def system_facts(self) -> dict[str, object]:
        facts: dict[str, object] = {
            "root": str(self.root),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "version": self.version,
            "hostname": platform.node(),
        }
        try:
            load = os.getloadavg()
            facts["load"] = f"{load[0]:.2f} {load[1]:.2f} {load[2]:.2f}"
        except (OSError, AttributeError):
            pass
        try:
            usage = shutil.disk_usage(self.root)
            facts["disk_total_gb"] = round(usage.total / 1024**3, 1)
            facts["disk_free_gb"] = round(usage.free / 1024**3, 1)
            facts["disk_used_pct"] = round(100 * (usage.total - usage.free) / usage.total, 1)
        except OSError:
            pass
        return facts

    def json_file(self, path: Path, default: object = None) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up until the compose file + core package are found.

    The CLI is often invoked from an arbitrary directory (cron, CI, a wrapper in
    /usr/local/bin), so the root cannot be assumed to be the cwd.
    """
    env_root = os.environ.get("ATF_ROOT")
    if env_root and Path(env_root).exists():
        return Path(env_root).resolve()
    here = (start or Path.cwd()).resolve()
    markers = (Path("docker-compose.yml"), Path("api/main.go"))
    for candidate in [here, *here.parents]:
        if all((candidate / m).exists() for m in markers):
            return candidate
    return here


def parse_jsonish(text: str) -> object:
    """Parse a CLI `--set key=value` value into a Python scalar/collection.

    Values arrive as strings but the API wants JSON types: `--set enabled=false`
    must send a boolean, not the string "false".
    """
    raw = text.strip()
    if raw == "":
        return ""
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    if raw.startswith(("{", "[")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def merge_dotted(target: dict, key: str, value: object) -> dict:
    """`a.b.c=1` -> nested dict, so `--set detection.min_length=3` works."""
    parts = key.split(".")
    cursor = target
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value
    return target


def collect_settings(pairs: Iterable[str]) -> dict:
    out: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise CommandFailed(f"--set expects key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        merge_dotted(out, key.strip(), parse_jsonish(value))
    return out
