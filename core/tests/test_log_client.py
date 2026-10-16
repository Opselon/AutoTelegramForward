"""LogClient delivery tests — the producer side of the logging contract.

These use a fake stub (no gRPC, no server) because the properties under test are
about OWNERSHIP of a record across a failure, not about the wire. The defect
being pinned: `_pump` used to dequeue a record and drop it whenever the RPC
raised, so a Logger outage destroyed exactly the evidence of the outage.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.observability.log_client import LogClient  # noqa: E402


class FakeStub:
    """Stands in for the gRPC stub: records what arrived, fails on demand."""

    def __init__(self, fail_on: tuple[int, ...] = ()) -> None:
        self.fail_on = set(fail_on)
        self.calls = 0
        self.delivered: list[str] = []

    async def Log(self, request):  # noqa: N802 - mirrors the generated stub
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("logger unavailable")
        self.delivered.append(request.entry.message)
        return SimpleNamespace(success=True, message="logged")


class AlwaysFailStub:
    def __init__(self) -> None:
        self.calls = 0

    async def Log(self, request):  # noqa: N802
        self.calls += 1
        raise RuntimeError("logger unavailable")


def _client(stub, queue_size: int = 4096) -> LogClient:
    client = LogClient("127.0.0.1:1", max_queue_size=queue_size)
    client.connect = lambda: None       # never open a real channel in a unit test
    client._stub = stub
    client._backoff = lambda n: 0.005   # keep the retry loop fast and deterministic
    return client


def _run(coro):
    """Run a coroutine on a loop that STAYS current for this thread.

    `asyncio.run` clears the thread's event loop on the way out, which breaks any
    other module that grabs a loop at import time. Keeping one loop is also
    closer to how these clients actually live (inside a running service).
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


async def _until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _fill(client: LogClient, count: int, prefix: str = "m") -> None:
    for i in range(1, count + 1):
        client.log("ERROR", "core", "forward", f"{prefix}{i}")


def test_a_failed_rpc_does_not_lose_the_record():
    """A failure must keep the record: same record retried, nothing dropped."""

    async def go():
        stub = FakeStub(fail_on=(1, 2))
        client = _client(stub)
        _fill(client, 3)
        await client.start()
        assert await _until(lambda: len(stub.delivered) == 3), stub.delivered
        counters = client.counters()
        assert stub.delivered == ["m1", "m2", "m3"], stub.delivered
        assert counters["sent"] == 3
        assert counters["failed"] >= 2
        assert counters["dropped"] == 0
        assert counters["pending"] == 0
        await client.close()

    _run(go())


def test_a_mid_burst_failure_keeps_order_and_loses_nothing():
    """A failure in the middle of a burst must not reorder or drop records."""

    async def go():
        stub = FakeStub(fail_on=(3, 7))
        client = _client(stub)
        _fill(client, 8, prefix="burst")
        await client.start()
        assert await _until(lambda: len(stub.delivered) == 8), stub.delivered
        assert stub.delivered == [f"burst{i}" for i in range(1, 9)], stub.delivered
        assert client.counters()["dropped"] == 0
        await client.close()

    _run(go())


def test_records_logged_before_start_are_delivered():
    """The queue is the buffer: logging before start() must not vanish."""

    async def go():
        stub = FakeStub()
        client = _client(stub)
        _fill(client, 4, prefix="early")
        assert client.pending == 4       # admitted into memory, not yet durable
        assert client.counters()["sent"] == 0
        await client.start()
        assert await _until(lambda: len(stub.delivered) == 4), stub.delivered
        assert stub.delivered == [f"early{i}" for i in range(1, 5)]
        await client.close()

    _run(go())


def test_queue_overflow_drops_the_oldest_and_counts_it():
    """Bounded memory: the oldest queued record goes, and the drop is counted."""

    async def go():
        stub = FakeStub()
        client = _client(stub, queue_size=2)
        _fill(client, 5, prefix="over")
        counters = client.counters()
        assert counters["dropped"] == 3, counters
        assert client.pending == 2
        await client.start()
        assert await _until(lambda: len(stub.delivered) == 2), stub.delivered
        # The newest context is what survives.
        assert stub.delivered == ["over4", "over5"], stub.delivered
        await client.close()

    _run(go())


def test_counters_account_for_every_record():
    """accepted == sent + dropped + still-pending while running."""

    async def go():
        stub = FakeStub()
        client = _client(stub, queue_size=3)
        _fill(client, 10, prefix="acct")
        counters = client.counters()
        assert counters["accepted"] == 10, counters
        assert counters["accepted"] == (
            counters["sent"] + counters["dropped"] + counters["pending"]
        ), counters
        await client.start()
        assert await _until(lambda: client.pending == 0), client.counters()
        final = client.counters()
        assert final["sent"] + final["dropped"] == 10, final
        await client.close()

    _run(go())


def test_close_reports_undelivered_records_instead_of_discarding_them():
    """A logger that never answers must produce a loud, counted loss report."""

    async def go():
        stub = AlwaysFailStub()
        client = _client(stub)
        _fill(client, 3)
        await client.start()
        await asyncio.sleep(0.05)
        await client.close(drain_timeout=0.2)
        counters = client.counters()
        assert stub.calls > 0, "the client never even tried"
        assert counters["sent"] == 0
        assert counters["shutdown_dropped"] == 3, counters
        assert counters["dropped"] == 0, "an outage is not an overflow"

    _run(go())


def test_a_disabled_client_counts_what_it_refuses():
    """Silence is not an option: a disabled client still reports refusals."""

    async def go():
        stub = FakeStub()
        client = _client(stub)
        client._enabled = False
        _fill(client, 2)
        assert client.counters()["disabled"] == 2
        assert stub.delivered == []
        await client.close(drain_timeout=0.05)

    _run(go())


def test_the_shutdown_drain_delivers_what_it_can():
    """A working logger gets the queued records flushed before the socket dies."""

    async def go():
        stub = FakeStub()

        class SlowStub(FakeStub):
            async def Log(self, request):  # noqa: N802
                await asyncio.sleep(0.01)
                return await super().Log(request)

        slow = SlowStub()
        client = _client(slow)
        _fill(client, 20, prefix="drain")
        await client.start()
        await client.close(drain_timeout=3.0)
        assert len(slow.delivered) == 20, slow.delivered
        assert client.counters()["shutdown_dropped"] == 0

    _run(go())


# --------------------------------------------------------------------------- #
# The collection policy is enforced AT THE DOOR: this client is the one place
# every emission path funnels through (the info/warn/error helpers and the
# stdlib logging bridge included), so a policy attached here governs them all.
# --------------------------------------------------------------------------- #
def _policy(**kw):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from core.application.log_policy import LogPolicy

    return LogPolicy(**kw)


def test_without_a_policy_nothing_is_filtered():
    """Backward compatible: the client only filters when given a policy."""

    async def go():
        stub = FakeStub()
        client = _client(stub)
        client.log("INFO", "core", "boot", "no policy")
        assert client.counters()["accepted"] == 1
        await client.close(drain_timeout=0.05)

    _run(go())


def test_the_default_policy_suppresses_info_reached_through_every_helper():
    async def go():
        stub = FakeStub()
        client = _client(stub)
        client._policy = _policy()

        client.info("core", "boot", "not collected by default")
        client.log("INFO", "core", "audit", "also suppressed")
        client.debug("core", "boot", "verbose")

        assert client.counters()["accepted"] == 0, client.counters()
        assert client._policy.snapshot()["suppressed_level"] == 3

        client.warn("core", "queue", "kept")
        client.error("core", "queue", "kept")
        assert client.counters()["accepted"] == 2
        await client.close(drain_timeout=0.05)

    _run(go())


def test_a_security_event_passes_the_gate_below_the_threshold():
    async def go():
        client = _client(FakeStub())
        client._policy = _policy(levels=("CRITICAL",))

        client.log("INFO", "core", "audit", "denied", event_type="auth.failed",
                   reason_code="UNAUTHORIZED")
        client.info("core", "audit", "normal event")

        assert client.counters()["accepted"] == 1, client.counters()
        snapshot = client._policy.snapshot()
        assert snapshot["emitted_security"] == 1
        assert snapshot["suppressed_level"] == 1
        await client.close(drain_timeout=0.05)

    _run(go())


def test_a_suppressed_event_is_counted_not_silently_dropped():
    async def go():
        client = _client(FakeStub())
        client._policy = _policy()
        for _ in range(4):
            client.info("core", "boot", "noise")
        snapshot = client._policy.snapshot()
        assert snapshot["suppressed_level"] == 4
        assert snapshot["emitted"] == 0
        assert client.counters()["dropped"] == 0, "suppression is not a queue drop"
        await client.close(drain_timeout=0.05)

    _run(go())
