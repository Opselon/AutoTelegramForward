"""Part 2 STEER 11 — what a worker restart must not lose.

A redeploy, an OOM kill or a crashed worker all look the same to the pipeline:
the process disappears between two statements. Everything the next process needs
must therefore already be in the database — the queued job and its payload, the
dedupe record that stops a replay from delivering twice, the delay gate that
holds a scheduled forward, and the lease that keeps two workers off one job.

This file drives the real pipeline into the real ``DurableQueueManager`` and then
**closes the database and opens a new one** — the closest thing to a restart a
single process can perform — and asserts each of those survived. It is also the
restart half of the cross-feature proof: the same source message, processed again
by a brand-new pipeline, must not produce a second delivery.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import ForwardRule, MessagePayload, TelegramSession
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager

OWNER = 5094837833
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


def make_rule(rule_id: str = "rule-r", **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id, session_id="sess-r", source_chat_id=SOURCE, target_chat_id=TARGET,
        is_active=True, owner_user_id=OWNER, **kwargs,
    )
    rule.duplicate_filters = False
    return rule


def payload(message_id: int = 501, text: str = "restart me") -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text=text)


def queue_for(db) -> DurableQueueManager:
    return DurableQueueManager(
        queue_repo=SqliteDeliveryQueueRepository(db),
        message_map_repo=SqliteMessageMapRepository(db),
        client_pool=MagicMock(),
        rule_repo=SqliteForwardRuleRepository(db),
        num_workers=1,
    )


def pipeline_for(db, queue) -> DurableMessagePipeline:
    return DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )


def seed(db, *rules) -> None:
    async def go():
        await SqliteSessionRepository(db, CryptoService("test-key")).add(TelegramSession(
            id="sess-r", phone_number="+100****0000", is_active=True,
            is_authorized=True, owner_user_id=OWNER,
        ))
        repo = SqliteForwardRuleRepository(db)
        for rule in rules:
            await repo.add(rule)

    asyncio.run(go())


def job_rows(db) -> list[dict]:
    return [dict(r) for r in db.query_all("SELECT * FROM delivery_jobs")]


@pytest.fixture
def workspace(tmp_path):
    path = os.path.join(tmp_path, "restart.db")
    db = SqliteDatabase(path)
    try:
        yield path, db
    finally:
        db.close()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The queued work itself
# --------------------------------------------------------------------------- #
def test_a_queued_job_survives_a_restart_with_its_payload(workspace):
    path, db = workspace
    seed(db, make_rule())
    run(pipeline_for(db, queue_for(db)).process(payload(), trigger="NEW_MESSAGE"))
    before = job_rows(db)
    assert len(before) == 1, before
    db.close()

    reopened = SqliteDatabase(path)
    try:
        after = job_rows(reopened)
        assert len(after) == 1, after
        assert after[0]["id"] == before[0]["id"]
        assert after[0]["status"] == "PENDING"
        body = json.loads(after[0]["payload_json"])
        assert "restart me" in json.dumps(body), body
        assert after[0]["job_kind"] == "forward", after[0]["job_kind"]
    finally:
        reopened.close()


def test_a_replay_after_a_restart_is_still_deduped(workspace):
    """The dedupe record is the only thing stopping a second delivery."""
    path, db = workspace
    seed(db, make_rule())
    first = pipeline_for(db, queue_for(db))
    run(first.process(payload(message_id=502), trigger="NEW_MESSAGE"))
    assert len(job_rows(db)) == 1
    db.close()

    reopened = SqliteDatabase(path)
    try:
        second = pipeline_for(reopened, queue_for(reopened))
        run(second.process(payload(message_id=502), trigger="NEW_MESSAGE"))
        assert len(job_rows(reopened)) == 1, (
            "a replayed message produced a second delivery after the restart: "
            + str(job_rows(reopened))
        )
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# The delay gate
# --------------------------------------------------------------------------- #
def test_a_delayed_job_is_still_gated_after_a_restart(workspace):
    """The delay lives in `next_retry_at`, not in a timer a restart would drop."""
    path, db = workspace
    seed(db, make_rule(delay_seconds=600.0))
    run(pipeline_for(db, queue_for(db)).process(payload(message_id=503), trigger="NEW_MESSAGE"))
    row = job_rows(db)[0]
    assert row["next_retry_at"] and float(row["next_retry_at"]) > time.time(), row
    db.close()

    reopened = SqliteDatabase(path)
    try:
        claimed = run(SqliteDeliveryQueueRepository(reopened).claim_batch(
            worker_id="w-after-restart", batch_size=5, lease_duration=60))
        assert claimed == [], f"the restart released a delayed job early: {claimed}"

        # Time passes: the same gate now admits it.
        reopened.execute(
            "UPDATE delivery_jobs SET next_retry_at = ?", (time.time() - 1,))
        claimed = run(SqliteDeliveryQueueRepository(reopened).claim_batch(
            worker_id="w-after-restart", batch_size=5, lease_duration=60))
        assert len(claimed) == 1, claimed
        assert claimed[0].id == row["id"]
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# The lease
# --------------------------------------------------------------------------- #
def test_a_job_held_by_the_dead_worker_is_reclaimed_as_the_same_job(workspace):
    path, db = workspace
    seed(db, make_rule())
    run(pipeline_for(db, queue_for(db)).process(payload(message_id=504), trigger="NEW_MESSAGE"))
    job_id = job_rows(db)[0]["id"]

    claimed = run(SqliteDeliveryQueueRepository(db).claim_batch(
        worker_id="w-before-crash", batch_size=5, lease_duration=60))
    assert [job.id for job in claimed] == [job_id], claimed
    db.close()  # the worker died holding a live lease

    reopened = SqliteDatabase(path)
    try:
        busy = run(SqliteDeliveryQueueRepository(reopened).claim_batch(
            worker_id="w-after-restart", batch_size=5, lease_duration=60))
        assert busy == [], f"a live lease was stolen: {busy}"

        reopened.execute("UPDATE delivery_jobs SET lease_until = ?", (time.time() - 1,))
        recovered = run(SqliteDeliveryQueueRepository(reopened).claim_batch(
            worker_id="w-after-restart", batch_size=5, lease_duration=60))
        assert [job.id for job in recovered] == [job_id], recovered
        assert len(job_rows(reopened)) == 1, "the recovery forked a second job row"
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# The branded stage
# --------------------------------------------------------------------------- #
def test_a_branded_job_keeps_the_post_it_must_forward_from(workspace):
    """Stage 2 reproduces C's post; a restart must not lose which post that was.

    The hop itself runs on the dispatch path (it needs a live client), so what the
    database owes the next process is the reference: C's chat id and the post it
    made there. Driven at the queue boundary, which is where that reference is
    written, because that is also where a restart would lose it.
    """
    path, db = workspace
    seed(db, make_rule())
    c_channel, post_id = "-1001234567890", 909
    accepted = run(queue_for(db).enqueue_delivery(
        make_rule(), payload(message_id=505), TARGET, "branded text",
        branding={"chat_id": c_channel, "message_id": post_id, "force_forward": False},
    ))
    assert accepted is True
    row = job_rows(db)[0]
    assert row["intermediate_chat_id"] == c_channel, row
    assert int(row["intermediate_message_id"]) == post_id, row
    assert row["delivery_stage"] == "BRANDED", row
    db.close()

    reopened = SqliteDatabase(path)
    try:
        after = job_rows(reopened)[0]
        assert after["intermediate_chat_id"] == c_channel, after
        assert int(after["intermediate_message_id"]) == post_id, after
        assert after["delivery_stage"] == "BRANDED", after
        body = json.loads(after["payload_json"])
        assert body["branding"]["chat_id"] == c_channel, body.get("branding")
        assert int(body["branding"]["message_id"]) == post_id, body.get("branding")
    finally:
        reopened.close()
