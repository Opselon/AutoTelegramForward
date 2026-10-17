"""Per-bot defaults, and the precedence a task resolves through.

A bot can now carry settings for *every* task that uses it, so a tenant with ten
tasks on one bot configures the bot once. Three facts have to hold for that to be
safe rather than merely convenient, and each has tests here:

1. **Precedence is explicit.** A task that chose a value keeps it; a task that
   did not inherits the bot's; a setting nobody chose is the registry default.
   "Chose" has to be observable for settings stored in real columns as well as
   for those in the rule's metadata, otherwise a bot default would silently
   override a task that had already set the very same setting — which is what
   `test_a_column_setting_pinned_to_its_default_still_overrides_the_bot` and
   `test_applying_a_setting_records_that_the_task_chose_it` pin down.
2. **A bot default cannot re-route a task.** Anything naming a chat, a route or
   a schedule is not inheritable, so one task's traffic can never be pointed at
   another task's chats by a default.
3. **Defaults are per bot and per owner.** Two bots — even two of the same
   tenant — inherit independently, and a write from another tenant is refused.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time

import pytest

from core.application.repositories import BotToken
from core.domain.bot_defaults import (
    SOURCE_BOT,
    SOURCE_DEFAULT,
    SOURCE_TASK,
    describe,
    effective,
    inherited_count,
    merge_defaults,
    provenance,
    validate_defaults,
)
from core.domain.entities import ForwardRule
from core.domain.rule_settings import (
    PIN_INDEX,
    SETTING_SPECS,
    SettingError,
    apply_settings,
    clear_setting,
    pinned_keys,
)
from core.infrastructure.persistence.database import SCHEMA_VERSION, SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqliteBotTokenRepository
from core.infrastructure.security.crypto import CryptoService

#: Settings that describe *which* messages a task handles, or *when* — never
#: how. The registry marks them, and this list is the contract: adding a key to
#: it, or removing one, has to be a deliberate edit here.
NOT_INHERITABLE = {
    "delivery_transport",
    "bot_token_id",
    "source_policies",
    "target_policies",
    "link_buttons",
    "affiliate_kit",
    "auto_post_scheduler",
    "power_schedule",
    "auto_posting",
    "power_control",
}


def make_rule(**kwargs) -> ForwardRule:
    base = dict(id="r-1", name="t", source_chat_id="-1001", target_chat_id="-1002",
                owner_user_id=7)
    base.update(kwargs)
    return ForwardRule(**base)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# What may be inherited
# --------------------------------------------------------------------------- #
def test_exactly_the_routing_and_schedule_settings_are_not_inheritable():
    marked = {k for k, spec in SETTING_SPECS.items() if not spec.inheritable}
    assert marked == NOT_INHERITABLE
    # The greatest number of settings stay inheritable: this is the whole point
    # of a bot default.
    assert len(SETTING_SPECS) - len(marked) > 50


def test_a_default_that_names_a_chat_route_or_schedule_is_refused():
    for key in sorted(NOT_INHERITABLE):
        with pytest.raises(SettingError) as err:
            validate_defaults({key: SETTING_SPECS[key].default})
        assert err.value.code == "not_inheritable", key


def test_the_reason_is_stated_in_the_error():
    with pytest.raises(SettingError) as err:
        validate_defaults({"source_policies": {"-1001": {}}})
    assert "cannot be a bot default" in str(err.value)


def test_an_unknown_default_is_refused():
    with pytest.raises(SettingError) as err:
        validate_defaults({"watermark_txt": "typo"})
    assert err.value.code == "unknown_setting"


def test_an_invalid_default_value_is_refused():
    with pytest.raises(SettingError):
        validate_defaults({"ai_max_tokens": "lots"})
    with pytest.raises(SettingError):
        validate_defaults({"watermark_text": ["not", "a", "string"]})


def test_an_invalid_default_never_leaves_a_partial_set():
    with pytest.raises(SettingError):
        validate_defaults({"remove_emojis": True, "ai_max_tokens": "lots"})
    assert validate_defaults({"remove_emojis": True}) == {"remove_emojis": True}


def test_defaults_must_be_an_object():
    with pytest.raises(SettingError):
        validate_defaults(["remove_emojis"])


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #
def test_task_beats_bot_beats_registry():
    rule = make_rule()
    defaults = {"watermark_text": "from the bot", "footer": "also the bot"}

    assert provenance(rule, defaults, "footer") == SOURCE_BOT
    assert provenance(rule, defaults, "remove_emojis") == SOURCE_DEFAULT

    apply_settings(rule, {"watermark_text": "from the task"})
    assert provenance(rule, defaults, "watermark_text") == SOURCE_TASK

    values = effective(rule, defaults)
    assert values["watermark_text"] == "from the task"
    assert values["footer"] == "also the bot"
    assert values["remove_emojis"] == SETTING_SPECS["remove_emojis"].default


def test_a_default_for_a_non_inheritable_setting_cannot_reach_a_task():
    rule = make_rule()
    # Even if such an entry reached storage (an older build, a hand-edited row),
    # the resolver ignores it rather than re-routing the task.
    smuggled = {"delivery_transport": "BOT", "bot_token_id": "someone-elses"}
    values = effective(rule, smuggled)
    assert values["delivery_transport"] == SETTING_SPECS["delivery_transport"].default
    assert values["bot_token_id"] == SETTING_SPECS["bot_token_id"].default
    assert provenance(rule, smuggled, "delivery_transport") == SOURCE_DEFAULT


def test_a_task_value_that_equals_the_default_is_still_its_own_choice():
    """The user's screen showed the bot's value; they chose the registry's.

    Judged against what the task was using, that is a change, so the choice is
    recorded even though the stored value equals the default.
    """
    rule = make_rule()
    defaults = {"watermark_text": "from the bot"}
    apply_settings(rule, {"watermark_text": ""}, effective_before=defaults)
    assert provenance(rule, defaults, "watermark_text") == SOURCE_TASK
    assert effective(rule, defaults)["watermark_text"] == ""
    # Clearing hands it back to the bot.
    assert clear_setting(rule, "watermark_text") is True
    assert provenance(rule, defaults, "watermark_text") == SOURCE_BOT


def test_a_column_setting_pinned_to_its_default_still_overrides_the_bot():
    # `remove_links` is a real column, so its value is the only trace of the
    # choice; a bot default that turned link removal on must not beat the task
    # that turned it off — even though "off" is also the registry default.
    rule = make_rule()
    apply_settings(rule, {"remove_links": False}, effective_before={"remove_links": True})
    assert "remove_links" in pinned_keys(rule)
    defaults = {"remove_links": True}
    assert provenance(rule, defaults, "remove_links") == SOURCE_TASK
    assert effective(rule, defaults)["remove_links"] is False


def test_applying_a_setting_records_that_the_task_chose_it():
    rule = make_rule()
    apply_settings(rule, {"branding_triggers_enabled": True})
    assert "branding_triggers_enabled" in rule.metadata.get(PIN_INDEX, [])


def test_restating_the_registry_default_does_not_pin_it():
    """A form posts every field. That must not turn every field into a choice."""
    rule = make_rule()
    apply_settings(rule, {"watermark_text": ""}, effective_before={"watermark_text": ""})
    assert rule.metadata.get(PIN_INDEX, []) == []
    assert "watermark_text" not in pinned_keys(rule)
    # ...and a value the task genuinely holds on to stays its own.
    rule2 = make_rule()
    apply_settings(rule2, {"watermark_text": "mine"})
    assert "watermark_text" in pinned_keys(rule2)


def test_a_setting_left_at_the_registry_default_is_not_a_choice():
    """Metadata presence alone is not a choice: a stored default is still unset.

    This is what keeps a client that posts the whole settings object from
    pinning every setting on the task.
    """
    rule = make_rule()
    apply_settings(rule, {"footer": ""})
    assert "footer" in rule.metadata  # written, but equal to the default
    assert "footer" not in pinned_keys(rule)
    assert effective(rule, {"footer": "from the bot"})["footer"] == "from the bot"


def test_a_fresh_task_pins_nothing_so_every_default_applies():
    rule = make_rule()
    assert pinned_keys(rule) == set()
    defaults = {"remove_emojis": True, "footer": "bot"}
    assert inherited_count(defaults, rule) == 2


def test_clearing_a_setting_hands_it_back_to_the_bot():
    rule = make_rule()
    defaults = {"watermark_text": "from the bot"}
    apply_settings(rule, {"watermark_text": "from the task"})
    assert clear_setting(rule, "watermark_text") is True
    assert provenance(rule, defaults, "watermark_text") == SOURCE_BOT
    assert effective(rule, defaults)["watermark_text"] == "from the bot"
    # Clearing twice is honest about having nothing to do.
    assert clear_setting(rule, "watermark_text") is False


def test_clearing_a_column_setting_drops_the_recorded_choice():
    rule = make_rule()
    apply_settings(rule, {"remove_links": False}, effective_before={"remove_links": True})
    assert clear_setting(rule, "remove_links") is True
    assert "remove_links" not in pinned_keys(rule)
    assert "remove_links" not in rule.metadata.get(PIN_INDEX, [])
    assert effective(rule, {"remove_links": True})["remove_links"] is True


def test_clearing_an_unknown_setting_is_refused():
    with pytest.raises(SettingError):
        clear_setting(make_rule(), "not_a_setting")


def test_describe_says_where_every_value_came_from():
    rule = make_rule()
    apply_settings(rule, {"watermark_text": "from the task"})
    described = describe(rule, {"watermark_text": "from the bot", "footer": "bot footer"})
    assert described["watermark_text"]["source"] == SOURCE_TASK
    assert described["watermark_text"]["pinned"] is True
    assert described["watermark_text"]["value"] == "from the task"
    assert described["footer"]["source"] == SOURCE_BOT
    assert described["footer"]["bot_default"] == "bot footer"
    assert described["remove_emojis"]["source"] == SOURCE_DEFAULT
    assert described["remove_emojis"]["default"] == SETTING_SPECS["remove_emojis"].default
    assert set(described) == set(SETTING_SPECS)


# --------------------------------------------------------------------------- #
# One bot's defaults must not touch another's
# --------------------------------------------------------------------------- #
def test_two_bots_do_not_leak_into_each_other():
    first, second = make_rule(id="r-a"), make_rule(id="r-b")
    defaults_a = {"watermark_text": "A"}
    defaults_b = {"watermark_text": "B", "footer": "B only"}
    assert effective(first, defaults_a)["watermark_text"] == "A"
    assert effective(second, defaults_b)["watermark_text"] == "B"
    # A's bot says nothing about the footer, so B's footer cannot reach A.
    assert effective(first, defaults_a)["footer"] == ""
    assert provenance(first, defaults_a, "footer") == SOURCE_DEFAULT


def test_a_task_on_the_same_bot_resolves_the_same_defaults():
    defaults = {"watermark_text": "shared"}
    one, two = make_rule(id="r-1"), make_rule(id="r-2")
    assert effective(one, defaults)["watermark_text"] == "shared"
    assert effective(two, defaults)["watermark_text"] == "shared"
    # ...and one task pinning it does not change the other.
    apply_settings(one, {"watermark_text": "mine"})
    assert effective(one, defaults)["watermark_text"] == "mine"
    assert effective(two, defaults)["watermark_text"] == "shared"


# --------------------------------------------------------------------------- #
# Merge / replace / clear
# --------------------------------------------------------------------------- #
def test_merge_keeps_settings_the_caller_did_not_mention():
    merged = merge_defaults({"remove_emojis": True, "footer": "kept"}, {"footer": "new"})
    assert merged == {"remove_emojis": True, "footer": "new"}


def test_a_full_replace_is_explicit_and_drops_the_rest():
    merged = merge_defaults(
        {"remove_emojis": True, "footer": "kept"},
        {"footer": "only"},
        replace_all=True,
    )
    assert merged == {"footer": "only"}


def test_clear_removes_named_settings():
    merged = merge_defaults({"remove_emojis": True, "footer": "x"}, {}, clear=["footer"])
    assert merged == {"remove_emojis": True}
    with pytest.raises(SettingError):
        merge_defaults({}, {}, clear=["nope"])


def test_merge_validates_what_it_stores():
    with pytest.raises(SettingError):
        merge_defaults({}, {"delivery_transport": "BOT"})


# --------------------------------------------------------------------------- #
# Storage: per bot, per owner, and readable from a database that predates it
# --------------------------------------------------------------------------- #
def _repo() -> SqliteBotTokenRepository:
    path = os.path.join(tempfile.mkdtemp(), "tokens.db")
    return SqliteBotTokenRepository(SqliteDatabase(path), CryptoService("k" * 32))


def _token(token_id: str, owner: int, defaults=None) -> BotToken:
    now = int(time.time())
    return BotToken(id=token_id, label=token_id, token="123:secret", owner_user_id=owner,
                    created_at=now, updated_at=now, defaults=defaults or {})


def test_defaults_round_trip_and_another_tenant_cannot_write_them():
    repo = _repo()

    async def main():
        await repo.add(_token("t-mine", 7, {"remove_emojis": True}))
        await repo.add(_token("t-theirs", 9))
        assert (await repo.get_owned("t-mine", 7)).defaults == {"remove_emojis": True}
        # A transplanted id reads as "not found" and writes as a refusal.
        assert await repo.get_owned("t-mine", 9) is None
        assert await repo.set_defaults("t-mine", 9, {"remove_emojis": False}) is False
        assert (await repo.get_owned("t-mine", 7)).defaults == {"remove_emojis": True}
        assert await repo.set_defaults("t-mine", 7, {"footer": "mine"}) is True
        assert (await repo.get_owned("t-mine", 7)).defaults == {"footer": "mine"}
        # The other tenant's bot is untouched by any of that.
        assert (await repo.get_owned("t-theirs", 9)).defaults == {}

    run(main())


def test_an_unreadable_stored_mapping_reads_as_no_defaults():
    repo = _repo()
    token = _token("t-broken", 7)
    run(repo.add(token))
    # A mapping that cannot be parsed must not fail the token: the bot still
    # forwards, with the registry defaults.
    repo._db.execute(
        "UPDATE bot_tokens SET defaults_json=? WHERE id=?",
        ("{not json", "t-broken"),
    )
    assert run(repo.get_owned("t-broken", 7)).defaults == {}
    repo._db.execute(
        "UPDATE bot_tokens SET defaults_json=? WHERE id=?",
        (json.dumps(["a", "list"]), "t-broken"),
    )
    assert run(repo.get_owned("t-broken", 7)).defaults == {}


def test_a_database_from_before_this_change_gains_the_column_and_keeps_its_rows():
    """The upgrade path for an install that already has bots attached."""
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (19);
        CREATE TABLE bot_tokens (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            token_encrypted TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    legacy.execute(
        "INSERT INTO bot_tokens (id, label, token_encrypted, is_active, created_at,"
        " updated_at, owner_user_id) VALUES ('t-old', 'attached before', '', 1, 1, 1, 7)"
    )
    legacy.commit()
    legacy.close()

    db = SqliteDatabase(path)
    version = db.query_one("SELECT version FROM schema_version")["version"]
    assert version == SCHEMA_VERSION == 20
    assert "defaults_json" in {
        r["name"] for r in db.query_all("PRAGMA table_info(bot_tokens)")
    }
    repo = SqliteBotTokenRepository(db, CryptoService("k" * 32))
    existing = run(repo.get_owned("t-old", 7))
    assert existing is not None and existing.label == "attached before"
    assert existing.defaults == {}
    # And it can now carry defaults like any other bot.
    assert run(repo.set_defaults("t-old", 7, {"remove_emojis": True})) is True
    assert run(repo.get_owned("t-old", 7)).defaults == {"remove_emojis": True}
