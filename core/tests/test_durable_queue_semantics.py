"""Regression tests for DurableQueueManager semantics, return values,
atomic enqueuing across non-SQLite adapters, backpressure, and worker notification.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
import pytest

from core.domain.entities import ForwardRule, MessagePayload, DeliveryJob, MessageMapping
from core.domain.value_objects import DeliveryStatus, MediaType
from core.infrastructure.telegram.durable_queue import DurableQueueManager


@pytest.fixture
def sample_rule():
    return ForwardRule(
        id="rule-test-durable",
        source_chat_id="-100111",
        target_chat_id="-100222",
        is_active=True,
    )


@pytest.fixture
def sample_payload():
    return MessagePayload(
        message_id=555,
        chat_id="-100111",
        text="Test message for durable queue",
        media_type=MediaType.TEXT,
    )


@pytest.mark.asyncio
async def test_enqueue_atomic_non_sqlite_success(sample_rule, sample_payload):
    """When using non-SQLite repos (PostgreSQL / adapter), _enqueue_atomic must return True,

    bump stats['enqueued'], and signal _notify_event.
    """
    queue_repo = AsyncMock()
    queue_repo.enqueue = AsyncMock(return_value=True)
    queue_repo.get_pending_count = AsyncMock(return_value=0)
    queue_repo._db = None  # force non-SQLite fallback path

    map_repo = AsyncMock()
    map_repo.add_or_update = AsyncMock()
    map_repo._db = None

    manager = DurableQueueManager(
        queue_repo=queue_repo,
        message_map_repo=map_repo,
        client_pool=MagicMock(),
        max_queue_size=1000,
    )

    manager._notify_event.clear()
    assert manager.stats["enqueued"] == 0

    res = await manager.enqueue_delivery(sample_rule, sample_payload, target_chat_id="-100222", text="Hello")

    assert res is True
    assert manager.stats["enqueued"] == 1
    assert manager._notify_event.is_set() is True
    map_repo.add_or_update.assert_called_once()
    queue_repo.enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_enqueue_atomic_non_sqlite_duplicate_rejected(sample_rule, sample_payload):
    """When a job is already queued (duplicate insert), _enqueue_atomic returns False,

    does NOT bump stats['enqueued'], and does NOT set _notify_event.
    """
    queue_repo = AsyncMock()
    queue_repo.enqueue = AsyncMock(return_value=False)
    queue_repo.get_pending_count = AsyncMock(return_value=0)
    queue_repo._db = None  # force non-SQLite fallback path

    map_repo = AsyncMock()
    map_repo.add_or_update = AsyncMock()
    map_repo._db = None

    manager = DurableQueueManager(
        queue_repo=queue_repo,
        message_map_repo=map_repo,
        client_pool=MagicMock(),
        max_queue_size=1000,
    )

    manager._notify_event.clear()
    assert manager.stats["enqueued"] == 0

    res = await manager.enqueue_delivery(sample_rule, sample_payload, target_chat_id="-100222", text="Hello")

    assert res is False
    assert manager.stats["enqueued"] == 0
    assert manager._notify_event.is_set() is False


@pytest.mark.asyncio
async def test_queue_saturation_backpressure(sample_rule, sample_payload):
    """When pending jobs meet or exceed max_queue_size, enqueue must fail-fast with False."""
    queue_repo = AsyncMock()
    queue_repo.get_pending_count = AsyncMock(return_value=10)
    queue_repo._pending_estimate = 10
    queue_repo._db = None

    manager = DurableQueueManager(
        queue_repo=queue_repo,
        message_map_repo=AsyncMock(),
        client_pool=MagicMock(),
        max_queue_size=10,
    )

    res = await manager.enqueue_delivery(sample_rule, sample_payload, target_chat_id="-100222", text="Hello")
    assert res is False
    assert manager.stats["backpressure_drops"] == 1


@pytest.mark.asyncio
async def test_correlation_id_preserved_in_durable_job(sample_rule, sample_payload):
    """Job payload must preserve the correlation_id across queue persistence."""
    queue_repo = AsyncMock()
    queue_repo.enqueue = AsyncMock(return_value=True)
    queue_repo.get_pending_count = AsyncMock(return_value=0)
    queue_repo._db = None
    map_repo = AsyncMock()
    map_repo._db = None

    manager = DurableQueueManager(
        queue_repo=queue_repo,
        message_map_repo=map_repo,
        client_pool=MagicMock(),
    )

    corr_id = "corr-tracing-uuid-99887766"
    await manager.enqueue_delivery(
        sample_rule,
        sample_payload,
        target_chat_id="-100222",
        text="Sample text",
        correlation_id=corr_id,
    )

    job_arg: DeliveryJob = queue_repo.enqueue.call_args[0][0]
    assert job_arg.payload_data.get("correlation_id") == corr_id
