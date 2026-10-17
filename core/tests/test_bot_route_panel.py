"""The task panel's bot-route surface: attach a token, switch route, stay safe.

The delivery route already worked (``test_bot_route.py``), but a route a user
cannot set is a route nobody uses. These tests hold the panel to the promises a
credential screen has to keep:

  * the task panel always shows where a task's copies come from, and how to
    change it — including when no bot is attached yet;
  * switching to the bot route without a token is *refused* instead of
    silently pointing the task at nothing;
  * a pasted token is deleted from the chat, validated with Telegram *before*
    it is stored, and never appears in a log record or a reply;
  * a token still used by a task cannot be deleted out from under it;
  * callback payloads stay inside Telegram's 64-byte limit and resolve through
    an index that refuses a stale screen.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from core.domain.entities import ForwardRule
from core.infrastructure.telegram.bot_transport import BotTokenInvalid
from core.infrastructure.telegram.pro_bot_ui import ProBotUI
from core.infrastructure.telegram.i18n import I18n

OWNER = 5094837833
TOKEN = "7878773795:AAExampleOnlyNotARealToken-xxxxxxxxxxx"
TOKEN_ID = "b0t0k3n-0000-4000-8000-000000000001"


# --------------------------------------------------------------------------- #
# Fakes: only the seams the panel actually touches
# --------------------------------------------------------------------------- #


class FakeBot:
    """Records every handler the panel registers (no network, no client)."""

    def __init__(self) -> None:
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


class FakeRecord:
    def __init__(self, id=TOKEN_ID, label="@PanelBot", token=TOKEN, is_active=True, created_at=1):
        self.id = id
        self.label = label
        self.token = token
        self.is_active = is_active
        self.created_at = created_at


class FakeRules:
    def __init__(self, rules=()):
        self.rules = list(rules)
        self.updated = []

    async def get_owned(self, rule_id, owner_user_id):
        for r in self.rules:
            if r.id != rule_id:
                continue
            if owner_user_id is not None and r.owner_user_id != owner_user_id:
                return None
            return r
        return None

    async def list_by_owner(self, owner_user_id):
        return list(self.rules)

    async def update_owned(self, rule, owner_user_id, expected_version=None):
        self.updated.append(rule)
        return rule


class FakeTokens:
    def __init__(self, records=()):
        self.records = list(records)
        self.created = []
        self.deleted = []

    async def get(self, token_id):
        for r in self.records:
            if r.id == token_id:
                return r
        return None

    async def list_all(self):
        return list(self.records)

    async def create(self, label, token):
        rec = FakeRecord(id="new-token-id", label=label, token=token, created_at=2)
        self.records.append(rec)
        self.created.append((label, token))
        return rec

    async def delete(self, token_id):
        self.deleted.append(token_id)
        self.records = [r for r in self.records if r.id != token_id]
        return True


class FakeRoute:
    """The delivery cache: validation verdict + forget() calls."""

    def __init__(self, me=None, error=None):
        self.me = me if me is not None else {"id": 7878773795, "username": "PanelBot"}
        self.error = error
        self.forgotten = []
        self.validated = []

    async def validate(self, token, token_id=""):
        self.validated.append(token)
        if self.error is not None:
            raise self.error
        return dict(self.me)

    def forget(self, token_id):
        self.forgotten.append(token_id)


class FakeCQ:
    def __init__(self, data, uid=OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


class FakeMessage:
    def __init__(self, text, uid=OWNER):
        self.text = text
        self.from_user = SimpleNamespace(id=uid)
        self.deleted = 0
        self.replies = []

    async def delete(self):
        self.deleted += 1

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append((text, reply_markup))


def build_ui(rules=(), tokens=(), route=None, scoped=False):
    bot = FakeBot()
    ui = ProBotUI(
        bot=bot,
        i18n=I18n(),
        sessions=SimpleNamespace(),
        rules=FakeRules(rules),
        filter_rules=SimpleNamespace(),
        ai_configs=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        pool=SimpleNamespace(),
        login=SimpleNamespace(),
        log_client=None,
        admin_ids=[1] if scoped else [],
        bot_tokens=FakeTokens(tokens),
        bot_route=route if route is not None else FakeRoute(),
    )
    return ui, bot


def handler(bot, name):
    for fn in bot.callbacks:
        seen = fn
        while seen is not None:
            if getattr(seen, "__name__", "") == name:
                return fn
            seen = getattr(seen, "__wrapped__", None)
    raise AssertionError(f"handler {name} was never registered")


def callbacks_of(markup):
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            if getattr(btn, "callback_data", None):
                out.append(btn.callback_data.decode("utf-8") if isinstance(btn.callback_data, bytes) else btn.callback_data)
    return out


def texts_of(markup):
    return [btn.text for row in markup.inline_keyboard for btn in row]


def a_rule(**kwargs):
    rule = ForwardRule()
    rule.id = kwargs.pop("id", "rule-1")
    rule.owner_user_id = OWNER
    rule.source_chat_id = "-1004400615270"
    rule.target_chat_ids = ["-1003915313919"]
    for key, value in kwargs.items():
        setattr(rule, key, value)
    return rule


# --------------------------------------------------------------------------- #
# The panel shows the route, and can be reached from the task
# --------------------------------------------------------------------------- #


def test_task_panel_offers_the_route_and_the_bot_token_rows():
    ui, _ = build_ui(rules=[a_rule()])

    text, markup = ui._render_rule_detail(ui._rules.rules[0])
    cbs = callbacks_of(markup)

    assert "rdt:rule-1" in cbs, "the task panel must offer a route switch"
    assert "rbt:rule-1" in cbs, "the task panel must offer the bot screen"
    assert "Account" in "\n".join(texts_of(markup)) or "Account" in text


def test_the_bot_screen_lists_tokens_and_says_when_none_is_attached():
    rule = a_rule()
    ui, _ = build_ui(rules=[rule], tokens=[FakeRecord()])
    st = ui._state(OWNER)

    text, markup = asyncio_run(ui._render_bot_route_screen(rule, st))

    assert "@PanelBot" in text
    assert "not set" in text.lower() or "none" in text.lower()
    assert "rbts:rule-1:0" in callbacks_of(markup)
    assert "rbta:rule-1" in callbacks_of(markup)
    assert "rbtd:rule-1:0" in callbacks_of(markup)
    assert "rbtb:rule-1" in callbacks_of(markup)


def test_every_bot_screen_callback_fits_telegrams_64_byte_limit():
    rule = a_rule(id="7815fbe6-3581-4787-b3bf-557da00197f8")
    ui, _ = build_ui(rules=[rule], tokens=[FakeRecord(id="t" * 36)])
    st = ui._state(OWNER)

    _, markup = asyncio_run(ui._render_bot_route_screen(rule, st))

    for data in callbacks_of(markup):
        assert len(data.encode("utf-8")) <= 64, data


def test_the_token_row_names_the_attached_bot_without_its_token():
    rule = a_rule(bot_token_id=TOKEN_ID)
    rule.bot_token_label = "@PanelBot"
    ui, _ = build_ui(rules=[rule], tokens=[FakeRecord()])

    text, markup = ui._render_rule_detail(rule)

    assert "@PanelBot" in text
    assert TOKEN not in text
    assert all(TOKEN not in data for data in callbacks_of(markup))


# --------------------------------------------------------------------------- #
# Switching route
# --------------------------------------------------------------------------- #


def asyncio_run(coro):
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_toggle_to_bot_without_a_token_is_refused_and_persists_nothing():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule])
    cq = FakeCQ("rdt:rule-1")

    asyncio_run(handler(bot, "_cb_rule_route_toggle")(None, cq))

    assert cq.answers and cq.answers[-1][1] is True, "must alert, not edit"
    assert "token" in (cq.answers[-1][0] or "").lower()
    assert rule.delivery_transport == "USER"
    assert ui._rules.updated == [], "a refused switch must not be written"
    assert cq.edits == []


def test_toggle_to_bot_with_an_attached_token_persists_the_bot_route():
    rule = a_rule(bot_token_id=TOKEN_ID)
    ui, bot = build_ui(rules=[rule], tokens=[FakeRecord()])
    cq = FakeCQ("rdt:rule-1")

    asyncio_run(handler(bot, "_cb_rule_route_toggle")(None, cq))

    assert rule.delivery_transport == "BOT"
    assert ui._rules.updated == [rule]
    assert cq.edits, "the panel must re-render with the new route"


def test_toggle_back_to_the_account_keeps_the_token_for_one_tap_return():
    rule = a_rule(bot_token_id=TOKEN_ID)
    rule.delivery_transport = "BOT"
    ui, bot = build_ui(rules=[rule], tokens=[FakeRecord()])
    cq = FakeCQ("rdt:rule-1")

    asyncio_run(handler(bot, "_cb_rule_route_toggle")(None, cq))

    assert rule.delivery_transport == "USER"
    assert rule.bot_token_id == TOKEN_ID, "switching back must not discard the bot"


# --------------------------------------------------------------------------- #
# Pasting a token: the credential path
# --------------------------------------------------------------------------- #


def test_pasting_a_valid_token_deletes_the_message_then_stores_it_encrypted():
    rule = a_rule()
    ui, _ = build_ui(rules=[rule])
    st = ui._state(OWNER)
    st.step = f"bot_route_token_{rule.id}"
    msg = FakeMessage(TOKEN)

    asyncio_run(ui._step_bot_route_token(msg, st, TOKEN, rule.id))

    assert msg.deleted == 1, "the credential message must be removed"
    assert ui._bot_tokens.created == [("@PanelBot", TOKEN)]
    assert rule.bot_token_id == "new-token-id"
    assert rule.delivery_transport == "BOT"
    assert ui._rules.updated == [rule]
    assert ui._bot_route.validated == [TOKEN]
    assert st.step == "", "the step must not stay armed"


def test_the_pasted_token_never_reaches_a_reply_or_a_log_record(caplog):
    rule = a_rule()
    ui, _ = build_ui(rules=[rule])
    st = ui._state(OWNER)
    msg = FakeMessage(TOKEN)

    with caplog.at_level(logging.DEBUG):
        asyncio_run(ui._step_bot_route_token(msg, st, TOKEN, rule.id))

    assert caplog.records, "expected the attach to be logged"
    for record in caplog.records:
        assert TOKEN not in record.getMessage(), f"token leaked into log: {record.name}"
    for text, _ in msg.replies:
        assert TOKEN not in text
    assert TOKEN not in msg.text or msg.deleted, "message deleted before replying"


def test_a_malformed_token_is_rejected_before_telegram_is_even_asked():
    rule = a_rule()
    ui, _ = build_ui(rules=[rule])
    st = ui._state(OWNER)
    msg = FakeMessage("not-a-token")

    asyncio_run(ui._step_bot_route_token(msg, st, "not-a-token", rule.id))

    assert msg.deleted == 1
    assert ui._bot_route.validated == []
    assert ui._bot_tokens.created == []
    assert ui._rules.updated == []
    assert rule.delivery_transport == "USER"
    assert msg.replies and "token" in msg.replies[-1][0].lower()


def test_a_token_telegram_refuses_is_never_stored():
    rule = a_rule()
    ui, _ = build_ui(rules=[rule], route=FakeRoute(error=BotTokenInvalid("401 Unauthorized")))
    st = ui._state(OWNER)
    msg = FakeMessage(TOKEN)

    asyncio_run(ui._step_bot_route_token(msg, st, TOKEN, rule.id))

    assert ui._bot_tokens.created == [], "a refused token must not be written"
    assert ui._rules.updated == []
    assert rule.bot_token_id == ""
    assert rule.delivery_transport == "USER"


def test_a_token_that_validates_but_cannot_be_stored_leaves_the_task_alone():
    rule = a_rule()
    ui, _ = build_ui(rules=[rule])
    ui._bot_tokens.create = _raise(OSError("vault offline"))
    st = ui._state(OWNER)
    msg = FakeMessage(TOKEN)

    asyncio_run(ui._step_bot_route_token(msg, st, TOKEN, rule.id))

    assert rule.delivery_transport == "USER"
    assert ui._rules.updated == []


# --------------------------------------------------------------------------- #
# Using / removing a listed token
# --------------------------------------------------------------------------- #


def test_tapping_use_attaches_that_exact_token():
    rule = a_rule()
    other = FakeRecord(id="other-token", label="@Other", created_at=0)
    ui, bot = build_ui(rules=[rule], tokens=[FakeRecord(), other])
    st = ui._state(OWNER)
    asyncio_run(ui._render_bot_route_screen(rule, st))

    # The screen sorts by created_at: the older token (@Other) is index 0.
    cq = FakeCQ("rbts:rule-1:0")
    asyncio_run(handler(bot, "_cb_rule_bot_token_use")(None, cq))

    assert rule.bot_token_id == "other-token", "the index must resolve to the built order"
    assert rule.delivery_transport == "BOT"
    assert ui._rules.updated == [rule]


def test_a_stale_screen_never_attaches_the_wrong_token():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], tokens=[FakeRecord()])
    st = ui._state(OWNER)
    asyncio_run(ui._render_bot_route_screen(rule, st))
    ui._bot_tokens.records = []  # token removed from another device

    cq = FakeCQ("rbts:rule-1:0")
    asyncio_run(handler(bot, "_cb_rule_bot_token_use")(None, cq))

    assert rule.bot_token_id == ""
    assert ui._rules.updated == []
    assert cq.answers and cq.answers[-1][1] is True


def test_a_token_a_task_still_routes_through_cannot_be_deleted():
    rule = a_rule(bot_token_id=TOKEN_ID)
    rule.delivery_transport = "BOT"
    ui, bot = build_ui(rules=[rule], tokens=[FakeRecord()])
    st = ui._state(OWNER)
    asyncio_run(ui._render_bot_route_screen(rule, st))

    cq = FakeCQ("rbtd:rule-1:0")
    asyncio_run(handler(bot, "_cb_rule_bot_token_remove")(None, cq))

    assert ui._bot_tokens.deleted == [], "in-use token must survive"
    assert ui._bot_route.forgotten == [], "its pooled client must survive too"
    assert cq.answers and cq.answers[-1][1] is True
    assert "1" in (cq.answers[-1][0] or ""), "the alert must name how many tasks"


def test_removing_an_unused_token_deletes_it_and_drops_its_pooled_client():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], tokens=[FakeRecord()])
    st = ui._state(OWNER)
    asyncio_run(ui._render_bot_route_screen(rule, st))

    cq = FakeCQ("rbtd:rule-1:0")
    asyncio_run(handler(bot, "_cb_rule_bot_token_remove")(None, cq))

    assert ui._bot_tokens.deleted == [TOKEN_ID]
    assert ui._bot_route.forgotten == [TOKEN_ID]
    assert cq.edits, "the screen must re-render after a delete"


def test_route_choice_refuses_a_screen_built_for_another_task():
    ui, _ = build_ui()
    st = ui._state(OWNER)
    st.buffer["bot_route_rule"] = "rule-1"
    st.buffer["bot_route_tokens"] = [TOKEN_ID]

    assert ui._route_token_choice(st, "rule-2", 0) == ""
    assert ui._route_token_choice(st, "rule-1", 3) == ""
    assert ui._route_token_choice(st, "rule-1", 0) == TOKEN_ID
    st.buffer["bot_route_tokens"] = "not-a-list"
    assert ui._route_token_choice(st, "rule-1", 0) == ""


def test_the_add_button_arms_the_paste_step():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule])
    cq = FakeCQ("rbta:rule-1")

    asyncio_run(handler(bot, "_cb_rule_bot_token_attach")(None, cq))

    st = ui._state(OWNER)
    assert st.step == "bot_route_token_rule-1"
    assert cq.edits and "token" in cq.edits[-1][0].lower()


def test_the_route_switch_is_reachable_from_a_foreign_owner_only_through_ownership():
    rule = a_rule()
    ui, bot = build_ui(rules=[rule], scoped=True)
    cq = FakeCQ("rdt:rule-1", uid=999)

    asyncio_run(handler(bot, "_cb_rule_route_toggle")(None, cq))

    assert rule.delivery_transport == "USER"
    assert ui._rules.updated == []
    assert cq.answers and cq.answers[-1][1] is True


def _raise(exc):
    async def _call(*args, **kwargs):
        raise exc

    return _call
