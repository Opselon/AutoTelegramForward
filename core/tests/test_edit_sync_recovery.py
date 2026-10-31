"""Edit synchronisation: pending edits and transient failures (E2E-3, E2E-4).

Two related defects from the end-to-end audit, both reproduced here against
the real repositories:

* **E2E-3** — an edit arriving while the initial delivery was still PENDING
  was dropped by ``handle_edit`` (only ``SENT`` mappings are updated in
  place). The destination was then posted with the *old* text and no later
  update could ever reach it. Fix: the edit is folded into the pending
  delivery job's payload, so the first post carries the new text.

* **E2E-4** — a failed edit was logged once and never retried, so a transient
  Telegram error permanently drifted the source and the destination. Fix: a
  bounded in-call retry with backoff recovers the transient case.

Deterministic and in-process: the queue, the mapping and the job payload are
real; only Telegram is faked, with controlled failure injection.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from core.domain.entities import DeliveryStatus, ForwardRule, MessagePayload
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
)
from core.infrastructure.telegram.sync_engine import SyncEngine

SOURCE = "-100S"
TARGET = "-100T"


class EditTelegram:
    """Records edit calls and fails the first N with a configurable error."""

    def __init__(self, fail_times: int = 0, error: Exception = None):
        self.fail_times = fail_times
        self.error = error or RuntimeError("500 INTERNAL")
        self.edits: list[dict] = []
        self.sleeps: list[float] = []

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append(dict(chat_id=chat_id, message_id=message_id, text=text))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        return SimpleNamespace(id=message_id)


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "edit.db"))


@pytest.fixture
def mmap(db):
    return SqliteMessageMapRepository(db)


@pytest.fixture
def rules(db):
    return SqliteForwardRuleRepository(db)


@pytest.fixture
def queue(db):
    return SqliteDeliveryQueueRepository(db)


@pytest.fixture(autouse=True)
def _session_row(db):
    """forward_rules carries an FK to sessions; give every rule its session."""
    db.execute(
        "INSERT OR IGNORE INTO sessions "
        "(id, phone_number, user_id, is_active, is_authorized, created_at, updated_at) "
        "VALUES ('sess', '100', '1', 1, 1, ?, ?)",
        (int(time.time()), int(time.time())),
    )
    return db


def make_rule(**overrides):
    base = dict(
        id="rule-1", session_id="sess", source_chat_id=SOURCE,
        target_chat_id=TARGET, target_chat_ids=[TARGET], is_active=True,
    )
    base.update(overrides)
    rule = ForwardRule(**base)
    # sync_edits_effective is a derived property: ignore_edits=False and the
    # legacy sync_edits=True yield "edits are synced" (see the entity).
    rule.ignore_edits = False
    rule.sync_edits = True
    return rule


def make_payload(message_id: int = 500, text: str = "original"):
    return MessagePayload(
        message_id=message_id, chat_id=SOURCE, text=text,
        has_media=False, media_type=None, edit_date=int(time.time()),
    )


def make_mapping(status=DeliveryStatus.SENT, target_message_id=1001, **kw):
    from core.domain.entities import MessageMapping
    base = dict(
        rule_id="rule-1", source_chat_id=SOURCE, source_message_id=500,
        target_chat_id=TARGET, target_message_id=target_message_id,
        delivery_status=status,
    )
    base.update(kw)
    mapping = MessageMapping(**base)
    # The repository owns the insert; use its own API.
    return mapping


class Pool:
    def __init__(self, tg):
        self._tg = tg

    def get(self, sid):
        return self._tg


async def _store_mapping(repo, mapping):
    """Insert through the repository so the row shape is the real one."""
    db = repo._db
    # message_map carries a foreign key to sessions: give the row a session.
    db.execute(
        "INSERT OR IGNORE INTO sessions "
        "(id, phone_number, user_id, is_active, is_authorized, created_at, updated_at) "
        "VALUES ('sess', '100', '1', 1, 1, ?, ?)",
        (int(time.time()), int(time.time())),
    )
    db.execute(
        "INSERT OR REPLACE INTO message_map "
        "(id, rule_id, source_chat_id, source_message_id, target_chat_id, "
        " target_message_id, delivery_status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (mapping.id, mapping.rule_id, mapping.source_chat_id, mapping.source_message_id,
         mapping.target_chat_id, mapping.target_message_id,
         str(mapping.delivery_status.value if hasattr(mapping.delivery_status, "value") else mapping.delivery_status),
         int(time.time()), int(time.time())),
    )


async def _store_job(queue_repo, payload_data, status="PENDING"):
    db = queue_repo._db
    job_id = f"job-{payload_data['source_message_id']}-{payload_data['target_chat_id']}"
    db.execute(
        "INSERT OR REPLACE INTO delivery_jobs "
        "(id, rule_id, source_chat_id, source_message_id, target_chat_id, status, "
        " payload_json, attempts, max_attempts, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, payload_data["rule_id"], payload_data["source_chat_id"],
         payload_data["source_message_id"], payload_data["target_chat_id"], status,
         json.dumps(payload_data), 0, 5, int(time.time()), int(time.time())),
    )
    return job_id


# --------------------------------------------------------------------------- #
# E2E-3: the edit is folded into the pending delivery job
# --------------------------------------------------------------------------- #

async def test_an_edit_arriving_while_delivery_is_pending_is_folded_in(
    db, mmap, rules, queue
):
    """The destination must not be posted with the stale text."""
    await rules.add(make_rule())
    tg = EditTelegram()
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)

    # A delivery job is queued but has not run yet, and a mapping exists.
    await _store_job(queue, dict(
        rule_id="rule-1", source_chat_id=SOURCE, source_message_id=500,
        target_chat_id=TARGET, text="original", caption="",
    ), status="PENDING")
    await _store_mapping(mmap, make_mapping(status=DeliveryStatus.PENDING, target_message_id=None))

    # The source message is edited before the worker picks the job up.
    result = await engine.handle_edit(make_payload(text="EDITED"), "EDITED")

    row = queue._db.query_one(
        "SELECT payload_json FROM delivery_jobs WHERE source_message_id = ?", (500,)
    )
    payload = json.loads(row["payload_json"])
    assert payload["text"] == "EDITED", (
        f"the edit must be folded into the pending job, got {payload['text']!r}"
    )
    assert payload.get("is_edit_folded") is True


async def test_a_sent_mapping_is_still_updated_in_place(db, mmap, rules, queue):
    """The existing path must keep working: a delivered mapping is edited."""
    await rules.add(make_rule())
    tg = EditTelegram()
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)

    await _store_mapping(mmap, make_mapping(status=DeliveryStatus.SENT, target_message_id=1001))
    result = await engine.handle_edit(make_payload(text="EDITED"), "EDITED")

    assert result == 1, f"exactly one target should be updated, got {result}"
    assert tg.edits and tg.edits[0]["text"] == "EDITED"


async def test_a_pending_edit_neither_duplicate_posts_nor_drops(db, mmap, rules, queue):
    """The fold updates the job in place; it must not create a second post."""
    await rules.add(make_rule())
    tg = EditTelegram()
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)

    await _store_job(queue, dict(
        rule_id="rule-1", source_chat_id=SOURCE, source_message_id=500,
        target_chat_id=TARGET, text="original", caption="",
    ), status="PENDING")
    await _store_mapping(mmap, make_mapping(status=DeliveryStatus.PENDING, target_message_id=None))

    await engine.handle_edit(make_payload(text="EDITED"), "EDITED")

    # No in-place edit happened (nothing to edit yet) and no new job appeared.
    assert tg.edits == [], "nothing was posted yet, so no edit call may happen"
    n = queue._db.query_one(
        "SELECT COUNT(*) AS n FROM delivery_jobs WHERE source_message_id = ?", (500,)
    )["n"]
    assert n == 1, f"the fold must not create a second job, got {n}"


async def test_the_fold_leaves_identity_keys_alone(db, mmap, rules, queue):
    """The fence, the mapping and retries still key on the original event."""
    await rules.add(make_rule())
    tg = EditTelegram()
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)

    await _store_job(queue, dict(
        rule_id="rule-1", source_chat_id=SOURCE, source_message_id=500,
        target_chat_id=TARGET, text="original", caption="", job_kind="forward",
        correlation_id="corr-1",
    ), status="PENDING")
    await _store_mapping(mmap, make_mapping(status=DeliveryStatus.PENDING, target_message_id=None))

    await engine.handle_edit(make_payload(text="EDITED"), "EDITED")

    row = queue._db.query_one(
        "SELECT payload_json, rule_id, source_chat_id, source_message_id, target_chat_id "
        "FROM delivery_jobs WHERE source_message_id = ?", (500,)
    )
    payload = json.loads(row["payload_json"])
    assert payload["job_kind"] == "forward"
    assert payload["correlation_id"] == "corr-1"
    assert row["rule_id"] == "rule-1" and row["source_message_id"] == 500
    assert row["target_chat_id"] == TARGET


async def test_the_fold_never_touches_a_sent_job(db, mmap, rules, queue):
    """A delivered job is the mapping's business, not the fold's."""
    await rules.add(make_rule())
    tg = EditTelegram()
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)

    await _store_job(queue, dict(
        rule_id="rule-1", source_chat_id=SOURCE, source_message_id=500,
        target_chat_id=TARGET, text="original", caption="",
    ), status="SENT")
    await _store_mapping(mmap, make_mapping(status=DeliveryStatus.SENT, target_message_id=1001))

    await engine.handle_edit(make_payload(text="EDITED"), "EDITED")

    row = queue._db.query_one(
        "SELECT payload_json FROM delivery_jobs WHERE source_message_id = ?", (500,)
    )
    assert json.loads(row["payload_json"])["text"] == "original", (
        "a SENT job must not be rewritten by the fold"
    )


# --------------------------------------------------------------------------- #
# E2E-4: a transient edit failure is retried, not dropped
# --------------------------------------------------------------------------- #

async def test_a_transient_edit_failure_is_retried(db, mmap, rules, queue):
    """One transient failure must not permanently drift source and target."""
    await rules.add(make_rule())
    tg = EditTelegram(fail_times=1, error=RuntimeError("500 INTERNAL"))
    # Keep the retry fast so the test stays deterministic in time, not in logic.
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)
    engine._sleep = lambda s: tg.sleeps.append(s) or asyncio.sleep(0)
    engine._edit_max_attempts = 3

    await _store_mapping(mmap, make_mapping(status=DeliveryStatus.SENT, target_message_id=1001))

    result = await engine.handle_edit(make_payload(text="EDITED"), "EDITED")

    assert result == 1, "the retry should succeed"
    assert len(tg.edits) == 2, f"one failed attempt plus one retry, got {len(tg.edits)}"


async def test_a_permanent_edit_failure_is_not_retried_forever(db, mmap, rules, queue):
    """A permission error will not fix itself: stop, and say so."""
    from pyrogram.errors import ChatAdminRequired
    await rules.add(make_rule())
    tg = EditTelegram(fail_times=99, error=ChatAdminRequired("400 CHAT_ADMIN_REQUIRED"))
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)
    engine._sleep = lambda s: tg.sleeps.append(s) or asyncio.sleep(0)
    engine._edit_max_attempts = 3

    await _store_mapping(mmap, make_mapping(status=DeliveryStatus.SENT, target_message_id=1001))

    result = await engine.handle_edit(make_payload(text="EDITED"), "EDITED")

    assert result == 0, "a permanent failure must not report success"
    assert len(tg.edits) == 1, (
        f"a permanent error must not be retried, got {len(tg.edits)} attempts"
    )


async def test_a_floodwait_edit_failure_retries(db, mmap, rules, queue):
    """A FloodWait is transient by definition and must be honoured."""
    await rules.add(make_rule())
    error = RuntimeError("FLOOD_WAIT_2")
    tg = EditTelegram(fail_times=1, error=error)
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)
    engine._sleep = lambda s: tg.sleeps.append(s) or asyncio.sleep(0)
    engine._edit_max_attempts = 3

    await _store_mapping(mmap, make_mapping(status=DeliveryStatus.SENT, target_message_id=1001))

    result = await engine.handle_edit(make_payload(text="EDITED"), "EDITED")

    assert result == 1, "the FloodWait retry should succeed"
    assert len(tg.sleeps) == 1, "the retry must actually back off"


# --------------------------------------------------------------------------- #
# has_mapping fails closed (unchanged, but load-bearing for the edit path)
# --------------------------------------------------------------------------- #

async def test_a_mapping_lookup_failure_claims_the_edit(db, mmap, rules, queue):
    """A broken lookup must not let the edit be re-posted as a new message."""
    await rules.add(make_rule())
    tg = EditTelegram()
    engine = SyncEngine(mmap, rules, Pool(tg), jobs_repo=queue)

    class BrokenMap:
        async def get_by_source(self, *a, **kw):
            raise RuntimeError("db unavailable")

    engine._map = BrokenMap()
    assert await engine.has_mapping(SOURCE, 500) is True
