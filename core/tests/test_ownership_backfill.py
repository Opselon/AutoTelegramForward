"""Ownership backfill: assign what is provable, quarantine what is not.

A pre-ownership row at `owner_user_id = 0` belongs to nobody: it vanishes from
every tenant's list and stays reachable by id. The tempting fix — "there is only
one real user here, give it to them" — is a guess, and a wrong guess is a
cross-tenant disclosure. These tests pin the three outcomes:

  * verifiable evidence assigns (a rule's session, an AI config's rules);
  * no evidence, or contradictory evidence, quarantines and changes nothing;
  * an operator's explicit decision is the only evidence-free assignment.

They also pin the properties that make the tool safe to run on a live database:
idempotent, never touching an owned row, and leaving the table untouched when it
cannot decide.
"""
from __future__ import annotations

import asyncio

from core.application import ownership_backfill as ob
from core.application.repositories import BotToken
from core.application.use_cases import ForwardRuleUseCases
from core.domain.entities import ForwardRule
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


def fresh_db() -> SqliteDatabase:
    return SqliteDatabase(":memory:")


def seed_session(db, session_id: str, owner: int) -> None:
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at,"
        " owner_user_id) VALUES (?, '+1000', '', '', '', '', 1, 1, 0, 0, ?)",
        (session_id, owner),
    )


def seed_owned_rule(db, rule_id: str, session_id: str, owner: int, **extra) -> None:
    """A rule the way today's code writes one: it has an owner."""
    rule = ForwardRule(
        id=rule_id, name="a task", session_id=session_id,
        source_chat_id="-1001", target_chat_id="-1002", owner_user_id=owner,
    )
    for key, value in extra.items():
        setattr(rule, key, value)
    run(ForwardRuleUseCases(SqliteForwardRuleRepository(db)).create(rule))


def seed_rule(db, rule_id: str, session_id: str, owner: int, **extra) -> None:
    """A rule the way a pre-ownership database has one: owner_user_id = 0."""
    seed_owned_rule(db, rule_id, session_id, owner, **extra)
    db.execute("UPDATE forward_rules SET owner_user_id = 0 WHERE id = ?", (rule_id,))


# --------------------------------------------------------------------------- #
# Evidence that proves an owner
# --------------------------------------------------------------------------- #
def test_a_rule_on_an_owned_session_is_assigned_to_that_owner():
    db = fresh_db()
    seed_session(db, "s-a", ALICE)
    seed_rule(db, "r1", "s-a", ALICE)

    report = ob.apply(db)

    assert report.assigned == {"forward_rules": 1}
    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r1'")[0] == ALICE
    assert ob.quarantined(db) == []


def test_a_rule_follows_its_session_not_the_only_user_in_the_database():
    """The evidence is the session row, never 'the database looks single-tenant'."""
    db = fresh_db()
    seed_session(db, "s-b", BOB)
    seed_rule(db, "r1", "s-b", BOB)

    ob.apply(db)

    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r1'")[0] == BOB


def test_an_ai_config_used_by_one_owners_rules_inherits_that_owner():
    db = fresh_db()
    seed_session(db, "s-a", ALICE)
    db.execute(
        "INSERT INTO ai_configs (id, name, provider, model) VALUES ('cfg1', 'x', 'openai', 'm')",
        (),
    )
    db.execute("UPDATE ai_configs SET owner_user_id = 0 WHERE id = 'cfg1'", ())
    # The rule is owned; the config it points at is the legacy row.
    seed_owned_rule(db, "r1", "s-a", ALICE, ai_config_id="cfg1")

    report = ob.apply(db)

    assert report.assigned.get("ai_configs") == 1
    assert db.query_one("SELECT owner_user_id FROM ai_configs WHERE id='cfg1'")[0] == ALICE


# --------------------------------------------------------------------------- #
# Evidence that does not exist, or contradicts itself
# --------------------------------------------------------------------------- #
def test_a_rule_with_an_unresolvable_owner_is_quarantined_and_listed():
    """FK guarantees the session row exists; what it cannot guarantee is an owner.

    A legacy database can hold a session whose own owner_user_id is zero (deleted
    account, pre-ownership row). The rule then has no evidence at all, and the
    honest answer is "a human decides" — not a guess.
    """
    db = fresh_db()
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at,"
        " owner_user_id) VALUES ('s-nobody', '+1000', '', '', '', '', 1, 1, 0, 0, 0)"
        , (),
    )
    seed_rule(db, "r-orphan", "s-nobody", ALICE)

    report = ob.apply(db)

    assert report.assigned == {}
    # the rule and the ownerless session it hangs off are both waiting on a human
    assert report.quarantined == {"forward_rules": 1, "sessions": 1}
    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r-orphan'")[0] in (0, None)
    waiting = {(row["table_name"], row["row_id"]): row for row in ob.quarantined(db)}
    assert ("forward_rules", "r-orphan") in waiting
    assert "no_owned_session" in waiting[("forward_rules", "r-orphan")]["reason"]


def test_a_rule_whose_session_is_itself_unowned_is_quarantined():
    db = fresh_db()
    seed_session(db, "s-nobody", 0)
    seed_rule(db, "r1", "s-nobody", ALICE)

    report = ob.apply(db)

    assert report.assigned == {}
    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r1'")[0] in (0, None)


def test_a_bot_token_is_never_guessed():
    """A bot token names no upstream row, so there is nothing to prove an owner."""
    db = fresh_db()
    seed_session(db, "s-a", ALICE)
    run(SqliteBotTokenRepository(db, CryptoService("k")).add(BotToken(
        id="tok", label="@t", token="7878773795:AAx", owner_user_id=ALICE,
        created_at=1, updated_at=1, defaults={},
    )))
    db.execute("UPDATE bot_tokens SET owner_user_id = 0 WHERE id='tok'", ())

    report = ob.apply(db)

    assert report.assigned == {}
    assert report.quarantined == {"bot_tokens": 1}
    assert db.query_one("SELECT owner_user_id FROM bot_tokens WHERE id='tok'")[0] in (0, None)


def test_a_config_used_by_two_owners_is_quarantined():
    db = fresh_db()
    seed_session(db, "s-a", ALICE)
    seed_session(db, "s-b", BOB)
    db.execute(
        "INSERT INTO ai_configs (id, name, provider, model) VALUES ('cfg1', 'x', 'openai', 'm')",
        (),
    )
    seed_owned_rule(db, "r1", "s-a", ALICE, ai_config_id="cfg1")
    seed_owned_rule(db, "r2", "s-b", BOB, ai_config_id="cfg1")
    db.execute("UPDATE ai_configs SET owner_user_id = 0 WHERE id='cfg1'", ())

    report = ob.apply(db)

    assert report.assigned.get("ai_configs") is None
    assert any(d.table == "ai_configs" and d.quarantined for d in report.decisions)
    assert db.query_one("SELECT owner_user_id FROM ai_configs WHERE id='cfg1'")[0] in (0, None)


# --------------------------------------------------------------------------- #
# Safety of the tool itself
# --------------------------------------------------------------------------- #
def test_an_owned_row_is_never_touched():
    db = fresh_db()
    seed_session(db, "s-a", ALICE)
    seed_owned_rule(db, "r1", "s-a", ALICE)
    db.execute("UPDATE forward_rules SET owner_user_id = ? WHERE id='r1'", (BOB,))

    report = ob.apply(db)

    assert report.assigned == {}
    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r1'")[0] == BOB


def test_the_backfill_is_idempotent():
    db = fresh_db()
    seed_session(db, "s-a", ALICE)
    seed_rule(db, "r1", "s-a", ALICE)

    first = ob.apply(db)
    second = ob.apply(db)

    assert first.assigned == {"forward_rules": 1}
    assert second.assigned == {}
    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r1'")[0] == ALICE


def test_plan_does_not_write():
    db = fresh_db()
    seed_session(db, "s-a", ALICE)
    seed_rule(db, "r1", "s-a", ALICE)

    report = ob.plan(db)

    assert [d.table for d in report.decisions] == ["forward_rules"]
    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r1'")[0] == 0


def test_an_operator_can_resolve_a_quarantined_row_and_the_entry_closes():
    db = fresh_db()
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at,"
        " owner_user_id) VALUES ('s-nobody', '+1000', '', '', '', '', 1, 1, 0, 0, 0)"
        , (),
    )
    seed_rule(db, "r-orphan", "s-nobody", ALICE)
    ob.apply(db)
    assert any(row["row_id"] == "r-orphan" for row in ob.quarantined(db))

    assert ob.resolve(db, "forward_rules", "r-orphan", ALICE) is True

    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r-orphan'")[0] == ALICE
    assert not any(row["table_name"] == "forward_rules" for row in ob.quarantined(db))


def test_resolving_refuses_a_row_that_already_has_an_owner():
    db = fresh_db()
    seed_session(db, "s-a", ALICE)
    seed_owned_rule(db, "r1", "s-a", ALICE)

    assert ob.resolve(db, "forward_rules", "r1", BOB) is False
    assert db.query_one("SELECT owner_user_id FROM forward_rules WHERE id='r1'")[0] == ALICE


def test_resolve_requires_a_positive_owner():
    db = fresh_db()
    try:
        ob.resolve(db, "forward_rules", "anything", 0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a zero owner was accepted")
