"""The pro UI state manager: durable, validated, single-writer只剩下 flow state."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.infrastructure.telegram.ui_state_manager import (
    MAX_BUFFER_KEYS,
    MAX_STACK,
    RESUME_SCHEMA,
    UiBufferError,
    UiState_v2_from_payload,
    UiState_v2_payload,
    UiStateManager,
    UiStepError,
    _safe_buffer,
    _safe_step,
)
from core.infrastructure.telegram.pro_bot_ui import UiState


class FakeRepo:
    """Same shape as SqliteUiStateRepository (async save/load/clear)."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.saved = 0
        self.cleared = 0

    async def save(self, user_id: int, step: str, payload: dict) -> None:
        self.saved += 1
        self.rows[int(user_id)] = dict(payload)

    async def load(self, user_id: int):
        return self.rows.get(int(user_id))

    async def clear(self, user_id: int) -> None:
        self.cleared += 1
        self.rows.pop(int(user_id), None)


@pytest.mark.asyncio
async def test_step_namespace_is_validated_and_fails_closed():
    assert _safe_step("login_phone") == "login_phone"
    assert _safe_step("") == ""
    assert _safe_step(None) == ""
    assert _safe_step("made_up_step_x") == ""
    assert _safe_step("  login_phone  ") == "login_phone"


def test_buffer_bounds_enforced():
    ok = _safe_buffer({"k": "v"})
    assert ok == {"k": "v"}
    # oversized values are truncated to fit, never dropped outright
    big = {f"big_{i}": "x" * 3000 for i in range(MAX_BUFFER_KEYS + 5)}
    assert len(_safe_buffer(big)) < MAX_BUFFER_KEYS
    # non-JSON-safe value is stringified rather than corrupting the row
    weird = _safe_buffer({"obj": {1, 2}})
    assert isinstance(weird.get("obj"), (str, list, dict))


@pytest.mark.asyncio
async def test_manager_single_writer_and_stack():
    repo = FakeRepo()
    mgr = UiStateManager(repo=repo)
    st = await mgr.enter(77, "login_code")
    assert st.step == "login_code"
    assert st.user_id == 77
    await mgr.write_buffer(77, "page", 3)
    await mgr.enter_screen(77, "rules")
    await mgr.enter_screen(77, "rule_detail")
    stack = await mgr.current_stack(77)
    assert stack == ["rules", "rule_detail"]
    top, st2 = await mgr.pop_screen(77)
    assert top == "rule_detail"
    stured = await mgr.get(77)
    assert stured.user_id == 77
    # durable payload round trip
    payload = repo.rows[77]
    assert payload["v"] == RESUME_SCHEMA
    st3, stack3 = UiState_v2_from_payload(payload)
    assert st3.step == "login_code"
    assert stack3 == ["rules"]


@pytest.mark.asyncio
async def test_manager_reset_and_persistence_clears():
    repo = FakeRepo()
    mgr = UiStateManager(repo=repo)
    await mgr.enter(9, "ai_name")
    await mgr.write_buffer(9, "x", 1)
    assert repo.rows.get(9)
    await mgr.reset(9)
    assert 9 not in repo.rows


@pytest.mark.asyncio
async def test_enter_rejects_unknown_step_explicitly():
    mgr = UiStateManager(repo=FakeRepo())
    with pytest.raises(UiStepError):
        await mgr.enter(5, "not_a_step")
    with pytest.raises(UiBufferError):
        # a single oversized value is refused by the manager (fails closed)
        await mgr.write_buffer(11, "payload", "y" * 5000)


@pytest.mark.asyncio
async def test_absorb_adopts_handler_state_as_the_durable_object():
    repo = FakeRepo()
    mgr = UiStateManager(repo=repo)
    owned = await mgr.enter(21, "")
    handed = UiState()
    handed.step = "ai_provider"
    handed.rule_source = "123"
    handed.buffer = {"draft": "abc"}
    await mgr.absorb(21, handed)
    assert repo.rows[21]["step"] == "ai_provider"
    assert repo.rows[21]["buffer"]["draft"] == "abc"
    # the caller's hand-in is NOT the stored object: single-writer preserved
    handed.step = "mutated_after"
    assert repo.rows[21]["step"] == "ai_provider"


@pytest.mark.asyncio
async def test_legacy_payload_still_restores():
    st, stack = UiState_v2_from_payload({"step": "cred_label", "buffer": {"a": 1}})
    assert st.step == "cred_label"
    assert st.rule_source == ""
    assert stack == []
