"""`/forward list|edit|delete` and the bulk screen, end to end.

The harness is the real panel over a real SQLite store: the commands are called
as the dispatcher would call them, and the bulk screen is driven by pressing its
real callback payloads. Every assertion is about what happened to stored tasks,
not about a render helper's return value.

The acceptance list this file covers:

* a task is addressed by its stable id (and by its name), never by a row number;
* another tenant's id is answered exactly like a missing one, and never renders
  their task's name;
* delete asks first and only then writes;
* a batch reports each rule separately, survives one failing rule, refuses an
  empty selection, and prunes a selection whose rules were deleted elsewhere.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.tests.bot_dispatch import BotHarness

OWNER = 5094837833
OTHER = 7711223344

TASK_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TASK_B = "b1b2c3d4e5f60718293a4b5c6d7e8f91"


class Msg:
    """The slice of a pyrogram Message these handlers read."""

    def __init__(self, uid: int, text: str) -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.text = text
        self.sent: list = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))
        return SimpleNamespace(text=text)

    @property
    def last(self) -> str:
        return self.sent[-1][0] if self.sent else ""


def edits(cq) -> str:
    """The last screen a pressed callback rendered."""
    for kind, text, _kw in reversed(cq.message.sent):
        if kind == "edit_message_text":
            return text
    return ""


async def forward(h: BotHarness, uid: int, text: str) -> Msg:
    msg = Msg(uid, text)
    await h.ui._handle_forward_command(msg, uid)
    return msg


async def seed(h: BotHarness) -> None:
    await h.seed_session("sess-a", OWNER)
    await h.seed_session("sess-b", OTHER)
    await h.seed_rule(TASK_A, OWNER, name="Nightly", session_id="sess-a")
    await h.seed_rule(TASK_B, OWNER, name="Daily", session_id="sess-a")


@pytest.fixture
def harness(tmp_path):
    h = BotHarness(str(tmp_path / "fwd.db"))
    asyncio.run(seed(h))
    yield h
    h.close()


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def test_list_shows_every_task_with_its_stable_short_id(harness):
    msg = asyncio.run(forward(harness, OWNER, "/forward list"))
    assert "Nightly" in msg.last and "Daily" in msg.last
    assert f"#{TASK_A[:8]}" in msg.last
    assert f"#{TASK_B[:8]}" in msg.last
    assert "`" in msg.last and "2" in msg.last


def test_list_filters_by_name_and_says_so_when_nothing_matches(harness):
    msg = asyncio.run(forward(harness, OWNER, "/forward list night"))
    assert "Nightly" in msg.last and "Daily" not in msg.last

    empty = asyncio.run(forward(harness, OWNER, "/forward list zzz"))
    assert "zzz" in empty.last and "Nightly" not in empty.last


# --------------------------------------------------------------------------- #
# edit
# --------------------------------------------------------------------------- #
def test_edit_by_id_renders_that_task_only(harness):
    msg = asyncio.run(forward(harness, OWNER, f"/forward edit {TASK_A}"))
    assert "Nightly" in msg.last and "Daily" not in msg.last
    assert f"#{TASK_A[:8]}" in msg.last
    assert "-1004400615270" in msg.last        # the seeded source


def test_edit_by_name_uses_the_same_resolver(harness):
    msg = asyncio.run(forward(harness, OWNER, "/forward edit daily"))
    assert "Daily" in msg.last and "Nightly" not in msg.last


def test_edit_with_an_unknown_id_explains_and_opens_the_list(harness):
    msg = asyncio.run(forward(harness, OWNER, "/forward edit #deadbeef"))
    assert "Nothing matches" in msg.sent[0][0]
    assert any("Nightly" in text for text, _kbd in msg.sent[1:])


def test_a_foreign_task_id_is_answered_as_missing(harness):
    msg = asyncio.run(forward(harness, OTHER, f"/forward edit {TASK_A}"))
    assert "Nothing matches" in msg.sent[0][0]
    assert "Nightly" not in msg.sent[0][0]


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
def test_delete_asks_first_and_writes_nothing(harness):
    msg = asyncio.run(forward(harness, OWNER, f"/forward delete {TASK_A}"))
    assert "Nightly" in msg.last and "cannot be undone" in msg.last
    assert harness.rule_row(TASK_A) != {}
    assert any(getattr(btn, "callback_data", "") == f"rdel:{TASK_A}"
               for row in msg.sent[-1][1].inline_keyboard for btn in row)


# --------------------------------------------------------------------------- #
# bulk
# --------------------------------------------------------------------------- #
def test_bulk_screen_lists_tasks_with_checkboxes(harness):
    cq = asyncio.run(harness.press("fml:open", OWNER))
    body = edits(cq)
    assert "0/2" in body
    assert "⬜" in body and "Nightly" in body


def test_selecting_one_task_updates_the_counter(harness):
    asyncio.run(harness.press("fml:open", OWNER))
    cq = asyncio.run(harness.press(f"fml:t:{TASK_A}", OWNER))
    assert "1/2" in edits(cq) and "✅" in edits(cq)


def test_bulk_disable_applies_to_every_selected_task(harness):
    asyncio.run(harness.press("fml:open", OWNER))
    asyncio.run(harness.press("fml:a", OWNER))
    cq = asyncio.run(harness.press("fml:op:disable", OWNER))

    assert harness.rule_row(TASK_A)["is_active"] in (0, False)
    assert harness.rule_row(TASK_B)["is_active"] in (0, False)
    report = edits(cq)
    assert "Bulk result" in report and "Nightly" in report and "Daily" in report


def test_bulk_delete_needs_confirmation_then_removes_every_selected(harness):
    asyncio.run(harness.press("fml:open", OWNER))
    asyncio.run(harness.press("fml:a", OWNER))
    ask = asyncio.run(harness.press("fml:conf:delete", OWNER))
    assert "Delete 2 selected" in edits(ask)
    assert harness.rule_count() == 2

    done = asyncio.run(harness.press("fml:go:delete", OWNER))
    assert harness.rule_count() == 0
    assert "Nightly" in edits(done)


def test_empty_selection_is_refused_instead_of_running(harness):
    asyncio.run(harness.press("fml:open", OWNER))
    cq = asyncio.run(harness.press("fml:op:disable", OWNER))
    assert "Select at least one rule" in str(cq.answers.calls)
    assert harness.rule_row(TASK_A)["is_active"] in (1, True)


def test_a_selection_whose_rule_disappeared_is_pruned(harness):
    asyncio.run(harness.press("fml:open", OWNER))
    asyncio.run(harness.press(f"fml:t:{TASK_A}", OWNER))
    asyncio.run(harness.rules.delete(TASK_A))

    cq = asyncio.run(harness.press("fml:op:enable", OWNER))
    assert "Select at least one rule" in str(cq.answers.calls)


def test_one_failing_task_does_not_hide_the_others(harness, monkeypatch):
    asyncio.run(harness.press("fml:open", OWNER))
    asyncio.run(harness.press("fml:a", OWNER))

    real = harness.rules.delete_owned

    async def flaky(rule_id, owner):
        if rule_id == TASK_A:
            raise RuntimeError("row locked")
        return await real(rule_id, owner)

    monkeypatch.setattr(harness.rules, "delete_owned", flaky)
    cq = asyncio.run(harness.press("fml:go:delete", OWNER))

    report = edits(cq)
    assert "FAILED" in report
    assert harness.rule_row(TASK_A) != {}      # the failing one survived
    assert harness.rule_row(TASK_B) == {}      # the other one still went through
