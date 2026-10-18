"""A task may only name a bot its own owner holds.

The route pair is stored in the rule's metadata, and the forwarder reads the
attached bot twice: to inherit that bot's defaults, and to deliver through that
bot's credential. Until this guard existed only the *settings* endpoint checked
it — `CreateRule` and `UpdateRule` copied `custom_metadata_json` straight into
the entity, so:

  * a payload naming another tenant's bot was accepted, and the task then ran
    with that tenant's defaults and delivered on that tenant's credential;
  * an unknown id was accepted just as readily, so the task silently fell back
    to the account lane with no way for the user to tell why.

Both defects are the same missing question: *is this bot mine?* These tests pin
the answer on every path a rule is written through.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.application.repositories import BotToken
from core.application.use_cases import BotTokenUseCases, ForwardRuleUseCases
from core.infrastructure.grpc_server.servicers import (
    BotTokenControlServicer,
    ForwardRuleControlServicer,
)
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteBotTokenRepository,
    SqliteForwardRuleRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.proto import autoforward_pb2 as pb

ALICE = 5094837833
BOB = 777000111


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class Req:
    """A request message, as the servicers see one.

    Unset proto3 fields are empty rather than missing, and the servicers rely on
    that (`int(r.priority or 10)`, `list(r.target_chat_ids or [])`), so a missing
    attribute answers `""` instead of raising — the same shape the wire gives.
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return ""


class Aborted(Exception):
    """Raised by the fake context: the servicer asked gRPC to end the call."""

    def __init__(self, code, details):
        super().__init__(f"{code.name if hasattr(code, 'name') else code}: {details}")
        self.code = code
        self.details = details


class Context:
    """A context that records an abort instead of raising inside the servicer."""

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
    def __init__(self):
        self.db = SqliteDatabase(":memory:")
        self.tokens_repo = SqliteBotTokenRepository(self.db, CryptoService("test-master-key"))
        self.rules_repo = SqliteForwardRuleRepository(self.db)
        self.rules = ForwardRuleUseCases(self.rules_repo)
        self.bot_tokens = BotTokenControlServicer(
            BotTokenUseCases(self.tokens_repo), rules=self.rules_repo,
        )
        self.control = ForwardRuleControlServicer(self.rules, bot_tokens=self.tokens_repo)
        self.db.execute(
            "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
            " username, first_name, is_active, is_authorized, created_at, updated_at)"
            " VALUES ('s1', '+1000', '', '', '', '', 1, 1, 0, 0)", (),
        )

    def seed_token(self, token_id: str, owner: int) -> str:
        run(self.tokens_repo.add(BotToken(
            id=token_id, label=f"@{token_id}", token="7878773795:AAsecret",
            owner_user_id=owner, created_at=1, updated_at=1, defaults={},
        )))
        return token_id

    @staticmethod
    def _rule_pb(owner: int, metadata: dict, **fields):
        rule = pb.ForwardRule()
        rule.session_id = "s1"
        rule.source_chat_id = "-1001"
        rule.target_chat_id = "-1002"
        rule.routing_type = pb.CHANNEL_TO_CHANNEL
        rule.forward_mode = pb.COPY_MESSAGE
        rule.is_active = True
        rule.owner_user_id = owner
        rule.custom_metadata_json = json.dumps(metadata)
        for key, value in fields.items():
            setattr(rule, key, value)
        return rule

    def create(self, **fields):
        ctx = Context()
        metadata = fields.pop("metadata", {})
        owner = fields.pop("owner", ALICE)
        rule = self._rule_pb(owner, metadata, **fields)
        try:
            out = run(self.control.CreateRule(pb.CreateRuleRequest(rule=rule), ctx))
        except Aborted as exc:
            return ctx, None, exc
        return ctx, out, None

    def update(self, rule_id: str, owner: int, **fields):
        ctx = Context()
        metadata = fields.pop("metadata", {})
        rule = self._rule_pb(owner, metadata, **fields)
        rule.id = rule_id
        try:
            out = run(self.control.UpdateRule(
                pb.UpdateRuleRequest(rule=rule, update_mask=[]), ctx))
        except Aborted as exc:
            return ctx, None, exc
        return ctx, out, None

    def stored_rule(self, rule_id: str):
        return run(self.rules_repo.get_by_id(rule_id))


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def test_create_with_own_bot_is_accepted_and_attached():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    ctx, out, exc = h.create(metadata={"bot_token_id": "tok-a"})
    assert exc is None, exc
    assert ctx.aborted is False
    assert out.id
    assert h.stored_rule(out.id).bot_token_id == "tok-a"


def test_create_with_another_tenants_bot_is_refused():
    h = Harness()
    h.seed_token("tok-b", BOB)
    ctx, out, exc = h.create(metadata={"bot_token_id": "tok-b"}, owner=ALICE)
    assert isinstance(exc, Aborted)
    assert "bot_token_not_owned" in exc.details
    assert ctx.aborted is True
    # and nothing was written
    assert run(h.rules_repo.list_by_owner(ALICE)) == []


def test_create_with_unknown_bot_is_refused_the_same_way():
    h = Harness()
    ctx, out, exc = h.create(metadata={"bot_token_id": "tok-does-not-exist"})
    assert isinstance(exc, Aborted)
    # the same code as a foreign id: an id must not be probeable
    assert "bot_token_not_owned" in exc.details


def test_create_without_a_bot_still_works():
    """The account lane is not a privilege: a task with no attached bot is the
    normal case and must keep working exactly as before."""
    h = Harness()
    ctx, out, exc = h.create()
    assert exc is None, exc
    assert out.id
    assert h.stored_rule(out.id).bot_token_id == ""


def test_create_naming_a_bot_without_an_owner_is_refused():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    ctx, out, exc = h.create(metadata={"bot_token_id": "tok-a"}, owner=0)
    assert isinstance(exc, Aborted)
    assert "bot_token_not_owned" in exc.details


def test_create_fails_closed_when_the_token_registry_is_unavailable():
    h = Harness()
    h.control = ForwardRuleControlServicer(h.rules, bot_tokens=None)
    ctx, out, exc = h.create(metadata={"bot_token_id": "tok-a"})
    assert isinstance(exc, Aborted)
    assert "bot_token_registry_unavailable" in exc.details


# --------------------------------------------------------------------------- #
# Update: the legacy path and the masked path
# --------------------------------------------------------------------------- #
def _rule(h: Harness, owner: int = ALICE, token_id: str = "") -> str:
    ctx, out, exc = h.create(metadata={"bot_token_id": token_id} if token_id else {},
                             owner=owner)
    assert exc is None, exc
    return out.id


def test_update_to_another_tenants_bot_is_refused():
    h = Harness()
    h.seed_token("tok-b", BOB)
    rule_id = _rule(h)
    ctx, out, exc = h.update(rule_id, ALICE, metadata={"bot_token_id": "tok-b"})
    assert isinstance(exc, Aborted)
    assert "bot_token_not_owned" in exc.details
    assert h.stored_rule(rule_id).bot_token_id == ""


def test_update_to_own_bot_is_accepted():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    rule_id = _rule(h)
    ctx, out, exc = h.update(rule_id, ALICE, metadata={"bot_token_id": "tok-a"})
    assert exc is None, exc
    assert h.stored_rule(rule_id).bot_token_id == "tok-a"


def test_masked_update_to_a_foreign_bot_is_refused():
    h = Harness()
    h.seed_token("tok-b", BOB)
    rule_id = _rule(h)
    ctx = Context()
    rule = h._rule_pb(ALICE, {"bot_token_id": "tok-b"})
    rule.id = rule_id
    try:
        run(h.control.UpdateRule(
            pb.UpdateRuleRequest(rule=rule, update_mask=["custom_metadata_json"]), ctx))
    except Aborted as exc:
        assert "bot_token_not_owned" in exc.details
    else:  # pragma: no cover - the guard must bite
        raise AssertionError("a masked update attached a foreign bot")
    assert h.stored_rule(rule_id).bot_token_id == ""


def test_update_that_keeps_the_existing_own_bot_still_works():
    """The guard must not break the common edit: the rule already points at a
    bot its owner holds, and an unrelated field changes."""
    h = Harness()
    h.seed_token("tok-a", ALICE)
    rule_id = _rule(h, token_id="tok-a")
    ctx, out, exc = h.update(rule_id, ALICE, name="renamed",
                             metadata={"bot_token_id": "tok-a"})
    assert exc is None, exc
    assert h.stored_rule(rule_id).bot_token_id == "tok-a"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
