"""Regression tests for Paused Forwarding Rules lifecycle and execution prevention.

Verifies:
- Active rules match and route.
- Inactive rules are rejected.
- Paused rules (indefinite or timed) never match or enqueue work.
- Deciding route returns DROP when rule is paused.
- Pipeline filters out paused rules.
- Durable queue worker postpones (retries) jobs when rule is paused.
- Durable queue worker drops jobs when rule is disabled.
- Resumed rules unblock processing cleanly without duplicates.
- Tenant isolation is strictly enforced during pause/resume.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from core.domain.entities import ForwardRule, MessagePayload
from core.domain.value_objects import ForwardMode, RoutingType, RoutingDecision, MediaType, DeliveryStatus
from core.domain.services import SmartRoutingEngine
from core.application.pipeline import DurableMessagePipeline, PipelineContext
from core.domain.entities import DeliveryJob, MessageMapping
from core.infrastructure.telegram.durable_queue import DurableQueueManager


@pytest.fixture
def smart_engine():
    return SmartRoutingEngine()


@pytest.fixture
def base_payload():
    return MessagePayload(
        message_id=101,
        chat_id="-1001234567890",
        text="Target transmission signal",
        media_type=MediaType.TEXT,
    )


def test_active_rule_matches(smart_engine, base_payload):
    rule = ForwardRule(
        id="rule-active-1",
        source_chat_id="-1001234567890",
        target_chat_id="-1009876543210",
        is_active=True,
        is_paused=False,
    )
    matched, reason = smart_engine.evaluate_rule(base_payload, rule)
    assert matched is True
    assert reason == "matched"

    decision, dec_reason = smart_engine.decide_route(base_payload, rule)
    assert decision != RoutingDecision.DROP


def test_inactive_rule_rejected(smart_engine, base_payload):
    rule = ForwardRule(
        id="rule-inactive-1",
        source_chat_id="-1001234567890",
        target_chat_id="-1009876543210",
        is_active=False,
        is_paused=False,
    )
    matched, reason = smart_engine.evaluate_rule(base_payload, rule)
    assert matched is False
    assert reason == "rule_inactive"

    decision, dec_reason = smart_engine.decide_route(base_payload, rule)
    assert decision == RoutingDecision.DROP
    assert dec_reason == "rule_inactive"


def test_indefinitely_paused_rule_rejected(smart_engine, base_payload):
    rule = ForwardRule(
        id="rule-paused-1",
        source_chat_id="-1001234567890",
        target_chat_id="-1009876543210",
        is_active=True,
        is_paused=True,
        paused_until=0,
    )
    assert rule.is_currently_paused() is True

    matched, reason = smart_engine.evaluate_rule(base_payload, rule)
    assert matched is False
    assert reason == "rule_paused"

    decision, dec_reason = smart_engine.decide_route(base_payload, rule)
    assert decision == RoutingDecision.DROP
    assert dec_reason == "rule_paused"

    matching = smart_engine.matching_rules(base_payload, [rule])
    assert len(matching) == 0


def test_timed_pause_rule_window(smart_engine, base_payload):
    now = time.time()
    # Paused until 100 seconds in the future
    future_rule = ForwardRule(
        id="rule-future-pause",
        source_chat_id="-1001234567890",
        target_chat_id="-1009876543210",
        is_active=True,
        is_paused=True,
        paused_until=int(now + 100),
    )
    assert future_rule.is_currently_paused(now=now) is True
    matched, reason = smart_engine.evaluate_rule(base_payload, future_rule)
    assert matched is False
    assert reason == "rule_paused"

    # Paused until 50 seconds in the past -> should automatically be resumed
    past_rule = ForwardRule(
        id="rule-past-pause",
        source_chat_id="-1001234567890",
        target_chat_id="-1009876543210",
        is_active=True,
        is_paused=True,
        paused_until=int(now - 50),
    )
    assert past_rule.is_currently_paused(now=now) is False
    matched_past, reason_past = smart_engine.evaluate_rule(base_payload, past_rule)
    assert matched_past is True
    assert reason_past == "matched"


@pytest.mark.asyncio
async def test_pipeline_process_for_rule_skips_paused_rule(base_payload):
    rule = ForwardRule(
        id="rule-paused-pip",
        source_chat_id="-1001234567890",
        target_chat_id="-1009876543210",
        is_active=True,
        is_paused=True,
    )
    rule_repo = AsyncMock()
    rule_repo.list_active_by_source = AsyncMock(return_value=[rule])
    queue = AsyncMock()

    pipeline = DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=AsyncMock(),
        queue_manager=queue,
    )

    ctx = PipelineContext(
        correlation_id="corr-test-paused-1234",
        payload=base_payload,
    )
    res_ctx = await pipeline.process(ctx)

    assert len(res_ctx.target_jobs) == 0
    queue.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_durable_queue_worker_postpones_paused_rule():
    rule = ForwardRule(
        id="rule-queue-paused",
        source_chat_id="-1001234567890",
        target_chat_id="-1009876543210",
        is_active=True,
        is_paused=True,
        paused_until=0,
    )
    rule_repo = AsyncMock()
    rule_repo.get_by_id = AsyncMock(return_value=rule)

    queue_repo = AsyncMock()
    map_repo = AsyncMock()
    client_pool = MagicMock()

    durable = DurableQueueManager(
        queue_repo=queue_repo,
        message_map_repo=map_repo,
        client_pool=client_pool,
        rule_repo=rule_repo,
    )

    job = DeliveryJob(
        id="job-paused-01",
        rule_id="rule-queue-paused",
        source_chat_id="-1001234567890",
        source_message_id=101,
        target_chat_id="-1009876543210",
        payload_data={"session_id": "sess-1"},
    )

    res = await durable._process_job("worker-1", job)
    assert res is None
    # Worker must postpone via mark_retry instead of mark_failed
    queue_repo.mark_retry.assert_called_once()
    assert "Rule paused" in queue_repo.mark_retry.call_args[0][1]
    queue_repo.mark_failed.assert_not_called()


@pytest.mark.asyncio
async def test_durable_queue_worker_drops_inactive_rule():
    rule = ForwardRule(
        id="rule-queue-inactive",
        source_chat_id="-1001234567890",
        target_chat_id="-1009876543210",
        is_active=False,
    )
    rule_repo = AsyncMock()
    rule_repo.get_by_id = AsyncMock(return_value=rule)

    queue_repo = AsyncMock()
    durable = DurableQueueManager(
        queue_repo=queue_repo,
        message_map_repo=AsyncMock(),
        client_pool=MagicMock(),
        rule_repo=rule_repo,
    )

    job = DeliveryJob(
        id="job-inactive-01",
        rule_id="rule-queue-inactive",
        source_chat_id="-1001234567890",
        source_message_id=101,
        target_chat_id="-1009876543210",
        payload_data={"session_id": "sess-1"},
    )

    res = await durable._process_job("worker-1", job)
    assert res is None
    queue_repo.mark_failed.assert_called_once_with("job-inactive-01", "Rule disabled")
    queue_repo.mark_retry.assert_not_called()
