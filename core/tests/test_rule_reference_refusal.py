"""A rule that names a session that does not exist is refused, not a 500.

Found on the deployed v2.2.4 by the live QA probe: creating a task through
`POST /api/rules` with an unknown `session_id` escaped as

    HTTP 500 {"error": "Unexpected <class 'sqlite3.IntegrityError'>: FOREIGN KEY
              constraint failed"}

Two problems in one response. The user is told nothing they can act on — the
field they got wrong is not named, and the message is a Python driver string —
and the API published an internal failure (`grpcError` maps anything unclassified
to 500), which is exactly the class of answer that hides a real outage behind a
"try again".

A reference is the caller's business, so the servicer now classifies a storage
constraint failure as INVALID_ARGUMENT (HTTP 400, `code: invalid_reference`) and
logs anything else. These tests pin both halves: the refusal a client can act on,
and that an unrelated storage failure does not get mislabelled as the caller's
fault.
"""
from __future__ import annotations

import asyncio
import json

import grpc
import pytest

from core.application.use_cases import ForwardRuleUseCases
from core.infrastructure.grpc_server.servicers import ForwardRuleControlServicer
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqliteForwardRuleRepository
from core.proto import autoforward_pb2 as pb

ALICE = 5094837833


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class Aborted(Exception):
    def __init__(self, code, details):
        super().__init__(f"{code.name if hasattr(code, 'name') else code}: {details}")
        self.code = code
        self.details = details


class Context:
    def __init__(self):
        self.aborted = False
        self.code = None
        self.details = ""

    async def abort(self, code, details):
        self.aborted = True
        self.code = code
        self.details = details
        raise Aborted(code, details)


class Harness:
    def __init__(self, with_session: str | None = "s1"):
        self.db = SqliteDatabase(":memory:")
        self.repo = SqliteForwardRuleRepository(self.db)
        self.control = ForwardRuleControlServicer(ForwardRuleUseCases(self.repo))
        if with_session:
            self.db.execute(
                "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
                " username, first_name, is_active, is_authorized, created_at, updated_at)"
                " VALUES (?, '+1000', '', '', '', '', 1, 1, 0, 0)", (with_session,),
            )

    @staticmethod
    def _rule(owner: int = ALICE, session_id: str = "s1", **fields):
        rule = pb.ForwardRule()
        rule.session_id = session_id
        rule.source_chat_id = "-1001"
        rule.target_chat_id = "-1002"
        rule.routing_type = pb.CHANNEL_TO_CHANNEL
        rule.forward_mode = pb.COPY_MESSAGE
        rule.is_active = True
        rule.owner_user_id = owner
        rule.custom_metadata_json = json.dumps(fields.pop("metadata", {}))
        for key, value in fields.items():
            setattr(rule, key, value)
        return rule

    def create(self, **fields):
        ctx = Context()
        try:
            out = run(self.control.CreateRule(
                pb.CreateRuleRequest(rule=self._rule(**fields)), ctx))
        except Aborted as exc:
            return ctx, None, exc
        return ctx, out, None

    def update(self, rule_id: str, session_id: str = "s1", mask=None, **fields):
        ctx = Context()
        rule = self._rule(session_id=session_id, **fields)
        rule.id = rule_id
        try:
            out = run(self.control.UpdateRule(
                pb.UpdateRuleRequest(rule=rule, update_mask=mask or []), ctx))
        except Aborted as exc:
            return ctx, None, exc
        return ctx, out, None


# --------------------------------------------------------------------------- #
# the refusal
# --------------------------------------------------------------------------- #
def test_a_rule_naming_an_unknown_session_is_refused_as_invalid():
    harness = Harness(with_session=None)          # no session row exists at all

    ctx, out, exc = harness.create(session_id="s-does-not-exist")

    assert exc is not None, "an unknown session was accepted"
    assert ctx.code == grpc.StatusCode.INVALID_ARGUMENT
    assert ctx.details.startswith("invalid_reference")
    assert out is None


def test_a_rule_naming_a_known_session_is_still_created():
    harness = Harness()

    ctx, out, exc = harness.create()

    assert exc is None and ctx.aborted is False
    assert out is not None and out.id


def test_a_session_cannot_be_moved_by_an_update():
    """The reference is not a field an update may rewrite, so it cannot be
    re-pointed at a session that does not exist: the payload naming one is
    accepted for the fields that are editable and the session stays put."""
    harness = Harness()
    _, created, exc = harness.create()
    assert exc is None and created is not None

    _, out, exc = harness.update(created.id, session_id="s-theirs", name="renamed")

    assert exc is None and out is not None
    stored = run(harness.repo.get_by_id(created.id))
    assert stored is not None and stored.session_id == "s1"


def test_a_session_is_not_a_maskable_field():
    """Naming it in the mask is refused by name, not silently ignored."""
    harness = Harness()
    _, created, _ = harness.create()

    ctx, out, exc = harness.update(created.id, session_id="s-theirs", mask=["session_id"])

    assert exc is not None, "a masked session move was accepted"
    assert ctx.code == grpc.StatusCode.INVALID_ARGUMENT
    assert "session_id" in ctx.details
    stored = run(harness.repo.get_by_id(created.id))
    assert stored is not None and stored.session_id == "s1"


def test_an_update_keeping_its_session_still_works():
    harness = Harness()
    _, created, _ = harness.create()

    ctx, out, exc = harness.update(created.id, name="renamed")

    assert exc is None and out is not None
    assert ctx.aborted is False


# --------------------------------------------------------------------------- #
# the classification itself
# --------------------------------------------------------------------------- #
def test_a_storage_failure_that_is_not_a_constraint_is_not_blamed_on_the_caller():
    """An internal failure stays internal; only the reference case is a refusal."""
    import sqlite3

    harness = Harness()
    ctx = Context()

    class Boom(sqlite3.OperationalError):
        pass

    async def scenario():
        # A constraint violation is the caller's fault: INVALID_ARGUMENT, with a
        # code a client can translate.
        # (The fake context raises from abort the way gRPC ends the call, so the
        # classification is read off the exception.)
        try:
            await harness.control._refuse_reference_failure(
                ctx, sqlite3.IntegrityError("FOREIGN KEY constraint failed"))
        except Aborted as exc:
            assert exc.code == grpc.StatusCode.INVALID_ARGUMENT
            assert exc.details.startswith("invalid_reference")
        else:  # pragma: no cover
            raise AssertionError("an integrity error was not refused")

        # Anything else is ours, and must not be reported as the caller's fault.
        ctx2 = Context()
        try:
            await harness.control._refuse_reference_failure(ctx2, Boom("disk I/O error"))
        except Aborted as exc:
            assert exc.code == grpc.StatusCode.INTERNAL
            assert "storage_error" in exc.details
        else:  # pragma: no cover
            raise AssertionError("an operational error was not aborted")

    run(scenario())
    assert ctx.code == grpc.StatusCode.INVALID_ARGUMENT
    assert ctx.details.startswith("invalid_reference")
