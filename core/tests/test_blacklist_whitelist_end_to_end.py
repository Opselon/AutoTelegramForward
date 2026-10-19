"""STEER 07 / STEER 08 — Blacklist and Whitelist: one authority, enforced.

Both features have a bot menu (`rfl_bl:` / `rfl_wl:`), a settings surface
(`rule_settings.py` lists `blacklist_words` / `whitelist_words`), and exactly one
place where the forwarding engine looks at them: `ForwardRule.policy_chain()` ->
`ContentPolicy.evaluate()`, which reads the *content policy*
(`metadata["content_policy"]["blacklist_words"] / ["whitelist_words"]`).

What the bot menu wrote instead was `metadata["blacklist"]` / `metadata["whitelist"]`
— the pre-policy storage keys. Nothing on the forwarding path reads them, so a
word added in the menu was stored, rendered, counted, and never consulted: the
message went out anyway. That is the defect these tests pin.

Expected behaviour fixed here:

* one authoritative home for both lists (the content policy), so the menu, the
  settings API and the engine can never disagree;
* legacy rows written before the fix keep working (read-compat);
* a blacklist word drops the message; a whitelist turns the task into
  "only these words"; blacklist wins when both match;
* empty lists constrain nothing, malformed entries are ignored, keywords are
  matched literally (a keyword is never a pattern), and the master switch
  (`content_policy_enabled`) still turns the whole policy off;
* a task's lists never touch another task.
"""

from __future__ import annotations

import asyncio

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import ForwardRule, MessagePayload
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
)

OWNER = 5094837833
RULE_ID = "rule-bl-1"
RULE_ID_2 = "rule-bl-2"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


class FakeQueue:
    """Records accepted deliveries."""

    def __init__(self):
        self.jobs: list[tuple[str, int, str, str]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        return True


def make_rule(rule_id: str = RULE_ID, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=OWNER,
    )
    # Each test drives exactly one gate; a duplicate-filter match would hide it.
    rule.duplicate_filters = False
    for key, value in kwargs.items():
        setattr(rule, key, value)
    return rule


def build(db, queue, map_repo=None) -> DurableMessagePipeline:
    return DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=map_repo or SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )


async def seed(db, *rules) -> None:
    from core.domain.entities import TelegramSession
    from core.infrastructure.persistence.sqlite_repositories import SqliteSessionRepository
    from core.infrastructure.security.crypto import CryptoService

    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id="sess-a", phone_number="+100****0000", is_active=True,
                        is_authorized=True, owner_user_id=OWNER)
    )
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def payload(message_id: int, text: str, chat_id: str = SOURCE) -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=chat_id, text=text)


async def forwarded_ids(db, queue, text: str, *, rule_id: str = RULE_ID,
                        message_id: int = 1) -> list[int]:
    """Run one message through the real pipeline and report what was delivered."""
    queue.jobs.clear()
    pipe = build(db, queue)
    await pipe.process(payload(message_id, text, chat_id=SOURCE), trigger="NEW_MESSAGE")
    return [job[1] for job in queue.jobs]


def _write_through_the_menu(rule: ForwardRule, *, blacklist=None, whitelist=None) -> None:
    """The exact storage writes the bot's menus perform.

    `_step_add_bl` / `_cb_rule_blacklist_clear` assign `rule.blacklist`;
    `_step_add_wl` / `_cb_rule_whitelist_clear` assign `rule.whitelist`.
    """
    if blacklist is not None:
        rule.blacklist = blacklist
    if whitelist is not None:
        rule.whitelist = whitelist


async def _reload(db, rule: ForwardRule) -> ForwardRule:
    """Re-read the rule the way a restart would (fresh repo, fresh object)."""
    stored = await SqliteForwardRuleRepository(db).get_by_id(rule.id)
    assert stored is not None
    return stored


# --------------------------------------------------------------------------- #
# STEER 07 — Blacklist
# --------------------------------------------------------------------------- #
class TestTheBlacklistMenuDrawsTheLineTheEngineEnforces:
    def test_a_word_added_in_the_menu_drops_a_matching_message(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["spam"])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "buy spam now") == []
            finally:
                db.close()

        asyncio.run(run())

    def test_a_message_without_the_word_is_still_forwarded(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["spam"])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "buy gold now") == [1]
            finally:
                db.close()

        asyncio.run(run())

    def test_the_word_survives_a_restart_and_still_drops(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["spam"])
                await seed(db, rule)
                reloaded = await _reload(db, rule)
                assert list(reloaded.blacklist) == ["spam"], "the word must be stored once"
                assert await forwarded_ids(db, FakeQueue(), "spam again", message_id=2) == []
            finally:
                db.close()

        asyncio.run(run())

    def test_clearing_in_the_menu_stops_the_dropping(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["spam"])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "spam", message_id=1) == []
                stored = await _reload(db, rule)
                _write_through_the_menu(stored, blacklist=[])
                await SqliteForwardRuleRepository(db).update(stored)
                assert await forwarded_ids(db, FakeQueue(), "spam", message_id=2) == [2]
            finally:
                db.close()

        asyncio.run(run())

    def test_a_word_only_matches_whole_words(self, tmp_path):
        """The policy's documented default: `spam` does not match `spammer`."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["spam"])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "the spammer arrived") == [1]
                assert await forwarded_ids(db, FakeQueue(), "the spam arrived", message_id=2) == []
            finally:
                db.close()

        asyncio.run(run())

    def test_matching_ignores_letter_case(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["spam"])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "SPAM SPAM SPAM") == []
            finally:
                db.close()

        asyncio.run(run())

    def test_a_persian_word_matches_its_diacritic_and_letter_variants(self, tmp_path):
        """`normalize` folds diacritics and the Arabic/Farsi letter forms, so a
        keyword still catches the same word typed another way."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["تست"])
                await seed(db, rule)
                # The diacritic (ُ) is folded away before matching.
                assert await forwarded_ids(db, FakeQueue(), "این یک تُست است") == []
                # The plain spelling matches too.
                assert await forwarded_ids(db, FakeQueue(), "تست", message_id=2) == []
                # ... and an unrelated word is not caught by the variant machinery.
                assert await forwarded_ids(db, FakeQueue(), "این یک پیام است", message_id=3) == [3]
            finally:
                db.close()

        asyncio.run(run())

    def test_an_empty_entry_never_matches_everything(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["", "   "])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "a perfectly normal post") == [1]
            finally:
                db.close()

        asyncio.run(run())

    def test_a_regex_shaped_entry_is_a_literal_not_a_pattern(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=[".*", "a{1,99999}"])
                await seed(db, rule)
                # A pattern would drop everything here, and `a{1,99999}` would
                # be a ReDoS lever; as literals they match nothing in this text.
                assert await forwarded_ids(db, FakeQueue(), "nothing to see") == [1]
                assert await forwarded_ids(db, FakeQueue(), "literal .* here", message_id=2) == []
            finally:
                db.close()

        asyncio.run(run())

    def test_one_tasks_blacklist_cannot_suppress_another_tasks_message(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                first = make_rule(RULE_ID, owner_user_id=OWNER, multi_route=True)
                second = make_rule(RULE_ID_2, owner_user_id=OWNER, multi_route=True,
                                   target_chat_id="-1003915313920")
                _write_through_the_menu(first, blacklist=["spam"])
                await seed(db, first, second)
                queue = FakeQueue()
                pipe = build(db, queue)
                await pipe.process(payload(1, "spam"), trigger="NEW_MESSAGE")
                assert [job[0] for job in queue.jobs] == [RULE_ID_2], (
                    "the rule without the word must still deliver"
                )
            finally:
                db.close()

        asyncio.run(run())

    def test_the_menu_renders_the_word_the_engine_enforces(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["spam"])
                await seed(db, rule)
                ui = _ui()
                text, _kbd = ui._render_blacklist_menu(await _reload(db, rule))
                assert "spam" in text
                # ... and after the operator clears it, the menu says so.
                stored = await _reload(db, rule)
                _write_through_the_menu(stored, blacklist=[])
                await SqliteForwardRuleRepository(db).update(stored)
                emptied, _kbd2 = ui._render_blacklist_menu(await _reload(db, rule))
                assert "spam" not in emptied
            finally:
                db.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# STEER 08 — Whitelist
# --------------------------------------------------------------------------- #
class TestTheWhitelistGatesTheWayTheEngineEnforces:
    def test_a_message_without_a_whitelisted_word_is_not_forwarded(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=["gold"])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "just chatting") == []
            finally:
                db.close()

        asyncio.run(run())

    def test_a_message_with_a_whitelisted_word_is_forwarded(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=["gold"])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "GOLD to the moon") == [1]
            finally:
                db.close()

        asyncio.run(run())

    def test_an_empty_whitelist_constrains_nothing(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=[])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "anything at all") == [1]
            finally:
                db.close()

        asyncio.run(run())

    def test_a_whitelist_on_one_task_does_not_block_another(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                first = make_rule(RULE_ID, multi_route=True)
                second = make_rule(RULE_ID_2, multi_route=True,
                                   target_chat_id="-1003915313920")
                _write_through_the_menu(first, whitelist=["gold"])
                await seed(db, first, second)
                queue = FakeQueue()
                pipe = build(db, queue)
                await pipe.process(payload(1, "silver only"), trigger="NEW_MESSAGE")
                assert [job[0] for job in queue.jobs] == [RULE_ID_2]
            finally:
                db.close()

        asyncio.run(run())

    def test_all_mode_requires_every_word(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "wl.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, whitelist=["gold", "signal"])
                rule.whitelist_mode = "all"
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "gold only") == []
                assert await forwarded_ids(db, FakeQueue(), "gold signal", message_id=2) == [2]
            finally:
                db.close()

        asyncio.run(run())


class TestBlacklistWinsOverWhitelist:
    def test_a_word_in_both_lists_is_dropped(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "both.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["scam"], whitelist=["scam"])
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "this is a scam") == []
            finally:
                db.close()

        asyncio.run(run())

    def test_the_master_switch_turns_the_whole_policy_off(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "both.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["scam"], whitelist=["gold"])
                rule.content_policy_enabled = False
                await seed(db, rule)
                # Neither the blacklist nor the whitelist may act while off.
                assert await forwarded_ids(db, FakeQueue(), "scam") == [1]
            finally:
                db.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# One authority, and legacy rows keep working
# --------------------------------------------------------------------------- #
class TestOneAuthorityForTheLists:
    def test_a_menu_write_lands_where_the_engine_reads(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "one.db"))
            try:
                rule = make_rule()
                _write_through_the_menu(rule, blacklist=["spam"], whitelist=["gold"])
                await seed(db, rule)
                stored = await _reload(db, rule)
                assert list(stored.blacklist_words) == ["spam"]
                assert list(stored.whitelist_words) == ["gold"]
            finally:
                db.close()

        asyncio.run(run())

    def test_rows_written_before_the_fix_are_still_enforced(self, tmp_path):
        """A task whose words live in the legacy keys must not start leaking."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "legacy.db"))
            try:
                rule = make_rule()
                rule.metadata["blacklist"] = ["spam"]
                rule.metadata["whitelist"] = []
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "buy spam") == []
            finally:
                db.close()

        asyncio.run(run())

    def test_a_legacy_whitelist_row_is_still_enforced(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "legacy.db"))
            try:
                rule = make_rule()
                rule.metadata["whitelist"] = ["gold"]
                await seed(db, rule)
                assert await forwarded_ids(db, FakeQueue(), "silver only here") == []
                assert await forwarded_ids(db, FakeQueue(), "gold here", message_id=2) == [2]
            finally:
                db.close()

        asyncio.run(run())

    def test_a_malformed_policy_never_crashes_the_pipeline(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "bad.db"))
            try:
                rule = make_rule()
                rule.metadata["content_policy"] = {
                    "blacklist_words": {"not": "a list"},
                    "whitelist_words": 12345,
                    "whitelist_mode": "nonsense",
                }
                await seed(db, rule)
                # The coercion drops what it cannot read; nothing is matched,
                # nothing is dropped, and the message still forwards.
                assert await forwarded_ids(db, FakeQueue(), "looks normal") == [1]
                assert rule.content_policy.whitelist_mode == "any"
            finally:
                db.close()

        asyncio.run(run())



def _ui():
    """A bot-ui instance for the pure render helper (a loop must exist first)."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from core.infrastructure.telegram.i18n import I18n
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI

    ui = ProBotUI.__new__(ProBotUI)
    ui.i18n = I18n()
    return ui
