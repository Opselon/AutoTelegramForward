"""Part 2 STEER 12 — the whole thing together, not one switch at a time.

Every steer before this one proved a property in isolation. This file runs the
real ``DurableMessagePipeline``, the real ``DurableQueueManager``, the real SQL
repositories and the real worker path against **two tenants and three tasks at
once**, and pins the properties that only exist in combination:

* a filter is per task — the same message that one task drops is delivered by
  the next tenant's task, and neither task rewrites the other's text;
* a credential is per tenant — a task naming another tenant's AI config is
  refused in the integrated flow and still delivers its text (fail-safe), and
  the refusal is logged;
* the run is durable — closing and reopening the database keeps every job, and
  a replay delivers nothing twice;
* a failed delivery is recorded with the secret redacted, so the operator can
  act on the error without the log or the row leaking the bot token.

Nothing here is a mock of the behaviour under test: only the Telegram client is
faked (it is the network), everything else is the production object.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import types

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import AIConfig, ForwardRule, MessagePayload, TelegramSession
from core.infrastructure.ai.circuit_breaker import CircuitBreakerRegistry
from core.infrastructure.ai.transformer import AITransformer
from core.infrastructure.ai.validator import AIOutputValidator
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAIConfigRepository,
    SqliteDeliveryQueueRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager

OWNER_A = 5094837833
OWNER_B = 5989354154
SOURCE = "-1004400615270"
TARGET_A1 = "-1003915313919"
TARGET_A2 = "-1003915313920"
TARGET_B1 = "-1003915313921"
CONFIG_A = "cfg-a"


class FakeClient:
    """The network, and the only fake: it can also fail on demand."""

    def __init__(self) -> None:
        self.fail_with: Exception | None = None
        self.sent: list[dict] = []
        self._next = 900

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self._next += 1
        self.sent.append({"chat_id": str(chat_id), "text": text, "id": self._next})
        return types.SimpleNamespace(id=self._next)

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self._next += 1
        self.sent.append({"chat_id": str(chat_id), "from_chat_id": str(from_chat_id),
                          "message_id": int(message_id), "id": self._next})
        return types.SimpleNamespace(id=self._next)

    async def forward_message(self, chat_id, from_chat_id, message_id, **kwargs):
        return await self.copy_message(chat_id, from_chat_id, message_id, **kwargs)

    async def copy_media_group(self, chat_id, from_chat_id, message_id, **kwargs):
        return await self.copy_message(chat_id, from_chat_id, message_id, **kwargs)


class FakePool:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def get(self, session_id):
        return self._client


class FakeProvider:
    async def rewrite(self, config: AIConfig, text: str) -> str:
        return f"rewritten-by-{config.id}"


class FakeFactory:
    def get(self, config: AIConfig) -> FakeProvider:
        return FakeProvider()


def make_rule(rule_id: str, owner: int, target: str, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id, session_id=f"sess-{owner}", source_chat_id=SOURCE,
        target_chat_id=target, is_active=True, owner_user_id=owner, **kwargs,
    )
    rule.duplicate_filters = False
    return rule


def payload(message_id: int, text: str) -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text=text)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def stack(tmp_path):
    path = os.path.join(tmp_path, "full_stack.db")
    db = SqliteDatabase(path)
    crypto = CryptoService("test-key")

    async def setup():
        sessions = SqliteSessionRepository(db, crypto)
        for owner in (OWNER_A, OWNER_B):
            await sessions.add(TelegramSession(
                id=f"sess-{owner}", phone_number="+100****0000", is_active=True,
                is_authorized=True, owner_user_id=owner,
            ))
        configs = SqliteAIConfigRepository(db, crypto)
        await configs.add(AIConfig(
            id=CONFIG_A, name="tenant-a", api_key="sk-tenant-a",
            base_url="https://api.example.com/v1", is_enabled=True,
            owner_user_id=OWNER_A,
        ))
        rules = SqliteForwardRuleRepository(db)
        # Tenant A, task 1: a filter, a replacement and a header.
        rule_a1 = make_rule("a1", OWNER_A, TARGET_A1)
        # Two tasks of one tenant on one source: without multi_route the
        # single-match policy gives the message to exactly one of them.
        rule_a1.multi_route = True
        rule_a1.blacklist_words = ["spam"]
        rule_a1.replacements = {"sale": "offer"}
        rule_a1.custom_header = "— A1 channel"
        await rules.add(rule_a1)
        # Tenant A, task 2: a different filter, no header.
        rule_a2 = make_rule("a2", OWNER_A, TARGET_A2)
        rule_a2.multi_route = True
        rule_a2.blacklist_words = ["lottery"]
        await rules.add(rule_a2)
        # Tenant B, its own task — naming tenant A's AI config on purpose.
        rule_b1 = make_rule("b1", OWNER_B, TARGET_B1,
                            ai_config_id=CONFIG_A, ai_fallback_policy="SEND_ORIGINAL")
        rule_b1.smart_ai_rewrite = True
        rule_b1.paid_addons = {"ai_rewrite": True}
        await rules.add(rule_b1)

    run(setup())
    client = FakeClient()
    queue = DurableQueueManager(
        queue_repo=SqliteDeliveryQueueRepository(db),
        message_map_repo=SqliteMessageMapRepository(db),
        client_pool=FakePool(client),
        rule_repo=SqliteForwardRuleRepository(db),
        num_workers=1,
    )
    transformer = AITransformer(
        ai_config_repo=SqliteAIConfigRepository(db, crypto),
        circuit_registry=CircuitBreakerRegistry(),
        validator=AIOutputValidator(),
        provider_factory=FakeFactory(),
    )
    pipeline = DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
        ai_transformer=transformer,
        ai_repo=SqliteAIConfigRepository(db, crypto),
    )
    try:
        yield path, db, queue, pipeline, client
    finally:
        db.close()


def jobs(db) -> list[dict]:
    rows = [dict(r) for r in db.query_all("SELECT * FROM delivery_jobs")]
    return sorted(rows, key=lambda r: (r["rule_id"], r["source_message_id"]))


def delivered_texts(db) -> dict[tuple[str, int], str]:
    out = {}
    for row in jobs(db):
        body = json.loads(row["payload_json"])
        out[(row["rule_id"], int(row["source_message_id"]))] = body.get("text") or ""
    return out


# --------------------------------------------------------------------------- #
# Composition + per-task isolation
# --------------------------------------------------------------------------- #
def test_two_tenants_and_three_tasks_compose_without_crossing(stack):
    _path, db, _queue, pipeline, _client = stack
    run(pipeline.process(payload(1, "sale item"), trigger="NEW_MESSAGE"))
    run(pipeline.process(payload(2, "spam item"), trigger="NEW_MESSAGE"))
    run(pipeline.process(payload(3, "lottery item"), trigger="NEW_MESSAGE"))

    texts = delivered_texts(db)
    # Task a1: replacement applied, header prepended, its blacklist honoured.
    assert "offer item" in texts[("a1", 1)], texts[("a1", 1)]
    assert "sale item" not in texts[("a1", 1)], texts[("a1", 1)]
    assert "— A1 channel" in texts[("a1", 1)], texts[("a1", 1)]
    assert ("a1", 2) not in texts, "a task delivered a blacklisted word"
    # Task a2 has its own filter: the message a1 dropped still reaches it.
    assert ("a2", 2) in texts, "one task's blacklist blocked another task"
    # Task a2's blacklist is its own, and its text carries no a1 header.
    assert ("a2", 3) not in texts, texts
    assert "— A1 channel" not in texts[("a2", 1)], "one task's header leaked into another"
    assert "offer" not in texts[("a2", 1)], "one task's replacement leaked into another"
    # Tenant B's task saw every message, unchanged, on its own target.
    assert set(texts) >= {("b1", 1), ("b1", 2), ("b1", 3)}, texts
    for mid in (1, 2, 3):
        assert texts[("b1", mid)] == f"{['sale', 'spam', 'lottery'][mid - 1]} item", texts
    targets = {row["rule_id"]: row["target_chat_id"] for row in jobs(db)}
    assert targets == {"a1": TARGET_A1, "a2": TARGET_A2, "b1": TARGET_B1}, targets


# --------------------------------------------------------------------------- #
# Per-tenant credentials, in the integrated flow
# --------------------------------------------------------------------------- #
def test_a_task_cannot_borrow_another_tenants_ai_config_here_either(stack, caplog):
    _path, db, _queue, pipeline, _client = stack
    with caplog.at_level(logging.WARNING):
        run(pipeline.process(payload(4, "plain text"), trigger="NEW_MESSAGE"))

    texts = delivered_texts(db)
    assert texts[("b1", 4)] == "plain text", texts
    assert "rewritten-by-" not in texts[("b1", 4)], "a foreign credential was spent"
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "[AI:CONFIG_REFUSED]" in log, log


# --------------------------------------------------------------------------- #
# Durability of the integrated run
# --------------------------------------------------------------------------- #
def test_the_integrated_run_survives_a_restart_and_a_replay(stack):
    path, db, _queue, pipeline, _client = stack
    run(pipeline.process(payload(5, "durable text"), trigger="NEW_MESSAGE"))
    before = jobs(db)
    assert len(before) == 3, before
    db.close()

    reopened = SqliteDatabase(path)
    try:
        after = [dict(r) for r in reopened.query_all("SELECT * FROM delivery_jobs")]
        assert len(after) == 3, after
        assert {r["id"] for r in after} == {r["id"] for r in before}

        replay = DurableMessagePipeline(
            rule_repo=SqliteForwardRuleRepository(reopened),
            filter_repo=SqliteFilterRuleRepository(reopened),
            queue_manager=DurableQueueManager(
                queue_repo=SqliteDeliveryQueueRepository(reopened),
                message_map_repo=SqliteMessageMapRepository(reopened),
                client_pool=FakePool(FakeClient()),
                rule_repo=SqliteForwardRuleRepository(reopened),
                num_workers=1,
            ),
            msg_map_repo=SqliteMessageMapRepository(reopened),
            processed_repo=SqliteProcessedMessageRepository(reopened),
        )
        run(replay.process(payload(5, "durable text"), trigger="NEW_MESSAGE"))
        again = [dict(r) for r in reopened.query_all("SELECT * FROM delivery_jobs")]
        assert len(again) == 3, f"a replay after the restart delivered again: {again}"
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# The worker path, and what it writes when it fails
# --------------------------------------------------------------------------- #
def test_a_failed_delivery_is_recorded_without_the_bot_token(stack):
    """The operator needs the reason; the row and the log must not carry the token."""
    _path, db, queue, pipeline, client = stack
    run(pipeline.process(payload(6, "will fail"), trigger="NEW_MESSAGE"))
    row = next(r for r in jobs(db) if r["rule_id"] == "a1")
    secret = "123456789:AAH-super-secret-token-value"
    client.fail_with = RuntimeError(
        f"Telegram says: POST https://api.telegram.org/bot{secret}/sendMessage failed")

    claimed = run(SqliteDeliveryQueueRepository(db).claim_batch(
        worker_id="w-integration", batch_size=1, lease_duration=30))
    assert claimed and claimed[0].id == row["id"], claimed
    run(queue._process_job("w-integration", claimed[0]))

    after = next(r for r in jobs(db) if r["id"] == row["id"])
    detail = after["error_detail"] or ""
    assert detail, "a failed delivery recorded no reason"
    assert secret not in detail, f"the bot token reached delivery_jobs.error_detail: {detail}"
    assert "[REDACTED]" in detail, detail
    assert after["attempts"] >= 1, after
    assert after["status"] in {"PENDING", "RETRY_WAIT", "FAILED"}, after
    assert after["next_retry_at"], "a retryable failure was left with no retry time"
    assert float(after["next_retry_at"]) > 0


# --------------------------------------------------------------------------- #
# Task isolation, stated as a rule rather than implied
# --------------------------------------------------------------------------- #
def test_one_message_is_consumed_by_one_task_per_tenant_until_multi_route(tmp_path):
    """Two tasks of one tenant on one source: one consumes, the other is starved.

    That is the product's deterministic single-match policy, and this test pins
    it inside the full stack rather than in isolation — because it is also the
    policy that would starve a *second tenant* if it were applied globally. With
    multi_route on, both tasks of the tenant run; the other tenant runs either way.
    """
    db = SqliteDatabase(os.path.join(tmp_path, "single_match.db"))
    crypto = CryptoService("test-key")

    async def setup():
        sessions = SqliteSessionRepository(db, crypto)
        for owner in (OWNER_A, OWNER_B):
            await sessions.add(TelegramSession(
                id=f"sess-{owner}", phone_number="+100****0000", is_active=True,
                is_authorized=True, owner_user_id=owner,
            ))
        rules = SqliteForwardRuleRepository(db)
        high = make_rule("hi", OWNER_A, TARGET_A1, priority=20)
        low = make_rule("lo", OWNER_A, TARGET_A2, priority=1)
        other = make_rule("other", OWNER_B, TARGET_B1)
        for rule in (high, low, other):
            await rules.add(rule)

    run(setup())
    queue = DurableQueueManager(
        queue_repo=SqliteDeliveryQueueRepository(db),
        message_map_repo=SqliteMessageMapRepository(db),
        client_pool=FakePool(FakeClient()),
        rule_repo=SqliteForwardRuleRepository(db),
        num_workers=1,
    )
    pipeline = DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )
    try:
        run(pipeline.process(payload(7, "single match"), trigger="NEW_MESSAGE"))
        served = {row["rule_id"] for row in jobs(db)}
        assert served == {"hi", "other"}, f"single-match did not hold per tenant: {served}"

        # The same tenant opts in to fan-out: now its second task runs as well.
        rule = run(SqliteForwardRuleRepository(db).get_by_id("hi"))
        rule.multi_route = True
        run(SqliteForwardRuleRepository(db).update(rule))
        run(pipeline.process(payload(8, "multi route"), trigger="NEW_MESSAGE"))
        served = {row["rule_id"] for row in jobs(db) if int(row["source_message_id"]) == 8}
        assert served == {"hi", "lo", "other"}, f"multi_route did not fan out: {served}"
    finally:
        db.close()
