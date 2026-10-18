"""Optimistic concurrency for the persisted authentication flow (DB-14).

The `auth_flows` row carries a `version` column and both the repository and the
state machine describe their writes as version-guarded. Neither was: `save()`
was an unconditional upsert, so the column was decoration and the *last* writer
won. Two real consequences, both covered here:

  * a callback for a flow that has already moved on overwrites the newer state
    (a login regresses to a state the user has left); and
  * two concurrent callbacks bumping an attempt counter lose one bump, which is
    the one thing the state machine promises cannot happen ("a crash cannot be
    used to reset the Telegram-side attempt budget").

The fix is a guarded statement (`WHERE excluded.version > auth_flows.version`)
plus a state machine that re-reads and re-applies when its write is refused
instead of assuming it landed.
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
    ConcurrentFlowUpdate,
    InvalidTransition,
)

UID = 501
DAY = 86400


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "atf_flow.db")
    database = SqliteDatabase(path)
    yield database
    database.connection.close()


@pytest.fixture()
def repo(db):
    return SqliteAuthFlowRepository(db)


@pytest.fixture()
def machine(repo):
    return AuthStateMachine(repo, ttl_seconds=600)


@pytest.fixture()
def other_machine(repo):
    """A second machine over the same rows: a second process, or a second task."""
    return AuthStateMachine(repo, ttl_seconds=600)


def row_for(user_id, state, version, **over):
    row = {
        "user_id": int(user_id),
        "state": state.value if isinstance(state, AuthState) else state,
        "phone_number": "",
        "credential_id": "default",
        "phone_code_hash": "",
        "code_attempts": 0,
        "password_attempts": 0,
        "version": int(version),
        "created_at": 1000,
        "updated_at": 1000,
        "expires_at": 10_000,
        "last_error": "",
        "correlation_id": "corr",
    }
    row.update(over)
    return row


async def stored(repo, user_id=UID):
    return await repo.get(user_id)


# --------------------------------------------------------------------------- #
# the repository enforces the revision
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_first_write_for_a_user_is_written(repo):
    """No row yet: there is nothing to be stale against."""
    assert await repo.save(row_for(UID, AuthState.NEW, 1)) is True
    assert (await stored(repo))["state"] == AuthState.NEW.value


@pytest.mark.asyncio
async def test_the_same_revision_is_refused(repo):
    """Two writers that both read v5 both present v6: the second must lose."""
    assert await repo.save(row_for(UID, AuthState.AUTH_PHONE_REQUIRED, 6)) is True
    assert (
        await repo.save(
            row_for(UID, AuthState.AUTH_CODE_REQUIRED, 6, phone_number="+15550000002")
        )
        is False
    )
    row = await stored(repo)
    assert row["state"] == AuthState.AUTH_PHONE_REQUIRED.value
    assert row["version"] == 6
    assert row["phone_number"] == ""


@pytest.mark.asyncio
async def test_an_older_revision_is_refused(repo):
    """A late callback carrying an old revision cannot roll the row back."""
    assert await repo.save(row_for(UID, AuthState.AUTH_CODE_REQUIRED, 9)) is True
    assert await repo.save(row_for(UID, AuthState.AUTH_PHONE_REQUIRED, 3)) is False
    row = await stored(repo)
    assert row["state"] == AuthState.AUTH_CODE_REQUIRED.value
    assert row["version"] == 9


@pytest.mark.asyncio
async def test_a_newer_revision_is_written(repo):
    assert await repo.save(row_for(UID, AuthState.AUTH_PHONE_REQUIRED, 6)) is True
    assert await repo.save(row_for(UID, AuthState.AUTH_CODE_REQUIRED, 7)) is True
    row = await stored(repo)
    assert row["state"] == AuthState.AUTH_CODE_REQUIRED.value
    assert row["version"] == 7


@pytest.mark.asyncio
async def test_an_insert_racing_an_existing_row_loses(repo):
    """A fresh flow (version 1) must not overwrite a row a peer just inserted."""
    assert await repo.save(row_for(UID, AuthState.NEW, 1)) is True
    assert await repo.save(row_for(UID, AuthState.AUTH_PHONE_REQUIRED, 1)) is False
    assert (await stored(repo))["state"] == AuthState.NEW.value


# --------------------------------------------------------------------------- #
# deletes are revision-guarded too
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_guarded_delete_removes_only_the_revision_it_saw(repo):
    assert await repo.save(row_for(UID, AuthState.AUTH_FAILED, 4)) is True
    assert await repo.delete(UID, version=3) is False
    assert await stored(repo) is not None
    assert await repo.delete(UID, version=4) is True
    assert await stored(repo) is None


@pytest.mark.asyncio
async def test_an_unguarded_delete_still_works(repo):
    """Callers that genuinely mean "remove whatever is there" keep the old shape."""
    assert await repo.save(row_for(UID, AuthState.AUTH_FAILED, 4)) is True
    assert await repo.delete(UID) is True
    assert await stored(repo) is None


# --------------------------------------------------------------------------- #
# the state machine: refused writes are re-applied, not assumed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_stale_transition_cannot_regress_a_newer_state(machine, other_machine):
    """The DB-14 defect: the stale copy used to win because both wrote v3."""
    await machine.transition(UID, AuthState.AUTH_PHONE_REQUIRED, phone_number="+1555")
    first = await machine.current(UID)
    stale = await other_machine.current(UID)  # a second process reading the same row
    assert first.version == stale.version == 2

    await machine.transition(UID, AuthState.AUTH_PHONE_SUBMITTED, flow=first)

    # WELCOME is legal from the state the stale copy was in, but not from the
    # state the flow actually reached — so the re-apply is refused for the real
    # state instead of silently overwriting it.
    with pytest.raises(InvalidTransition):
        await other_machine.transition(UID, AuthState.WELCOME, flow=stale)

    row = await stored(other_machine._repo)
    assert row["state"] == AuthState.AUTH_PHONE_SUBMITTED.value
    assert row["version"] == 3


@pytest.mark.asyncio
async def test_two_concurrent_callbacks_do_not_lose_an_attempt_count(machine, other_machine):
    """Both bumps survive: the refused writer re-applies onto the winning revision."""
    await machine.transition(UID, AuthState.AUTH_PHONE_REQUIRED, phone_number="+1555")
    await machine.transition(UID, AuthState.AUTH_PHONE_SUBMITTED, phone_number="+1555")
    await machine.transition(UID, AuthState.AUTH_CODE_REQUIRED, phone_number="+1555")
    a = await machine.current(UID)
    b = await other_machine.current(UID)
    assert a.version == b.version == 4
    assert a.code_attempts == b.code_attempts == 0

    await machine.transition(UID, AuthState.AUTH_CODE_REQUIRED, flow=a, bump_code=True,
                             last_error="a")
    await other_machine.transition(UID, AuthState.AUTH_CODE_REQUIRED, flow=b, bump_code=True,
                                   last_error="b")

    row = await stored(other_machine._repo)
    assert row["code_attempts"] == 2, "one of the two attempt bumps was lost"
    assert row["last_error"] == "b"
    assert row["state"] == AuthState.AUTH_CODE_REQUIRED.value
    assert row["version"] == 6


@pytest.mark.asyncio
async def test_a_duplicate_callback_is_still_absorbed(machine, other_machine):
    """Idempotency is unchanged: a repeat with no side effect writes nothing."""
    await machine.transition(UID, AuthState.AUTH_PHONE_REQUIRED, phone_number="+1555")
    flow = await machine.current(UID)
    version = flow.version

    again = await other_machine.transition(UID, AuthState.AUTH_PHONE_REQUIRED, flow=flow)
    assert again.state == AuthState.AUTH_PHONE_REQUIRED
    assert (await stored(other_machine._repo))["version"] == version


@pytest.mark.asyncio
async def test_a_permanently_refused_write_raises_instead_of_claiming_success(machine, repo):
    """If every attempt is refused the caller is told, and nothing is half-applied."""
    await machine.transition(UID, AuthState.AUTH_PHONE_REQUIRED, phone_number="+1555")
    flow = await machine.current(UID)

    class AlwaysRefuse:
        """A writer whose every save loses the race."""

        def __init__(self, inner):
            self._inner = inner
            self.saves = 0

        async def save(self, row):
            self.saves += 1
            return False

        def __getattr__(self, name):
            return getattr(self._inner, name)

    stub = AlwaysRefuse(repo)
    blocked = AuthStateMachine(stub, ttl_seconds=600)

    with pytest.raises(ConcurrentFlowUpdate) as exc:
        await blocked.transition(UID, AuthState.AUTH_PHONE_SUBMITTED, flow=flow)

    assert isinstance(exc.value, InvalidTransition), "callers catch InvalidTransition"
    assert stub.saves == AuthStateMachine.MAX_WRITE_ATTEMPTS, "no unbounded retry loop"
    row = await stored(repo)
    assert row["state"] == AuthState.AUTH_PHONE_REQUIRED.value
    assert row["version"] == flow.version


@pytest.mark.asyncio
async def test_a_reset_cannot_delete_a_login_the_user_just_started(machine, repo):
    """reset() reads, then deletes — another process can start a login in between.

    The wrapper writes a newer revision *after* handing reset() its row, which is
    precisely the window a plain `DELETE ... WHERE user_id=?` cannot see.
    """
    await machine.transition(UID, AuthState.AUTH_PHONE_REQUIRED, phone_number="+1555")
    await machine.transition(UID, AuthState.AUTH_FAILED, last_error="bad code")
    current = await machine.current(UID)
    newer = row_for(UID, AuthState.AUTH_PHONE_REQUIRED, current.version + 1,
                    phone_number="+1555", expires_at=2_000_000_000, updated_at=2000)

    class Interleaved:
        """Delegates everything, but slips a competing write in after a read."""

        def __init__(self, inner):
            self._inner = inner
            self.armed = True

        async def get(self, user_id):
            row = await self._inner.get(user_id)  # what reset() will act on
            if self.armed:
                self.armed = False
                await self._inner.save(newer)  # the user starts a new login
            return row

        def __getattr__(self, name):
            return getattr(self._inner, name)

    blocked = AuthStateMachine(Interleaved(repo), ttl_seconds=600)
    await blocked.reset(UID)

    row = await stored(repo)
    assert row is not None, "reset() deleted a login the user had just started"
    assert row["state"] == AuthState.AUTH_PHONE_REQUIRED.value
    assert row["version"] == newer["version"]


@pytest.mark.asyncio
async def test_reset_still_clears_a_flow_it_owns(machine, repo):
    await machine.transition(UID, AuthState.AUTH_PHONE_REQUIRED, phone_number="+1555")
    await machine.transition(UID, AuthState.AUTH_FAILED, last_error="bad code")
    await machine.reset(UID)
    assert await stored(repo) is None


@pytest.mark.asyncio
async def test_reset_keeping_counters_restates_the_same_revision(machine, repo):
    await machine.transition(UID, AuthState.AUTH_PHONE_REQUIRED, phone_number="+1555")
    await machine.transition(UID, AuthState.AUTH_PHONE_SUBMITTED, bump_code=True)
    before = await stored(repo)

    await machine.reset(UID, keep_counters=True)

    after = await stored(repo)
    assert after["state"] == AuthState.NEW.value
    assert after["code_attempts"] == before["code_attempts"], "counters survive a reset"
    assert after["phone_number"] == "+1555"
    assert after["version"] == before["version"] + 1


# --------------------------------------------------------------------------- #
# the boundary is enforced where it can be atomic
# --------------------------------------------------------------------------- #


def test_the_guard_is_in_the_statement_not_the_python():
    """A check in Python cannot be atomic; the comparison has to be in the SQL.

    This is the drift alarm for the finding: if a future edit drops the WHERE
    clause the behaviour tests above still pass on a quiet database, because
    nothing races them. This one does not.
    """
    import inspect

    from core.infrastructure.persistence import sqlite_repositories as mod

    source = inspect.getsource(mod.SqliteAuthFlowRepository.save)
    assert "excluded.version > auth_flows.version" in source
    assert "WHERE" in source.split("ON CONFLICT")[1]
