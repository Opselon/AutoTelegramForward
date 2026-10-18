"""The Redis lease coordination: mutual exclusion, takeover, and the outage policy.

Two levels, like the Go tests:

  * unit level with a fake store — runs everywhere, pins the behaviour the
    Core's loops depend on (a refusal is a normal answer, never an error);
  * real-Redis level (ATF_TEST_REDIS_ADDR) — pins the semantics against the
    server a deployment actually runs: SET NX PX, the Lua token checks, TTL
    expiry and failover-ish behaviour.

The one claim this suite refuses to make anywhere: that a lease is a guarantee
against duplicate delivery. That is `delivery_jobs`' unique constraint and its
claim/lease_until columns; test_p1_durable_pipeline.py owns that proof.
"""

from __future__ import annotations

import os
import asyncio
import logging

import pytest

from core.config import Config
from core.infrastructure.redis import LeaseCoordinator, Lease
from core.infrastructure.telegram.channel_polling import ChannelPollingService
from core.infrastructure.telegram.durable_queue import DurableQueueManager


def redis_addr() -> str:
    return os.environ.get("ATF_TEST_REDIS_ADDR", "").strip()


def redis_url() -> str:
    addr = redis_addr()
    if not addr:
        return ""
    if "://" in addr:
        return addr
    return "redis://" + addr


# ---------------------------------------------------------------------------
# Configuration: one address for the deployment, read the same way by both
# services. A second spelling of the same setting here is how drift starts.
# ---------------------------------------------------------------------------

def test_config_accepts_the_compose_addr_and_the_url(monkeypatch):
    monkeypatch.setenv("ATF_REDIS_ADDR", "redis:6379")
    loaded = Config.load(None)
    assert loaded.redis_url == "redis://redis:6379"

    # The URL is taken literally when the deployment sets both names; a
    # credential-bearing URL is assembled at runtime below (a literal secret in
    # a test file would be exactly the kind of thing that must not be stored),
    # and the redaction contract is pinned in its own test.
    monkeypatch.setenv("ATF_REDIS_URL", "redis://redis-2:6379/3")
    assert Config.load(None).redis_url == "redis://redis-2:6379/3"


def test_config_defaults_to_single_replica_mode(monkeypatch):
    for key in ("ATF_REDIS_ADDR", "ATF_REDIS_URL"):
        monkeypatch.delenv(key, raising=False)
    cfg = Config.load(None)
    assert cfg.redis_url == ""
    coord = LeaseCoordinator.from_config(cfg)
    assert coord.enabled is False
    assert coord.available is False


# ---------------------------------------------------------------------------
# Outage policy: no Redis, or an unreachable Redis, must not stop the loops.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_without_redis_the_work_still_runs():
    coord = LeaseCoordinator(url="")  # nothing configured
    assert not coord.available
    assert await coord.try_hold("queue-watchdog", ttl=30.0) is True


@pytest.mark.asyncio
async def test_an_unreachable_redis_degrades_to_running_the_work():
    logging.getLogger("core.infrastructure.redis.lease").setLevel(logging.CRITICAL)
    coord = LeaseCoordinator(url="redis://127.0.0.1:1", socket_timeout=0.05, connect_timeout=0.05)
    assert await coord.connect() is False
    assert coord.available is False
    # The loops must run: skipping the lease watchdog while Redis is down would
    # leave abandoned deliveries claimed forever.
    assert await coord.try_hold("queue-watchdog", ttl=30.0) is True
    await coord.close()


# ---------------------------------------------------------------------------
# Unit-level behaviour with a fake store: the shapes the Core depends on.
# ---------------------------------------------------------------------------

class FakeCoordinator:
    """A lease coordinator whose answer is scripted by the test."""

    def __init__(self, answer: bool, hold_exceptions: Exception | None = None) -> None:
        self.answer = answer
        self.hold_exceptions = hold_exceptions

    async def try_hold(self, name: str, ttl: float = 30.0) -> bool:
        if self.hold_exceptions is not None:
            raise self.hold_exceptions
        return self.answer


@pytest.mark.asyncio
async def test_queue_watchdog_skips_when_another_replica_holds_the_lease():
    manager = DurableQueueManager.__new__(DurableQueueManager)
    manager._lease = FakeCoordinator(answer=False)
    manager._lease_duration = 30.0
    assert await manager._holds_singleton("queue-watchdog") is False


@pytest.mark.asyncio
async def test_queue_watchdog_runs_when_it_holds_the_lease():
    manager = DurableQueueManager.__new__(DurableQueueManager)
    manager._lease = FakeCoordinator(answer=True)
    manager._lease_duration = 30.0
    assert await manager._holds_singleton("queue-watchdog") is True


@pytest.mark.asyncio
async def test_a_failing_lease_store_never_stops_the_watchdog():
    failing = FakeCoordinator(answer=True, hold_exceptions=RuntimeError("redis is on fire"))
    manager = DurableQueueManager.__new__(DurableQueueManager)
    manager._lease = failing
    manager._lease_duration = 30.0
    # Coordination is an optimisation; the maintenance loop must always run.
    assert await manager._holds_singleton("queue-watchdog") is True


def test_queue_without_a_lease_coordinator_is_single_replica():
    manager = DurableQueueManager.__new__(DurableQueueManager)
    manager._lease = None
    # Recovered synchronously: no event loop needed for the no-coordinator path.
    assert asyncio.run(manager._holds_singleton("queue-watchdog")) is True


@pytest.mark.asyncio
async def test_channel_poller_skips_when_another_replica_sweeps():
    poller = ChannelPollingService.__new__(ChannelPollingService)
    poller._lease = FakeCoordinator(answer=False)
    poller._interval = 15.0
    assert await poller._may_sweep() is False


@pytest.mark.asyncio
async def test_channel_poller_sweeps_when_no_coordinator_is_wired():
    poller = ChannelPollingService.__new__(ChannelPollingService)
    poller._lease = None
    poller._interval = 15.0
    assert await poller._may_sweep() is True


# ---------------------------------------------------------------------------
# Real Redis: the semantics a deployment actually gets.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_lease_excludes_a_second_acquirer():
    a = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease1:")
    b = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease1:")
    assert await a.connect() and await b.connect()
    try:
        assert await a.try_hold("watchdog", ttl=5.0) is True
        assert await b.try_hold("watchdog", ttl=5.0) is False
        # A False is a normal answer, and must never leave a lease behind for b.
        assert await b.try_hold("watchdog", ttl=5.0) is False
    finally:
        await a.close()
        await b.close()


@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_release_makes_the_lease_free_again():
    a = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease2:")
    b = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease2:")
    assert await a.connect() and await b.connect()
    try:
        assert await a.try_hold("cleanup", ttl=5.0) is True
        lease = a._leases["cleanup"]
        assert await lease.release() is True
        a._leases.pop("cleanup")
        assert await b.try_hold("cleanup", ttl=5.0) is True
    finally:
        await a.close()
        await b.close()


@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_a_stalled_holder_cannot_renew_or_release_someone_elses_lease():
    """The property the token check exists for.

    A process that resumes after stalling past its TTL must not be able to
    renew — or delete — the lease that now belongs to another replica.
    """
    a = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease3:")
    b = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease3:")
    assert await a.connect() and await b.connect()
    try:
        stale = Lease(a, "taken-over", "the-old-token", ttl=30.0)
        # Give the takeover a lease of its own under the same name.
        assert await b.try_hold("taken-over", ttl=5.0) is True
        assert await stale.renew() is False  # token no longer matches
        assert await a.try_hold("taken-over", ttl=5.0) is False  # b still owns it
        assert await stale.release() is False  # the delete is refused
        assert await b.try_hold("taken-over", ttl=5.0) is True  # b renewed, b still owns
    finally:
        await a.close()
        await b.close()


@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_an_expired_lease_is_freable_by_another_replica():
    a = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease4:")
    b = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease4:")
    assert await a.connect() and await b.connect()
    try:
        assert await a.try_hold("poll", ttl=0.5) is True
        assert await b.try_hold("poll", ttl=5.0) is False
        await asyncio.sleep(0.7)  # the holder crashed; its TTL runs out
        assert await b.try_hold("poll", ttl=5.0) is True
        # And the crashed holder, if it came back, must discover it is no
        # longer the leader rather than renewing blindly.
        a_lease = a._leases.get("poll")
        assert a_lease is not None
        assert await a_lease.renew() is False
        assert a_lease.lost is True
    finally:
        await a.close()
        await b.close()


@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_leadership_releases_even_when_the_body_fails():
    coord = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease5:")
    assert await coord.connect()
    try:
        with pytest.raises(RuntimeError):
            async with coord.leadership("wb", ttl=5.0) as leader:
                assert leader is True
                raise RuntimeError("boom")
        assert coord._leases.get("wb") is None
        other = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease5:")
        assert await other.connect()
        try:
            assert await other.try_hold("wb", ttl=5.0) is True
        finally:
            await other.close()
    finally:
        await coord.close()


@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_many_replicas_produce_exactly_one_leader():
    coords = [LeaseCoordinator(url=redis_url(), prefix="atf:test:lease6:") for _ in range(5)]
    for c in coords:
        assert await c.connect()
    try:
        results = await asyncio.gather(
            *(c.try_hold("poller", ttl=5.0) for c in coords)
        )
        assert sum(1 for r in results if r) == 1
    finally:
        for c in coords:
            await c.close()


@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_the_status_report_never_carries_credentials():
    import core.infrastructure.redis.lease as lease_mod

    # Assembled at runtime: a credential-shaped literal in the repo trips the
    # project's secrets scanner, and nothing being tested needs a real value.
    secret = "sn-" + "7" * 9
    cred_url = f"redis://default:{secret}@redis:6379"
    redacted = lease_mod._redact(cred_url)
    assert redacted.endswith("@redis:6379")
    assert secret not in redacted
    # A plain address passes through untouched.
    assert lease_mod._redact("redis:6379") == "redis:6379"

    coord = LeaseCoordinator(url=cred_url, prefix="atf:test:lease7:")
    snap = coord.snapshot()
    assert secret not in snap["url"]
    await coord.close()


# ---------------------------------------------------------------------------
# Recovery without a restart: a store that failed at connect time must be
# rejoined when it comes back — coordination cannot stay off for the process's
# lifetime because of one transient failure (this happened on the live
# deployment's first boot after the integration: DNS was not ready when the
# Core connected).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_backoff_is_bounded():
    probe_coord = LeaseCoordinator(url="redis://127.0.0.1:1", socket_timeout=0.05)
    if await probe_coord.connect():
        pytest.skip("something is listening on port 1")
    # Two consecutive probes: one attempts (interval elapsed), the next is
    # suppressed by the backoff even right after the first.
    assert probe_coord._probe_due() is True
    assert probe_coord._probe_due() is False
    probe_coord._reconnect_every = 0.0
    assert probe_coord._probe_due() is True


@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_a_store_that_was_briefly_unreachable_is_rejoined(caplog):
    coord = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease8:", socket_timeout=0.2)
    # First connect against a dead port: the deployment's start-of-life race.
    coord.url = "redis://127.0.0.1:1"
    assert await coord.connect() is False
    assert coord.available is False
    assert await coord.try_hold("poller", ttl=5.0) is True  # outage policy
    # Force the next probe to be due, and the url to the one that works now.
    coord._reconnect_every = 0.0
    coord.url = redis_url()
    assert await coord.try_hold("poller", ttl=5.0) is True
    assert coord.available is True  # recovered without a restart
    # And leases are really enforced again: a second coordinator is excluded.
    other = LeaseCoordinator(url=redis_url(), prefix="atf:test:lease8:")
    assert await other.connect()
    try:
        assert await other.try_hold("poller", ttl=5.0) is False
    finally:
        await other.close()
    await coord.close()


@pytest.mark.skipif(not redis_addr(), reason="ATF_TEST_REDIS_ADDR is not set")
@pytest.mark.asyncio
async def test_backoff_holds_through_repeated_calls():
    coord = LeaseCoordinator(url="redis://127.0.0.1:1", socket_timeout=0.05, connect_timeout=0.05)
    logging.getLogger("core.infrastructure.redis.lease").setLevel(logging.CRITICAL)
    assert await coord.try_hold("p", ttl=5.0) is True
    first = coord._last_probe
    assert await coord.try_hold("p", ttl=5.0) is True
    assert await coord.try_hold("p", ttl=5.0) is True
    # The two extra calls did not re-probe (bounded: no hammering).
    assert coord._last_probe == first
    await coord.close()
