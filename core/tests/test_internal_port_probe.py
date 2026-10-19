"""Ports that answer can still be reported as silent. These tests pin the fix.

Defect being pinned (found by running `atf doctor` against the live stack): the
docker-only `logger` service was reported as

    logger:50052 silent inside its container

while the container was healthy and logging

    listening on 0.0.0.0:50052

The check probed with `docker exec <c> python3 -c …` and, failing that, with
`sh -c 'exec 3<>/dev/tcp/…'`. The Rust logger image ships neither a python nor a
shell with /dev/tcp, so *every* probe failed and a healthy service was declared
broken. "I could not ask" is not the same answer as "nothing answered", and the
port check now says so: the ladder tries the bridge address, then every probe the
image might carry, then a sibling container on the same network, and only then
reports `unknown` instead of manufacturing a failure.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from atfcli import ports as portmap
from atfcli.console import Console
from atfcli.context import find_repo_root

REPO_ROOT = find_repo_root()


class Result:
    """The slice of `CommandResult` the probes read."""

    def __init__(self, argv: list[str], *, ok: bool, returncode: int = 0, stdout: str = "") -> None:
        self.argv = argv
        self.ok = ok
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class FakeRepo:
    """Answers only the docker calls the ladder makes, and records all of them.

    Note: `ports._run` swallows every exception ("probing must never raise"), so
    a mistake inside here looks like a probe that never answered, not like a
    traceback. Keep the branches explicit.
    """

    def __init__(
        self,
        *,
        address: str = "",
        networks: dict[str, str] | None = None,
        tools: dict[str, set[str]] | None = None,
        listening: set[tuple[str, int]] | None = None,
        peer_refuses: bool = False,
        running: set[str] | None = None,
        published: str = "{}",
    ) -> None:
        self.address = address
        self.networks = networks or {}
        self.tools = tools or {}
        self.listening = listening or set()
        self.peer_refuses = peer_refuses
        self.running = running if running is not None else set(portmap.CONTAINERS)
        self.published = published
        self.calls: list[list[str]] = []
        self.dry_run = False

    # -- the surface ports.py touches
    def container_running(self, service: str) -> bool:
        return service in self.running

    def path(self, *parts: str) -> Path:
        return REPO_ROOT.joinpath(*parts)

    def run(self, argv, **kwargs) -> Result:  # noqa: ANN003 - mirrors RepoContext.run
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[:2] != ["docker", "exec"] and argv[:2] != ["docker", "inspect"] and argv[:1] != ["docker"]:
            return Result(argv, ok=False, returncode=127)
        if argv[:2] == ["docker", "inspect"]:
            template, container = argv[3], argv[4]
            if "Ports" in template:
                return Result(argv, ok=True, stdout=self.published)
            return Result(argv, ok=bool(container), stdout=self._inspect_value(template, container))
        if argv[:2] == ["docker", "exec"]:
            return self._exec(argv)
        return Result(argv, ok=False, returncode=1)

    def _inspect_value(self, template: str, container: str) -> str:
        if "NetworkSettings.Networks}}" in template and "IPAddress" in template:
            return self.address if container in portmap.CONTAINERS.values() else ""
        if "NetworkSettings.Networks" in template:
            net = self.networks.get(container, "")
            return f"{net} " if net else ""
        return ""

    def _answers(self, container: str, host: str, port: str) -> bool:
        """`127.0.0.1` inside the container means "the container itself"."""
        target = container if host in ("127.0.0.1", "localhost") else host
        return (target, int(port)) in self.listening

    def _has(self, container: str, *tools: str) -> bool:
        available = self.tools.get(container, set())
        return any(tool in available for tool in tools)

    def _exec(self, argv: list[str]) -> Result:
        """Answer like docker would: a missing binary is exit 127, not a refusal."""
        container, rest = argv[2], argv[3:]
        if rest[:2] == ["sh", "-c"] and "for t in" in rest[2]:
            # the capability probe itself: "does this image carry any probe tool"
            wanted = re.search(r"for t in ([^;]+);", rest[2])
            tools = wanted.group(1).split() if wanted else []
            available = self.tools.get(container, set())
            hit = any(tool in available for tool in tools)
            return Result(argv, ok=hit, returncode=0 if hit else 1)
        if rest[:2] == ["sh", "-c"] and "command -v" in rest[2]:
            tool = rest[2].split("command -v", 1)[1].split()[0].strip("'\"")
            hit = self._has(container, tool)
            return Result(argv, ok=hit, returncode=0 if hit else 1)
        if not rest:
            return Result(argv, ok=False, returncode=127)
        if rest[0] in ("python3", "python"):
            if not self._has(container, rest[0]):
                return Result(argv, ok=False, returncode=127)
            hit = self._answers(container, rest[-2], rest[-1])
            return Result(argv, ok=hit, returncode=0 if hit else 1)
        if rest[0] in ("nc", "busybox"):
            if not self._has(container, rest[0]):
                return Result(argv, ok=False, returncode=127)
            hit = self._answers(container, rest[-2], rest[-1])
            return Result(argv, ok=hit, returncode=0 if hit else 1)
        if rest[0] in ("bash", "sh") and len(rest) > 2 and "/dev/tcp/" in rest[2]:
            if not self._has(container, rest[0]):
                return Result(argv, ok=False, returncode=127)
            host, _, port = rest[2].split("/dev/tcp/")[1].partition("/")
            hit = self._answers(container, host, port)
            if not hit and self.peer_refuses:
                return Result(argv, ok=False, returncode=1)
            return Result(argv, ok=hit, returncode=0 if hit else 1)
        return Result(argv, ok=False, returncode=127)


def _ctx(repo: FakeRepo):
    sink = io.StringIO()
    console = Console(stream=sink, color=False)
    return SimpleNamespace(repo=repo, out=console, args=None)


LOGGER = portmap.CONTAINERS["logger"]
CORE = portmap.CONTAINERS["core"]


# --------------------------------------------------------------- the ladder
def test_the_bridge_address_is_asked_first(monkeypatch):
    """A reachable container address answers without any tool inside it."""
    monkeypatch.setattr(portmap, "port_open", lambda host, port, timeout=1.5: host == "172.18.0.5" and port == 50052)
    repo = FakeRepo(address="172.18.0.5")

    assert portmap.internal_port_probe(_ctx(repo), LOGGER, 50052) is True
    assert not [call for call in repo.calls if call[:2] == ["docker", "exec"]], "no exec needed"


def test_an_unroutable_address_is_not_a_negative(monkeypatch):
    """Docker Desktop hides bridge IPs: a failed address probe proves nothing."""
    monkeypatch.setattr(portmap, "port_open", lambda *a, **k: False)
    repo = FakeRepo(address="172.18.0.5", tools={LOGGER: {"python3"}}, listening={(LOGGER, 50052)})

    assert portmap.internal_port_probe(_ctx(repo), LOGGER, 50052) is True


def test_an_image_with_a_probe_reports_a_closed_port_as_closed():
    repo = FakeRepo(tools={LOGGER: {"python3"}})  # has a probe, nothing listening

    assert portmap.internal_port_probe(_ctx(repo), LOGGER, 50052) is False


def test_the_logger_image_is_probed_through_a_sibling(monkeypatch):
    """No probe tool inside: ask from a container that shares the network."""
    monkeypatch.setattr(portmap, "port_open", lambda *a, **k: False)
    repo = FakeRepo(
        tools={CORE: {"python3", "sh"}},  # logger has none
        networks={LOGGER: "atf-net", CORE: "atf-net"},
        listening={(LOGGER, 50052)},
    )

    assert portmap.internal_port_probe(_ctx(repo), LOGGER, 50052) is True
    assert any(call[:3] == ["docker", "exec", CORE] for call in repo.calls), "the sibling did the asking"


def test_a_sibling_on_another_network_is_not_used(monkeypatch):
    """The hardened stack is a different network: its core cannot see the logger."""
    monkeypatch.setattr(portmap, "port_open", lambda *a, **k: False)
    repo = FakeRepo(
        tools={CORE: {"python3"}},
        networks={LOGGER: "atf-net", CORE: "atf-hardened-net"},
        running={"core"},
    )

    assert portmap.internal_port_probe(_ctx(repo), LOGGER, 50052) is None


def test_no_probe_anywhere_is_unknown_not_closed(monkeypatch):
    """The reported defect: healthy port, no way to ask, verdict was 'silent'."""
    monkeypatch.setattr(portmap, "port_open", lambda *a, **k: False)
    repo = FakeRepo(tools={}, networks={LOGGER: "atf-net"})

    assert portmap.internal_port_probe(_ctx(repo), LOGGER, 50052) is None
    assert portmap.internal_port_open(_ctx(repo), LOGGER, 50052) is False, "the boolean wrapper stays conservative"


def test_a_refused_sibling_probe_is_a_real_no(monkeypatch):
    monkeypatch.setattr(portmap, "port_open", lambda *a, **k: False)
    repo = FakeRepo(tools={CORE: {"python3"}}, networks={LOGGER: "atf-net", CORE: "atf-net"})

    assert portmap.internal_port_probe(_ctx(repo), LOGGER, 50052) is False


# ------------------------------------------------------------- the diagnosis
def _diagnose(repo: FakeRepo, service: str):
    return portmap.diagnose(_ctx(repo), services=[service], ports={service: portmap.CONTAINER_PORTS[service]})


def test_the_diagnosis_says_it_could_not_verify_instead_of_silent(monkeypatch):
    monkeypatch.setattr(portmap, "port_open", lambda *a, **k: False)
    diagnosis = _diagnose(FakeRepo(tools={}), "logger")[0]

    assert diagnosis.verdict == "unknown"
    assert "could not be verified" in diagnosis.detail
    assert diagnosis.good, "an unverifiable internal port must not fail the doctor"
    assert not diagnosis.fix, "and it must not send the operator off to restart a healthy service"


def test_the_diagnosis_still_calls_a_provable_closure_closed():
    diagnosis = _diagnose(FakeRepo(tools={LOGGER: {"python3"}}), "logger")[0]

    assert diagnosis.verdict == "closed"
    assert not diagnosis.good
    assert diagnosis.fix, "a real closure needs the restart hint"


def test_a_reachable_logger_is_ok(monkeypatch):
    monkeypatch.setattr(portmap, "port_open", lambda *a, **k: False)
    diagnosis = _diagnose(FakeRepo(tools={LOGGER: {"python3"}}, listening={(LOGGER, 50052)}), "logger")[0]

    assert diagnosis.verdict == "ok"
    assert diagnosis.listening is True


@pytest.mark.parametrize("tool", ["python3", "nc", "busybox", "bash"])
def test_any_of_the_shipped_probes_is_enough(tool):
    """Each probe in the ladder is exercised: the images differ in what they carry."""
    repo = FakeRepo(tools={LOGGER: {tool}}, listening={(LOGGER, 50052)})

    assert portmap.internal_port_probe(_ctx(repo), LOGGER, 50052) is True


def test_the_capability_probe_reads_its_own_output():
    """`command -v python3 nc …` must be read, not guessed from the exit code alone."""
    repo = FakeRepo(tools={LOGGER: {"nc"}})
    assert portmap.container_has_tool(_ctx(repo), LOGGER, "nc") is True
    assert portmap.container_has_tool(_ctx(repo), LOGGER, "python3") is False
