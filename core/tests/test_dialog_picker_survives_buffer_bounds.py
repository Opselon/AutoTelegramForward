"""A fetched dialog list must survive the durable bounds it is persisted under.

The source picker fetches up to 100 dialogs, stores them in the flow buffer and
renders from that same object. The durable buffer bounds (64-char keys, 2 KiB
per value, 4 KiB total) exist so a runaway flow cannot grow the `ui_states` row
forever - but the fetch result (~16 KiB for 100 chats) is larger than the per
value cap, so any code that bounds the *live* state drops the list and the
picker renders "Source dialog list not found automatically" on the very press
that just fetched 100 chats.

These tests pin the fix at both ends: the live render keeps the full list, the
durable copy stays inside the documented bounds, and an oversized value is
truncated instead of silently dropped.
"""

from __future__ import annotations

import json

import pytest

from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.pro_bot_ui import ProBotUI
from core.infrastructure.telegram.ui_state import UiState
from core.infrastructure.telegram.ui_state_manager import (
    MAX_BUFFER_TOTAL,
    MAX_BUFFER_VALUE,
    UiStateManager,
    _safe_buffer,
)

UID = 5094837833


class FakeRepo:
    """Same shape as SqliteUiStateRepository (async save/load/clear)."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.saved = 0

    async def save(self, user_id: int, step: str, payload: dict) -> None:
        self.saved += 1
        self.rows[int(user_id)] = dict(payload)

    async def load(self, user_id: int):
        return self.rows.get(int(user_id))

    async def clear(self, user_id: int) -> None:
        self.rows.pop(int(user_id), None)


def _dialogs(n: int = 100) -> list[dict]:
    """Dialogs shaped like the ones _fetch_dialogs builds."""
    return [
        {
            "id": str(-1000000000000 - i),
            "title": f"Real Channel {i} with a realistic title",
            "emoji": "📢",
            "kind": "channel",
            "username": f"realchan{i}",
        }
        for i in range(n)
    ]


def _ui() -> tuple[ProBotUI, FakeRepo]:
    """The shipped renderer + the shipped persist path, wired like the handler."""
    i18n = I18n()
    ui = object.__new__(ProBotUI)
    ui.i18n = i18n
    ui._states = {}
    ui._users = None
    ui._t = lambda key, lang=None, **kw: i18n.t(key, lang=lang or "en", **kw)
    ui._kbd = lambda rows: rows
    ui._resolve_lang = lambda uid=None, user_obj=None: "en"
    repo = FakeRepo()
    ui._ui_manager = UiStateManager(repo=repo)
    return ui, repo


def _state_with(chats: list[dict]) -> UiState:
    return UiState(step="rule_source", lang="en", user_id=UID,
                   buffer={"chats": chats, "page": 0})


def test_a_fetched_dialog_list_is_larger_than_the_durable_value_cap():
    """The premise of the bug: real dialogs do not fit the per-value cap."""
    chats = _dialogs(100)
    assert len(json.dumps(chats, default=str)) > MAX_BUFFER_VALUE


@pytest.mark.asyncio
async def test_persisting_does_not_empty_the_list_the_screen_is_rendered_from():
    """The bug: persist pruned the live buffer, so the next render fell back."""
    ui, _repo = _ui()
    st = _state_with(_dialogs(100))

    await ui._persist(UID, st)

    assert len(st.buffer["chats"]) == 100, "the live render state lost its dialogs"
    text, _kbd = ui._render_chat_picker(st, role="source", page=0, uid=UID)
    assert "not found automatically" not in text
    assert "Real Channel 0" in text
    assert "1 of 20" in text


@pytest.mark.asyncio
async def test_the_durable_copy_stays_within_the_documented_bounds():
    ui, repo = _ui()
    st = _state_with(_dialogs(100))

    await ui._persist(UID, st)

    payload = repo.rows[UID]
    stored = payload["buffer"]
    assert len(json.dumps(stored, default=str)) <= MAX_BUFFER_TOTAL
    for key, value in stored.items():
        assert len(key) <= 64
        assert len(json.dumps(value, default=str)) <= MAX_BUFFER_VALUE
    # the durable copy is useful, not empty: a prefix of the same dialogs
    assert stored.get("chats"), "the durable copy dropped every dialog"
    assert stored["chats"] == _dialogs(100)[: len(stored["chats"])]


def test_an_oversized_value_is_truncated_rather_than_dropped():
    chats = _dialogs(100)
    out = _safe_buffer({"chats": chats, "page": 0})

    assert "chats" in out, "an oversized list must not vanish from the buffer"
    kept = out["chats"]
    assert 0 < len(kept) < len(chats)
    assert kept == chats[: len(kept)]
    assert len(json.dumps(kept, default=str)) <= MAX_BUFFER_VALUE
    assert out["page"] == 0


def test_an_oversized_string_is_truncated_rather_than_dropped():
    out = _safe_buffer({"note": "x" * (MAX_BUFFER_VALUE * 2)})

    assert isinstance(out.get("note"), str)
    assert 0 < len(out["note"]) < MAX_BUFFER_VALUE * 2
    assert len(json.dumps(out["note"], default=str)) <= MAX_BUFFER_VALUE


@pytest.mark.asyncio
async def test_paging_reads_the_full_list_after_persisting():
    """Pages beyond the durable prefix still work while the flow is live."""
    ui, _repo = _ui()
    st = _state_with(_dialogs(100))

    await ui._persist(UID, st)

    text, _kbd = ui._render_chat_picker(st, role="source", page=3, uid=UID)
    assert "Real Channel 15" in text
    assert "4 of 20" in text


def test_an_empty_dialog_list_still_reports_the_honest_fallback():
    """The fallback is correct when there genuinely are no dialogs."""
    ui, _repo = _ui()
    st = _state_with([])

    text, _kbd = ui._render_chat_picker(st, role="source", page=0, uid=UID)

    assert "not found automatically" in text
