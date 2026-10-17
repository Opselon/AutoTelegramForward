"""The credential half of the source -> BOT -> target route.

The route itself is a per-task setting; what makes it usable from a dashboard or
a phone is a way to attach the bot a task forwards *as*. That is this servicer,
and it holds three rules the rest of the control plane does not need:

  * the secret goes in and never comes out — the response message has no field
    for it, so a leak would have to be a deliberate protocol change;
  * a token is checked with Telegram *before* it is stored, because a dead token
    turns a working task into one that silently stops forwarding;
  * a token a task still routes through cannot be deleted out from under it.

These tests run the real servicer against the real SQLite repositories, so the
ownership rule is proven in the SQL and not only in this file's fakes.
"""
from __future__ import annotations

import asyncio

from core.application.repositories import BotToken
from core.application.use_cases import BotTokenUseCases
from core.infrastructure.grpc_server.servicers import BotTokenControlServicer
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteBotTokenRepository,
    SqliteForwardRuleRepository,
)
from core.infrastructure.security.crypto import CryptoService

ALICE = 5094837833
BOB = 777000111


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeTransportCache:
    """Telegram, as far as the servicer can tell.

    `answer` is either the getMe payload or an exception to raise, so the
    "validated before stored" rule is testable without a network call.
    """

    def __init__(self, answer=None, exc: Exception | None = None):
        self.answer = answer if answer is not None else {"id": 7878773795, "username": "AutoForwarderFreeBot"}
        self.exc = exc
        self.asked: list[str] = []
        self.forgotten: list[str] = []

    async def validate(self, token, token_id="probe"):
        self.asked.append(token)
        if self.exc is not None:
            raise self.exc
        return self.answer

    def forget(self, token_id):
        self.forgotten.append(token_id)


class Harness:
    def __init__(self, transport=None):
        self.db = SqliteDatabase(":memory:")
        self.tokens_repo = SqliteBotTokenRepository(self.db, CryptoService("test-master-key"))
        self.rules_repo = SqliteForwardRuleRepository(self.db)
        self.tokens = BotTokenUseCases(self.tokens_repo)
        self.transport = transport or FakeTransportCache()
        self.servicer = BotTokenControlServicer(
            self.tokens, rules=self.rules_repo, bot_route=self.transport,
        )

    def seed_token(self, token_id: str, owner: int, label: str = "@bot", token: str = "7878773795:AAsecret"):
        run(self.tokens_repo.add(BotToken(
            id=token_id, label=label, token=token, owner_user_id=owner,
            created_at=1, updated_at=1,
        )))
        return token_id


def rules_using(harness: Harness, rule_id: str, token_id: str) -> None:
    """Point a task at a bot, the way the settings registry would."""
    from core.domain.entities import ForwardRule
    from core.application.use_cases import ForwardRuleUseCases

    # A task belongs to a session (FK); create the one it hangs off.
    harness.db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES ('s1', '+1000', '', '', '', '', 1, 1, 0, 0)", (),
    )
    rule = ForwardRule(
        id=rule_id, name="uses a bot", session_id="s1",
        source_chat_id="-1001", target_chat_id="-1002", owner_user_id=ALICE,
    )
    rule.bot_token_id = token_id
    rule.delivery_transport = "BOT"
    run(ForwardRuleUseCases(harness.rules_repo).create(rule))


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_list_is_scoped_to_the_owner():
    h = Harness()
    h.seed_token("tok-a", ALICE, "@alice_bot")
    h.seed_token("tok-b", BOB, "@bob_bot")

    mine = run(h.servicer.ListBotTokens(
        type("R", (), {"owner_user_id": ALICE})(), None,
    ))
    theirs = run(h.servicer.ListBotTokens(
        type("R", (), {"owner_user_id": BOB})(), None,
    ))
    nobody = run(h.servicer.ListBotTokens(
        type("R", (), {"owner_user_id": 0})(), None,
    ))

    assert [t.id for t in mine.tokens] == ["tok-a"]
    assert [t.id for t in theirs.tokens] == ["tok-b"]
    # Fail closed: an unscoped caller gets nothing rather than everything.
    assert list(nobody.tokens) == []


def test_list_never_carries_the_secret():
    h = Harness()
    h.seed_token("tok-a", ALICE, token="7878773795:AA-super-secret")

    resp = run(h.servicer.ListBotTokens(type("R", (), {"owner_user_id": ALICE})(), None))

    # The message itself has no token field; assert it also isn't smuggled into
    # a label or a serialised form.
    payload = resp.SerializeToString().decode("utf-8", "ignore")
    assert "AA-super-secret" not in payload


# --------------------------------------------------------------------------- #
# Creating
# --------------------------------------------------------------------------- #


def test_create_rejects_a_malformed_token_without_storing_anything():
    h = Harness()
    resp = run(h.servicer.CreateBotToken(
        type("R", (), {"owner_user_id": ALICE, "label": "", "token": "not-a-token"})(), None,
    ))

    assert resp.success is False
    assert resp.error_code == "invalid_token"
    assert run(h.tokens.list_all()) == []
    assert h.transport.asked == []


def test_create_asks_telegram_before_storing():
    h = Harness()
    resp = run(h.servicer.CreateBotToken(
        type("R", (), {"owner_user_id": ALICE, "label": "", "token": "7878773795:AAHfakeSecretForTests_0123456789abc"})(), None,
    ))

    assert resp.success is True
    assert h.transport.asked == ["7878773795:AAHfakeSecretForTests_0123456789abc"]
    # No label supplied: the resolved @username is the label, never the token.
    assert resp.token.label == "@AutoForwarderFreeBot"
    stored = run(h.tokens.list_by_owner(ALICE))
    assert [t.id for t in stored] == [resp.token.id]


def test_a_refused_token_is_not_stored():
    h = Harness(transport=FakeTransportCache(exc=RuntimeError("401 Unauthorized")))
    resp = run(h.servicer.CreateBotToken(
        type("R", (), {"owner_user_id": ALICE, "label": "", "token": "7878773795:AAHbadSecretForTests_0123456789abcd"})(), None,
    ))

    assert resp.success is False
    assert resp.error_code == "rejected"
    assert run(h.tokens.list_all()) == []


def test_the_stored_row_belongs_to_the_caller():
    h = Harness()
    resp = run(h.servicer.CreateBotToken(
        type("R", (), {"owner_user_id": ALICE, "label": "mine", "token": "7878773795:AAHfakeSecretForTests_0123456789abc"})(), None,
    ))

    stored = run(h.tokens.get_owned(resp.token.id, ALICE))
    assert stored is not None
    assert stored.owner_user_id == ALICE
    # And not to anyone else.
    assert run(h.tokens.get_owned(resp.token.id, BOB)) is None


def test_the_secret_is_stored_but_never_returned():
    h = Harness()
    resp = run(h.servicer.CreateBotToken(
        type("R", (), {"owner_user_id": ALICE, "label": "", "token": "7878773795:AAHfakeSecretForTests_0123456789abc"})(), None,
    ))

    assert "AAHfakeSecretForTests" not in resp.token.SerializeToString().decode("utf-8", "ignore")
    # The worker path still resolves the real credential for delivery.
    assert run(h.tokens.get(resp.token.id)).token == "7878773795:AAHfakeSecretForTests_0123456789abc"


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #


def test_delete_refuses_a_foreign_token_without_revealing_it_exists():
    h = Harness()
    h.seed_token("tok-bob", BOB)

    resp = run(h.servicer.DeleteBotToken(
        type("R", (), {"id": "tok-bob", "owner_user_id": ALICE})(), None,
    ))

    assert resp.success is False
    assert resp.message == "not_found"
    # The row survives, and the pooled client is untouched.
    assert run(h.tokens.get_owned("tok-bob", BOB)) is not None
    assert h.transport.forgotten == []


def test_delete_refuses_while_a_task_still_routes_through_the_bot():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    rules_using(h, "rule-1", "tok-a")

    resp = run(h.servicer.DeleteBotToken(
        type("R", (), {"id": "tok-a", "owner_user_id": ALICE})(), None,
    ))

    assert resp.success is False
    assert "still used by 1 task(s)" in resp.message
    assert run(h.tokens.get_owned("tok-a", ALICE)) is not None


def test_delete_removes_the_row_and_the_pooled_client():
    h = Harness()
    h.seed_token("tok-a", ALICE)

    resp = run(h.servicer.DeleteBotToken(
        type("R", (), {"id": "tok-a", "owner_user_id": ALICE})(), None,
    ))

    assert resp.success is True
    assert run(h.tokens.get_owned("tok-a", ALICE)) is None
    # A deleted credential must not keep posting from a cached transport.
    assert h.transport.forgotten == ["tok-a"]


def test_delete_of_a_missing_id_is_not_found_not_a_crash():
    h = Harness()
    resp = run(h.servicer.DeleteBotToken(
        type("R", (), {"id": "nope", "owner_user_id": ALICE})(), None,
    ))
    assert resp.success is False
    assert resp.message == "not_found"
