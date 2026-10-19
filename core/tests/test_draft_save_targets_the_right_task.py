"""A staged draft must never be written onto the wrong task.

The draft editor stages the task's settings into the user's UI-state buffer and
``ed_save:<id>`` applies them. The handler checked ownership of the task named in
the callback and that a draft existed — but never that the staged draft belonged
to *that* task. Two tasks commonly share a version, so the optimistic-concurrency
check cannot catch it either: it compares against a version that came from the
same wrong buffer.

The result is silent cross-task corruption: with task A's draft staged, a
leftover ``ed_save:B`` wrote A's whole configuration — name, endpoints, every
switch — onto B, and the user was told "saved successfully".

The fix binds the staged draft to the task it was staged from: a mismatched Save
is refused, the stale buffer is dropped, and the user is told the draft expired
rather than being told it saved.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from core.tests.bot_dispatch import BotHarness

OWNER = 5094837833
OTHER = 5989354154


@pytest.fixture
def harness(tmp_path):
    h = BotHarness(os.path.join(tmp_path, "draft.db"), admin_ids=[OWNER])
    try:
        yield h
    finally:
        h.close()


def run(coro):
    return asyncio.run(coro)


def stage_draft(h, user: int, rule_id: str, name: str) -> None:
    """Put a draft for ``rule_id`` into the user's buffer, as the editor does."""
    st = h.ui._state(user)
    st.step = "rule_editing_draft"
    st.buffer = {
        "rule_id": rule_id,
        "version": 1,
        "draft": {
            "source_chat_id": "-1004400615270",
            "target_chat_id": "-1003915313919",
            "name": name,
        },
        "in_draft": True,
    }


def test_a_leftover_save_cannot_write_task_as_draft_onto_task_b(harness):
    run(harness.seed_session("sess-a", OWNER))
    run(harness.seed_rule("task-a", OWNER, name="Task A"))
    run(harness.seed_rule("task-b", OWNER, name="Task B"))

    # Task A's draft is staged — the user edited A and has not saved yet.
    stage_draft(harness, OWNER, "task-a", "Task A (renamed)")
    before_b = harness.rule_row("task-b")

    cq = run(harness.press("ed_save:task-b", OWNER))

    after_b = harness.rule_row("task-b")
    assert after_b.get("source_chat_id") == before_b.get("source_chat_id"), (
        "task B absorbed task A's staged draft — silent cross-task corruption"
    )
    assert not any("saved" in t.lower() and "success" in t.lower() for t in cq.answers.texts), (
        f"the wrong-task save reported success: {cq.answers.texts}"
    )
    assert cq.answers.texts, "the refusal must be shown to the user, not swallowed"


def test_the_draft_itself_still_saves_onto_its_own_task(harness):
    run(harness.seed_session("sess-a", OWNER))
    run(harness.seed_rule("task-a", OWNER, name="Task A"))

    stage_draft(harness, OWNER, "task-a", "Task A (renamed)")
    cq = run(harness.press("ed_save:task-a", OWNER))

    assert harness.rule_row("task-a")["source_chat_id"] == "-1004400615270"
    assert cq.answers.texts, "the successful save must confirm to the user"


def test_the_stale_buffer_is_dropped_after_a_refusal(harness):
    """A refused save must not leave the wrong draft armed for the next press."""
    run(harness.seed_session("sess-a", OWNER))
    run(harness.seed_rule("task-a", OWNER, name="Task A"))
    run(harness.seed_rule("task-b", OWNER, name="Task B"))
    stage_draft(harness, OWNER, "task-a", "Task A (renamed)")
    run(harness.press("ed_save:task-b", OWNER))

    st = harness.ui._state(OWNER)
    assert not (st.buffer or {}).get("draft"), "the refused draft is still staged"


def test_another_user_cannot_save_a_draft_he_never_staged(harness):
    """The buffer is per-user; ownership is checked before the draft is used."""
    run(harness.seed_session("sess-a", OWNER))
    run(harness.seed_rule("task-a", OWNER, name="Task A"))
    stage_draft(harness, OTHER, "task-a", "Hijacked")

    run(harness.press("ed_save:task-a", OTHER))

    assert harness.rule_row("task-a").get("source_chat_id", "-1004400615270") == "-1004400615270"
