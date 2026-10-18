"""The dead-letter screen a Telegram user sees, and the actions behind it.

The diagnostics screen is the only place a user whose sole client is Telegram
can see what failed and do something about it. Three properties are asserted:

* **The screen reads through the repository's owner-scoped contract.**
  `collect_dead_letters` takes the caller's owned rule ids and hands them to
  `list_dead_letters`; an owner with no tasks sees no entries, never the
  global queue. Another tenant's entry is not rendered at all.
* **A refused replay is reported, not swallowed.** The repository returns
  documented result codes; every one has an i18n key, so a refusal reaches the
  user in their language instead of a silent success or an English traceback.
  The pre-contract gRPC replay inserted a *second* `delivery_jobs` row that the
  UNIQUE constraint silently ignored, deleted the entry, and answered
  ``success: true`` — the message and the entry were both gone.
* **An index names a row on this user's page, never an id.** Callback payloads
  carry ``replay:<uid>:<index>``; the id is resolved from this user's in-memory
  page, so a payload cannot name another tenant's entry and a stale index is a
  no-op.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.domain.entities import DeliveryJob, DeliveryStatus  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402
from core.infrastructure.persistence.sqlite_repositories import (  # noqa: E402
    SqliteDeliveryQueueRepository,
)
from core.infrastructure.telegram import diagnostics as dg  # noqa: E402

MINE = "rule-mine-screen"
THEIRS = "rule-theirs-screen"


@pytest.fixture
def db(tmp_path):
    return SqliteDatabase(str(tmp_path / "dlq_screen.db"))


@pytest.fixture
def repo(db):
    return SqliteDeliveryQueueRepository(db)


def _t():
    """A translator that returns the key plus its params, so assertions can
    distinguish "the key was resolved" from "the fallback printed the code"."""
    def translate(key: str, **params: Any) -> str:
        rendered = " ".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"[{key}]{(' ' + rendered) if rendered else ''}"
    return translate


async def _exhaust(repo: SqliteDeliveryQueueRepository, job_id: str, attempts: int = 5) -> None:
    for _ in range(attempts):
        await repo.mark_retry(job_id, "boom", backoff_seconds=0.0)


async def _enqueue(repo: SqliteDeliveryQueueRepository, i: int, rule: str, target: str = "-100777") -> None:
    await repo.enqueue(DeliveryJob(
        id=f"job:{rule}:{i}",
        rule_id=rule,
        source_chat_id="-100111",
        source_message_id=200 + i,
        target_chat_id=target,
        status=DeliveryStatus.PENDING,
        max_attempts=5,
        payload_data={"text": f"body {i}"},
    ))


# --------------------------------------------------------------------- #
# The screen reads through the owner-scoped contract
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_screen_shows_my_entries_only(db, repo):
    await _enqueue(repo, 0, MINE)
    await _enqueue(repo, 1, THEIRS, target="-100888")
    await _exhaust(repo, f"job:{MINE}:0")
    await _exhaust(repo, f"job:{THEIRS}:1")

    collected = await dg.collect_dead_letters(queue=repo, owned_rule_ids=[MINE])
    assert [e["rule_id"] for e in collected["entries"]] == [MINE]
    rendered = dg.render_dead_letters(_t(), collected)
    assert THEIRS not in rendered


@pytest.mark.asyncio
async def test_an_owner_with_no_tasks_sees_no_entries_not_everyones(db, repo):
    await _enqueue(repo, 0, THEIRS)
    await _exhaust(repo, f"job:{THEIRS}:0")

    collected = await dg.collect_dead_letters(queue=repo, owned_rule_ids=[])
    assert collected["entries"] == []
    # And it says so, rather than rendering an empty header.
    assert dg.render_dead_letters(_t(), collected) == "[ui_dlq_empty]"


@pytest.mark.asyncio
async def test_count_matches_the_list_for_the_same_scope(db, repo):
    await _enqueue(repo, 0, MINE)
    await _enqueue(repo, 1, MINE)
    await _exhaust(repo, f"job:{MINE}:0")
    await _exhaust(repo, f"job:{MINE}:1")

    n = await dg.count_dead_letters(queue=repo, owned_rule_ids=[MINE])
    page = await dg.collect_dead_letters(queue=repo, owned_rule_ids=[MINE])
    assert n == 2
    assert len(page["entries"]) == 2 and page["more"] is False


@pytest.mark.asyncio
async def test_the_page_declares_more_instead_of_a_false_total(db, repo):
    for i in range(6):  # one page of four plus a remainder
        await _enqueue(repo, i, MINE)
        await _exhaust(repo, f"job:{MINE}:{i}")

    collected = await dg.collect_dead_letters(queue=repo, owned_rule_ids=[MINE], limit=4)
    assert len(collected["entries"]) == 4
    assert collected["more"] is True
    # "more" means the total is unknown, not "four".
    assert collected["total"] is None
    rendered = dg.render_dead_letters(_t(), collected)
    assert "ui_dlq_more" in rendered


# --------------------------------------------------------------------- #
# A refused replay is reported in the caller's language
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_every_documented_result_code_has_an_i18n_key(repo):
    """A refusal the user cannot read is a refusal they cannot act on."""
    import json

    path = Path(__file__).resolve().parents[2] / "core/infrastructure/telegram/locales/translations.json"
    locales = json.loads(path.read_text(encoding="utf-8"))
    langs = locales["locales"] if isinstance(locales, dict) and "locales" in locales else locales

    for code in ("requeued", "not_found", "already_pending", "already_sent", "replay_failed"):
        key = dg.result_code_key(code)
        assert key, f"result code {code} has no i18n key"
        for lang, catalog in langs.items():
            assert key in catalog, f"{lang} is missing {key} (result code {code})"


@pytest.mark.asyncio
async def test_replay_reports_the_refusal_and_keeps_the_entry(db, repo):
    """`already_sent` must reach the user, and the entry must survive it."""
    await _enqueue(repo, 0, MINE)
    await _exhaust(repo, f"job:{MINE}:0")
    # The store now records this delivery as sent.
    db.execute(
        "UPDATE delivery_jobs SET status='SENT' WHERE id=?", (f"job:{MINE}:0",)
    )

    result = await dg.replay_dead_letter(
        queue=repo, entry_id=f"dlq_job:{MINE}:0", owned_rule_ids=[MINE]
    )
    assert result == "already_sent"
    key = dg.result_code_key(result)
    assert key == "ui_dlq_already_sent"

    rows = db.query_all("SELECT id FROM dead_letter_queue WHERE id=?", (f"dlq_job:{MINE}:0",))
    assert len(rows) == 1, "a refused replay consumed the entry anyway"


@pytest.mark.asyncio
async def test_replay_through_the_screen_actually_requeues(db, repo):
    await _enqueue(repo, 0, MINE)
    await _exhaust(repo, f"job:{MINE}:0")

    result = await dg.replay_dead_letter(
        queue=repo, entry_id=f"dlq_job:{MINE}:0", owned_rule_ids=[MINE]
    )
    assert result == "requeued"
    job = db.query_one("SELECT status, attempts FROM delivery_jobs WHERE id=?", (f"job:{MINE}:0",))
    assert dict(job)["status"] == "PENDING"
    assert dict(job)["attempts"] == 0
    left = db.query_all("SELECT id FROM dead_letter_queue")
    assert len(left) == 0


@pytest.mark.asyncio
async def test_another_tenants_entry_is_not_found_not_an_error(repo, db):
    await _enqueue(repo, 0, THEIRS)
    await _exhaust(repo, f"job:{THEIRS}:0")

    result = await dg.replay_dead_letter(
        queue=repo, entry_id=f"dlq_job:{THEIRS}:0", owned_rule_ids=[MINE]
    )
    # Identical to an unknown id: no probe for existence.
    assert result == "not_found"


@pytest.mark.asyncio
async def test_a_missing_queue_reports_replay_failed_not_an_exception():
    """Reading never raises; a missing store degrades to a reported failure."""
    result = await dg.replay_dead_letter(
        queue=None, entry_id="dlq_x", owned_rule_ids=[MINE]
    )
    assert result == "replay_failed"


@pytest.mark.asyncio
async def test_purge_reports_the_scope_count(db, repo):
    await _enqueue(repo, 0, MINE)
    await _enqueue(repo, 1, THEIRS, target="-100888")
    await _exhaust(repo, f"job:{MINE}:0")
    await _exhaust(repo, f"job:{THEIRS}:1")

    assert await dg.purge_dead_letters(queue=repo, owned_rule_ids=[MINE]) == 1
    # The other tenant's entry is untouched.
    assert len(db.query_all("SELECT id FROM dead_letter_queue")) == 1


# --------------------------------------------------------------------- #
# The payload carries an index, and the resolution honours it
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_index_resolves_against_this_users_page_only(db, repo):
    """The id never travels in the payload; the page is the authority."""
    await _enqueue(repo, 0, MINE)
    await _enqueue(repo, 1, THEIRS, target="-100888")
    await _exhaust(repo, f"job:{MINE}:0")
    await _exhaust(repo, f"job:{THEIRS}:1")

    mine = await dg.collect_dead_letters(queue=repo, owned_rule_ids=[MINE])
    theirs = await dg.collect_dead_letters(queue=repo, owned_rule_ids=[THEIRS])

    # The same index (0) names a different entry per owner's page: the payload
    # alone never identifies an entry.
    assert mine["entries"][0]["rule_id"] == MINE
    assert theirs["entries"][0]["rule_id"] == THEIRS

    # And replaying through MY page cannot touch THEIRS' first entry.
    result = await dg.replay_dead_letter(
        queue=repo, entry_id=mine["entries"][0]["id"], owned_rule_ids=[MINE]
    )
    assert result == "requeued"
    left = db.query_all("SELECT rule_id FROM dead_letter_queue")
    assert [r[0] for r in left] == [THEIRS]


# --------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_screen_neither_shows_nor_replays_payload_secrets(db, repo):
    await repo.enqueue(DeliveryJob(
        id="job:secret", rule_id=MINE, source_chat_id="-100111",
        source_message_id=999, target_chat_id="-100777",
        status=DeliveryStatus.PENDING, max_attempts=5,
        payload_data={"text": "hi", "api_key": "sk-liv...screen"},
    ))
    await _exhaust(repo, "job:secret")

    collected = await dg.collect_dead_letters(queue=repo, owned_rule_ids=[MINE])
    rendered = dg.render_dead_letters(_t(), collected)
    assert "sk-liv...screen" not in rendered
    # The error column is what the screen shows, not the payload.
    assert "boom" in rendered


# --------------------------------------------------------------------- #
# The repository contract the screen depends on
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_filters_are_part_of_the_read_not_post_processing(db, repo):
    """`rule_id` narrows inside the scope; a filter must not widen it."""
    await _enqueue(repo, 0, MINE)
    await _enqueue(repo, 1, THEIRS, target="-100888")
    await _exhaust(repo, f"job:{MINE}:0")
    await _exhaust(repo, f"job:{THEIRS}:1")

    # Asking for THEIRS' rule while scoped to MINE sees nothing.
    rows = await repo.list_dead_letters([MINE], limit=10, rule_id=THEIRS)
    assert rows == []
    # And the empty scope still sees nothing even with a rule_id filter.
    assert await repo.list_dead_letters([], limit=10, rule_id=MINE) == []
