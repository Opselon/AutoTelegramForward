"""The bot route is a first-class per-task setting on the API too.

The Telegram panel is not the only place an owner works. The web dashboard and
the mobile app reach tasks through `/api/rules/{id}/settings`, which is driven by
the same registry — so if the route is not in the registry, those clients cannot
see or set it at all, no matter what their UI draws.

These tests run the REAL registry, the REAL sqlite repositories and the REAL
gRPC servicer, because the failure mode to guard against is a control plane that
reports success while writing nothing (or writing someone else's bot).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.application.use_cases import BotTokenUseCases, ForwardRuleUseCases
from core.domain import rule_settings as RS
from core.domain.entities import ForwardRule
from core.infrastructure.grpc_server.servicers import ForwardRuleControlServicer
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteBotTokenRepository,
    SqliteForwardRuleRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.proto import autoforward_pb2 as pb

OWNER = 5094837833
OTHER = 777000111


class Ctx:
    def __init__(self):
        self.code = None
        self.details = ""

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture()
def env(tmp_path):
    db = SqliteDatabase(str(tmp_path / "route.db"))
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES ('s1', '+1000', '', '', '', '', 1, 1, 0, 0)", ())
    rules = ForwardRuleUseCases(SqliteForwardRuleRepository(db))
    tokens = BotTokenUseCases(SqliteBotTokenRepository(db, CryptoService("route-test-key")))
    servicer = ForwardRuleControlServicer(rules, bot_tokens=tokens)
    rule = run(rules.create(ForwardRule(
        session_id="s1", source_chat_id="-100", target_chat_id="-900", owner_user_id=OWNER)))

    def stored() -> ForwardRule:
        return run(rules.get(rule.id))

    return servicer, rules, tokens, rule, stored


def update(servicer, rule_id, payload, owner=OWNER, replace_all=False):
    req = pb.UpdateRuleSettingsRequest(
        rule_id=rule_id, owner_user_id=owner,
        settings_json=json.dumps(payload), replace_all=replace_all,
    )
    return run(servicer.UpdateRuleSettings(req, Ctx()))


# --------------------------------------------------------------------------- #
# Registry: the setting exists, and the pair cannot be half-applied
# --------------------------------------------------------------------------- #


def test_the_schema_exposes_the_route_as_its_own_group():
    schema = RS.schema()
    by_key = {s["key"]: s for s in schema["settings"]}

    assert "route" in schema["groups"]
    assert by_key["delivery_transport"]["group"] == "route"
    assert by_key["delivery_transport"]["choices"] == ["USER", "BOT"]
    assert by_key["delivery_transport"]["default"] == "USER"
    assert by_key["bot_token_id"]["group"] == "route"
    assert by_key["bot_token_id"]["default"] == ""


def test_switching_to_bot_without_a_token_is_refused_not_ignored():
    rule = ForwardRule()
    with pytest.raises(RS.SettingError) as exc:
        RS.apply_settings(rule, {"delivery_transport": "BOT"})
    assert exc.value.code == "invalid_state"
    assert rule.delivery_transport == "USER"


def test_token_and_transport_in_one_request_apply_in_a_safe_order():
    rule = ForwardRule()
    # Transport first in the payload: the transport setter would drop BOT if it
    # ran before the token existed, so apply_settings must reorder.
    changed = RS.apply_settings(rule, {"delivery_transport": "BOT", "bot_token_id": "tok-1"})

    assert rule.delivery_transport == "BOT"
    assert rule.bot_token_id == "tok-1"
    assert set(changed) == {"delivery_transport", "bot_token_id"}


def test_the_route_round_trips_through_a_read():
    rule = ForwardRule()
    RS.apply_settings(rule, {"bot_token_id": "tok-1", "delivery_transport": "BOT"})
    read = RS.read_settings(rule)
    assert read["delivery_transport"] == "BOT"
    assert read["bot_token_id"] == "tok-1"


# --------------------------------------------------------------------------- #
# Servicer: the wire may only name a token the caller owns
# --------------------------------------------------------------------------- #


def test_a_made_up_token_is_rejected_and_nothing_is_written(env):
    servicer, _, _, rule, stored = env

    resp = update(servicer, rule.id, {"bot_token_id": "does-not-exist", "delivery_transport": "BOT"})

    assert resp.success is False
    assert resp.error_code == "unknown_token"
    assert stored().delivery_transport == "USER"
    assert stored().bot_token_id == ""


def test_another_owners_token_is_rejected(env):
    servicer, _, tokens, rule, stored = env
    foreign = run(tokens.create("@ForeignBot", "7878773795:AAforeign-token-value", owner_user_id=OTHER))

    resp = update(servicer, rule.id, {"bot_token_id": foreign.id, "delivery_transport": "BOT"})

    assert resp.success is False
    assert resp.error_code == "unknown_token"
    assert stored().bot_token_id == ""
    assert stored().delivery_transport == "USER"


def test_attaching_an_owned_token_persists_the_route_and_its_label(env):
    servicer, _, tokens, rule, stored = env
    mine = run(tokens.create("@MyBot", "7878773795:AAmy-own-token-value", owner_user_id=OWNER))

    resp = update(servicer, rule.id, {"bot_token_id": mine.id, "delivery_transport": "BOT"})

    assert resp.success is True
    after = stored()
    assert after.delivery_transport == "BOT"
    assert after.bot_token_id == mine.id
    assert after.bot_token_label == "@MyBot", "the panel and clients show the bot, never the token"
    settings = json.loads(resp.settings_json)
    assert settings["delivery_transport"] == "BOT"
    assert mine.token not in resp.settings_json


def test_switching_back_to_the_account_keeps_the_attached_bot(env):
    servicer, _, tokens, rule, stored = env
    mine = run(tokens.create("@MyBot", "7878773795:AAmy-own-token-value", owner_user_id=OWNER))
    update(servicer, rule.id, {"bot_token_id": mine.id, "delivery_transport": "BOT"})

    resp = update(servicer, rule.id, {"delivery_transport": "USER"})

    assert resp.success is True
    after = stored()
    assert after.delivery_transport == "USER"
    assert after.bot_token_id == mine.id, "one tap returns to the bot; it was not a delete"


def test_clearing_the_token_also_returns_the_task_to_the_account(env):
    servicer, _, tokens, rule, stored = env
    mine = run(tokens.create("@MyBot", "7878773795:AAmy-own-token-value", owner_user_id=OWNER))
    update(servicer, rule.id, {"bot_token_id": mine.id, "delivery_transport": "BOT"})

    resp = update(servicer, rule.id, {"bot_token_id": "", "delivery_transport": "USER"})

    assert resp.success is True
    after = stored()
    assert after.bot_token_id == ""
    assert after.bot_token_label == ""
    assert after.delivery_transport == "USER"


def test_clearing_a_token_while_still_on_the_bot_route_is_refused(env):
    servicer, _, tokens, rule, stored = env
    mine = run(tokens.create("@MyBot", "7878773795:AAmy-own-token-value", owner_user_id=OWNER))
    update(servicer, rule.id, {"bot_token_id": mine.id, "delivery_transport": "BOT"})

    resp = update(servicer, rule.id, {"bot_token_id": ""})

    assert resp.success is False
    assert resp.error_code == "invalid_state"
    assert stored().bot_token_id == mine.id, "the task must keep working"
