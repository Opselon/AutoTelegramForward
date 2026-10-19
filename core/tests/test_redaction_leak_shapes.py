"""Part 2 STEER 09 — the log tells the truth and carries no credential.

A delivery failure is the one place where a *third party's* exception text is
persisted (``delivery_jobs.error_detail``, the dead-letter entry) and logged, and
that text is the one an SDK builds out of the request it failed on:

* an HTTP client puts the request URL in its connection/status error, and the
  Bot API URL *is* ``https://api.telegram.org/bot<id>:<secret>/sendMessage``;
* a database/queue/cache client puts its DSN in its connection error, and a DSN
  carries the password: ``redis://:<password>@host``.

``tg_detail`` funnels every failure through ``redact()``, so the redactor is the
only thing between that exception text and an operator's log or the dashboard's
"last error" field. It masked nothing in either shape:

* ``_BOT_TOKEN`` was anchored with ``\\b``, which does not exist between the
  ``t`` of ``bot`` and the first digit — so it matched the *spaced prose* form
  from its own unit test and no real request URL;
* URL userinfo had no rule at all, so every DSN password was stored in clear
  text (only ``password=…``/``token=…`` key-value shapes were covered).

Both are proved end to end here: a real ``SqliteDeliveryQueueRepository`` stores
a redacted reason, and the dead-letter entry carries none of it either.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.domain.entities import ForwardRule, MessagePayload, TelegramSession
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDeliveryQueueRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.durable_queue import DurableQueueManager
from core.infrastructure.telegram.errors import redact, tg_detail

OWNER = 5094837833
SOURCE = "-1004400615270"
TARGET = "-1003915313919"
BOT_SECRET = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
BOT_TOKEN = f"123456789:{BOT_SECRET}"
OWNERLESS_PASSWORD = "hunter2-not-a-real-password"


class TransportError(Exception):
    """Shaped like an SDK transport error: the message carries the request URL."""


@pytest.fixture
def db(tmp_path):
    database = SqliteDatabase(str(tmp_path / "redaction.db"))
    try:
        yield database
    finally:
        database.close()


def _seed(db) -> None:
    async def run():
        sessions = SqliteSessionRepository(db, CryptoService("test-key"))
        await sessions.add(TelegramSession(
            id="sess-r", phone_number="+100****0000", is_active=True,
            is_authorized=True, owner_user_id=OWNER,
        ))
        await SqliteForwardRuleRepository(db).add(ForwardRule(
            id="rule-r", session_id="sess-r", source_chat_id=SOURCE,
            target_chat_id=TARGET, is_active=True, owner_user_id=OWNER,
        ))

    asyncio.run(run())


def _enqueue(db) -> str:
    async def run():
        rule = await SqliteForwardRuleRepository(db).get_by_id("rule-r")
        queue = DurableQueueManager(
            queue_repo=SqliteDeliveryQueueRepository(db),
            message_map_repo=SqliteMessageMapRepository(db),
            client_pool=None,
            rule_repo=SqliteForwardRuleRepository(db),
            num_workers=1,
        )
        assert await queue.enqueue_delivery(
            rule, MessagePayload(message_id=41, chat_id=SOURCE, text="hello"), TARGET, "hello",
        ) is True
        row = db.query_one("SELECT id FROM delivery_jobs LIMIT 1")
        return str(row["id"])

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# The two shapes that leaked
# --------------------------------------------------------------------------- #
def test_a_bot_token_in_a_request_url_is_masked():
    """The shape an HTTP client puts in its error: the URL it failed on."""
    raw = (
        "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
        f"with url: /bot{BOT_TOKEN}/sendMessage (Caused by ConnectTimeoutError(...))"
    )
    out = redact(raw)
    assert BOT_SECRET not in out, out
    assert "[REDACTED]" in out, out


def test_a_bot_token_in_a_full_url_is_masked():
    out = redact(f"Client error '404 Not Found' for url 'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'")
    assert BOT_SECRET not in out, out
    assert "[REDACTED]" in out, out


@pytest.mark.parametrize(
    "dsn",
    [
        f"postgresql://atf:{OWNERLESS_PASSWORD}@db:5432/atf",
        f"postgres://atf:{OWNERLESS_PASSWORD}@db:5432/atf",
        f"postgresql://:{OWNERLESS_PASSWORD}@db:5432/atf",
        f"redis://:{OWNERLESS_PASSWORD}@redis:6379/0",
        f"rediss://:{OWNERLESS_PASSWORD}@redis:6379/0",
        f"redis://default:{OWNERLESS_PASSWORD}@redis:6379/0",
        f"amqp://atf:{OWNERLESS_PASSWORD}@rabbit:5672/",
        f"https://atf:{OWNERLESS_PASSWORD}@hooks.example.com/atf",
        f"smtps://atf:{OWNERLESS_PASSWORD}@smtp.example.com:465",
    ],
)
def test_a_dsn_password_is_masked(dsn):
    out = redact(f"connection failed: {dsn}")
    assert OWNERLESS_PASSWORD not in out, out
    assert "[REDACTED]" in out, out
    # The destination must survive: an operator needs to know *which* peer failed.
    assert "db:5432" in out or "redis:6379" in out or "rabbit:5672" in out \
        or "hooks.example.com" in out or "smtp.example.com" in out, out


def test_an_ordinary_url_is_left_alone():
    """Over-redaction hides the very defect the line exists to report."""
    for raw in (
        "https://example.com:8443/hook",
        "https://t.me/ad",
        "https://api.telegram.org/bot/getMe",
        "chat_id=-1001234567890",
        "forwarded 42 messages",
        "msg_id=930629",
    ):
        assert redact(raw) == raw, raw


# --------------------------------------------------------------------------- #
# End to end: what actually gets stored and logged
# --------------------------------------------------------------------------- #
def test_the_stored_delivery_reason_carries_no_secret(db):
    _seed(db)
    job_id = _enqueue(db)
    detail = tg_detail(TransportError(
        f"Max retries exceeded with url: /bot{BOT_TOKEN}/sendMessage"
    ))
    assert BOT_SECRET not in detail, detail

    asyncio.run(SqliteDeliveryQueueRepository(db).mark_retry(job_id, detail, 5.0))
    row = db.query_one("SELECT status, attempts, error_detail FROM delivery_jobs WHERE id = ?", (job_id,))
    assert row["status"] == "RETRY_WAIT"
    assert BOT_SECRET not in (row["error_detail"] or ""), row["error_detail"]
    assert "[REDACTED]" in (row["error_detail"] or ""), row["error_detail"]


def test_the_dead_letter_entry_carries_no_secret(db):
    _seed(db)
    job_id = _enqueue(db)
    repo = SqliteDeliveryQueueRepository(db)
    detail = tg_detail(TransportError(
        f"connection failed: redis://:{OWNERLESS_PASSWORD}@redis:6379/0"
    ))
    assert OWNERLESS_PASSWORD not in detail, detail

    async def exhaust():
        for _ in range(10):
            await repo.mark_retry(job_id, detail, 0.0)

    asyncio.run(exhaust())
    job = db.query_one("SELECT status, attempts, max_attempts FROM delivery_jobs WHERE id = ?", (job_id,))
    assert job["status"] == "FAILED", dict(job)

    entry = db.query_one("SELECT last_error, payload_json FROM dead_letter_queue WHERE job_id = ?", (job_id,))
    assert entry is not None, "an exhausted job must be visible to an operator"
    assert OWNERLESS_PASSWORD not in (entry["last_error"] or ""), entry["last_error"]
    assert OWNERLESS_PASSWORD not in json.dumps(dict(entry)), dict(entry)


def test_a_reason_that_holds_no_secret_is_stored_verbatim(db):
    """Redaction must not rewrite an ordinary, actionable reason."""
    _seed(db)
    job_id = _enqueue(db)
    asyncio.run(SqliteDeliveryQueueRepository(db).mark_retry(job_id, "Flood wait 42s", 30.0))
    row = db.query_one("SELECT error_detail FROM delivery_jobs WHERE id = ?", (job_id,))
    assert row["error_detail"] == "Flood wait 42s", row["error_detail"]
