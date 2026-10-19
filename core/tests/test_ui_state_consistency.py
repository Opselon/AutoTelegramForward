"""STEER 08 — the UI state store must survive a restart, and must not be
overwritten by a copy that is older than the live flow.

The manager promises durable, single-writer, resumable per-user flow state. Two
things broke that promise, and one of them was invisible to the existing suite
for a specific reason worth naming: `test_ui_state_manager.py` drives the
manager through a `FakeRepo` that exposes `load()`, and the manager calls
`load()` — but the real repository interface (`IUiStateRepository`) and its real
implementations expose `get()`. The double was written to the manager's
assumption instead of the interface, so the resume path was exercised only
against a method production does not have.

D6 — the resume was dead code in production. `await self._repo.load(uid)` on
`SqliteUiStateRepository` raises `AttributeError`, which the `except Exception`
above it swallowed, so every restart resumed an empty flow and the navigation
stack was gone (`Back` after a redeploy was a guess). The failure was also
invisible: no log line, no metric.

D7 — a loaded user was re-loaded on every touch. `_ensure_loaded` set
`self._loaded[uid] = False` in the branch taken *when the user was already
loaded*, so the next call re-read the durable row and replaced the live object
with the persisted copy. Every mutation goes through `_ensure_loaded`, so a
persisted copy that trails the live state (a persist that failed and was
swallowed, a replica that wrote an older payload, a write that lost the race)
would silently overwrite newer edits on the next press — precisely the failure
the manager exists to prevent.

These tests use the real `SqliteUiStateRepository` over real SQLite, so the
interface is the production one and a rename cannot pass again.
"""
from __future__ import annotations

import logging
import os

import pytest

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqliteUiStateRepository
from core.infrastructure.telegram.pro_bot_ui import UiState
from core.infrastructure.telegram.ui_state_manager import (
    UiStateManager,
    UiState_v2_payload,
)

TENANT_A = 5989354154
TENANT_B = 6278059256

#: Mirrors the confirm handler's own pattern, `^rdelyes:(.+)$`. It is not in
#: `CB` because it is only ever emitted as a button, never routed by key.
DELETE_CONFIRM = "rdelyes"


@pytest.fixture()
def repo(tmp_path):
    db = SqliteDatabase(str(tmp_path / "ui.db"))
    yield SqliteUiStateRepository(db)
    db.close()


class _CountingRepo:
    """The real repository, with a load counter on the interface method."""

    def __init__(self, inner):
        self.inner = inner
        self.loads = 0

    async def get(self, user_id: int):
        self.loads += 1
        return await self.inner.get(user_id)

    async def save(self, user_id: int, step: str, buffer: dict) -> None:
        await self.inner.save(user_id, step, buffer)

    async def clear(self, user_id: int) -> None:
        await self.inner.clear(user_id)


@pytest.mark.asyncio
async def test_the_manager_resumes_through_the_real_repository_interface(repo):
    """A new process must come back to the same step *and* the same stack."""
    first = UiStateManager(repo=repo)
    await first.enter(TENANT_A, "rule_target")
    await first.write_buffer(TENANT_A, "draft_name", "Gold signals")
    await first.enter_screen(TENANT_A, "rules")
    await first.enter_screen(TENANT_A, "rule_detail")

    # a redeploy: a brand new manager over the same durable store
    resumed = UiStateManager(repo=repo)
    state = await resumed.get(TENANT_A)
    assert state.step == "rule_target", "the flow did not resume after a restart"
    assert state.buffer.get("draft_name") == "Gold signals"
    assert await resumed.current_stack(TENANT_A) == ["rules", "rule_detail"], (
        "the navigation stack was lost, so Back is a guess after a restart"
    )


@pytest.mark.asyncio
async def test_a_loaded_user_is_not_reloaded_on_every_touch(repo):
    """The durable row is read once per process, not once per press."""
    counting = _CountingRepo(repo)
    mgr = UiStateManager(repo=counting)
    await mgr.enter(TENANT_A, "login_code")
    for _ in range(5):
        await mgr.get(TENANT_A)
    assert counting.loads == 1, f"the row was re-read {counting.loads} times"


@pytest.mark.asyncio
async def test_a_stale_durable_row_cannot_overwrite_live_state(repo):
    """An older persisted copy must never replace newer in-memory edits."""
    mgr = UiStateManager(repo=repo)
    await mgr.enter(TENANT_A, "rule_source")
    await mgr.enter(TENANT_A, "rule_target")

    # Something wrote an older payload after the live state moved on: a lost
    # write, a replica, a persist that failed and was swallowed.
    stale = UiState_v2_payload(UiState(step="login_phone"), [])
    await repo.save(TENANT_A, "login_phone", stale)

    state = await mgr.get(TENANT_A)
    assert state.step == "rule_target", (
        "a stale durable copy overwrote the user's live flow"
    )


@pytest.mark.asyncio
async def test_one_users_flow_never_appears_in_anothers(repo):
    """State is per verified user; resetting one leaves the other alone."""
    mgr = UiStateManager(repo=repo)
    await mgr.enter(TENANT_A, "rule_source")
    await mgr.write_buffer(TENANT_A, "secret_draft", "A-only")
    await mgr.enter(TENANT_B, "ai_api_key")
    await mgr.write_buffer(TENANT_B, "other_draft", "B-only")

    a = await mgr.get(TENANT_A)
    b = await mgr.get(TENANT_B)
    assert a.step == "rule_source" and b.step == "ai_api_key"
    assert "other_draft" not in a.buffer and "secret_draft" not in b.buffer

    await mgr.reset(TENANT_A)
    assert (await mgr.get(TENANT_A)).step == ""
    assert (await mgr.get(TENANT_B)).step == "ai_api_key", (
        "resetting one user cleared another user's flow"
    )
    assert (await repo.get(TENANT_B)) is not None


@pytest.mark.asyncio
async def test_a_resumed_stack_is_seeded_once_and_never_clobbers_a_live_one(repo):
    """The handover from the bot UI restores Back, without overriding a live stack."""
    mgr = UiStateManager(repo=repo)
    handed = UiState(step="rule_detail")
    await mgr.absorb(TENANT_A, handed, stack=["rules", "rule_detail"], restore_stack=True)
    assert await mgr.current_stack(TENANT_A) == ["rules", "rule_detail"]

    # the live stack has moved on; a second handover must not rewind it
    await mgr.enter_screen(TENANT_A, "rule_settings")
    await mgr.absorb(TENANT_A, UiState(step="rule_detail"),
                     stack=["rules"], restore_stack=True)
    assert await mgr.current_stack(TENANT_A) == [
        "rules", "rule_detail", "rule_settings",
    ], "a stale stack was allowed to rewind the live navigation"


@pytest.mark.asyncio
async def test_a_load_failure_is_reported_and_does_not_poison_the_cache(repo, caplog):
    """A transient store failure is visible, and a later call still resumes."""

    class FlakyRepo:
        def __init__(self, inner):
            self.inner = inner
            self.fail = True

        async def get(self, user_id: int):
            if self.fail:
                raise RuntimeError("database is locked")
            return await self.inner.get(user_id)

        async def save(self, user_id: int, step: str, buffer: dict) -> None:
            await self.inner.save(user_id, step, buffer)

        async def clear(self, user_id: int) -> None:
            await self.inner.clear(user_id)

    await repo.save(TENANT_A, "rule_name",
                    UiState_v2_payload(UiState(step="rule_name"), []))

    flaky = FlakyRepo(repo)
    mgr = UiStateManager(repo=flaky)
    with caplog.at_level(logging.WARNING):
        state = await mgr.get(TENANT_A)
    assert state.step == ""  # nothing to resume from: fail visibly, not wrongly
    assert any("UI_STATE" in rec.getMessage() for rec in caplog.records), (
        "the load failure was swallowed without a log line"
    )

    flaky.fail = False
    resumed = UiStateManager(repo=flaky)
    assert (await resumed.get(TENANT_A)).step == "rule_name", (
        "one transient failure permanently disabled resume"
    )


@pytest.mark.asyncio
async def test_a_deleted_task_cannot_be_reached_through_stale_ui_state():
    """A stale step naming a task that is gone must refuse, not act."""
    import tempfile

    from core.infrastructure.telegram.pro_bot_ui import CB
    from core.tests.bot_dispatch import BotHarness

    owner = 5989354154
    h = BotHarness(os.path.join(tempfile.mkdtemp(), "stale.db"), admin_ids=[5094837833])
    try:
        await h.seed_session("sess-a", owner)
        await h.seed_rule("rule-stale", owner)
        # the user is mid-flow on a screen armed for that task
        await h.ui._ui_manager.enter(owner, "rge_edit_rule-stale")

        # `rdel:<id>` opens the confirm, `rdelyes:<id>` performs it
        await h.press(f"{CB['rule_del']}:rule-stale", owner)
        await h.press(f"{DELETE_CONFIRM}:rule-stale", owner)
        assert h.rule_count(owner) == 0, "the task was not actually deleted"

        cq = await h.press(f"{CB['rule_del']}:rule-stale", owner)
        assert cq.answers, "a dead task id answered with silence"
    finally:
        h.close()
