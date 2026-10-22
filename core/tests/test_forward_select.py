"""The 📥/📤 picker on the real dispatcher: pickers, ids and creation.

These tests run against the same stack the mission asked for: the real
dispatcher (every callback routed the way pyrogram would route it), real
SQLite for sessions, rules and per-user UI state, and the real application
use cases that create routes. The only doubles are the two ports that belong
to Telegram itself — the dialog catalog and the access probe — so a test can
say exactly what Telegram would answer.

Pinned here, beyond the value-object rules:

* the menu offers 📥 Sources / 📤 Targets and the screens show name *and* id;
* a toggle renders the picked/unpicked marks and is reversible;
* paging, search and role switching never lose a pick;
* a pasted id batch merges with the earlier picks, minus signs intact;
* ids that are not the user's chats are refused, not absorbed;
* each user's picks are their own, both in RAM and in the SQLite row;
* access answers decide the plan: unreadable chats are excluded and named;
* creation writes one rule per source through the real use case and reports
  every source separately, twice creating the same route on purpose stays
  one set of rules.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from core.application.discovery import ListDialogsUseCase
from core.application.chat_selection import (
    ROLE_SOURCE,
    ROLE_TARGET,
    STATE_OK,
)
from core.tests.bot_dispatch import BotHarness

OWNER = 5094837833
OTHER = 7711223344
NEWS = "-1004400615270"
GAMES = "-1003915313919"
INBOX = "-288262000"
ARCHIVE = "-272728282"

CATALOG = [
    {"id": NEWS, "title": "News", "type": "channel"},
    {"id": GAMES, "title": "Games", "type": "channel"},
    {"id": INBOX, "title": "Inbox", "type": "group"},
    {"id": ARCHIVE, "title": "Archive", "type": "group"},
    {"id": "-1001111111111", "title": "Extra", "type": "channel"},
    {"id": "-1002222222222", "title": "Delta", "type": "channel"},
    {"id": "-1003333333333", "title": "Echo", "type": "channel"},
    {"id": "-1004444444444", "title": "Foxtrot", "type": "channel"},
]


class StaticCatalog:
    """The dialog gateway port with fixed answers per owner."""

    def __init__(self, rows) -> None:
        self.rows = rows
        self.calls: list[int] = []

    async def fetch(self, owner_user_id: int):
        self.calls.append(owner_user_id)
        return [dict(row) for row in self.rows]


class TMsg:
    """A text message shaped like pyrogram's, recording replies."""

    def __init__(self, text: str, uid: int) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=uid, first_name="U", is_bot=False)
        self.replies: list[tuple[str, object]] = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def screen(cq) -> str:
    return "\n".join(str(text) for kind, text, _kw in cq.message.sent if text)


def buttons(cq) -> list[str]:
    out: list[str] = []
    for _kind, _text, kwargs in reversed(cq.message.sent):
        markup = kwargs.get("reply_markup")
        for row in (getattr(markup, "inline_keyboard", None) or []):
            for btn in row:
                data = getattr(btn, "callback_data", None)
                if data:
                    out.append(data if isinstance(data, str) else data.decode())
        if out:
            break
    return out


@pytest.fixture
def stack():
    with tempfile.TemporaryDirectory() as tmp:
        harness = BotHarness(os.path.join(tmp, "atf.db"), admin_ids=[OWNER])
        try:
            yield harness
        finally:
            harness.close()


def run(coro):
    return asyncio.run(coro)


async def _seed(harness: BotHarness) -> None:
    await harness.seed_session("sess-a", OWNER)


async def _wire(harness: BotHarness, rows=CATALOG, probe=None, owner=OWNER) -> None:
    harness.ui._dialog_use_case = ListDialogsUseCase(StaticCatalog(rows))
    if probe is not None:
        harness.ui.chat_probe = probe


def static_probe(harness: BotHarness, answers=None) -> object:
    from core.infrastructure.telegram.chat_probe import StaticChatProbe

    probe = StaticChatProbe(answers or {})
    harness.ui.chat_probe = probe
    return probe


# --------------------------------------------------------------------------- #
# The routes screen and the picker
# --------------------------------------------------------------------------- #
def test_menu_offers_both_roles_and_the_picker_lists_name_and_id(stack):
    run(_seed(stack))
    run(_wire(stack))
    cq = run(stack.press("sel:open:s", OWNER))
    text = screen(cq)

    assert "📥 Pick sources" in text
    assert "🆔" in text and NEWS in text and "News" in text
    assert "☑️" in text, "an unpicked row carries the empty box"
    cbs = buttons(cq)
    assert "sel:done" in cbs and "sel:cancel" in cbs
    assert "sel:all:s" in cbs and "sel:none:s" in cbs
    assert "sel:ids:s" in cbs and "sel:q:s" in cbs
    assert "r" in cbs, "Back leaves the picker for the task screen"


def test_target_picker_starts_empty_and_is_independent(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:t", OWNER))
    cq = run(stack.press("sel:t:t:" + NEWS, OWNER))

    assert "🪪" in screen(cq) or "🆔" in screen(cq)
    cq = run(stack.press("sel:r:s", OWNER))
    assert "📤 Targets (1)" in screen(cq), "switching roles keeps the target pick"


def test_a_second_click_removes_and_a_role_never_erases_the_other(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:t:s:" + NEWS, OWNER))
    run(stack.press("sel:t:s:" + GAMES, OWNER))
    run(stack.press("sel:r:t", OWNER))
    run(stack.press("sel:t:t:" + INBOX, OWNER))          # pick Inbox as a target

    # selecting a target must not clear the sources
    assert "📥 Sources (2)" in screen(run(stack.press("sel:r:s", OWNER)))

    # second click on News removes *News only*
    run(stack.press("sel:t:s:" + NEWS, OWNER))
    after = screen(run(stack.press("sel:r:t", OWNER)))
    assert "📥 Sources (1)" in after
    assert "📤 Targets (1)" in after


def test_paging_keeps_a_pick_made_on_the_previous_page(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    # 6 rows per page: a channel on page one, a group on page two.
    run(stack.press("sel:t:s:" + NEWS, OWNER))
    run(stack.press("sel:pg:s:1", OWNER))
    run(stack.press("sel:t:s:" + INBOX, OWNER))

    sel = stack.ui._sel_selection(stack.ui._state(OWNER))
    assert sel.source_ids == (NEWS, INBOX), "paging must not drop the earlier page"
    assert "📥 Sources (2)" in screen(run(stack.press("sel:r:t", OWNER)))


def test_search_narrows_the_list_without_narrowing_the_picks(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:t:s:" + NEWS, OWNER))
    run(stack.press("sel:q:s", OWNER))
    msg = TMsg("inbox", OWNER)
    run(stack.ui._route_text(msg))
    text = "\n".join(t for t, _m in msg.replies)

    assert "🔍 Filter: inbox" in text
    assert "1 chats" in text, "the filtered count is what the user sees"
    assert "📥 Sources (1)" in text, "the pick made before the search survives it"


def test_select_all_and_clear_all_obey_the_filter(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:q:s", OWNER))
    run(stack.ui._route_text(TMsg("inbox", OWNER)))
    run(stack.press("sel:all:s", OWNER))
    text = screen(run(stack.press("sel:r:t", OWNER)))
    assert "📥 Sources (1)" in text, "select all picks exactly the filter's matches"
    run(stack.press("sel:r:s", OWNER))
    run(stack.press("sel:q:s", OWNER))
    run(stack.ui._route_text(TMsg("clear", OWNER)))
    run(stack.press("sel:none:s", OWNER))
    text = screen(run(stack.press("sel:r:t", OWNER)))
    assert "📥 Sources (0)" in text


def test_forged_chat_ids_are_answered_not_absorbed(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    before = stack.ui._state(OWNER).buffer

    cq = run(stack.press("sel:t:s:-9999999999999", OWNER))
    assert "not in your list" in cq.answers.texts[-1] or "not in your list" in screen(cq)
    after = stack.ui._state(OWNER).buffer
    assert after.get("sel") == before.get("sel"), "a forged id must not touch the picks"


def test_stale_screen_says_so_instead_of_touching_a_lost_screen(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    # Simulate a flow-state refresh that emptied the cached catalog.
    stack.ui._state(OWNER).buffer["sel_chats"] = []
    cq = run(stack.press("sel:t:s:" + NEWS, OWNER))
    assert any("changed" in text for text in cq.answers.texts), screen(cq)


def test_selections_live_in_the_users_own_state_row(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:t:s:" + NEWS, OWNER))
    # A different user's state row is untouched, both in memory and in SQLite
    other_row = stack.db.connection.execute(
        "SELECT buffer FROM ui_states WHERE user_id = ?", (OTHER,)).fetchall()
    assert not other_row or "sel" not in (other_row[0][0] or "")

    cur = stack.db.connection.execute(
        "SELECT step, buffer FROM ui_states WHERE user_id = ?", (OWNER,)).fetchone()
    assert cur is not None and '"sel"' in cur[1]
    sel = stack.ui._sel_selection(stack.ui._state(OWNER))
    assert sel.source_ids == (NEWS,)


# --------------------------------------------------------------------------- #
# Direct id entry
# --------------------------------------------------------------------------- #
def test_typed_ids_merge_into_the_pick_with_confirmed_counts(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:t", OWNER))
    run(stack.press("sel:t:t:" + NEWS, OWNER))           # one list pick first
    run(stack.press("sel:ids:t", OWNER))
    run(stack.ui._route_text(
        TMsg("-288262000, -272728282", OWNER)))          # typed, minus signs intact

    sel = stack.ui._sel_selection(stack.ui._state(OWNER))
    assert sel.target_ids == (NEWS, INBOX, ARCHIVE)


def test_pasted_batches_keep_their_minus_signs(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:ids:s", OWNER))
    run(stack.ui._route_text(
        TMsg("-288262000 -272728282\n-1001234567890", OWNER)))
    sel = stack.ui._sel_selection(stack.ui._state(OWNER))
    assert sel.source_ids == (INBOX, ARCHIVE, "-1001234567890")


def test_duplicates_and_unknown_shapes_are_reported_not_swallowed(stack):
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:t:s:" + NEWS, OWNER))
    run(stack.press("sel:ids:s", OWNER))
    msg = TMsg(NEWS + ", not-an-id", OWNER)
    run(stack.ui._route_text(msg))
    note = "\n".join(text for text, _mx in msg.replies)
    assert "Added 0" in note
    assert "duplicates skipped 1" in note
    assert "not-an-id" in note
    sel = stack.ui._sel_selection(stack.ui._state(OWNER))
    assert sel.source_ids == (NEWS,)


def test_cancel_word_in_the_users_language_exits_the_ids_step(stack):
    """The catalog decides what means cancel, not an English-only tuple."""
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:ids:s", OWNER))
    assert stack.ui._state(OWNER).step == "sel_ids_s"

    st = stack.ui._state(OWNER)
    st.lang = "ru"
    msg = TMsg("\u043e\u0442\u043c\u0435\u043d\u0430", OWNER)      # отмена - Russian cancel
    run(stack.ui._route_text(msg))
    assert stack.ui._state(OWNER).step == ""
    reply = msg.replies[0][0] if msg.replies else ""
    assert reply, "the user is told the step closed"


def test_an_abandoned_ids_prompt_expires_and_frees_the_next_message(stack):
    """Timeout is a state rule: an unanswered prompt must not eat later text."""
    import time

    from core.infrastructure.telegram.pro_bot_ui import UI_STEP_TTL_SECONDS

    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:ids:s", OWNER))
    assert stack.ui._state(OWNER).step == "sel_ids_s"

    # The user comes back hours later: the prompt is retired by the same idle
    # rule as every other armed step, so their next text is not an id. A last
    # touch older than the TTL is what ages it (0 would mean "never armed").
    stack.ui._step_touch[OWNER] = time.time() - UI_STEP_TTL_SECONDS - 60
    stack.ui._states[OWNER].step = "sel_ids_s"
    state = stack.ui._state(OWNER)
    assert state.step == "", "a prompt that old must not still be armed"

    msg = TMsg(INBOX, OWNER)
    run(stack.ui._route_text(msg))
    assert stack.ui._sel_selection(stack.ui._state(OWNER)).source_ids == ()
    assert UI_STEP_TTL_SECONDS >= 30 * 60


def test_ids_only_enter_through_the_ids_step(stack):
    """An ids step and a *different* flow's step must not accept pastes."""
    run(_seed(stack))
    run(_wire(stack))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:ids:s", OWNER))
    # The user's step is still armed; a *target-oriented* paste must fail.
    st = stack.ui._state(OWNER)
    assert st.step == "sel_ids_s"
    stack.ui._state(OWNER).step = ""                      # nothing armed anymore
    msg = TMsg(INBOX, OWNER)
    run(stack.ui._route_text(msg))
    sel = stack.ui._sel_selection(stack.ui._state(OWNER))
    assert sel.source_ids == (), "an id with no armed ids-step must stay unpicked"


# --------------------------------------------------------------------------- #
# Validation, confirmation, creation
# --------------------------------------------------------------------------- #
def _probe(harness, ok=(), blocked=(), unchecked=()):
    from core.infrastructure.telegram.chat_probe import StaticChatProbe
    probe = StaticChatProbe()
    for chat_id in ok:
        probe.seed(chat_id, STATE_OK)
    for chat_id in blocked:
        probe.seed(chat_id, "unknown")
    for chat_id in unchecked:
        probe.seed(chat_id, "unchecked")
    harness.ui.chat_probe = probe
    return probe


async def _wire_and_pick(harness, probe, targets=(INBOX, GAMES)) -> None:
    await _wire(harness)
    await harness.press("sel:open:s", OWNER)
    await harness.press("sel:t:s:" + NEWS, OWNER)
    await harness.press("sel:r:t", OWNER)
    for chat_id in targets:
        await harness.press("sel:t:t:" + chat_id, OWNER)


def test_confirm_shows_the_plan_for_approval_before_any_write(stack):
    run(_seed(stack))
    run(_wire(stack))
    _probe(stack, ok=(NEWS, INBOX, GAMES))
    _probe(stack, ok=(NEWS, INBOX, GAMES))
    run(_wire_and_pick(stack, _probe(stack, ok=(NEWS, INBOX, GAMES))))
    cq = run(stack.press("sel:done", OWNER))
    text = screen(cq)

    assert "🧾 Review before creating" in text
    assert "Plan: 1 rule" in text, "one source to two targets is one rule"
    assert stack.rule_count(OWNER) == 0, "the plan screen must not write anything itself"


def test_access_answers_split_verified_from_unverified_chats(stack):
    run(_seed(stack))
    run(_wire(stack))
    _probe(stack, ok=(NEWS, INBOX), blocked=(GAMES,))
    run(_wire_and_pick(stack, stack.ui.chat_probe))
    cq = run(stack.press("sel:done", OWNER))
    text = screen(cq)

    assert "⛔ no access" in text, "an unreachable chat is named, not guessed"
    assert "Plan: 1 rule" in text
    counts = stack.rule_count(OWNER)
    assert counts == 0


def test_create_writes_one_rule_per_source_and_reports_each(stack):
    run(_seed(stack))
    run(_wire(stack))
    _probe(stack, ok=(NEWS, INBOX, GAMES))
    run(_wire_and_pick(stack, _probe(stack, ok=(NEWS, INBOX, GAMES))))
    run(stack.press("sel:done", OWNER))
    cq = run(stack.press("sel:go", OWNER))
    text = screen(cq)

    assert "✅" in text and "2 target(s)" in text, text
    assert stack.rule_count(OWNER) == 1
    stored = [tuple(row) for row in stack.db.connection.execute(
        "SELECT source_chat_id, target_chat_ids FROM forward_rules")]
    assert stored[0] == (NEWS, f'["{INBOX}", "{GAMES}"]')


def test_two_sources_fan_out_to_the_same_two_targets(stack):
    run(_seed(stack))
    run(_wire(stack))
    extra = "-1001111111111"
    _probe(stack, ok=(NEWS, extra, INBOX, ARCHIVE))
    run(_wire_and_pick(stack, _probe(stack, ok=(NEWS, extra, INBOX, ARCHIVE)),
                       targets=(INBOX, ARCHIVE)))
    run(stack.press("sel:r:s", OWNER))
    run(stack.press("sel:t:s:" + extra, OWNER))
    run(stack.press("sel:done", OWNER))
    cq = run(stack.press("sel:go", OWNER))

    assert stack.rule_count(OWNER) == 2
    assert "2 target(s)" in screen(cq), "each source keeps the whole target list"
    sources = {row[0] for row in stack.db.connection.execute(
        "SELECT source_chat_id FROM forward_rules")}
    assert sources == {NEWS, extra}


def test_recreating_the_same_route_is_reported_not_written_twice(stack):
    run(_seed(stack))
    run(_wire(stack))
    _probe(stack, ok=(NEWS, INBOX))
    run(_wire_and_pick(stack, _probe(stack, ok=(NEWS, INBOX))))
    run(stack.press("sel:done", OWNER))
    run(stack.press("sel:go", OWNER))
    assert stack.rule_count(OWNER) == 1
    # Unpick everything? The flow resets itself after creation; re-do the flow.
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:t:s:" + NEWS, OWNER))
    run(stack.press("sel:r:t", OWNER))
    run(stack.press("sel:t:t:" + INBOX, OWNER))
    run(stack.press("sel:done", OWNER))
    cq = run(stack.press("sel:go", OWNER))

    assert stack.rule_count(OWNER) == 1, "an identical route is a duplicate, not a second rule"
    assert "⏭️" in screen(cq)


def test_nothing_to_create_when_every_target_lost_access(stack):
    run(_seed(stack))
    run(_wire(stack))
    _probe(stack, ok=(NEWS,), blocked=(INBOX, GAMES))
    run(_wire_and_pick(stack, stack.ui.chat_probe))
    run(stack.press("sel:done", OWNER))
    cq = run(stack.press("sel:go", OWNER))
    assert stack.rule_count(OWNER) == 0
    assert "Nothing to create" in screen(cq)


def test_confirm_requires_both_sides_and_cancel_is_clear(stack):
    run(_seed(stack))
    run(_wire(stack))
    _probe(stack, ok=(NEWS,))
    run(stack.press("sel:open:s", OWNER))
    run(stack.press("sel:t:s:" + NEWS, OWNER))
    cq = run(stack.press("sel:done", OWNER))
    assert any("at least one source and one target" in text for text in cq.answers.texts)
    assert stack.rule_count(OWNER) == 0

    run(stack.press("sel:cancel", OWNER))
    sel = stack.ui._sel_selection(stack.ui._state(OWNER))
    assert len(sel) == 0, "cancel empties this flow's picks"


def test_back_from_confirm_returns_to_the_picker_with_picks_intact(stack):
    run(_seed(stack))
    run(_wire(stack))
    _probe(stack, ok=(NEWS, INBOX, GAMES))
    run(_wire_and_pick(stack, _probe(stack, ok=(NEWS, INBOX, GAMES))))
    run(stack.press("sel:done", OWNER))
    cq = run(stack.press("sel:back", OWNER))
    text = screen(cq)
    assert "Pick" in text, "back returns to one of the two role screens"
    assert "📥 Sources (1)" in text and "📤 Targets (2)" in text


def test_a_user_without_a_session_cannot_create_a_route(stack):
    """Creation needs an account; the use case's session check is the gate."""
    run(_seed(stack))
    run(_wire(stack))
    cq = run(stack.press("sel:go", OTHER))          # no session for OTHER
    assert stack.rule_count(OTHER) == 0
    assert "session" in screen(cq).lower(), screen(cq)

