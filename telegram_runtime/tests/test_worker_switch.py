"""Strangler-fig switch tests: ATF_WORKER_ENABLED must move delivery-job
ownership from the Python loop to the Go Worker, and ONLY that. Enqueue
(inserting into delivery_jobs) stays Python's; the consumption loop is what
moves. These tests are the guarantee that one — and only one — execution
path is ever active, and that rollback is a config flip.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def _start_with_env(env_value: str | None):
    """Construct and start a manager under the given flag value.

    Everything (construct AND start) runs inside one event loop: the manager
    binds asyncio primitives at construction time, so a loop created and
    closed between the two would leave the tasks bound to a dead loop.
    """
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    env = {} if env_value is None else {"ATF_WORKER_ENABLED": env_value}
    with mock.patch.dict(os.environ, env, clear=False):
        manager = DurableQueueManager(
            queue_repo=_FakeQueue(),
            message_map_repo=None,
            client_pool=None,
            rule_repo=None,
            num_workers=2,
            attempts_repo=None,
        )
        await manager.start()
        return manager


def _run_with_env(env_value: str | None):
    return asyncio.run(_start_with_env(env_value))


class _FakeQueue:
    """Minimal queue the manager's loop can drive without a database."""

    def __init__(self):
        self.recovered = 0

    async def recover_expired_leases(self):
        self.recovered += 1
        return 0

    async def claim_batch(self, worker_id, batch_size=5, lease_duration=30.0):
        return []

    async def get_pending_count(self):
        return 0

    async def cleanup(self, retention_seconds=None):
        return 0

    async def get_stats(self):
        return {}

    async def counts_for_rule(self, rule_id):
        return {}

    async def get_queue_age_metrics(self):
        return {"pending_count": 0, "oldest_job_age_seconds": 0.0}


def test_worker_enabled_makes_the_python_loop_stand_down():
    """With the flag set, start() must NOT spawn worker loops."""

    async def scenario():
        manager = await _start_with_env("1")
        try:
            assert manager._running is False, "the Python queue loop started under Go ownership"
            assert not manager._workers, "worker tasks were spawned while Go owns the queue"
            assert manager._watchdog_task is None
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_worker_disabled_keeps_the_current_behaviour():
    """Unset (the default) the Python loop behaves exactly as before."""

    async def scenario():
        manager = await _start_with_env(None)
        try:
            assert manager._running is True, "the Python queue loop must start by default"
            assert manager._workers, "the loop spawned no workers"
            assert manager._watchdog_task is not None
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_flag_is_reversible():
    """Rolling back is flipping the flag back off: the Python loop must be
    startable again afterwards (no latching state)."""

    async def scenario():
        first = await _start_with_env("true")
        await first.stop()
        second = await _start_with_env(None)
        try:
            assert second._running is True
            assert second._workers
        finally:
            await second.stop()

    asyncio.run(scenario())
