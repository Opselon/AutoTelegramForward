"""Manual chat-id entry in the rule wizard: ids round-trip, cancel words exit.

A user who types an id instead of tapping a picker button gets exactly one
resolution path — the buffered catalog first (either id spelling, or a
username), then the connected client — and a cancel alias text is always an
exit from the wizard, never a chat id. Every outcome is logged under
``[PIPELINE:WIZARD]``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

OWNER = 5094837833

CATALOG_ID = "-1001217668417"

class _Msg:
    def __init__(self, text: str, uid: int = OWNER):
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid, type="private")
        self.text = text
        self.sent: list = []

    async def reply_text(self, text=None, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))


class _State:
    def __init__(self):
        self.step = ""
        self.rule_source = ""
        self.rule_target = ""
        self.buffer: dict = {}


def _harness(tmp_path):
    from core.tests.bot_dispatch import BotHarness

    return BotHarness(str(tmp_path / "atf.db"))


# --------------------------------------------------------------------------- #
# unique_peer_key
# --------------------------------------------------------------------------- #


def test_unique_peer_key_collides_both_spellings_of_one_id():
    from core.infrastructure.telegram.route_lists import unique_peer_key

    assert unique_peer_key("-1001206461239") == unique_peer_key("1206461239")
    assert unique_peer_key("-1001206461239") == "1206461239"


def test_unique_peer_key_leaves_names_alone():
    from core.infrastructure.telegram.route_lists import unique_peer_key

    assert unique_peer_key("@News") == "news"
    assert unique_peer_key("https://t.me/news") == "news"


# --------------------------------------------------------------------------- #
# resolution through the wizard
# --------------------------------------------------------------------------- #


async def test_manual_raw_unique_id_matches_buffered_chat(tmp_path):
    harness = _harness(tmp_path)
    try:
        st = _State()
        st.buffer = {"chats": [{"id": CATALOG_ID, "title": "News",
                                "emoji": "📢", "username": "News"}]}
        cid, title = await harness.ui._resolve_wizard_chat(OWNER, st, "1217668417", role="source")
        assert cid == CATALOG_ID
        assert "News" in title

        cid, title = await harness.ui._resolve_wizard_chat(
            OWNER, st, "-1001217668417", role="target")
        assert cid == CATALOG_ID

        cid, title = await harness.ui._resolve_wizard_chat(
            OWNER, st, "@news", role="target")
        assert cid == CATALOG_ID
    finally:
        harness.close()


async def test_manual_token_falls_through_to_resolver(tmp_path):
    harness = _harness(tmp_path)
    try:
        from unittest.mock import AsyncMock

        st = _State()
        st.buffer = {"chats": [{"id": CATALOG_ID, "title": "News", "emoji": "📢"}]}
        harness.ui._resolve_chat_token = AsyncMock(return_value=("-100777", "📢 Something", "resolved"))
        cid, title = await harness.ui._resolve_wizard_chat(OWNER, st, "-100777", role="source")
        assert cid == "-100777"
        assert harness.ui._resolve_chat_token.await_count == 1
    finally:
        harness.close()


async def test_cancel_word_exits_the_source_step(tmp_path):
    harness = _harness(tmp_path)
    try:
        st = _State()
        st.step = "rule_source"
        st.confirmed_login_flow = False
        harness.ui._states[OWNER] = st
        msg = _Msg("cancel")
        await harness.ui._step_rule_source(msg, st, "cancel")
        assert st.step == ""
        assert st.rule_source == "" and st.rule_target == ""
        assert msg.sent, "the exit must answer the user"
    finally:
        harness.close()


async def test_cancel_word_exits_the_target_step(tmp_path):
    harness = _harness(tmp_path)
    try:
        st = _State()
        st.step = "rule_target"
        harness.ui._states[OWNER] = st
        msg = _Msg("بازگشت")
        await harness.ui._step_rule_target(msg, st, "بازگشت")
        assert st.step == ""
        assert st.rule_target == ""
        assert msg.sent
    finally:
        harness.close()


async def test_manual_input_completes_a_rule_end_to_end(tmp_path):
    """Source pasted by id, target typed as "-100…" Bot-API id: one rule created."""
    harness = _harness(tmp_path)
    try:
        await harness.seed_session("sess-a", OWNER)
        st = _State()
        st.step = "rule_source"
        st.buffer = {"chats": [
            {"id": CATALOG_ID, "title": "News", "emoji": "📢"},
            {"id": "-1003915313919", "title": "Archive", "emoji": "📢"},
        ]}
        harness.ui._states[OWNER] = st
        msg = _Msg("1217668417")
        await harness.ui._step_rule_source(msg, st, "1217668417")
        assert st.step == "rule_target"
        assert st.rule_source == CATALOG_ID

        msg2 = _Msg("-1003915313919")
        await harness.ui._step_rule_target(msg2, st, "-1003915313919")
        # The finisher settles the flow: no armed step is left, even though
        # the picker's own source/target strings stay on the state object.
        assert st.step == ""
        assert harness.rule_count(OWNER) == 1
        rules = await harness.rules.list_by_owner(OWNER)
        rule = rules[0]
        assert rule.source_chat_id == CATALOG_ID
        assert rule.target_chat_id == "-1003915313919"
        assert rule.source_chat_name and "News" in rule.source_chat_name
    finally:
        harness.close()
