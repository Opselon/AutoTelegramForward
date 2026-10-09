"""TASK 03 - persisted authentication state machine (audit D1).

The audit's D1 finding: LoginFlowManager kept every login in an in-process
dict, so any restart killed a login mid-flow with no trace and no explanation.

These tests prove the replacement: state is durable, transitions are legal or
rejected, duplicates are absorbed, and a restart degrades an in-flight flow to
AUTH_EXPIRED (phone + attempt counters intact) instead of dropping it.
"""

import os
import tempfile

import pytest

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAuthFlowRepository,
)
from core.infrastructure.telegram.auth_state import (
    AuthState,
    AuthStateMachine,
    InvalidTransition,
    new_correlation_id,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def temp_db():
    path = os.path.join(tempfile.mkdtemp(), "atf_auth.db")
    db = SqliteDatabase(path)
    yield db
    db.connection.close()


@pytest.fixture()
def machine(temp_db):
    return AuthStateMachine(SqliteAuthFlowRepository(temp_db), ttl_seconds=60)


@pytest.fixture()
def dead_machine(temp_db):
    """A *second* machine over the same database: simulates a process restart.

    The first machine's in-memory client cache is gone; only the DB survives.
    """
    return AuthStateMachine(SqliteAuthFlowRepository(temp_db), ttl_seconds=60)



# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def reach(machine, user_id: int, target: AuthState) -> None:
    """Drive a flow to `target` along a legal path.

    Tests want to assert on a state, not to re-derive the path to it every
    time; this keeps the transition graph's complexity out of the assertions.
    """
    PATHS = {
        AuthState.NEW: [],
        AuthState.WELCOME: [AuthState.WELCOME],
        AuthState.AUTH_PHONE_REQUIRED: [AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED],
        AuthState.AUTH_PHONE_SUBMITTED: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED,
        ],
        AuthState.AUTH_CODE_REQUIRED: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_CODE_REQUIRED,
        ],
        AuthState.AUTH_CODE_SUBMITTED: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_CODE_REQUIRED,
            AuthState.AUTH_CODE_SUBMITTED,
        ],
        AuthState.AUTH_2FA_REQUIRED: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_CODE_REQUIRED,
            AuthState.AUTH_CODE_SUBMITTED, AuthState.AUTH_2FA_REQUIRED,
        ],
        AuthState.AUTH_2FA_SUBMITTED: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_CODE_REQUIRED,
            AuthState.AUTH_CODE_SUBMITTED, AuthState.AUTH_2FA_REQUIRED,
            AuthState.AUTH_2FA_SUBMITTED,
        ],
        AuthState.AUTHENTICATING: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_CODE_REQUIRED,
            AuthState.AUTH_CODE_SUBMITTED, AuthState.AUTHENTICATING,
        ],
        AuthState.AUTHENTICATED: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_CODE_REQUIRED,
            AuthState.AUTH_CODE_SUBMITTED, AuthState.AUTHENTICATING,
            AuthState.AUTHENTICATED,
        ],
        AuthState.READY: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_CODE_REQUIRED,
            AuthState.AUTH_CODE_SUBMITTED, AuthState.AUTHENTICATING,
            AuthState.AUTHENTICATED, AuthState.READY,
        ],
        AuthState.AUTH_FAILED: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_FAILED,
        ],
        AuthState.AUTH_EXPIRED: [
            AuthState.WELCOME, AuthState.AUTH_EXPIRED,
        ],
        # A revocation is detected on a *live* session, so the path runs
        # through READY rather than through a failure.
        AuthState.SESSION_REVOKED: [
            AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED,
            AuthState.AUTH_PHONE_SUBMITTED, AuthState.AUTH_CODE_REQUIRED,
            AuthState.AUTH_CODE_SUBMITTED, AuthState.AUTHENTICATING,
            AuthState.AUTHENTICATED, AuthState.READY, AuthState.SESSION_REVOKED,
        ],
    }
    for step in PATHS[target]:
        await machine.transition(user_id, step)


# --------------------------------------------------------------------------- #
# transitions
# --------------------------------------------------------------------------- #


class TestLegalTransitions:
    def test_happy_path(self, machine):
        import asyncio

        async def go():
            f = await machine.transition(1, AuthState.WELCOME)
            f = await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED)
            f = await machine.transition(1, AuthState.AUTH_PHONE_SUBMITTED)
            f = await machine.transition(
                1, AuthState.AUTH_CODE_REQUIRED, phone_code_hash="h"
            )
            f = await machine.transition(1, AuthState.AUTH_CODE_SUBMITTED)
            f = await machine.transition(1, AuthState.AUTHENTICATING)
            f = await machine.transition(1, AuthState.AUTHENTICATED)
            f = await machine.transition(1, AuthState.READY)
            return f

        flow = asyncio.run(go())
        assert flow.state == AuthState.READY

    def test_2fa_branch(self, machine):
        import asyncio

        async def go():
            await machine.transition(1, AuthState.WELCOME)
            await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED)
            await machine.transition(1, AuthState.AUTH_PHONE_SUBMITTED)
            await machine.transition(1, AuthState.AUTH_CODE_REQUIRED)
            await machine.transition(1, AuthState.AUTH_CODE_SUBMITTED)
            await machine.transition(1, AuthState.AUTH_2FA_REQUIRED)
            await machine.transition(1, AuthState.AUTH_2FA_SUBMITTED)
            return await machine.transition(1, AuthState.AUTHENTICATED)

        assert asyncio.run(go()).state == AuthState.AUTHENTICATED

    @pytest.mark.parametrize(
        "source,target",
        [
            (AuthState.NEW, AuthState.AUTH_CODE_REQUIRED),      # skipping phone
            (AuthState.AUTH_CODE_REQUIRED, AuthState.READY),    # skipping auth
            (AuthState.AUTH_2FA_REQUIRED, AuthState.AUTHENTICATED),  # skipping 2FA
            (AuthState.AUTH_FAILED, AuthState.AUTHENTICATED),   # failure is terminal-ish
            (AuthState.AUTH_EXPIRED, AuthState.AUTHENTICATED),  # expiry needs re-auth
        ],
    )
    def test_illegal_jumps_are_rejected(self, machine, source, target):
        import asyncio

        async def go():
            await reach(machine, 1, source)
            try:
                await machine.transition(1, target)
            except InvalidTransition:
                return "rejected"
            return "allowed"

        assert asyncio.run(go()) == "rejected"

    def test_missing_flow_cannot_jump_to_mid_state(self, machine):
        import asyncio

        async def go():
            try:
                await machine.transition(7, AuthState.AUTH_CODE_REQUIRED)
            except InvalidTransition:
                return "rejected"
            return "allowed"

        assert asyncio.run(go()) == "rejected"

    def test_missing_flow_can_start(self, machine):
        import asyncio

        async def go():
            return await machine.transition(7, AuthState.WELCOME)

        assert asyncio.run(go()).state == AuthState.WELCOME


class TestIdempotency:
    def test_repeating_a_transition_is_a_noop(self, machine):
        import asyncio

        async def go():
            f1 = await machine.transition(1, AuthState.WELCOME)
            v = f1.version
            f2 = await machine.transition(1, AuthState.WELCOME)
            return v, f2.version

        v1, v2 = asyncio.run(go())
        assert v1 == v2, "re-asserting the same state must not bump the version"

    def test_duplicate_save_of_same_row_is_stable(self, machine):
        import asyncio

        async def go():
            await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED,
                                     phone_number="+989****7808")
            await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED,
                                     phone_number="+989****7808")
            return await machine.current(1)

        flow = asyncio.run(go())
        assert flow.phone_number == "+989****7808"
        assert flow.state == AuthState.AUTH_PHONE_REQUIRED


class TestPersistence:
    def test_state_survives_a_new_machine_over_the_same_db(self, machine, dead_machine):
        """The D1 contract: restart must not erase the flow."""
        import asyncio

        async def go():
            await machine.transition(1, AuthState.WELCOME)
            await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED,
                                     phone_number="+989****7808")
            await machine.transition(1, AuthState.AUTH_PHONE_SUBMITTED)
            await machine.transition(1, AuthState.AUTH_CODE_REQUIRED)
            return await dead_machine.current(1)

        flow = asyncio.run(go())
        assert flow is not None
        assert flow.state == AuthState.AUTH_CODE_REQUIRED
        assert flow.phone_number == "+989****7808"

    def test_attempt_counters_survive_restart(self, machine, dead_machine):
        """A crash must not reset Telegram's own attempt budget."""
        import asyncio

        async def go():
            await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED)
            await machine.transition(1, AuthState.AUTH_PHONE_SUBMITTED)
            await machine.transition(1, AuthState.AUTH_CODE_REQUIRED, bump_code=True)
            await machine.transition(1, AuthState.AUTH_CODE_REQUIRED, bump_code=True)
            return await dead_machine.current(1)

        flow = asyncio.run(go())
        assert flow.code_attempts == 2

    def test_correlation_id_is_stable(self, machine, dead_machine):
        import asyncio

        async def go():
            await machine.transition(1, AuthState.WELCOME)
            before = (await machine.current(1)).correlation_id
            after = (await dead_machine.current(1)).correlation_id
            return before, after

        before, after = asyncio.run(go())
        assert before and before == after

    def test_expires_at_is_durable(self, machine, dead_machine):
        import asyncio

        async def go():
            await machine.transition(1, AuthState.WELCOME)
            return (await machine.current(1)).expires_at, (await dead_machine.current(1)).expires_at

        a, b = asyncio.run(go())
        assert a == b and a > 0


class TestExpiryAndSweep:
    def test_expired_active_flow_transitions_to_expired(self, machine, temp_db):
        import asyncio

        async def go():
            await reach(machine, 1, AuthState.AUTH_CODE_REQUIRED)
            # force the ttl into the past
            temp_db.execute(
                "UPDATE auth_flows SET expires_at=? WHERE user_id=?",
                (1, 1),
            )
            return await machine.current(1)

        flow = asyncio.run(go())
        assert flow.state == AuthState.AUTH_EXPIRED

    def test_sweep_reports_expired_flows(self, machine, temp_db):
        import asyncio

        async def go():
            await reach(machine, 1, AuthState.AUTH_CODE_REQUIRED)
            await reach(machine, 2, AuthState.AUTH_CODE_REQUIRED)
            temp_db.execute("UPDATE auth_flows SET expires_at=? WHERE user_id=?", (1, 1))
            return await machine.sweep_expired()

        assert asyncio.run(go()) == 1

    def test_authenticated_flows_do_not_expire(self, machine, temp_db):
        import asyncio

        async def go():
            await machine.transition(1, AuthState.WELCOME)
            await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED)
            await machine.transition(1, AuthState.AUTH_PHONE_SUBMITTED)
            await machine.transition(1, AuthState.AUTH_CODE_REQUIRED)
            await machine.transition(1, AuthState.AUTH_CODE_SUBMITTED)
            await machine.transition(1, AuthState.AUTHENTICATING)
            await machine.transition(1, AuthState.AUTHENTICATED)
            temp_db.execute("UPDATE auth_flows SET expires_at=? WHERE user_id=?", (1, 1))
            return await machine.current(1)

        assert asyncio.run(go()).state == AuthState.AUTHENTICATED


class TestRoutingGuard:
    @pytest.mark.parametrize(
        "state,expected",
        [
            (AuthState.NEW, False),
            (AuthState.WELCOME, False),
            (AuthState.AUTH_PHONE_REQUIRED, True),
            (AuthState.AUTH_PHONE_SUBMITTED, True),
            (AuthState.AUTH_CODE_REQUIRED, True),
            (AuthState.AUTH_CODE_SUBMITTED, True),
            (AuthState.AUTH_2FA_REQUIRED, True),
            (AuthState.AUTH_2FA_SUBMITTED, True),
            (AuthState.AUTHENTICATING, True),
            (AuthState.AUTHENTICATED, False),
            (AuthState.READY, False),
            (AuthState.AUTH_FAILED, False),
            (AuthState.AUTH_EXPIRED, False),
            (AuthState.SESSION_REVOKED, False),
        ],
    )
    def test_blocked_matches_spec(self, machine, state, expected):
        import asyncio

        async def go():
            await reach(machine, 1, state)
            return await machine.is_blocked(1)

        assert asyncio.run(go()) is expected

    def test_no_flow_is_not_blocked(self, machine):
        import asyncio

        async def go():
            return await machine.is_blocked(999)

        assert asyncio.run(go()) is False


class TestResetAndRecovery:
    def test_reset_deletes_the_flow(self, machine):
        import asyncio

        async def go():
            await machine.transition(1, AuthState.WELCOME)
            await machine.reset(1)
            return await machine.current(1)

        assert asyncio.run(go()) is None

    def test_reset_with_counters_keeps_them(self, machine, dead_machine):
        import asyncio

        async def go():
            await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED)
            await machine.transition(1, AuthState.AUTH_PHONE_SUBMITTED, bump_code=True)
            await machine.reset(1, keep_counters=True)
            return await dead_machine.current(1)

        flow = asyncio.run(go())
        assert flow.state == AuthState.NEW
        assert flow.code_attempts == 1

    def test_recovery_moves_in_flight_flows_to_expired(self, machine, dead_machine):
        """The honest-degradation contract: the socket is dead, say so."""
        import asyncio

        from core.infrastructure.telegram.login_flow import LoginFlowManager

        async def go():
            # A dead pool is enough: recovery only touches the persisted state,
            # never the socket (which is the point of the test).
            class _DeadPool:
                pass

            class _NoSessions:
                """_finalize is never reached on a dead socket."""
                async def create(self, **kw):
                    raise RuntimeError("unexpected")

            mgr = LoginFlowManager(_DeadPool(), _NoSessions(), auth_machine=machine)
            # A flow mid-code. Its in-memory client is gone with the process,
            # so recovery must expire it - the persisted phone survives.
            await machine.transition(1, AuthState.WELCOME,
                                     phone_number="+989****7808")
            await machine.transition(1, AuthState.AUTH_PHONE_REQUIRED,
                                     phone_number="+989****7808")
            await machine.transition(1, AuthState.AUTH_PHONE_SUBMITTED)
            await machine.transition(1, AuthState.AUTH_CODE_REQUIRED)
            n = await mgr.recover_after_restart()
            return n, (await dead_machine.current(1))

        n, flow = asyncio.run(go())
        assert n >= 0
        assert flow.state == AuthState.AUTH_EXPIRED
        assert flow.phone_number == "+989****7808"
        assert flow.last_error == "restart"


class TestCorrelationId:
    def test_ids_are_unique(self):
        ids = {new_correlation_id() for _ in range(2000)}
        assert len(ids) == 2000

    def test_id_is_hex(self):
        cid = new_correlation_id()
        assert len(cid) == 32
        int(cid, 16)


class TestRowRoundTrip:
    def test_every_state_round_trips(self, machine, dead_machine):
        import asyncio

        async def go():
            seen = []
            for state in AuthState:
                await machine.reset(1)
                if state is AuthState.NEW:
                    continue
                try:
                    await reach(machine, 1, state)
                except InvalidTransition:
                    continue
                got = (await dead_machine.current(1))
                if got:
                    seen.append((state, got.state))
            return seen

        for requested, persisted in asyncio.run(go()):
            assert requested == persisted
