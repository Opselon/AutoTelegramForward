"""A dead letter must be visible to its owner, and replayable exactly once.

A dead letter is the only queue state a user can act on: the delivery exhausted
its retry budget, nothing will happen unless a person intervenes, and the
message is sitting in `payload_json` waiting. Two properties make it worth
acting on, and both are asserted here:

* **Visibility is scoped.** The dead-letter table is global. A tenant sees an
  entry only when the entry's rule is one it owns, and an empty scope means
  *nothing* — never "no filter". An id belonging to another tenant answers
  `not_found`, identically to an id that does not exist, so it cannot be used
  to probe for someone else's traffic.
* **Replay delivers once.** Replaying re-queues the message with a fresh
  attempt budget and drops the entry, in one transaction — and refuses when the
  same source message is already in flight for that target, because two live
  jobs aimed at one channel is a duplicate post.

The regression this file was written for: the dead-letter entry used to be
written by two different copies of the same INSERT, and the copy in
`mark_dead_letter` named `error_message` — a column the schema does not have —
inside a bare `except: pass`. That path therefore recorded nothing and said
nothing, leaving a job nobody could see or retry.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.domain.entities import DeliveryJob, DeliveryStatus  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402
from core.infrastructure.persistence.sqlite_repositories import (  # noqa: E402
    SqliteDeliveryQueueRepository,
)

MINE = "rule-mine-0001"
THEIRS = "rule-theirs-0002"
PAYLOAD_SECRET = "sk-live-DEADBEEF-not-for-logs"


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "atf_dlq.db"))


@pytest.fixture
def repo(db):
    return SqliteDeliveryQueueRepository(db)


def _job(
    i: int,
    rule_id: str = MINE,
    status: DeliveryStatus = DeliveryStatus.PENDING,
    target: str = "-100777",
    source: str = "-100111",
    max_attempts: int = 5,
) -> DeliveryJob:
    return DeliveryJob(
        id=f"job:{i}",
        rule_id=rule_id,
        source_chat_id=source,
        source_message_id=100 + i,
        target_chat_id=target,
        status=status,
        max_attempts=max_attempts,
        payload_data={"text": f"hello {i}", "api_key": PAYLOAD_SECRET},
    )


async def _exhaust(repo, job_id: str, attempts: int = 5, error: str = "boom") -> None:
    """Drive a job through its retry budget until it is dead-lettered."""
    for _ in range(attempts):
        await repo.mark_retry(job_id, error, backoff_seconds=0.0)


# --------------------------------------------------------------------- #
# Visibility is scoped
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_exhausted_job_is_listed_for_its_owner(db, repo):
    await repo.enqueue(_job(0))
    await _exhaust(repo, "job:0", error="chat write forbidden")

    entries = await repo.list_dead_letters([MINE])

    assert len(entries) == 1
    entry = entries[0]
    assert entry["rule_id"] == MINE
    assert entry["job_id"] == "job:0"
    assert entry["last_error"] == "chat write forbidden"
    assert entry["error_category"] == "DELIVERY_EXHAUSTED"
    assert entry["attempts"] == 5
    assert entry["target_chat_id"] == "-100777"


@pytest.mark.asyncio
async def test_another_tenants_dead_letter_is_not_listed(db, repo):
    await repo.enqueue(_job(0, rule_id=MINE))
    await repo.enqueue(_job(1, rule_id=THEIRS, target="-100888"))
    await _exhaust(repo, "job:0")
    await _exhaust(repo, "job:1")

    mine = await repo.list_dead_letters([MINE])

    assert [e["job_id"] for e in mine] == ["job:0"]
    # ... and the other tenant really does have one, so this is a filter and
    # not an empty table.
    assert len(await repo.list_dead_letters([THEIRS])) == 1


@pytest.mark.asyncio
async def test_an_empty_scope_sees_nothing_at_all(db, repo):
    await repo.enqueue(_job(0))
    await _exhaust(repo, "job:0")

    # A tenant that owns no rules must not read the whole queue: an empty scope
    # is "match nothing", never "no filter".
    assert await repo.list_dead_letters([]) == []
    assert await repo.list_dead_letters(set()) == []
    # ... and only an explicitly unscoped caller sees everything.
    assert len(await repo.list_dead_letters(None)) == 1


@pytest.mark.asyncio
async def test_entries_are_newest_first(db, repo):
    await repo.enqueue(_job(0, target="-100777"))
    await repo.enqueue(_job(1, target="-100888"))
    await _exhaust(repo, "job:0")
    await _exhaust(repo, "job:1")

    entries = await repo.list_dead_letters([MINE])

    assert entries[0]["dead_lettered_at"] >= entries[-1]["dead_lettered_at"]
    assert len(entries) == 2


# --------------------------------------------------------------------- #
# Replay is scoped
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replaying_another_tenants_entry_is_not_found(db, repo):
    await repo.enqueue(_job(1, rule_id=THEIRS, target="-100888"))
    await _exhaust(repo, "job:1")
    entry_id = f"dlq_job:1"

    result = await repo.replay_dead_letter(entry_id, [MINE])

    assert result == "not_found"
    # Nothing was requeued and the other tenant's entry is untouched.
    rows = db.query_all("SELECT id FROM delivery_jobs WHERE id LIKE 'replay%'")
    assert rows == []
    assert len(await repo.list_dead_letters([THEIRS])) == 1


@pytest.mark.asyncio
async def test_replaying_an_unknown_entry_is_not_found(repo):
    assert await repo.replay_dead_letter("dlq_does-not-exist", [MINE]) == "not_found"


@pytest.mark.asyncio
async def test_replaying_with_an_empty_scope_is_not_found(db, repo):
    await repo.enqueue(_job(0))
    await _exhaust(repo, "job:0")

    assert await repo.replay_dead_letter("dlq_job:0", []) == "not_found"
    assert len(await repo.list_dead_letters(None)) == 1


# --------------------------------------------------------------------- #
# Replay delivers once
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replay_wakes_the_delivery_again_and_drops_the_entry(db, repo):
    await repo.enqueue(_job(0, max_attempts=3))
    await _exhaust(repo, "job:0", attempts=3, error="chat write forbidden")

    assert await repo.replay_dead_letter("dlq_job:0", [MINE]) == "requeued"

    # `delivery_jobs` is UNIQUE(rule_id, source_chat_id, source_message_id,
    # target_chat_id), so a replay is the same delivery starting over — never a
    # second row. That constraint is what makes a replay unable to double-post.
    rows = db.query_all("SELECT * FROM delivery_jobs")
    assert len(rows) == 1
    fresh = dict(rows[0])
    assert fresh["id"] == "job:0"
    assert fresh["status"] == "PENDING"
    assert fresh["attempts"] == 0
    # The original budget travels with the replay: a user retrying a delivery
    # gets the budget that delivery was configured with, not a new guess.
    assert fresh["max_attempts"] == 3
    assert fresh["rule_id"] == MINE
    assert "hello 0" in fresh["payload_json"]
    # Nothing left over from the failed attempt can stall the new one: a stale
    # lease or worker id would keep it unclaimable until the lease expired.
    assert fresh["next_retry_at"] == 0.0 and fresh["lease_until"] == 0.0
    assert fresh["error_detail"] == "" and fresh["worker_id"] == ""

    # The entry is consumed, so the screen cannot offer the same retry twice.
    assert await repo.list_dead_letters([MINE]) == []


async def test_replay_twice_never_delivers_twice(db, repo):
    await repo.enqueue(_job(0))
    await _exhaust(repo, "job:0")

    assert await repo.replay_dead_letter("dlq_job:0", [MINE]) == "requeued"
    assert await repo.replay_dead_letter("dlq_job:0", [MINE]) == "not_found"

    assert len(db.query_all("SELECT id FROM delivery_jobs")) == 1


async def test_replay_refuses_while_the_delivery_is_live(db, repo):
    await repo.enqueue(_job(0))
    await _exhaust(repo, "job:0")
    # The delivery was revived (a worker holds it) while its entry still sits in
    # the queue — the state a stale entry beside a live job leaves behind.
    db.execute("UPDATE delivery_jobs SET status='RETRY_WAIT', attempts=1, lease_until=0 WHERE id='job:0'")

    assert await repo.replay_dead_letter("dlq_job:0", [MINE]) == "already_pending"

    # The entry survives for a later attempt, and the live delivery is untouched.
    assert len(await repo.list_dead_letters([MINE])) == 1
    live = dict(db.query_one("SELECT status, attempts FROM delivery_jobs WHERE id='job:0'"))
    assert live == {"status": "RETRY_WAIT", "attempts": 1}


@pytest.mark.asyncio
async def test_replay_refuses_a_message_the_store_says_was_sent(db, repo):
    await repo.enqueue(_job(0))
    await _exhaust(repo, "job:0")
    db.execute("UPDATE delivery_jobs SET status='SENT' WHERE id='job:0'")

    assert await repo.replay_dead_letter("dlq_job:0", [MINE]) == "already_sent"

    # Untouched: a delivered message must not be posted a second time.
    assert dict(db.query_one("SELECT status FROM delivery_jobs WHERE id='job:0'"))["status"] == "SENT"
    assert len(await repo.list_dead_letters([MINE])) == 1


async def test_replay_keeps_the_intermediate_hop(db, repo):
    job = _job(0)
    job.intermediate_chat_id = "-100999"
    job.intermediate_message_id = 4242
    job.delivery_stage = "COPIED"
    await repo.enqueue(job)
    await _exhaust(repo, "job:0")

    assert await repo.replay_dead_letter("dlq_job:0", [MINE]) == "requeued"

    fresh = dict(db.query_one("SELECT * FROM delivery_jobs WHERE id='job:0'"))
    assert fresh["intermediate_chat_id"] == "-100999"
    assert fresh["intermediate_message_id"] == 4242
    assert fresh["delivery_stage"] == "COPIED"


async def test_replay_rebuilds_a_purged_delivery_from_its_own_entry(db, repo):
    await repo.enqueue(_job(0, max_attempts=3))
    await _exhaust(repo, "job:0", attempts=3)
    # `cleanup()` purges terminal jobs on a window, so the entry can outlive the
    # row it came from.
    db.execute("DELETE FROM delivery_jobs WHERE id='job:0'")

    assert await repo.replay_dead_letter("dlq_job:0", [MINE]) == "requeued"

    fresh = dict(db.query_one("SELECT * FROM delivery_jobs"))
    assert fresh["id"] == "job:0"
    assert fresh["status"] == "PENDING" and fresh["attempts"] == 0
    # The entry remembers how many attempts were spent, so the rebuilt delivery
    # gets its own budget back rather than the class default.
    assert fresh["max_attempts"] == 3
    assert "hello 0" in fresh["payload_json"]


async def test_pending_counter_follows_a_replay(db, repo):
    await repo.enqueue(_job(0))
    before = await repo.get_pending_count()
    await _exhaust(repo, "job:0")
    empty = await repo.get_pending_count()

    await repo.replay_dead_letter("dlq_job:0", [MINE])

    # The count is tracked in memory, so it only stays honest if every mutator
    # updates it — a replayed job that is invisible to backpressure reads low.
    assert empty == before - 1
    assert await repo.get_pending_count() == empty + 1


# --------------------------------------------------------------------- #
# The entry is written by one producer, and never silently lost
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mark_dead_letter_records_the_entry(db, repo):
    """The regression: this path named a column the schema does not have."""
    await repo.enqueue(_job(0))

    assert await repo.mark_dead_letter("job:0", "bot was kicked", "CHAT_UNAVAILABLE") is True

    entries = await repo.list_dead_letters([MINE])
    assert len(entries) == 1
    assert entries[0]["last_error"] == "bot was kicked"
    assert entries[0]["error_category"] == "CHAT_UNAVAILABLE"
    assert entries[0]["payload_json"]  # replayable, i.e. the payload came along
    assert dict(db.query_one("SELECT status FROM delivery_jobs WHERE id='job:0'"))["status"] == "DEAD_LETTER"


@pytest.mark.asyncio
async def test_mark_dead_letter_reports_a_missing_job_instead_of_saying_ok(db, repo):
    # Nothing to dead-letter: the caller must not be told the entry exists.
    assert await repo.mark_dead_letter("job:nope", "gone") is False
    assert await repo.list_dead_letters(None) == []


@pytest.mark.asyncio
async def test_a_lost_entry_is_logged(db, repo, caplog):
    caplog.set_level(logging.WARNING, logger="core.infrastructure.persistence.sqlite_repositories")

    await repo.mark_dead_letter("job:nope", "gone")

    assert any("dead-letter entry" in r.message for r in caplog.records)


# --------------------------------------------------------------------- #
# Purge
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_purge_only_touches_the_scope(db, repo):
    await repo.enqueue(_job(0))
    await repo.enqueue(_job(1, rule_id=THEIRS, target="-100888"))
    await _exhaust(repo, "job:0")
    await _exhaust(repo, "job:1")

    assert await repo.purge_dead_letters([MINE]) == 1
    assert len(await repo.list_dead_letters([THEIRS])) == 1
    assert await repo.purge_dead_letters([]) == 0
    assert await repo.purge_dead_letters(None) == 1


@pytest.mark.asyncio
async def test_purge_can_age_out_old_entries(db, repo):
    await repo.enqueue(_job(0))
    await _exhaust(repo, "job:0")
    future = db.query_one("SELECT dead_lettered_at FROM dead_letter_queue")[0] + 10

    assert await repo.purge_dead_letters([MINE], older_than=future) == 1


@pytest.mark.asyncio
async def test_filters_narrow_within_the_scope_before_the_limit(db, repo):
    """`rule_id`/`since` are WHERE clauses, applied before LIMIT.

    Filtering a truncated page would answer "no such entry" for an entry that
    is merely older than the page — and a count is a promise about the total,
    which a truncated page cannot make.
    """
    for i in range(3):
        await repo.enqueue(_job(i, rule_id=THEIRS, target="-100888"))
        await _exhaust(repo, f"job:{i}")

    page = await repo.list_dead_letters([THEIRS], limit=2, rule_id=THEIRS)
    assert len(page) == 2 and all(r["rule_id"] == THEIRS for r in page)

    recent_cutoff = int(page[0]["dead_lettered_at"]) + 1
    later_rows = await repo.list_dead_letters([THEIRS], limit=10, rule_id=THEIRS, since=recent_cutoff)
    assert later_rows == []  # entries that exist but are not yet `since`


@pytest.mark.asyncio
async def test_the_count_is_scoped_like_the_list(db, repo):
    await repo.enqueue(_job(0))
    await repo.enqueue(_job(1, rule_id=THEIRS, target="-100888"))
    await _exhaust(repo, "job:0")
    await _exhaust(repo, "job:1")

    assert await repo.count_dead_letters([MINE]) == 1
    assert await repo.count_dead_letters([THEIRS]) == 1
    # Empty scope owns nothing ⇒ a count of zero, not the global total.
    assert await repo.count_dead_letters([]) == 0


# --------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replay_logs_ids_but_never_the_payload(db, repo, caplog):
    caplog.set_level(logging.DEBUG)
    await repo.enqueue(_job(0))
    await _exhaust(repo, "job:0")

    await repo.replay_dead_letter("dlq_job:0", [MINE])

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "dlq_job:0" in logged          # the handle a user quotes
    assert PAYLOAD_SECRET not in logged   # the message body stays in the store
