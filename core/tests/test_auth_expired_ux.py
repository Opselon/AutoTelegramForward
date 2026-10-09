"""TASK 03 — the user-facing half of restart recovery (audit D1).

The state machine marks a flow AuthState.AUTH_EXPIRED on boot. That is only useful if
the *user* is told, instead of the bot silently dropping the code they type.
These tests pin that contract.
"""
import pytest

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAuthFlowRepository,
)
from core.infrastructure.telegram.auth_state import (
    ACTIVE_STATES,
    AuthState,
    AuthStateMachine,
)


def _machine(tmp_path) -> AuthStateMachine:
    db = SqliteDatabase(str(tmp_path / "atf.db"))
    return AuthStateMachine(SqliteAuthFlowRepository(db))


def _flow_states() -> list:
    """Legal state machine transitions, per the auth_state table."""
    return [
        (AuthState.WELCOME, {"phone_number": "+989****7808"}),
        (AuthState.AUTH_PHONE_REQUIRED, {"phone_number": "+989****7808"}),
        (AuthState.AUTH_PHONE_SUBMITTED, {}),
        (AuthState.AUTH_CODE_REQUIRED, {}),
    ]


async def _to_code(machine: AuthStateMachine, uid: int = 5094837833) -> None:
    for target, kw in _flow_states():
        await machine.transition(uid, target, **kw)


class TestActiveQuery:
    """expire_active() is what recovery is built on — it must be exact."""

    @pytest.mark.asyncio
    async def test_active_lists_only_waiting_states(self, tmp_path):
        machine = _machine(tmp_path)
        await _to_code(machine)  # user 1 mid-code

        rows = await machine._repo.active()
        assert [int(r["user_id"]) for r in rows] == [5094837833]
        assert rows[0]["phone_number"] == "+989****7808"

    @pytest.mark.asyncio
    async def test_terminal_states_excluded(self, tmp_path):
        machine = _machine(tmp_path)
        uid = 111
        await _to_code(machine, uid)
        # the only legal path to AUTHENTICATED runs through CODE_SUBMITTED
        await machine.transition(uid, AuthState.AUTH_CODE_SUBMITTED)
        await machine.transition(uid, AuthState.AUTHENTICATING)
        await machine.transition(uid, AuthState.AUTHENTICATED)

        # a live session is not a login to expire: nothing should be reported
        assert await machine._repo.active() == []

    @pytest.mark.asyncio
    async def test_expired_already_excluded(self, tmp_path):
        machine = _machine(tmp_path)
        await _to_code(machine)
        await machine.transition(5094837833, AuthState.AUTH_EXPIRED, last_error="restart")

        # already expired: recovery must not touch it twice
        assert await machine._repo.active() == []


class TestExpireActive:
    @pytest.mark.asyncio
    async def test_returns_count_and_degrades_state(self, tmp_path):
        machine = _machine(tmp_path)
        await _to_code(machine)

        n = await machine.expire_active()
        assert n == 1
        flow = await machine.current(5094837833)
        assert flow.state == AuthState.AUTH_EXPIRED
        # the durable bits survive — that's the whole point of the fix
        assert flow.phone_number == "+989****7808"
        assert flow.last_error == "restart"

    @pytest.mark.asyncio
    async def test_idempotent(self, tmp_path):
        machine = _machine(tmp_path)
        await _to_code(machine)

        assert await machine.expire_active() == 1
        assert await machine.expire_active() == 0

    @pytest.mark.asyncio
    async def test_multiple_users_expired_independently(self, tmp_path):
        machine = _machine(tmp_path)
        await _to_code(machine, 5094837833)
        await _to_code(machine, 222)

        assert await machine.expire_active() == 2
        assert (await machine.current(5094837833)).state == AuthState.AUTH_EXPIRED
        assert (await machine.current(222)).state == AuthState.AUTH_EXPIRED


class TestRestartRecoveryEndToEnd:
    """The scenario the audit found: process dies mid-login, comes back."""

    @pytest.mark.asyncio
    async def test_new_machine_recovers_the_flow(self, tmp_path):
        # simulate the crash: machine A is built, used, then thrown away
        machine = _machine(tmp_path)
        await _to_code(machine)
        del machine

        # a fresh process boots against the same DB
        machine2 = _machine(tmp_path)
        n = await machine2.expire_active()
        assert n == 1
        flow = await machine2.current(5094837833)
        assert flow.state == AuthState.AUTH_EXPIRED
        assert flow.phone_number == "+989****7808"
        # and the user is not rate-limited for a crash they did not cause
        assert await machine2.is_blocked(5094837833) is False

    @pytest.mark.asyncio
    async def test_terminal_flow_survives_restart_untouched(self, tmp_path):
        machine = _machine(tmp_path)
        await _to_code(machine)
        await machine.transition(5094837833, AuthState.AUTH_CODE_SUBMITTED)
        await machine.transition(5094837833, AuthState.AUTHENTICATING)
        await machine.transition(5094837833, AuthState.AUTHENTICATED)
        del machine

        machine2 = _machine(tmp_path)
        assert await machine2.expire_active() == 0
        assert (await machine2.current(5094837833)).state == \
            AuthState.AUTHENTICATED


class TestStateSpaceAssumptions:
    """The ACTIVE_STATES table is load-bearing — pin it."""

    def test_active_states_are_exactly_the_waiting_ones(self):
        # recovery touches exactly these; anything added here must also be
        # handled by expire_active(), or a restart silently drops a login
        expected = {
            AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED,
            AuthState.AUTH_CODE_REQUIRED,
            AuthState.AUTH_CODE_SUBMITTED,
            AuthState.AUTH_2FA_REQUIRED,
            AuthState.AUTH_2FA_SUBMITTED,
            AuthState.AUTHENTICATING,
        }
        assert set(ACTIVE_STATES) == expected

    def test_expired_is_not_active(self):
        # an already-expired flow is never re-expired
        assert AuthState.AUTH_EXPIRED not in set(ACTIVE_STATES)

    def test_welcome_is_not_active(self):
        # WELCOME means the bot asked for a number and got no reply: nothing
        # confidential is held, so the flow can simply restart fresh
        assert AuthState.WELCOME not in set(ACTIVE_STATES)
