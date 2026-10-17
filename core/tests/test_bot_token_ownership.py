"""A bot token belongs to the tenant that pasted it.

The first cut of the bot route stored tokens in one global table. Delivery needs
that (the worker resolves a token by id, with no request context), but every
*human* path must be tenant-scoped: otherwise one owner's route screen lists
another owner's bots, and any tenant who learns a token id can attach — or
delete — someone else's credential.

These tests pin the boundary at all three layers:
  * the schema upgrade (an old table gains the column, existing rows default to 0);
  * the repository (a scoped read never even decrypts another tenant's row);
  * the panel (a foreign token is not listed, cannot be attached, cannot be
    deleted, and a pasted token is stored against its owner).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.application.repositories import BotToken
from core.application.use_cases import BotTokenUseCases
from core.domain.entities import ForwardRule
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqliteBotTokenRepository
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

ALICE = 5094837833
BOB = 777000111
ALICE_TOKEN = "7878773795:AAAliceTokenValue-xxxxxxxxxxxx"
BOB_TOKEN = "7878773795:AABobTokenValue-xxxxxxxxxxxxxx"


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def repo(tmp_db: str = ":memory:") -> SqliteBotTokenRepository:
    return SqliteBotTokenRepository(SqliteDatabase(tmp_db), CryptoService("test-master-key"))


def seed(r: SqliteBotTokenRepository, token_id: str, token: str, owner: int, label: str = "@bot"):
    return run(r.add(BotToken(id=token_id, label=label, token=token, owner_user_id=owner,
                              created_at=1, updated_at=1)))


# --------------------------------------------------------------------------- #
# Schema: an existing database gains the column without losing rows
# --------------------------------------------------------------------------- #


def test_an_old_bot_tokens_table_gains_an_owner_column():
    db = SqliteDatabase(":memory:")
    # Recreate the pre-v19 shape, as a database carried over from an older release
    # would have it, with a row already in it.
    db.execute("DROP TABLE bot_tokens")
    db.execute(
        "CREATE TABLE bot_tokens (id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',"
        " token_encrypted TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1,"
        " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    db.execute(
        "INSERT INTO bot_tokens (id, label, token_encrypted, is_active, created_at, updated_at)"
        " VALUES ('legacy', '@legacy', '', 1, 1, 1)"
    )

    db._apply_additive_columns()

    cols = {r["name"] for r in db.query_all("PRAGMA table_info(bot_tokens)")}
    assert "owner_user_id" in cols
    row = db.query_one("SELECT owner_user_id FROM bot_tokens WHERE id='legacy'")
    assert row["owner_user_id"] == 0, "an unowned legacy row must not be handed to a tenant"
    idx = {r["name"] for r in db.query_all("PRAGMA index_list(bot_tokens)")}
    assert "idx_bot_tokens_owner" in idx


# --------------------------------------------------------------------------- #
# Repository: scoped reads, scoped deletes
# --------------------------------------------------------------------------- #


def test_list_by_owner_returns_only_that_owners_tokens():
    r = repo()
    seed(r, "alice-1", ALICE_TOKEN, ALICE)
    seed(r, "bob-1", BOB_TOKEN, BOB)

    alice = run(r.list_by_owner(ALICE))
    bob = run(r.list_by_owner(BOB))

    assert [t.id for t in alice] == ["alice-1"]
    assert [t.id for t in bob] == ["bob-1"]
    assert alice[0].token == ALICE_TOKEN, "the owner's own token still decrypts"


def test_get_owned_refuses_another_tenants_row_without_decrypting_it():
    r = repo()
    seed(r, "alice-1", ALICE_TOKEN, ALICE)

    assert run(r.get_owned("alice-1", BOB)) is None
    assert run(r.get_owned("alice-1", ALICE)).token == ALICE_TOKEN


def test_another_tenant_cannot_delete_a_token():
    r = repo()
    seed(r, "alice-1", ALICE_TOKEN, ALICE)

    assert run(r.delete_owned("alice-1", BOB)) is False
    assert run(r.get_owned("alice-1", ALICE)) is not None, "the token must survive"
    assert run(r.delete_owned("alice-1", ALICE)) is True


def test_the_worker_can_still_resolve_a_token_by_id_alone():
    # Delivery has no tenant context; this path must keep working, or the bot
    # route itself breaks.
    r = repo()
    seed(r, "alice-1", ALICE_TOKEN, ALICE)
    assert run(r.get_by_id("alice-1")).token == ALICE_TOKEN


def test_the_use_case_stamps_the_owner_it_is_given():
    r = repo()
    uc = BotTokenUseCases(r)

    rec = run(uc.create("@newbot", BOB_TOKEN, owner_user_id=BOB))

    assert rec.owner_user_id == BOB
    assert [t.id for t in run(uc.list_by_owner(BOB))] == [rec.id]
    assert run(uc.list_by_owner(ALICE)) == []
    assert run(uc.get_owned(rec.id, ALICE)) is None
    assert run(uc.delete_owned(rec.id, ALICE)) is False


# --------------------------------------------------------------------------- #
# Panel: what an owner can see and touch
# --------------------------------------------------------------------------- #


class FakeBot:
    def __init__(self):
        self.callbacks = []

    def __getattr__(self, name):
        if not name.startswith("on_"):
            raise AttributeError(name)

        def register(*args, **kwargs):
            def decorator(fn):
                if name == "on_callback_query":
                    self.callbacks.append(fn)
                return fn

            return decorator

        return register


class ScopedTokens:
    """BotTokenUseCases behaviour, owner-aware, no vault."""

    def __init__(self, records=()):
        self.records = list(records)
        self.created = []
        self.deleted = []

    async def list_by_owner(self, owner):
        return [r for r in self.records if r.owner_user_id == owner]

    async def get_owned(self, token_id, owner):
        for r in self.records:
            if r.id == token_id and r.owner_user_id == owner:
                return r
        return None

    async def get(self, token_id):
        for r in self.records:
            if r.id == token_id:
                return r
        return None

    async def create(self, label, token, owner_user_id=0):
        rec = BotToken(id="new", label=label, token=token, owner_user_id=owner_user_id,
                       created_at=2, updated_at=2)
        self.records.append(rec)
        self.created.append((label, token, owner_user_id))
        return rec

    async def delete_owned(self, token_id, owner):
        self.deleted.append((token_id, owner))
        before = len(self.records)
        self.records = [r for r in self.records if not (r.id == token_id and r.owner_user_id == owner)]
        return len(self.records) < before


class FakeRules:
    def __init__(self, rules):
        self.rules = list(rules)
        self.updated = []

    async def get_owned(self, rule_id, owner):
        for r in self.rules:
            if r.id == rule_id and (owner is None or r.owner_user_id == owner):
                return r
        return None

    async def list_by_owner(self, owner):
        return [r for r in self.rules if r.owner_user_id == owner]

    async def update_owned(self, rule, owner, expected_version=None):
        self.updated.append(rule)
        return rule


class FakeRoute:
    def __init__(self):
        self.forgotten = []
        self.validated = []

    async def validate(self, token, token_id=""):
        self.validated.append(token)
        return {"id": 7878773795, "username": "PanelBot"}

    def forget(self, token_id):
        self.forgotten.append(token_id)


class CQ:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class Msg:
    def __init__(self, text, uid):
        self.text = text
        self.from_user = SimpleNamespace(id=uid)
        self.deleted = 0
        self.replies = []

    async def delete(self):
        self.deleted += 1

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))


def rule_for(owner):
    rule = ForwardRule()
    rule.id = "rule-1"
    rule.owner_user_id = owner
    rule.source_chat_id = "-1004400615270"
    rule.target_chat_ids = ["-1003915313919"]
    return rule


def build(rule, tokens, scoped=True):
    bot = FakeBot()
    ui = ProBotUI(
        bot=bot,
        i18n=I18n(),
        sessions=SimpleNamespace(),
        rules=FakeRules([rule]),
        filter_rules=SimpleNamespace(),
        ai_configs=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        pool=SimpleNamespace(),
        login=SimpleNamespace(),
        log_client=None,
        admin_ids=[1] if scoped else [],
        bot_tokens=tokens,
        bot_route=FakeRoute(),
    )
    return ui, bot


def handler(bot, name):
    for fn in bot.callbacks:
        seen = fn
        while seen is not None:
            if getattr(seen, "__name__", "") == name:
                return fn
            seen = getattr(seen, "__wrapped__", None)
    raise AssertionError(name)


def callbacks(markup):
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            data = getattr(btn, "callback_data", None)
            if data:
                out.append(data.decode() if isinstance(data, bytes) else data)
    return out


def test_the_route_screen_lists_only_the_owners_own_bots():
    rule = rule_for(ALICE)
    tokens = ScopedTokens([
        BotToken(id="alice-1", label="@AliceBot", token=ALICE_TOKEN, owner_user_id=ALICE, created_at=1),
        BotToken(id="bob-1", label="@BobBot", token=BOB_TOKEN, owner_user_id=BOB, created_at=2),
    ])
    ui, _ = build(rule, tokens)

    text, markup = run(ui._render_bot_route_screen(rule, ui._state(ALICE)))

    assert "@AliceBot" in text
    assert "@BobBot" not in text, "another tenant's bot must not be listed"
    assert ui._state(ALICE).buffer["bot_route_tokens"] == ["alice-1"]


def test_another_tenants_token_cannot_be_attached_by_index():
    rule = rule_for(ALICE)
    tokens = ScopedTokens([
        BotToken(id="bob-1", label="@BobBot", token=BOB_TOKEN, owner_user_id=BOB, created_at=1),
    ])
    ui, bot = build(rule, tokens)
    st = ui._state(ALICE)
    # Even if a screen were built with a foreign id in its buffer (a stale or
    # tampered state), the handler must refuse to attach it.
    st.buffer["bot_route_rule"] = rule.id
    st.buffer["bot_route_tokens"] = ["bob-1"]

    cq = CQ("rbts:rule-1:0", ALICE)
    run(handler(bot, "_cb_rule_bot_token_use")(None, cq))

    assert rule.bot_token_id == ""
    assert ui._rules.updated == []
    assert cq.answers and cq.answers[-1][1] is True


def test_switching_to_bot_is_refused_when_the_token_belongs_to_someone_else():
    rule = rule_for(ALICE)
    rule.bot_token_id = "bob-1"
    tokens = ScopedTokens([
        BotToken(id="bob-1", label="@BobBot", token=BOB_TOKEN, owner_user_id=BOB, created_at=1),
    ])
    ui, bot = build(rule, tokens)

    cq = CQ("rdt:rule-1", ALICE)
    run(handler(bot, "_cb_rule_route_toggle")(None, cq))

    assert rule.delivery_transport == "USER"
    assert cq.answers and cq.answers[-1][1] is True


def test_a_foreign_token_cannot_be_deleted_from_another_owners_panel():
    rule = rule_for(ALICE)
    tokens = ScopedTokens([
        BotToken(id="bob-1", label="@BobBot", token=BOB_TOKEN, owner_user_id=BOB, created_at=1),
    ])
    ui, bot = build(rule, tokens)
    st = ui._state(ALICE)
    st.buffer["bot_route_rule"] = rule.id
    st.buffer["bot_route_tokens"] = ["bob-1"]

    cq = CQ("rbtd:rule-1:0", ALICE)
    run(handler(bot, "_cb_rule_bot_token_remove")(None, cq))

    assert tokens.deleted == [], "nothing may be deleted on a foreign id"
    assert cq.answers and cq.answers[-1][1] is True


def test_a_pasted_token_is_stored_against_its_owner():
    rule = rule_for(ALICE)
    tokens = ScopedTokens()
    ui, _ = build(rule, tokens)
    st = ui._state(ALICE)
    msg = Msg(ALICE_TOKEN, ALICE)

    run(ui._step_bot_route_token(msg, st, ALICE_TOKEN, rule.id))

    assert tokens.created == [("@PanelBot", ALICE_TOKEN, ALICE)]
    assert rule.bot_token_id == "new"
    assert rule.delivery_transport == "BOT"
