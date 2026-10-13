"""Tests for enhanced error and warning logging across pipeline, queue, and poller."""

from unittest.mock import MagicMock, AsyncMock
import pytest

from core.domain.entities import ForwardRule, FilterRule, MessagePayload, MediaType
from core.application.pipeline import DurableMessagePipeline
from core.infrastructure.telegram.durable_queue import DurableQueueManager, DeliveryJob
from core.infrastructure.telegram.dispatcher import MessageDispatcher


class MockLogClient:
    def __init__(self):
        self.logs = []

    def info(self, service, category, message, detail=None):
        self.logs.append(("INFO", service, category, message, detail))

    def warn(self, service, category, message, detail=None):
        self.logs.append(("WARN", service, category, message, detail))

    def error(self, service, category, message, detail=None):
        self.logs.append(("ERROR", service, category, message, detail))


@pytest.mark.asyncio
async def test_dispatcher_message_handler_retrieval():
    """dispatcher.message_handler returns the registered closure for a session."""
    pool = MagicMock()
    client = MagicMock()
    pool.get.return_value = client
    disp = MessageDispatcher(pool, MagicMock())

    assert disp.message_handler("sess1") is None
    ok = await disp.bind_session("sess1")
    assert ok is True
    handler = disp.message_handler("sess1")
    assert callable(handler)

    await disp.unbind_session("sess1")
    assert disp.message_handler("sess1") is None


@pytest.mark.asyncio
async def test_pipeline_filter_and_drop_logging(caplog):
    """Pipeline logs clear WARNING when a rule drops a message via filter."""
    rule_repo = MagicMock()
    filter_repo = MagicMock()
    queue = MagicMock()

    filt = FilterRule(
        id="filt-1",
        name="Block Spam",
        blacklist_keywords=["badword"],
    )
    filter_repo.get_by_id = AsyncMock(return_value=filt)

    rule = ForwardRule(
        id="rule-test-1",
        session_id="s1",
        source_chat_id="-100111",
        target_chat_id="-100222",
        filter_rule_id="filt-1",
    )
    rule_repo.list_active_by_source = AsyncMock(return_value=[rule])

    pipe = DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=filter_repo,
        queue_manager=queue,
    )

    payload = MessagePayload(
        chat_id="-100111",
        message_id=50,
        text="This contains badword in text",
        media_type=MediaType.TEXT,
    )

    with caplog.at_level("WARNING"):
        ctx = await pipe.process(payload)

    assert any("DROPPED msg 50 by filter rule filt-1" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_durable_queue_permanent_failure_logging():
    """Worker emits structured ERROR log to log_client on permanent failure."""
    queue = MagicMock()
    mapping = MagicMock()
    mapping.update_delivery = AsyncMock()
    queue.mark_failed = AsyncMock()
    queue.mark_retry = AsyncMock()
    pool = MagicMock()

    client = MagicMock()
    client.send_message = AsyncMock(side_effect=Exception("CHAT_WRITE_FORBIDDEN"))
    client.copy_message = AsyncMock(side_effect=Exception("CHAT_WRITE_FORBIDDEN"))
    pool.get.return_value = client

    log_client = MockLogClient()

    mgr = DurableQueueManager(
        queue_repo=queue,
        message_map_repo=mapping,
        client_pool=pool,
        log_client=log_client,
    )

    job = DeliveryJob(
        id="job-1",
        rule_id="rule-1",
        source_chat_id="-100111",
        source_message_id=123,
        target_chat_id="-100222",
        payload_data={"session_id": "s1", "effective_text": "hello"},
    )

    res = await mgr._process_job("w1", job)
    assert res is None
    queue.mark_failed.assert_awaited_once()

    errors = [l for l in log_client.logs if l[0] == "ERROR"]
    assert len(errors) >= 1
    _, svc, cat, msg, detail = errors[0]
    assert "Permanent delivery failure" in msg
    assert detail["target"] == "-100222"
    assert "CHAT_WRITE_FORBIDDEN" in detail["error"]


@pytest.mark.asyncio
async def test_durable_queue_transient_failure_logging():
    """Worker emits structured WARN log to log_client on transient failure."""
    queue = MagicMock()
    mapping = MagicMock()
    mapping.update_delivery = AsyncMock()
    queue.mark_retry = AsyncMock()
    pool = MagicMock()

    client = MagicMock()
    client.send_message = AsyncMock(side_effect=Exception("Connection reset by peer"))
    client.copy_message = AsyncMock(side_effect=Exception("Connection reset by peer"))
    pool.get.return_value = client

    log_client = MockLogClient()

    mgr = DurableQueueManager(
        queue_repo=queue,
        message_map_repo=mapping,
        client_pool=pool,
        log_client=log_client,
    )

    job = DeliveryJob(
        id="job-2",
        rule_id="rule-2",
        source_chat_id="-100111",
        source_message_id=456,
        target_chat_id="-100333",
        payload_data={"session_id": "s1", "effective_text": "test"},
    )

    res = await mgr._process_job("w1", job)
    assert res is None
    queue.mark_retry.assert_awaited_once()

    warns = [l for l in log_client.logs if l[0] == "WARN"]
    assert len(warns) >= 1
    _, svc, cat, msg, detail = warns[0]
    assert "Transient delivery error" in msg
    assert detail["target"] == "-100333"
