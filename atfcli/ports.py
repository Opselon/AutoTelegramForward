"""Host port ownership, conflict diagnosis, and self-healing rebinds.

`atf doctor` used to answer "port 8088 is closed" with a guess. That is the
worst kind of health check: the operator still has to work out *who* is holding
the port, whether the container lost its mapping, or whether a firewall is
dropping the traffic — three different problems that share one symptom.

This module turns the symptom into a verdict, and the verdict into an action:

* **who** is listening (docker's proxy for one of our containers, a foreign
  process with its pid and command line, or nobody);
* **why** a host port is closed while the container runs (mapping lost when it
  was recreated by hand, container dead, or nothing bound at all);
* **where to go instead** — the next free port, chosen so it collides with
  neither a running process nor another service in this stack;
* **how to get there** — `apply_rebind` writes the port into `.env` and
  recreates just that service, so the stack converges on a port that works.

Nothing here shells out blindly: every external tool (`ss`, `lsof`,
`docker`) is optional and the module degrades to `/proc`, then to "unknown",
so a minimal container still gets a diagnosis rather than a traceback.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .context import CONTAINERS, SERVICES
from .errors import AtfError

#: Default host ports, matching docker-compose.yml. These are the values the CLI
#: falls back to when `.env` does not override them.
DEFAULT_PORTS: dict[str, int] = {"api": 8080, "web": 8088, "core": 50051, "logger": 50052}
#: Services an operator reaches from outside the host. A closed port on these is
#: a failure; the gRPC ports are container-to-container and informational.
HOST_FACING: tuple[str, ...] = ("api", "web")
#: Container-side port for each service (what the mapping points at).
CONTAINER_PORTS: dict[str, int] = {"api": 8080, "web": 8088, "core": 50051, "logger": 50052}
#: `.env` keys that override the host port of a service.
ENV_PORT_KEYS: dict[str, str] = {
    "api": "ATF_API_PORT",
    "web": "ATF_WEB_PORT",
    "core": "ATF_CORE_PORT",
    "logger": "ATF_LOGGER_PORT",
}
#: Privileged ports need root, so an automatic rebind never lands below this.
LOWEST_AUTO_PORT = 1024
#: How far to walk from a conflicted port before giving up.
AUTO_PORT_SPAN = 64

_PORT_VAR_RE = re.compile(r"\$\{?(ATF_[A-Z0-9_]+PORT)(?::-(\d+))?\}?")
_SS_PID_RE = re.compile(r'"([^"]+)",pid=(\d+)')

#: What a diagnosis concluded. `ok` is the only good one.
VERDICTS = ("ok", "conflict", "unmapped", "closed", "down", "unknown")


@dataclass
class PortOwner:
    """Who is holding a port, as far as this host can tell."""

    port: int
    kind: str = "unknown"  # stack | foreign | unknown
    pid: int | None = None
    process: str = ""
    command: str = ""
    source: str = ""  # which probe answered: ss | lsof | proc | docker

    @property
    def label(self) -> str:
        bits = [self.process or "unknown process"]
        if self.pid:
            bits.append(f"pid {self.pid}")
        if self.source:
            bits.append(f"via {self.source}")
        return ", ".join(bits)

    def to_json(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "kind": self.kind,
            "pid": self.pid,
            "process": self.process,
            "command": self.command,
            "source": self.source,
        }


@dataclass
class Diagnosis:
    """The verdict for one service port, plus the way out of it."""

    service: str
    port: int
    expected: bool = True
    listening: bool = False
    owner: PortOwner | None = None
    container: str = ""
    container_running: bool = False
    published_port: int | None = None
    verdict: str = "unknown"
    detail: str = ""
    fix: str = ""
    alternative: int | None = None
    env_key: str = ""
    fixed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def good(self) -> bool:
        return self.verdict in ("ok", "unknown")

    @property
    def host_facing(self) -> bool:
        return self.service in HOST_FACING

    def to_json(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "port": self.port,
            "listening": self.listening,
            "verdict": self.verdict,
            "container": self.container,
            "container_running": self.container_running,
            "published_port": self.published_port,
            "owner": self.owner.to_json() if self.owner else None,
            "detail": self.detail,
            "fix": self.fix,
            "alternative": self.alternative,
            "env_key": self.env_key,
            "fixed": self.fixed,
            **({"extra": self.extra} if self.extra else {}),
        }


# --------------------------------------------------------------------- ports
def _valid_port(value: str) -> int | None:
    text = (value or "").strip()
    if text.isdigit() and 1 <= int(text) <= 65535:
        return int(text)
    return None


def compose_port_defaults(ctx) -> dict[str, int]:
    """Host ports declared by `docker-compose.yml` (`${ATF_API_PORT:-8080}`).

    Read rather than assumed: the compose file is the thing that actually
    publishes the port, so a rename or a changed default must not leave the CLI
    diagnosing a port nobody uses.
    """
    found: dict[str, int] = {}
    path = ctx.repo.compose_file
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return found
    for match in _PORT_VAR_RE.finditer(text):
        env_key, default = match.group(1), match.group(2)
        for service, key in ENV_PORT_KEYS.items():
            if key == env_key and default:
                found[service] = int(default)
    return found


def expected_ports(ctx) -> dict[str, int]:
    """The host port each service should be answering on, `.env` winning."""
    ports = dict(DEFAULT_PORTS)
    ports.update(compose_port_defaults(ctx))
    for service, key in ENV_PORT_KEYS.items():
        value = ""
        try:
            value = ctx.repo.env(key, "")
        except Exception:  # noqa: BLE001 - a broken .env must not break diagnosis
            value = ""
        parsed = _valid_port(value)
        if parsed:
            ports[service] = parsed
    return ports


def host_port_for(ctx, service: str) -> int:
    return expected_ports(ctx).get(service, DEFAULT_PORTS.get(service, 0))


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """Can this process actually connect? (What the operator cares about.)"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def free_port(start: int, *, host: str = "0.0.0.0", avoid: Iterable[int] = (), span: int = AUTO_PORT_SPAN) -> int | None:
    """The first bindable port at or after `start`.

    `avoid` carries the other services' ports: picking one of those would trade
    a conflict with a stranger for a conflict with ourselves.
    """
    taken = {int(p) for p in avoid}
    for candidate in range(max(LOWEST_AUTO_PORT, int(start)), min(65535, int(start) + span) + 1):
        if candidate in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
        return candidate
    return None


# ------------------------------------------------------------------- probing
def _run(ctx, argv: Sequence[str], *, timeout: float = 5.0):
    try:
        return ctx.repo.run(list(argv), check=False, quiet=True, timeout=timeout)
    except Exception:  # noqa: BLE001 - probing must never raise
        return None


def parse_ss(text: str, port: int) -> PortOwner | None:
    """Parse one `ss -ltnpH` line for `port` (kept public: it is unit-tested)."""
    for line in (text or "").splitlines():
        columns = line.split()
        if len(columns) < 4:
            continue
        local = columns[3]
        if local.rsplit(":", 1)[-1] not in (str(port), f"{port}"):
            continue
        owner = PortOwner(port=port, source="ss")
        found = _SS_PID_RE.search(line)
        if found:
            owner.process = found.group(1)
            owner.pid = int(found.group(2))
        owner.command = line.strip()
        owner.kind = classify(owner.process, owner.command)
        return owner
    return None


def parse_lsof(text: str, port: int) -> PortOwner | None:
    """Parse `lsof -Fpcn` machine-readable output."""
    owner = PortOwner(port=port, source="lsof")
    for line in (text or "").splitlines():
        if line.startswith("p") and line[1:].split("\n")[0].isdigit():
            owner.pid = int(line[1:].strip())
        elif line.startswith("c"):
            owner.process = line[1:].strip()
        elif line.startswith("n"):
            owner.command = owner.command or line[1:].strip()
    if not (owner.pid or owner.process):
        return None
    owner.kind = classify(owner.process, owner.command)
    return owner


def parse_proc_net(text: str, port: int) -> int | None:
    """The socket inode listening on `port`, straight from /proc/net/tcp."""
    for line in (text or "").splitlines()[1:]:
        columns = line.split()
        if len(columns) < 10 or columns[3] != "0A":  # 0A = LISTEN
            continue
        local_port = columns[1].rsplit(":", 1)[-1]
        try:
            if int(local_port, 16) == port:
                return int(columns[9])
        except ValueError:
            continue
    return None


def proc_owner(inode: int, port: int, *, proc_root: Path = Path("/proc")) -> PortOwner | None:
    """Which process owns a socket inode, by walking /proc/<pid>/fd."""
    if not inode:
        return None
    marker = f"socket:[{inode}]"
    try:
        pids = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return None
    for pid_dir in pids:
        fd_dir = pid_dir / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    if fd.is_symlink() and str(fd.readlink()) == marker:
                        name = _read(pid_dir / "comm").strip() or "unknown"
                        cmdline = _read(pid_dir / "cmdline").replace("\x00", " ").strip()
                        owner = PortOwner(port=port, pid=int(pid_dir.name), process=name, command=cmdline, source="proc")
                        owner.kind = classify(owner.process, owner.command)
                        return owner
                except OSError:
                    continue
        except OSError:
            continue
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def classify(process: str, command: str = "") -> str:
    """`docker-proxy`/`dockerd` is our stack; anything else is a foreign holder."""
    text = f"{process} {command}".lower()
    if "docker-proxy" in text or "dockerd" in text or "com.docker" in text:
        return "stack"
    if not text.strip():
        return "unknown"
    return "foreign"


def listeners(ctx, port: int) -> PortOwner | None:
    """Best available answer to "who is listening on this port".

    Tried in order of fidelity: `ss` names the process, `lsof` works on macOS,
    `/proc` works with neither installed. Every probe is optional.
    """
    res = _run(ctx, ["ss", "-ltnpH"])
    if res is not None and res.ok and res.stdout.strip():
        owner = parse_ss(res.stdout, port)
        if owner:
            return owner
    res = _run(ctx, ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpcn"])
    if res is not None and res.ok and res.stdout.strip():
        owner = parse_lsof(res.stdout, port)
        if owner:
            return owner
    for name in ("tcp", "tcp6"):
        inode = parse_proc_net(_read(Path("/proc/net") / name), port)
        if inode:
            owner = proc_owner(inode, port)
            if owner:
                return owner
            return PortOwner(port=port, kind="unknown", source="proc")
    return None


# ------------------------------------------------------------------- docker
def published_ports(ctx) -> dict[int, str]:
    """host port -> container name, for every running container on this host."""
    res = _run(ctx, ["docker", "ps", "--format", "{{.Names}}"])
    if res is None or not res.ok:
        return {}
    mapping: dict[int, str] = {}
    for name in [line.strip() for line in res.stdout.splitlines() if line.strip()]:
        info = container_ports(ctx, name)
        for host_port in info:
            mapping[host_port] = name
    return mapping


def container_ports(ctx, container: str) -> dict[int, int]:
    """host port -> container port, from `docker inspect`."""
    res = _run(ctx, ["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", container])
    if res is None or not res.ok or not res.stdout.strip():
        return {}
    try:
        raw = json.loads(res.stdout.strip())
    except ValueError:
        return {}
    ports: dict[int, int] = {}
    if not isinstance(raw, dict):
        return ports
    for container_port, bindings in raw.items():
        if not bindings:
            continue
        for binding in bindings:
            host_port = _valid_port(str(binding.get("HostPort", "")))
            if host_port:
                ports[host_port] = int(str(container_port).split("/")[0])
    return ports


def container_up(ctx, service: str) -> bool:
    try:
        return ctx.repo.container_running(service)
    except Exception:  # noqa: BLE001
        return False


#: The probe process, parameterised so one script serves every send. Sent as
#: argv after `-c`, so `sys.argv[1]` is the host and `[2]` the port.
_PROBE_SCRIPT = "import socket,sys;socket.create_connection((sys.argv[1],int(sys.argv[2])),2)"

#: Probe commands tried *inside* the container, cheapest and most exact first.
#: `python3` gives a definitive answer (the core/api/web images have it), then
#: busybox `nc` (the web image), then a shell with bash-style /dev/tcp. The Rust
#: logger image ships none of these — which is why the ladder continues with a
#: sibling container instead of declaring the port closed.
IN_CONTAINER_PROBES: tuple[tuple[str, ...], ...] = (
    ("python3", "-c", _PROBE_SCRIPT, "{host}", "{port}"),
    ("python", "-c", _PROBE_SCRIPT, "{host}", "{port}"),
    ("nc", "-z", "-w", "2", "{host}", "{port}"),
    ("busybox", "nc", "-z", "-w", "2", "{host}", "{port}"),
    ("bash", "-c", "exec 3<>/dev/tcp/{host}/{port}"),
    ("sh", "-c", "exec 3<>/dev/tcp/{host}/{port}"),
)

#: Tools any of the probes above can use. A container that has none of them
#: cannot be probed from the inside, and "cannot probe" is not "not listening".
PROBE_TOOLS: tuple[str, ...] = ("python3", "python", "nc", "busybox", "bash")
_PROBE_TOOL_CHECK = (
    "for t in " + " ".join(PROBE_TOOLS) + '; do command -v "$t" >/dev/null 2>&1 && exit 0; done; exit 1'
)

#: `docker exec` exit codes that mean "the probe never ran" (docker refused the
#: call, or the binary is not in the image) rather than "the port is closed".
_EXEC_CANNOT_RUN = (125, 126, 127)


def _inspect(ctx, container: str, template: str) -> str:
    res = _run(ctx, ["docker", "inspect", "-f", template, container])
    if res is None or not res.ok:
        return ""
    return (res.stdout or "").strip()


def container_address(ctx, container: str) -> str:
    """The container's first bridge IP, or "" (Docker Desktop hides these)."""
    line = _inspect(ctx, container, "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}")
    return line.split()[0] if line.split() else ""


def container_networks(ctx, container: str) -> set[str]:
    line = _inspect(ctx, container, "{{range $name, $net := .NetworkSettings.Networks}}{{$name}} {{end}}")
    return set(line.split())


def container_has_tool(ctx, container: str, tool: str, *, timeout: float = 10.0) -> bool:
    res = _run(ctx, ["docker", "exec", container, "sh", "-c", f'command -v {tool} >/dev/null 2>&1'], timeout=timeout)
    return bool(res is not None and res.ok)


def internal_port_probe(ctx, container: str, port: int, *, timeout: float = 15.0) -> bool | None:
    """Does `port` answer *inside* `container`? True, False, or None.

    Three ladders, because the images are not alike:

      1. the container's own bridge address, connected to from here — no tooling
         needed in the image at all. Docker Desktop does not route container
         addresses, so a failure here proves nothing and the ladder continues;
      2. a probe process inside the container (`python3`, busybox `nc`, or a
         shell with /dev/tcp), whichever the image ships;
      3. a probe from a *sibling* container on a shared network — the only way
         to reach a service whose image ships no probe at all (the Rust logger).

    `None` means no probe could be run. Callers must not report that as "nothing
    is listening": that false alarm costs an operator an evening.
    """
    address = container_address(ctx, container)
    if address and port_open(address, port, timeout=min(2.0, timeout)):
        return True  # positive only — an unroutable address is not a negative

    for probe in IN_CONTAINER_PROBES:
        argv = [part.format(host="127.0.0.1", port=int(port)) for part in probe]
        res = _run(ctx, ["docker", "exec", container, *argv], timeout=timeout)
        if res is not None and res.ok:
            return True

    if _any_probe_tool(ctx, container, timeout):
        return False  # a probe really ran inside the image and nothing answered

    peer = _peer_prober(ctx, container)
    if peer:
        res = _run(
            ctx,
            ["docker", "exec", peer, "python3", "-c", _PROBE_SCRIPT, container, str(int(port))],
            timeout=timeout,
        )
        if res is not None and res.ok:
            return True
        if res is not None and res.returncode not in _EXEC_CANNOT_RUN:
            return False  # the probe ran and the connection was refused
    return None


def _any_probe_tool(ctx, container: str, timeout: float) -> bool:
    res = _run(ctx, ["docker", "exec", container, "sh", "-c", _PROBE_TOOL_CHECK], timeout=timeout)
    return bool(res is not None and res.ok)


def _peer_prober(ctx, container: str) -> str:
    """A running sibling on a shared network that has python3, or ""."""
    networks = container_networks(ctx, container)
    for service, sibling in CONTAINERS.items():
        if sibling == container or not container_up(ctx, service):
            continue
        if networks and not (networks & container_networks(ctx, sibling)):
            continue
        if container_has_tool(ctx, sibling, "python3"):
            return sibling
    return ""


def internal_port_open(ctx, container: str, port: int, *, timeout: float = 15.0) -> bool:
    """`internal_port_probe`, collapsed to a yes/no for callers that need one."""
    return internal_port_probe(ctx, container, port, timeout=timeout) is True


# ---------------------------------------------------------------- diagnosis
def diagnose(ctx, *, services: Sequence[str] = SERVICES, ports: dict[str, int] | None = None) -> list[Diagnosis]:
    """One verdict per service port: who holds it, and what to do about it."""
    ports = dict(ports or expected_ports(ctx))
    published = published_ports(ctx)
    return [_diagnose_one(ctx, service, ports, published) for service in services]


def _diagnose_one(ctx, service: str, ports: dict[str, int], published: dict[int, str]) -> Diagnosis:
    port = ports.get(service, 0)
    container = CONTAINERS.get(service, service)
    running = container_up(ctx, service)
    diagnosis = Diagnosis(
        service=service,
        port=port,
        container=container,
        container_running=running,
        env_key=ENV_PORT_KEYS.get(service, ""),
    )
    mapped = container_ports(ctx, container) if running else {}
    diagnosis.published_port = next(
        (host for host, inner in mapped.items() if inner == CONTAINER_PORTS.get(service, inner)),
        None,
    )
    if service not in HOST_FACING:
        # gRPC ports are container-to-container and normally not published at
        # all, so the host-side test would report a healthy service as broken.
        # Ask the container whether its own port answers instead.
        if not running:
            diagnosis.verdict = "down"
            diagnosis.detail = f"{container} is not running (internal port {port})"
            diagnosis.fix = f"atf up {service}"
            return diagnosis
        listening = internal_port_probe(ctx, container, port)
        diagnosis.listening = listening is True
        if listening is True:
            diagnosis.verdict = "ok"
            diagnosis.detail = f"{port} answering inside {container} (container-only)"
        elif listening is None:
            # The image ships no probe tool: say that, instead of reporting a
            # healthy service as silent (the Rust logger has no python/nc/sh
            # /dev/tcp, and guessing "closed" there is simply wrong).
            diagnosis.verdict = "unknown"
            diagnosis.detail = (
                f"{container} is up, but its image has no probe tool "
                f"(python3/nc/bash), so {port} could not be verified from here"
            )
        else:
            diagnosis.verdict = "closed"
            diagnosis.detail = f"{container} is up but nothing answers on {port} inside it"
            diagnosis.fix = f"atf docker logs {service} --tail 50  (then: atf system restart {service})"
        return diagnosis
    diagnosis.listening = port_open("127.0.0.1", port)
    if diagnosis.listening:
        owner = listeners(ctx, port)
        diagnosis.owner = owner
        if owner is None:
            # Answers, but the holder is invisible (unprivileged `ss`, a
            # namespace). A port that answers is not a failure.
            diagnosis.verdict = "unknown"
            diagnosis.detail = f"{port} is answering but the holder could not be identified"
            return diagnosis
        if owner.kind == "stack" or port in published:
            owner.kind = "stack"
            diagnosis.verdict = "ok"
            diagnosis.detail = (
                f"{port} served by {published.get(port, container)} ({owner.label})"
                if port in published
                else f"{port} served by docker ({owner.label})"
            )
            return diagnosis
        if owner.kind == "unknown":
            diagnosis.verdict = "unknown"
            diagnosis.detail = f"{port} is answering ({owner.label})"
            return diagnosis
        # A stranger holds the port the stack wants.
        alt = free_port(port + 1, avoid=[p for p in ports.values() if p != port])
        diagnosis.verdict = "conflict"
        diagnosis.alternative = alt
        diagnosis.detail = f"{port} held by a process outside this stack: {owner.label}"
        diagnosis.fix = (
            f"publish {service} on {alt} — `atf net port fix {service}`"
            if alt
            else f"free port {port}, or stop `{owner.process}` and retry"
        )
        return diagnosis
    # Nothing is listening: why not?
    if not running:
        diagnosis.verdict = "down"
        diagnosis.detail = f"{port} closed and {container} is not running"
        diagnosis.fix = "atf up " + service if diagnosis.host_facing else "atf up " + service + "  # internal-only port"
        return diagnosis
    if not mapped:
        diagnosis.verdict = "unmapped"
        diagnosis.detail = f"{container} is running but publishes no host port (recreated without -p?)"
        diagnosis.fix = f"atf system restart {service}  # recreates it from docker-compose.yml"
        diagnosis.alternative = free_port(port + 1, avoid=[p for p in ports.values() if p != port])
        return diagnosis
    if port not in mapped:
        diagnosis.verdict = "conflict"
        diagnosis.alternative = free_port(port + 1, avoid=[p for p in ports.values() if p != port])
        diagnosis.detail = f"{container} publishes {sorted(mapped)}, but the expected host port {port} is not among them"
        diagnosis.fix = (
            f"rebind {service} to a free port — `atf net port fix {service}`"
            if diagnosis.alternative
            else f"check the port mapping for {container} in docker-compose.yml"
        )
        return diagnosis
    diagnosis.verdict = "closed"
    diagnosis.detail = f"{container} publishes {port} but nothing answers on it"
    diagnosis.fix = f"atf docker logs {service} --tail 50  (then: atf system restart {service})"
    return diagnosis


def alternatives(diagnoses: Sequence[Diagnosis]) -> list[Diagnosis]:
    return [d for d in diagnoses if d.alternative and not d.fixed]


def failing(diagnoses: Sequence[Diagnosis]) -> list[Diagnosis]:
    return [d for d in diagnoses if d.host_facing and d.verdict not in ("ok", "unknown")]


# ------------------------------------------------------------- self-healing
def put_env_value(text: str, key: str, value: str) -> str:
    """Replace `key` in an env file body in place, or append it.

    Comments and ordering survive, because an operator reads this file. Shared
    with the setup wizard so both write `.env` the same way.
    """
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.partition("=")[0].strip() == key:
            lines[index] = f"{key}={value}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


@dataclass
class Rebind:
    service: str
    current: int
    target: int
    env_key: str
    reason: str = ""
    #: Who holds the port we are moving away from ("", "free", or "nginx, pid 12").
    holder: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "current": self.current,
            "target": self.target,
            "env_key": self.env_key,
            "reason": self.reason,
            "holder": self.holder,
        }


def rebind_plan(ctx, service: str, *, target: int | None = None) -> Rebind:
    """Where this service should listen instead: the port asked for, or the
    next free one that no other service wants."""
    ports = expected_ports(ctx)
    current = ports.get(service, 0)
    env_key = ENV_PORT_KEYS.get(service, "")
    if not env_key:
        raise AtfError(f"{service} has no configurable host port")
    if target:
        if not _valid_port(str(target)) or int(target) < LOWEST_AUTO_PORT:
            raise AtfError(f"{target} is not a usable host port")
        taken = [p for name, p in ports.items() if name != service and p == int(target)]
        if taken:
            raise AtfError(f"port {target} is already assigned to another service in this stack")
        owner = listeners(ctx, int(target))
        return Rebind(service, current, int(target), env_key, reason="operator choice",
                      holder=owner.label if owner else "free")
    if current and port_open("127.0.0.1", current):
        owner = listeners(ctx, current)
        if owner and owner.kind == "stack":
            raise AtfError(
                f"{service} is already answering on {current}",
                hint="nothing to rebind — pass `--port <port>` to move it anyway",
            )
        alt = free_port(current + 1, avoid=[p for name, p in ports.items() if name != service])
        if alt is None:
            raise AtfError(f"no free port found near {current}")
        why = f"{current} is held by {owner.label if owner else 'another process'}"
        return Rebind(service, current, alt, env_key, reason=why,
                      holder=owner.label if owner else "another process")
    base = current or DEFAULT_PORTS.get(service, 8080)
    avoid = [p for name, p in ports.items() if name != service]
    alt = free_port(base, avoid=avoid)
    if alt is None:
        raise AtfError(f"no free host port found near {base} for {service}")
    return Rebind(service, current, alt, env_key, reason=f"{base} is not free", holder="free")


def apply_rebind(ctx, rebind: Rebind, *, recreate: bool = True, wait: float = 20.0) -> str:
    """Write the new port into `.env` and bring that one service back up.

    Only the affected service is recreated: an upgrade is not required to move a
    port, and restarting the whole stack to do it would drop forwarding for no
    reason.
    """
    from .context import COMPOSE_NETWORK, NETWORK_ALIASES

    env_path = ctx.repo.path(".env")
    if ctx.repo.dry_run:
        return f"[dry-run] would set {rebind.env_key}={rebind.target} in .env and recreate {rebind.service}"
    text = ""
    if env_path.exists():
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AtfError(f"cannot read {env_path}: {exc}") from exc
    ctx.repo.write_text(env_path, put_env_value(text, rebind.env_key, str(rebind.target)))
    detail = f"{rebind.env_key}={rebind.target} written to .env"
    if not recreate:
        return detail + " (restart the stack to apply it)"

    container = CONTAINERS.get(rebind.service, rebind.service)
    ctx.repo.run(["docker", "rm", "-f", container], check=False, quiet=True)
    res = ctx.repo.run(
        ["docker", "compose", "up", "-d", "--no-deps", rebind.service],
        check=False,
        quiet=True,
    )
    if not res.ok:
        raise AtfError(
            f"recreating {rebind.service} failed: {(res.text or '').strip().splitlines()[-1] if res.text.strip() else res.returncode}",
            hint=f"run `atf docker up {rebind.service}` and check `atf docker logs {rebind.service}`",
        )
    ctx.repo.run(
        ["docker", "network", "connect", "--alias", NETWORK_ALIASES.get(rebind.service, rebind.service), COMPOSE_NETWORK, container],
        check=False,
        quiet=True,
    )
    _wait_for_port(rebind.target, seconds=wait)
    return detail + f" and recreated {container}"


def _wait_for_port(port: int, *, seconds: float = 20.0, host: str = "127.0.0.1") -> bool:
    import time

    deadline = time.time() + max(1.0, seconds)
    while time.time() < deadline:
        if port_open(host, port):
            return True
        time.sleep(0.5)
    return port_open(host, port)


def heal(ctx, diagnoses: Sequence[Diagnosis], *, rebind: bool = False) -> list[str]:
    """Repair what is safe to repair, and say what was done.

    Restarting a dead container and recreating one whose mapping was lost are
    both safe: the compose file is the source of truth and the service comes
    back where it was. Moving a *port* changes the public URL, so it only
    happens when the caller asks for it (`--rebind-ports`).
    """
    done: list[str] = []
    for diagnosis in diagnoses:
        if diagnosis.verdict == "down" and diagnosis.container_running is False:
            ctx.repo.run(["docker", "start", diagnosis.container], check=False, quiet=True)
            if _wait_for_port(diagnosis.port, seconds=8.0):
                diagnosis.fixed = True
                diagnosis.verdict = "ok"
                diagnosis.detail = f"{diagnosis.port} answering after starting {diagnosis.container}"
                done.append(f"started {diagnosis.container}")
                continue
            done.append(f"started {diagnosis.container} (still not answering)")
            continue
        if diagnosis.verdict == "unmapped":
            ctx.repo.run(["docker", "rm", "-f", diagnosis.container], check=False, quiet=True)
            res = ctx.repo.run(
                ["docker", "compose", "up", "-d", "--no-deps", diagnosis.service], check=False, quiet=True
            )
            if res.ok and _wait_for_port(diagnosis.port, seconds=15.0):
                diagnosis.fixed = True
                diagnosis.verdict = "ok"
                diagnosis.detail = f"{diagnosis.container} recreated with its {diagnosis.port} mapping"
                done.append(f"recreated {diagnosis.container} with its port mapping")
            else:
                done.append(f"recreating {diagnosis.container} did not restore {diagnosis.port}")
            continue
        if diagnosis.verdict == "conflict" and rebind and diagnosis.alternative:
            try:
                plan = rebind_plan(ctx, diagnosis.service, target=diagnosis.alternative)
                detail = apply_rebind(ctx, plan)
            except AtfError as exc:
                done.append(f"could not rebind {diagnosis.service}: {exc.message}")
                continue
            diagnosis.fixed = True
            diagnosis.verdict = "ok"
            diagnosis.port = plan.target
            diagnosis.detail = f"moved {diagnosis.service} to {plan.target} ({plan.reason}) — {detail}"
            done.append(f"{diagnosis.service}: {plan.current} → {plan.target}")
    return done


# ----------------------------------------------------------------- firewall
def firewall_findings(ctx, ports: Iterable[int]) -> list[tuple[str, str]]:
    """Host-firewall rules that would drop traffic to these ports.

    A port can be perfectly bound and still unreachable from outside; on the
    hosts this stack runs on that is almost always `ufw` or a raw `iptables`
    DROP. Both are read-only probes — the CLI never edits firewall rules behind
    the operator's back.
    """
    wanted = sorted({int(p) for p in ports})
    findings: list[tuple[str, str]] = []
    res = _run(ctx, ["ufw", "status"], timeout=5.0)
    if res is not None and res.ok and "Status: active" in (res.stdout or ""):
        allowed = set()
        for line in res.stdout.splitlines():
            for match in re.finditer(r"(\d{2,5})(?:/tcp)?\s+ALLOW", line):
                allowed.add(int(match.group(1)))
        blocked = [p for p in wanted if p not in allowed and not any(lo <= p <= hi for lo, hi in _ufw_ranges(res.stdout))]
        if blocked:
            findings.append(
                (
                    f"ufw is active and does not allow {blocked}",
                    "allow them: " + " && ".join(f"ufw allow {p}/tcp" for p in blocked),
                )
            )
        return findings
    res = _run(ctx, ["iptables", "-S"], timeout=5.0)
    if res is not None and res.ok and res.stdout.strip():
        dropped = [p for p in wanted if re.search(rf"--dport\s+{p}\b.*(DROP|REJECT)", res.stdout)]
        if dropped:
            findings.append(
                (
                    f"an iptables rule drops {dropped}",
                    "review it with `iptables -S` — the CLI will not edit firewall rules",
                )
            )
    return findings


def _ufw_ranges(text: str) -> list[tuple[int, int]]:
    ranges = []
    for low, high in re.findall(r"(\d{2,5})(?:/tcp)?\s+ALLOW.*?(\d{2,5})", text or ""):
        try:
            ranges.append((int(low), int(high)))
        except ValueError:
            continue
    return ranges


# ------------------------------------------------------------------ render
_MARKS = {"ok": "OK", "conflict": "CONFLICT", "unmapped": "UNMAPPED", "closed": "CLOSED", "down": "DOWN", "unknown": "?"}


def render(ctx, diagnoses: Sequence[Diagnosis]) -> None:
    """Colourised port table, plus the fix line for anything wrong."""
    from .ui import table_with_marks

    if ctx.out.json_mode or not diagnoses:
        return
    ctx.out.title("Host ports")
    rows = []
    for diagnosis in diagnoses:
        owner = diagnosis.owner.label if diagnosis.owner else "—"
        rows.append(
            [
                f"{diagnosis.service} — {diagnosis.container}",
                str(diagnosis.port),
                _MARKS.get(diagnosis.verdict, "?"),
                "yes" if diagnosis.listening else "no",
                owner,
                str(diagnosis.alternative or "—"),
            ]
        )
    table_with_marks(ctx.out, ("SERVICE", "PORT", "STATUS", "OPEN", "HELD BY", "ALT"), rows, 2)
    for diagnosis in diagnoses:
        if diagnosis.verdict in ("ok", "unknown"):
            continue
        ctx.out.hint(f"{diagnosis.service}: {diagnosis.detail}")
        if diagnosis.fix:
            ctx.out.hint(f"  → {diagnosis.fix}")


def to_json(diagnoses: Sequence[Diagnosis], *, firewall: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
    return {
        "ports": [d.to_json() for d in diagnoses],
        "open": sum(1 for d in diagnoses if d.listening),
        "problems": [d.service for d in failing(diagnoses)],
        "fixed": [d.service for d in diagnoses if d.fixed],
        "firewall": [{"detail": detail, "fix": fix} for detail, fix in firewall],
    }
