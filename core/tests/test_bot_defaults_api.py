"""Per-bot defaults over the control plane: storage, validation, provenance.

The domain tests prove the precedence rule. These prove the two surfaces a user
actually reaches it through:

  * `SetBotDefaults` / `GetBotDefaults` on the bot-token service — owner-scoped
    in the SQL, validated against the registry *before* storage, and honest about
    what changed;
  * the per-task settings response — which now reports the values the forwarder
    will use and where each came from, while `settings_json` keeps its old
    meaning (what the task itself stores). A client that echoes a settings
    response back therefore cannot pin everything the task merely inherits.
"""
from __future__ import annotations

import asyncio
import json

from core.application.repositories import BotToken
from core.application.use_cases import (
    BotTokenUseCases,
    ForwardRuleUseCases,
)
from core.domain.entities import ForwardRule
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

ALICE = 5094837833
BOB = 777000111


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class Req:
    """A request message, as the servicers see one."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class CountingTokens(SqliteBotTokenRepository):
    """The real repository, counting the writes that reach the database."""

    def __init__(self, db, crypto):
        super().__init__(db, crypto)
        self.writes = 0

    async def set_defaults(self, token_id, owner_user_id, defaults):
        self.writes += 1
        return await super().set_defaults(token_id, owner_user_id, defaults)


class Harness:
    def __init__(self):
        self.db = SqliteDatabase(":memory:")
        self.tokens_repo = CountingTokens(self.db, CryptoService("test-master-key"))
        self.rules_repo = SqliteForwardRuleRepository(self.db)
        self.rules = ForwardRuleUseCases(self.rules_repo)
        self.bot_tokens = BotTokenControlServicer(
            BotTokenUseCases(self.tokens_repo), rules=self.rules_repo,
        )
        self.rule_control = ForwardRuleControlServicer(self.rules, bot_tokens=self.tokens_repo)
        self.db.execute(
            "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
            " username, first_name, is_active, is_authorized, created_at, updated_at)"
            " VALUES ('s1', '+1000', '', '', '', '', 1, 1, 0, 0)", (),
        )

    def seed_token(self, token_id: str, owner: int, defaults=None):
        run(self.tokens_repo.add(BotToken(
            id=token_id, label=f"@{token_id}", token="7878773795:AAsecret",
            owner_user_id=owner, created_at=1, updated_at=1, defaults=defaults or {},
        )))
        return token_id

    def seed_rule(self, rule_id: str, owner: int, token_id: str = ""):
        rule = ForwardRule(
            id=rule_id, name="a task", session_id="s1",
            source_chat_id="-1001", target_chat_id="-1002", owner_user_id=owner,
        )
        if token_id:
            rule.bot_token_id = token_id
        run(self.rules.create(rule))
        return rule

    def get(self, token_id: str, owner: int):
        return run(self.bot_tokens.GetBotDefaults(
            Req(id=token_id, owner_user_id=owner), None,
        ))

    def set(self, token_id: str, owner: int, defaults: dict, **kw):
        return run(self.bot_tokens.SetBotDefaults(
            Req(id=token_id, owner_user_id=owner,
                defaults_json=json.dumps(defaults),
                replace_all=kw.get("replace_all", False),
                clear=kw.get("clear", [])),
            None,
        ))

    def settings(self, rule_id: str, owner: int):
        return run(self.rule_control.GetRuleSettings(
            Req(rule_id=rule_id, owner_user_id=owner), None,
        ))

    def update_settings(self, rule_id: str, owner: int, values: dict,
                        replace_all=False, edited=None):
        return run(self.rule_control.UpdateRuleSettings(
            Req(rule_id=rule_id, owner_user_id=owner,
                settings_json=json.dumps(values), replace_all=replace_all,
                changed=edited or []),
            None,
        ))


# --------------------------------------------------------------------------- #
# Storing defaults
# --------------------------------------------------------------------------- #
def test_defaults_start_empty_and_read_back():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    resp = h.get("tok-a", ALICE)
    assert resp.success and json.loads(resp.defaults_json) == {}

    written = h.set("tok-a", ALICE, {"remove_emojis": True, "footer": "hello"})
    assert written.success and written.error_code == ""
    assert sorted(written.changed) == ["footer", "remove_emojis"]
    assert json.loads(h.get("tok-a", ALICE).defaults_json) == {
        "remove_emojis": True, "footer": "hello",
    }


def test_a_foreign_or_absent_bot_is_a_not_found_on_both_calls():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    assert h.get("tok-a", BOB).error_code == "not_found"
    assert h.set("tok-a", BOB, {"remove_emojis": True}).error_code == "not_found"
    assert h.get("no-such-bot", ALICE).error_code == "not_found"
    # Nothing about another tenant's bot changed.
    assert json.loads(h.get("tok-a", ALICE).defaults_json) == {}


def test_an_unscoped_caller_is_refused():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    assert h.get("tok-a", 0).error_code == "unauthorized"
    assert h.set("tok-a", 0, {"remove_emojis": True}).error_code == "unauthorized"


def test_a_setting_that_names_a_chat_route_or_schedule_is_refused_and_not_stored():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    h.set("tok-a", ALICE, {"footer": "kept"})
    resp = h.set("tok-a", ALICE, {"delivery_transport": "BOT"})
    assert not resp.success and resp.error_code == "not_inheritable"
    assert "cannot be a bot default" in resp.message
    # The refusal says what is still stored, and the stored set is untouched.
    assert json.loads(resp.defaults_json) == {"footer": "kept"}
    assert json.loads(h.get("tok-a", ALICE).defaults_json) == {"footer": "kept"}


def test_an_unknown_or_invalid_setting_is_refused():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    unknown = h.set("tok-a", ALICE, {"watermark_txt": "typo"})
    assert unknown.error_code == "unknown_setting"
    wrong_type = h.set("tok-a", ALICE, {"ai_max_tokens": "lots"})
    assert wrong_type.error_code == "invalid_type" and "integer" in wrong_type.message
    as_list = h.set("tok-a", ALICE, {"watermark_text": ["not", "a", "string"]})
    assert as_list.error_code == "invalid_type" and "string" in as_list.message
    # None of the refusals stored anything.
    assert json.loads(h.get("tok-a", ALICE).defaults_json) == {}


def test_broken_json_and_non_objects_are_refused():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    bad = run(h.bot_tokens.SetBotDefaults(
        Req(id="tok-a", owner_user_id=ALICE, defaults_json="{not json",
            replace_all=False, clear=[]), None,
    ))
    assert bad.error_code == "invalid_json"
    listed = run(h.bot_tokens.SetBotDefaults(
        Req(id="tok-a", owner_user_id=ALICE, defaults_json=json.dumps(["a"]),
            replace_all=False, clear=[]), None,
    ))
    assert listed.error_code == "invalid_type"


def test_a_write_merge_keeps_the_settings_it_did_not_mention():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    h.set("tok-a", ALICE, {"remove_emojis": True, "footer": "kept"})
    h.set("tok-a", ALICE, {"footer": "changed"})
    assert json.loads(h.get("tok-a", ALICE).defaults_json) == {
        "remove_emojis": True, "footer": "changed",
    }


def test_a_full_replace_drops_the_unlisted_defaults():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    h.set("tok-a", ALICE, {"remove_emojis": True, "footer": "kept"})
    h.set("tok-a", ALICE, {"footer": "only"}, replace_all=True)
    assert json.loads(h.get("tok-a", ALICE).defaults_json) == {"footer": "only"}


def test_a_named_clear_hands_one_default_back():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    h.set("tok-a", ALICE, {"remove_emojis": True, "footer": "kept"})
    resp = h.set("tok-a", ALICE, {}, clear=["footer"])
    assert resp.success and resp.changed == ["footer"]
    assert json.loads(h.get("tok-a", ALICE).defaults_json) == {"remove_emojis": True}


def test_a_write_that_changes_nothing_does_not_touch_the_database():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    h.set("tok-a", ALICE, {"remove_emojis": True})
    before = h.tokens_repo.writes
    again = h.set("tok-a", ALICE, {"remove_emojis": True})
    assert again.success and again.changed == []
    assert h.tokens_repo.writes == before


def test_two_bots_of_one_tenant_keep_their_own_defaults():
    h = Harness()
    h.seed_token("tok-a", ALICE)
    h.seed_token("tok-b", ALICE)
    h.set("tok-a", ALICE, {"remove_emojis": True})
    h.set("tok-b", ALICE, {"footer": "B"})
    assert json.loads(h.get("tok-a", ALICE).defaults_json) == {"remove_emojis": True}
    assert json.loads(h.get("tok-b", ALICE).defaults_json) == {"footer": "B"}


# --------------------------------------------------------------------------- #
# What a task resolves through
# --------------------------------------------------------------------------- #
def test_the_settings_response_reports_the_values_and_where_they_came_from():
    h = Harness()
    h.seed_token("tok-a", ALICE, {"footer": "from the bot", "watermark_text": "bot mark"})
    h.seed_rule("r-1", ALICE, token_id="tok-a")
    h.update_settings("r-1", ALICE, {"watermark_text": "from the task"})

    resp = h.settings("r-1", ALICE)
    sources = json.loads(resp.sources_json)
    effective = json.loads(resp.effective_json)
    stored = json.loads(resp.settings_json)

    assert sources["watermark_text"]["source"] == "task"
    assert sources["footer"]["source"] == "bot"
    assert sources["remove_emojis"]["source"] == "default"
    assert effective["footer"] == "from the bot"
    assert effective["watermark_text"] == "from the task"
    # `settings_json` is what the task *stores*: the key it chose, and the
    # registry default (not the bot's value) for the key it inherits. Keeping the
    # two views apart is what lets a client show inheritance without copying it.
    assert stored["watermark_text"] == "from the task"
    assert stored["footer"] == "" != effective["footer"]


def test_a_task_without_a_bot_resolves_to_the_registry_defaults():
    h = Harness()
    h.seed_rule("r-1", ALICE)
    resp = h.settings("r-1", ALICE)
    sources = json.loads(resp.sources_json)
    assert {entry["source"] for entry in sources.values()} <= {"task", "default"}
    assert json.loads(resp.effective_json)["footer"] == ""


def test_changing_a_bot_default_does_not_touch_the_tasks_own_values():
    h = Harness()
    h.seed_token("tok-a", ALICE, {"footer": "v1"})
    h.seed_rule("r-1", ALICE, token_id="tok-a")
    h.set("tok-a", ALICE, {"footer": "v2"})
    resp = h.settings("r-1", ALICE)
    assert json.loads(resp.effective_json)["footer"] == "v2"
    # The task was never rewritten: what it stores for the footer is still the
    # registry default, so changing the bot's default moved it too.
    assert json.loads(resp.settings_json)["footer"] == ""
    assert json.loads(resp.sources_json)["footer"]["source"] == "bot"


def test_a_rule_cannot_read_another_tenants_bot_defaults():
    h = Harness()
    h.seed_token("tok-b", BOB, {"footer": "bob's default"})
    rule = h.seed_rule("r-1", ALICE, token_id="tok-b")
    # The task points at a bot that is not this owner's; the resolution must not
    # hand Alice Bob's settings.
    resp = h.settings(rule.id, ALICE)
    assert json.loads(resp.effective_json)["footer"] == ""
    assert json.loads(resp.sources_json)["footer"]["source"] == "default"


def test_a_task_can_override_an_inherited_default_with_the_registry_value():
    """The case that makes per-task overrides real rather than nominal.

    The bot cleans links for every task; this task turns that off, and "off" is
    also the registry default — so the stored value does not differ from the
    default anywhere. Judged against what the task was *using*, it is a change.
    """
    h = Harness()
    h.seed_token("tok-a", ALICE, {"remove_links": True})
    h.seed_rule("r-1", ALICE, token_id="tok-a")
    assert json.loads(h.settings("r-1", ALICE).effective_json)["remove_links"] is True

    resp = h.update_settings("r-1", ALICE, {"remove_links": False})
    assert resp.success and "1 setting(s) updated" in resp.message
    after = h.settings("r-1", ALICE)
    assert json.loads(after.effective_json)["remove_links"] is False
    assert json.loads(after.sources_json)["remove_links"]["source"] == "task"
    # And handing it back gives the bot's rule again.
    stored = json.loads(after.settings_json)
    h.update_settings("r-1", ALICE, {"remove_links": stored["remove_links"]})
    # ...which is the registry default, so the pin has to be removed explicitly.
    from core.domain.rule_settings import clear_setting
    rule = run(h.rules.get_owned("r-1", ALICE))
    assert clear_setting(rule, "remove_links") is True
    run(h.rules.update(rule))
    final = h.settings("r-1", ALICE)
    assert json.loads(final.effective_json)["remove_links"] is True
    assert json.loads(final.sources_json)["remove_links"]["source"] == "bot"


def test_a_form_posts_every_field_and_names_the_one_the_user_edited():
    """Why `changed` exists, and what it protects.

    A settings form renders every field and posts every field back. Only the one
    the user touched is a choice; the rest are the values the form was shown, and
    a task that inherits its bot's footer would otherwise stop inheriting it the
    moment anything else was saved.
    """
    h = Harness()
    h.seed_token("tok-a", ALICE, {"footer": "from the bot"})
    h.seed_rule("r-1", ALICE, token_id="tok-a")
    stored = json.loads(h.settings("r-1", ALICE).settings_json)
    stored["watermark_text"] = "edited"

    resp = h.update_settings("r-1", ALICE, stored, edited=["watermark_text"])
    assert resp.success and "1 setting(s) updated" in resp.message
    after = h.settings("r-1", ALICE)
    assert json.loads(after.sources_json)["watermark_text"]["source"] == "task"
    assert json.loads(after.sources_json)["footer"]["source"] == "bot"
    assert json.loads(after.effective_json)["footer"] == "from the bot"


def test_naming_a_key_the_body_does_not_carry_is_refused():
    h = Harness()
    h.seed_token("tok-a", ALICE, {"footer": "from the bot"})
    h.seed_rule("r-1", ALICE, token_id="tok-a")
    resp = h.update_settings("r-1", ALICE, {"watermark_text": "x"}, edited=["footer"])
    assert not resp.success and resp.error_code == "unknown_setting"
    assert json.loads(h.settings("r-1", ALICE).sources_json)["footer"]["source"] == "bot"


def test_a_full_body_put_without_a_changed_list_is_taken_as_written():
    """The older contract, unchanged — and the reason the panels send `changed`.

    A body that states every setting is stating every value; the inherited
    footer it restates at its default is therefore overridden. Nothing here is
    silent: the response says which settings moved.
    """
    h = Harness()
    h.seed_token("tok-a", ALICE, {"footer": "from the bot"})
    h.seed_rule("r-1", ALICE, token_id="tok-a")
    stored = json.loads(h.settings("r-1", ALICE).settings_json)
    resp = h.update_settings("r-1", ALICE, stored)
    assert resp.success
    after = h.settings("r-1", ALICE)
    assert json.loads(after.sources_json)["footer"]["source"] == "task"
    assert json.loads(after.effective_json)["footer"] == ""
