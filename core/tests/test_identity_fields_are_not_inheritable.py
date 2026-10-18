"""A behavioural default must never become an identity.

Inheritance answers "what does this task do". Identity answers "whose task is
this, whose bot delivers it, whose session runs it". Those are two different
layers, and the danger is that they look the same in a settings blob:

    {"forward_delay_seconds": 30, "owner_user_id": 777000111}

If a bot default could carry the second key, attaching a bot would re-label every
task that used it — the receiver's tasks would appear in the sender's list, or the
sender's traffic would run on the receiver's session and token. Two of these keys
are legitimate *task* settings that are deliberately not inheritable
(`bot_token_id`, `delivery_transport`: they name the credential and the lane), and
the rest must not be settings at all.

Four guarantees are pinned here:

1. Identity keys are either unregistered, or registered and non-inheritable.
2. `validate_defaults` refuses all of them, before anything is stored.
3. The resolver never takes one from a bot, even if handed one directly.
4. A rebuild from storage keeps the columns' identity: identity smuggled into the
   metadata blob is not consulted, so a crafted row cannot re-own a task.
"""
from __future__ import annotations

import asyncio

from core.application.repositories import BotToken
from core.domain.bot_defaults import (
    SOURCE_BOT,
    SOURCE_DEFAULT,
    SOURCE_TASK,
    describe,
    effective,
    provenance,
    validate_defaults,
)
from core.domain.entities import ForwardRule
from core.domain.rule_settings import SETTING_SPECS, SettingError
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteBotTokenRepository,
    SqliteForwardRuleRepository,
)
from core.infrastructure.security.crypto import CryptoService

ALICE = 5094837833
BOB = 777000111

#: Registered settings that *name* the credential or the lane. They are part of a
#: task's configuration, so they are inherited by nothing: a bot cannot state
#: which bot delivers another task.
NON_INHERITABLE_IDENTITY = ("bot_token_id", "delivery_transport")

#: Everything that decides *who* — never *how*, and never a setting at all.
ABSENT_IDENTITY_KEYS = (
    "id",
    "owner_user_id",
    "user_id",
    "telegram_user_id",
    "session_id",
    "source_chat_id",
    "source_chat_ids",
    "target_chat_id",
    "target_chat_ids",
    "intermediate_channel_id",
    "api_key",
    "api_key_encrypted",
    "token",
    "is_admin",
    "admin",
    "plan",
    "created_at",
    "updated_at",
)

IDENTITY_KEYS = ABSENT_IDENTITY_KEYS + NON_INHERITABLE_IDENTITY


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _rule(owner: int = ALICE, bot_token_id: str = "tok-mine") -> ForwardRule:
    rule = ForwardRule(
        id="r-1", session_id="s-mine", source_chat_id="-1001", target_chat_id="-1002",
        owner_user_id=owner,
    )
    # The route pair is not a declared column on the entity; a rule carries it the
    # way the forwarder reads it.
    setattr(rule, "bot_token_id", bot_token_id)
    return rule

def _seed_session(db, session_id: str = "s-mine", owner: int = ALICE) -> None:
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at,"
        " owner_user_id) VALUES (?, '+1000', '', '', '', '', 1, 1, 0, 0, ?)",
        (session_id, owner),
    )

def test_identity_fields_are_never_inheritable_settings():
    """Registered or not, none of them may be inherited from a bot."""
    for key in NON_INHERITABLE_IDENTITY:
        assert key in SETTING_SPECS, f"{key} should still be a task setting"
        assert SETTING_SPECS[key].inheritable is False, key
    for key in ABSENT_IDENTITY_KEYS:
        assert key not in SETTING_SPECS, f"{key} must not be a setting at all"


def test_validate_defaults_refuses_every_identity_field():
    for key in IDENTITY_KEYS:
        try:
            validate_defaults({key: BOB})
        except SettingError as exc:
            assert exc.code in ("unknown_setting", "not_inheritable"), (key, exc.code)
        else:  # pragma: no cover - the failure message matters more than the branch
            raise AssertionError(f"{key} was accepted as a bot default")


def test_an_identity_field_is_refused_alongside_a_legitimate_one():
    """One bad key rejects the whole write; it is not quietly dropped."""
    try:
        validate_defaults({"forward_delay_seconds": 30, "owner_user_id": BOB})
    except SettingError as exc:
        assert exc.code == "unknown_setting"
        assert "owner_user_id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a mixed payload was accepted")


def test_the_resolver_never_takes_an_identity_value_from_a_bot():
    """Even handed a default it would never accept, the task keeps its own."""
    rule = _rule()
    smuggled = {"bot_token_id": "tok-theirs", "delivery_transport": "BOT",
                "forward_delay_seconds": 45}

    values = effective(rule, smuggled)

    assert values["bot_token_id"] == "tok-mine"
    assert getattr(rule, "bot_token_id") == "tok-mine"
    assert provenance(rule, smuggled, "bot_token_id") != SOURCE_BOT
    # the behavioural key *is* inherited, which is what inheritance is for
    assert values["forward_delay_seconds"] == 45
    assert provenance(rule, smuggled, "forward_delay_seconds") == SOURCE_BOT
    assert describe(rule, smuggled)["bot_token_id"]["source"] != SOURCE_BOT


def test_a_bot_default_cannot_outrank_the_tasks_own_choice():
    """Precedence is task > bot > registry, and identity is not on the ladder."""
    rule = _rule()
    rule.forward_delay_seconds = 7  # the task pinned this itself
    defaults = validate_defaults({"forward_delay_seconds": 99, "sync_replies": True})

    assert provenance(rule, defaults, "forward_delay_seconds") == SOURCE_TASK
    assert effective(rule, defaults)["forward_delay_seconds"] == 7
    assert provenance(rule, defaults, "sync_replies") == SOURCE_BOT
    assert provenance(rule, defaults, "remove_links") == SOURCE_DEFAULT


def test_identity_smuggled_into_the_metadata_blob_cannot_re_own_a_task(tmp_path):
    """A crafted row is the realistic attack: metadata is client-supplied JSON.

    Ownership and the session are columns, so a key invented in metadata is
    stored and then ignored — it cannot move the task to another owner or onto
    another account's session. The route pair (`bot_token_id`,
    `delivery_transport`) does live in metadata by design, which is why the write
    path checks that the caller owns the bot it names; that check is pinned in
    ``test_rule_bot_attachment_guard.py``.
    """
    db = SqliteDatabase(str(tmp_path / "identity.db"))
    rules = SqliteForwardRuleRepository(db)
    tokens = SqliteBotTokenRepository(db, CryptoService("test-key-material-000"))
    _seed_session(db)
    run(tokens.add(BotToken(
        id="tok-mine", label="@mine", token="7878773795:AAmine",
        owner_user_id=ALICE, created_at=1, updated_at=1, defaults={},
    )))

    rule = _rule(owner=ALICE)
    rule.metadata = {
        "owner_user_id": BOB,
        "session_id": "s-theirs",
        "bot_token_id": "tok-theirs",
        "delivery_transport": "BOT",
        "forward_delay_seconds": 12,
        "paid_addons": [],
    }
    run(rules.add(rule))

    loaded = run(rules.get_by_id("r-1"))

    assert loaded.owner_user_id == ALICE
    assert loaded.session_id == "s-mine"
    assert "owner_user_id" not in effective(loaded, {})
    assert "owner_user_id" not in describe(loaded, {})
    # the route pair is metadata-backed, and still never inherited from a bot
    assert describe(loaded, {"bot_token_id": "tok-theirs"})["bot_token_id"]["source"] != SOURCE_BOT
    # and the blob really did carry the attempt, so this is not a vacuous pass
    assert loaded.metadata.get("owner_user_id") == BOB


def test_a_bot_default_cannot_repoint_a_tasks_delivery_credential(tmp_path):
    """End to end: the credential a task uses is read by id from its owner."""
    db = SqliteDatabase(str(tmp_path / "credentials.db"))
    rules = SqliteForwardRuleRepository(db)
    tokens = SqliteBotTokenRepository(db, CryptoService("test-key-material-000"))
    _seed_session(db)

    run(tokens.add(BotToken(
        id="tok-alice", label="@alice", token="7878773795:AAalice",
        owner_user_id=ALICE, created_at=1, updated_at=1, defaults={},
    )))
    run(tokens.add(BotToken(
        id="tok-bob", label="@bob", token="7878773795:AAbob",
        owner_user_id=BOB, created_at=1, updated_at=1, defaults={},
    )))

    run(rules.add(_rule(owner=ALICE, bot_token_id="tok-alice")))

    stored = run(tokens.get_owned("tok-alice", ALICE))
    assert stored.owner_user_id == ALICE
    # the foreign bot is not readable as this tenant's, whatever the task asks
    assert run(tokens.get_owned("tok-bob", ALICE)) is None
    # and a default write cannot carry the attachment either
    for payload in ({"bot_token_id": "tok-bob"}, {"delivery_transport": "BOT"}):
        try:
            validate_defaults(payload)
        except SettingError:
            continue
        raise AssertionError(f"{payload} was accepted as a bot default")
