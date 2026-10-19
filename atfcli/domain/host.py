"""The machine this CLI runs against: services, host ports, revisions.

The CLI has two halves. One talks to the dashboard API; the other one *is* the
operator's hands on the host — it inspects containers, binds ports, reads git.
Both halves need the same facts about what the stack is, so those facts live
here, in the domain, instead of being copied into the compose file parser, the
doctor checks and the port diagnosis separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------- services
#: Services this stack runs, in startup order. One list, so compose, deploy,
#: doctor and the validators cannot disagree about what "everything" means.
SERVICES: tuple[str, ...] = ("logger", "core", "api", "web")

#: Container names the compose file creates.
CONTAINERS: dict[str, str] = {
    "logger": "atf-logger",
    "core": "atf-core",
    "api": "atf-api",
    "web": "atf-web",
}

#: Compose network aliases. A container recreated with `docker run` (rather than
#: `docker compose up`) loses these, and then `http://api:8080` stops resolving
#: inside the network while the container still looks healthy from the host.
NETWORK_ALIASES: dict[str, str] = {
    "core": "core",
    "api": "api",
    "web": "web",
    "logger": "logger",
}

COMPOSE_NETWORK = "autotelegramforward_atf-net"
AI_NETWORK = "9router_default"

#: Default host ports, matching docker-compose.yml. `.env` overrides them.
DEFAULT_PORTS: dict[str, int] = {"api": 8080, "web": 8088, "core": 50051, "logger": 50052}

#: Services an operator reaches from outside the host. A closed port on these is
#: a failure; the gRPC ports are container-to-container and informational.
HOST_FACING: tuple[str, ...] = ("api", "web")

#: Ports below this need root (or a privileged container) to bind, so offering
#: one as "the next free port" would be advice that cannot be followed.
LOWEST_AUTO_PORT = 1024


def container_name(service: str) -> str:
    """Accept `core`, `atf-core`, `CORE` and answer `atf-core`."""
    key = service.strip().lower()
    if key.startswith("atf-"):
        key = key[4:]
    return CONTAINERS.get(key, service)


def service_name(service: str) -> str:
    """Accept `atf-core`, `CORE` and answer `core`."""
    key = service.strip().lower()
    return key[4:] if key.startswith("atf-") else key


# ------------------------------------------------------------------------ ports
@dataclass(frozen=True)
class PortOwner:
    """Who holds a host port, and how we know."""

    port: int
    kind: str = "unknown"  # stack | foreign | unknown
    pid: int = 0
    process: str = ""
    source: str = ""  # ss | lsof | proc | docker
    command: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "pid": self.pid,
            "process": self.process,
            "source": self.source,
        }


@dataclass
class Diagnosis:
    """The verdict for one service's host port.

    ``listening`` is "something answers on 127.0.0.1:<port>"; the ``verdict`` is
    the part an operator acts on:

    ``ok``        our container holds the port and answers
    ``closed``    nothing answers where this stack expects to answer
    ``conflict``  somebody else holds the port
    ``lost``      the container runs but its published port vanished
    """

    service: str
    port: int
    listening: bool
    verdict: str
    container: str = ""
    running: bool = False
    owner: PortOwner | None = None
    fix: str = ""
    alternative: int | None = None
    detail: str = ""

    @property
    def host_facing(self) -> bool:
        return self.service in HOST_FACING

    @property
    def failed(self) -> bool:
        """A failure is a closed/conflicted *host-facing* port, not a gRPC one."""
        return self.host_facing and self.verdict in ("closed", "conflict", "lost")

    def to_json(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "port": self.port,
            "container": self.container,
            "listening": self.listening,
            "verdict": self.verdict,
            "host_facing": self.host_facing,
            "owner": self.owner.to_json() if self.owner else None,
            "fix": self.fix,
            "alternative": self.alternative,
            "detail": self.detail,
        }


# -------------------------------------------------------------------- revisions
@dataclass(frozen=True)
class Revision:
    """One point in the stack's history: a commit, a version, or both."""

    sha: str = ""
    short: str = ""
    version: str = ""
    branch: str = ""
    subject: str = ""
    date: str = ""

    @property
    def known(self) -> bool:
        return bool(self.sha or self.version)

    @property
    def label(self) -> str:
        if self.version and self.short:
            return f"v{self.version} ({self.short})"
        if self.version:
            return f"v{self.version}"
        return self.short or "unknown"

    def to_json(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "short": self.short,
            "version": self.version,
            "branch": self.branch,
            "subject": self.subject,
            "date": self.date,
        }


@dataclass
class UpgradeCheck:
    """What an operator needs to decide whether to upgrade."""

    current: Revision = field(default_factory=Revision)
    target: Revision = field(default_factory=Revision)
    behind: int = 0
    ahead: int = 0
    newer_version: bool = False
    dirty: Sequence[str] = ()
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    commits: list[dict[str, str]] = field(default_factory=list)

    @property
    def up_to_date(self) -> bool:
        return not self.behind and not self.newer_version and not self.blockers

    @property
    def can_apply(self) -> bool:
        return not self.blockers

    def to_json(self) -> dict[str, Any]:
        return {
            "current": self.current.to_json(),
            "target": self.target.to_json(),
            "behind": self.behind,
            "ahead": self.ahead,
            "newer_version": self.newer_version,
            "dirty": list(self.dirty),
            "blockers": list(self.blockers),
            "up_to_date": self.up_to_date,
            "can_apply": self.can_apply,
            "notes": list(self.notes),
            "commits": list(self.commits),
        }


@dataclass
class UpgradeStep:
    name: str
    status: str  # ok | fail | skip
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class UpgradeRun:
    """One attempt at moving the stack forward (or back), with its steps."""

    verb: str
    from_: Revision = field(default_factory=Revision)
    to: Revision = field(default_factory=Revision)
    steps: list[UpgradeStep] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    started: float = 0.0
    duration_s: float = 0.0

    def add(self, name: str, status: str, detail: str = "") -> UpgradeStep:
        step = UpgradeStep(name=name, status=status, detail=detail)
        self.steps.append(step)
        return step

    @property
    def ok(self) -> bool:
        return not any(step.status == "fail" for step in self.steps) and not self.findings

    @property
    def failures(self) -> list[UpgradeStep]:
        return [step for step in self.steps if step.status == "fail"]

    def to_json(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "ok": self.ok,
            "from": self.from_.to_json(),
            "to": self.to.to_json(),
            "steps": [step.to_json() for step in self.steps],
            "findings": list(self.findings),
            "duration_s": round(self.duration_s, 2),
        }


def semver_tuple(text: str) -> tuple[int, int, int] | None:
    """`v2.1.0` -> (2, 1, 0); unparseable -> None (never ordered by guess)."""
    raw = (text or "").strip().lstrip("vV")
    if not raw:
        return None
    core = raw.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts] + [0] * (3 - len(parts))
    return numbers[0], numbers[1], numbers[2]


def version_is_newer(candidate: str, base: str) -> bool:
    """A conservative comparison: an unparseable version is never "newer"."""
    left, right = semver_tuple(candidate), semver_tuple(base)
    if left is None or right is None:
        return False
    return left > right


def to_json(items: Sequence[Diagnosis]) -> dict[str, Any]:
    """The shape `atf net port status --json` promises."""
    return {
        "open": sum(1 for item in items if item.listening),
        "problems": [item.service for item in items if item.failed],
        "ports": [item.to_json() for item in items],
    }


def from_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Shallow copy helper used where a payload must not be mutated."""
    return {key: value for key, value in payload.items()}
