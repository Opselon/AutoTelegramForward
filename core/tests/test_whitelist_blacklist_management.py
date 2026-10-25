"""Regression tests for Whitelist/Blacklist management (v30).

A user opening either list on any of the three clients must see the entries
they actually saved for the currently selected task, and must be able to add,
edit, delete, enable/disable and inspect each entry — with the change
persisted, owner-scoped, and the two lists strictly independent.

These tests pin the behaviour the three clients share:

* ``FilterRule.is_enabled`` is stored, defaults to enabled, and a disabled
  rule is skipped by the engine (allow) rather than evaluated — so flipping
  the switch never deletes the words, and flipping it back restores them;
* ``blacklist_off`` / ``whitelist_off`` on the task's content policy are the
  same single source of truth the web modal, the bot menus and mobile read
  (through ``ForwardRule.policy_chain()[0]`` — the exact path the pipeline's
  FILTER stage evaluates), and switching one list off never touches the other;
* the settings surface (``read_settings``/``apply_settings``) — the write path
  every client uses — round-trips both switches, so a web write is what the
  engine, the bot and mobile all read back.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.domain import rule_settings as RS
from core.domain.entities import FilterRule, ForwardRule, MessagePayload
from core.domain.services import FilterEngine
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteFilterRuleRepository,
)

OWNER = 5094837833


def run(coro_factory, tmp_path: Path) -> None:
    async def runner() -> None:
        db = SqliteDatabase(str(tmp_path / "wbl.db"))
        try:
            await coro_factory(db)
        finally:
            db.close()

    asyncio.run(runner())


def payload(text: str) -> MessagePayload:
    return MessagePayload(message_id=1, chat_id="-1001", text=text)


def make_rule(**policy_overrides) -> ForwardRule:
    """A task carrying a content policy, as the three clients store one."""
    rule = ForwardRule(
        id="rule-a",
        session_id="sess-a",
        source_chat_id="-1001",
        target_chat_id="-1002",
        is_active=True,
        owner_user_id=OWNER,
    )
    policy = dict(
        blacklist_words=["spam", "scam"],
        whitelist_words=["gold"],
    )
    policy.update(policy_overrides)
    rule.content_policy_config = policy
    return rule


def decide(rule: ForwardRule, text: str) -> bool:
    """The verdict the pipeline's FILTER stage records for one message."""
    return rule.policy_chain()[0].evaluate(text, "text").allowed


# --------------------------------------------------------------------------- #
# FilterRule.is_enabled — the saved-filters surface
# --------------------------------------------------------------------------- #
class TestFilterRuleEnabledState:
    """The switch is persisted state, not a UI-only flag."""

    def test_new_filter_defaults_to_enabled(self, tmp_path):
        async def body(db):
            repo = SqliteFilterRuleRepository(db)
            saved = await repo.add(FilterRule(owner_user_id=OWNER, blacklist_keywords=["spam"]))
            assert saved.is_enabled is True
            back = await repo.get_by_id(saved.id)
            assert back is not None
            assert back.is_enabled is True

        run(body, tmp_path)

    def test_disabled_filter_is_skipped_not_evaluated(self):
        rule = FilterRule(
            owner_user_id=OWNER,
            blacklist_keywords=["spam"],
            whitelist_keywords=["gold"],
            is_enabled=False,
        )
        # A blacklist hit that would normally DROP, and a whitelist that would
        # normally require a match: a disabled rule allows both.
        assert FilterEngine().evaluate(payload("buy spam now"), rule).action.name == "ALLOW"
        assert FilterEngine().evaluate(payload("random text"), rule).action.name == "ALLOW"
        # ...and the words are still there, so re-enabling restores the gate.
        assert rule.blacklist_keywords == ["spam"]
        assert rule.whitelist_keywords == ["gold"]

    def test_enabled_state_survives_a_round_trip(self, tmp_path):
        async def body(db):
            repo = SqliteFilterRuleRepository(db)
            rule = await repo.add(
                FilterRule(owner_user_id=OWNER, whitelist_keywords=["gold"], is_enabled=False)
            )
            rule.is_enabled = True
            await repo.update(rule)
            back = await repo.get_by_id(rule.id)
            assert back is not None
            assert back.is_enabled is True
            # The words survive the toggle cycle unchanged.
            assert back.whitelist_keywords == ["gold"]

        run(body, tmp_path)

    def test_toggle_does_not_duplicate_or_corrupt_entries(self, tmp_path):
        async def body(db):
            repo = SqliteFilterRuleRepository(db)
            rule = await repo.add(
                FilterRule(
                    owner_user_id=OWNER,
                    blacklist_keywords=["spam", "scam"],
                    regex_patterns=["^BUY"],
                    is_enabled=True,
                )
            )
            for _ in range(3):
                rule.is_enabled = not rule.is_enabled
                await repo.update(rule)
            back = await repo.get_by_id(rule.id)
            assert back is not None
            assert back.blacklist_keywords == ["spam", "scam"]
            assert back.regex_patterns == ["^BUY"]
            # Three flips land back on disabled.
            assert back.is_enabled is False

        run(body, tmp_path)


# --------------------------------------------------------------------------- #
# Content policy — the task-level list switches the three clients share
# --------------------------------------------------------------------------- #
class TestContentPolicyListSwitches:
    """``blacklist_off`` / ``whitelist_off`` are one source of truth.

    The pipeline's FILTER stage evaluates exactly ``policy_chain()``, so these
    tests drive that same chain — not a private re-derivation of the policy.
    """

    def test_blacklist_off_skips_only_the_blacklist(self):
        on = make_rule()
        off = make_rule(blacklist_off=True)

        # "gold spam" carries a whitelist word (passes) and a blacklist word
        # (drops): the blacklist is what decides it.
        assert decide(on, "gold spam") is False, "blacklist must drop its words"
        # Switch the blacklist off and the same message walks through…
        assert decide(off, "gold spam") is True, "parked blacklist must not drop"
        # …while the whitelist still gates every message it always gated.
        assert decide(off, "random") is False, "whitelist must keep gating"
        assert decide(off, "gold signals") is True

    def test_whitelist_off_skips_only_the_whitelist(self):
        on = make_rule()
        off = make_rule(whitelist_off=True)

        # Whitelist on: an unmatched message is dropped.
        assert decide(on, "random") is False
        # Whitelist off: the same message walks through…
        assert decide(off, "random") is True, "parked whitelist must not drop"
        # …while the blacklist still drops what it always dropped.
        assert decide(off, "gold scam") is False, "blacklist must keep gating"

    def test_switching_off_never_deletes_the_words(self):
        rule = make_rule(blacklist_off=True, whitelist_off=True)
        policy = rule.policy_chain()[0]
        # The words are preserved while both lists are parked…
        assert list(policy.blacklist_words) == ["spam", "scam"]
        assert list(policy.whitelist_words) == ["gold"]
        # …and re-enabling restores enforcement exactly as it was.
        rule.blacklist_off = False
        assert decide(rule, "gold spam") is False, "re-enabling must restore the gate"
        assert rule.whitelist_off is True, "whitelist switch must be untouched"

    def test_policy_round_trips_both_switches(self):
        rule = make_rule(blacklist_off=True, whitelist_off=True)
        # The metadata blob is what the API persists and every client reads.
        blob = rule.content_policy_config
        assert blob["blacklist_off"] is True
        assert blob["whitelist_off"] is True
        back = make_rule(**blob)
        policy = back.policy_chain()[0]
        assert policy.blacklist_off is True
        assert policy.whitelist_off is True
        assert list(policy.blacklist_words) == ["spam", "scam"]
        assert list(policy.whitelist_words) == ["gold"]

    def test_default_is_both_lists_enabled(self):
        rule = make_rule()
        policy = rule.policy_chain()[0]
        assert policy.blacklist_off is False
        assert policy.whitelist_off is False
        # The switches live inside the persisted blob the clients write.
        blob = rule.content_policy_config
        assert blob.get("blacklist_off", False) is False
        assert blob.get("whitelist_off", False) is False

    def test_regex_lists_follow_their_own_list_switch(self):
        # regex_block is blacklist-side, regex_require whitelist-side: parking
        # a list parks its patterns, and only its patterns.
        #
        # "gold BUY" carries the whitelist word, so it passes the whitelist
        # check; the blocking regex would drop it only if the blacklist side
        # is live. With the blacklist parked it must walk through.
        bl_off = make_rule(blacklist_off=True, regex_block=[r"BUY"])
        assert decide(bl_off, "gold BUY") is True, "parked blacklist parks its regex"
        # And the parked blacklist's own words still do nothing.
        assert decide(bl_off, "gold spam") is True

        # whitelist parked → regex_require is parked with it, and only the
        # blacklist still gates.
        wl_off = make_rule(whitelist_off=True, regex_require=[r"GOLD"])
        assert decide(wl_off, "random") is True, "parked whitelist parks its regex"
        assert decide(wl_off, "scam") is False, "blacklist still gates"


# --------------------------------------------------------------------------- #
# The settings surface every client writes through
# --------------------------------------------------------------------------- #
class TestSettingsSurface:
    """The web modal, the bot and mobile all write through ``apply_settings``.

    One write path means one truth: a switch flipped on any client is the
    switch the engine, the other clients and a re-opened menu all read.
    """

    def test_toggle_round_trips_through_the_settings_api(self):
        rule = make_rule()
        # A client writes only the keys the user moved (the merge contract
        # UpdateRuleSettings serves): switching the blacklist off.
        changed = RS.apply_settings(rule, {"blacklist_off": True, "blacklist_words": ["spam", "scam"]})
        assert "blacklist_off" in changed
        assert rule.blacklist_off is True
        # The engine sees it through the same chain the pipeline evaluates.
        assert decide(rule, "gold spam") is True, "engine must honour the switch"
        # And the read path every client renders from sees it too.
        assert RS.read_settings(rule)["blacklist_off"] is True

        # Switch it back: the words the user typed never left the store.
        RS.apply_settings(rule, {"blacklist_off": False})
        assert rule.blacklist_off is False
        assert decide(rule, "gold spam") is False, "re-enabling restores the gate"
        assert rule.blacklist == ["spam", "scam"]

    def test_the_two_lists_are_independent_settings(self):
        rule = make_rule()
        RS.apply_settings(rule, {"blacklist_off": True})
        assert rule.whitelist_off is False, "blacklist switch must not move whitelist"
        RS.apply_settings(rule, {"whitelist_off": True})
        assert rule.blacklist_off is True, "whitelist switch must not move blacklist"
        # Both parked: everything walks through, words intact.
        assert decide(rule, "anything at all") is True
        assert rule.blacklist == ["spam", "scam"]
        assert rule.whitelist == ["gold"]

    def test_unknown_setting_is_rejected_before_any_mutation(self):
        rule = make_rule()
        before = dict(rule.metadata.get("content_policy", {}))
        try:
            RS.apply_settings(rule, {"blacklist_off": True, "no_such_key": 1})
            raise AssertionError("unknown setting must raise")
        except RS.SettingError:
            pass
        # Nothing was written: the rule is exactly as it was.
        assert rule.metadata.get("content_policy", {}) == before

    def test_schema_advertises_both_switches(self):
        schema = RS.schema()
        keys = {s["key"]: s for s in schema["settings"]}
        assert "blacklist_off" in keys, "schema must advertise the blacklist switch"
        assert "whitelist_off" in keys, "schema must advertise the whitelist switch"
        # Both are real bool settings in the content group, so a client can
        # render a switch from the schema alone.
        for key in ("blacklist_off", "whitelist_off"):
            spec = keys[key]
            assert spec["kind"] == "bool"
            assert spec["group"] == "content"
