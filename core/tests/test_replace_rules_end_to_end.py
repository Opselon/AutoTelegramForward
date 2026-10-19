"""STEER 06 — Replace: the editor, the engine, and who owns them.

Three confirmed defects are measured here through the real objects:

1. the durable pipeline applied the task's replacement rules **twice** — once
   before the AI stage and once after it. `str.replace` is not idempotent for an
   expanding rule, so `# -> ##` sent `####gold` for a `#gold` source;
2. the editor's delete button addressed the entry by *position*
   (`rtxd:<rule>:<idx>`), so a click from a menu rendered before another edit
   deleted whichever rule had meanwhile moved into that slot — silently, with
   the wrong content removed from every forwarded message;
3. the editor's own entry points (`rtx:` / `rtxa:`) were behind an
   installation-admin gate, so a task owner could not open their own
   replacement rules at all, while the rule they addressed was read unscoped
   from client-supplied callback data.

Replacement syntax is literal, case-sensitive-first, then a case-insensitive
fallback for handles and words — that behaviour is unchanged and pinned below,
because STEER 06 requires backward compatibility.
"""

from __future__ import annotations

import asyncio
import ast
import pathlib
import re

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import ForwardRule, MessagePayload
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
)

BOT_UI_PATH = pathlib.Path(__file__).resolve().parents[1] / "infrastructure" / "telegram" / "pro_bot_ui.py"

RULE_ID = "rule-rep-1"
RULE_ID_2 = "rule-rep-2"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"
TARGET_2 = "-1003915313920"
OWNER = 5094837833
OTHER_TENANT = 6278059256


class FakeQueue:
    def __init__(self):
        self.jobs: list[tuple[str, int, str, str]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        return True


class FakePool:
    def get_client(self, *a, **kw):  # pragma: no cover
        return None

    async def send_message(self, *a, **kw):  # pragma: no cover
        raise AssertionError("the pipeline must not talk to Telegram directly here")


def make_rule(rule_id: str = RULE_ID, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=OWNER,
    )
    rule.multi_route = True
    for k, v in kwargs.items():
        setattr(rule, k, v)
    return rule


def build(db, queue):
    from core.infrastructure.persistence.sqlite_repositories import SqliteFilterRuleRepository

    return DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )


async def seed(db, *rules):
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


def payload(message_id: int, text: str = "", chat_id: str = SOURCE, caption: str = "") -> MessagePayload:
    if caption:
        from core.domain.entities import MediaType
        return MessagePayload(message_id=message_id, chat_id=chat_id, text=text, caption=caption,
                              has_media=True, media_type=MediaType.PHOTO)
    return MessagePayload(message_id=message_id, chat_id=chat_id, text=text, caption=caption)


async def forward_once(db, queue, rule, message_id, text="", caption=""):
    """Drive the real pipeline for one message and return what it queued."""
    await seed(db, rule)
    pipeline = build(db, queue)
    await pipeline.process(payload(message_id, text=text, caption=caption), trigger="NEW_MESSAGE")
    return [j for j in queue.jobs if j[0] == rule.id]


# --------------------------------------------------------------------------- #
# 1. One authority: a replacement is applied exactly once
# --------------------------------------------------------------------------- #
class TestReplacementIsAppliedExactlyOnce:
    def test_an_expanding_rule_is_not_applied_twice(self, tmp_path):
        """`# -> ##` on `#gold` must send `##gold`, not `####gold`."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "rep.db"))
            try:
                queue = FakeQueue()
                rule = make_rule(replacements={"#": "##"})
                jobs = await forward_once(db, queue, rule, 701, text="#gold")
                assert len(jobs) == 1
                assert jobs[0][3] == "##gold", f"applied more than once: {jobs[0][3]!r}"
            finally:
                db.close()

        asyncio.run(run())

    def test_a_plain_replacement_is_applied_once(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "rep2.db"))
            try:
                queue = FakeQueue()
                rule = make_rule(replacements={"binance": "exchange"})
                jobs = await forward_once(db, queue, rule, 702, text="join binance now")
                assert jobs[0][3] == "join exchange now"
            finally:
                db.close()

        asyncio.run(run())

    def test_the_case_insensitive_fallback_is_not_applied_a_second_time(self, tmp_path):
        """Exact match wins; the fallback must not then re-scan the whole text.

        `Gold -> silver` on `Gold and GOLD` sends `silver and GOLD`. A second
        pass re-runs the fallback over the already-replaced text and turns both
        occurrences into `silver`.
        """

        async def run():
            db = SqliteDatabase(str(tmp_path / "rep3.db"))
            try:
                queue = FakeQueue()
                rule = make_rule(replacements={"Gold": "silver"})
                jobs = await forward_once(db, queue, rule, 703, text="Gold and GOLD")
                assert jobs[0][3] == "silver and GOLD"
            finally:
                db.close()

        asyncio.run(run())

    def test_the_caption_is_replaced_once(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "rep4.db"))
            try:
                queue = FakeQueue()
                rule = make_rule(replacements={"#": "##"})
                jobs = await forward_once(db, queue, rule, 704, caption="#gold")
                assert jobs[0][3] == "##gold"
            finally:
                db.close()

        asyncio.run(run())

    def test_the_new_message_path_agrees_with_the_edit_path(self):
        """A forwarded message and its later edit must render identically."""
        from core.domain.services import FilterEngine

        engine = FilterEngine()
        rule = make_rule(replacements={"#": "##", "binance": "exchange"})
        text = "#gold on binance"
        assert engine.transform_text(text, rule) == "##gold on exchange"

    def test_repeated_stages_do_not_compound_a_replacement(self, tmp_path):
        """Ten rules in a row: each is applied once, in insertion order."""

        async def run():
            db = SqliteDatabase(str(tmp_path / "rep5.db"))
            try:
                queue = FakeQueue()
                rule = make_rule(replacements={"a": "b", "b": "c", "c": "d"})
                jobs = await forward_once(db, queue, rule, 705, text="a b c")
                # sequential, insertion-ordered, one pass:
                #   a->b : "b b c"   b->c : "c c c"   c->d : "d d d"
                assert jobs[0][3] == "d d d"
            finally:
                db.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# 2. Backward compatibility of the rule syntax
# --------------------------------------------------------------------------- #
class TestTheSyntaxIsUnchanged:
    def test_an_exact_match_is_case_sensitive_and_wins(self):
        from core.domain.services import FilterEngine

        engine = FilterEngine()
        assert engine.replace_text("Gold GOLD gold", {"Gold": "X"}) == "X GOLD gold"

    def test_the_fallback_matches_handles_case_insensitively(self):
        from core.domain.services import FilterEngine

        engine = FilterEngine()
        assert engine.replace_text("@channel", {"@CHANNEL": "@mychannel"}) == "@mychannel"

    def test_an_empty_search_string_never_mangles_the_message(self):
        """`str.replace("", x)` would interleave the replacement between every
        character; the engine must ignore such an entry."""
        from core.domain.services import FilterEngine

        engine = FilterEngine()
        assert engine.replace_text("hello", {"": "X"}) == "hello"
        assert engine.replace_text("hello", {"  ": "X"}) == "hello"

    def test_a_replacement_is_literal_not_a_regex(self):
        """User input is never compiled as a pattern (no ReDoS, no surprise)."""
        from core.domain.services import FilterEngine

        engine = FilterEngine()
        out = engine.replace_text("a" * 5 + " (x)", {"(": "["})
        assert out == "aaaaa [x)"
        # a catastrophic pattern is just a literal that does not match
        assert engine.replace_text("aaaa", {"(a+)+$": "BOOM"}) == "aaaa"

    def test_unicode_and_rtl_replacements_survive(self):
        from core.domain.services import FilterEngine

        engine = FilterEngine()
        assert engine.replace_text("سلام دنیا", {"دنیا": "جهان"}) == "سلام جهان"


# --------------------------------------------------------------------------- #
# 3. Scoping and persistence
# --------------------------------------------------------------------------- #
class TestReplacementScoping:
    def test_one_tasks_replacement_never_touches_another_task(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "rep6.db"))
            try:
                queue = FakeQueue()
                rule_a = make_rule(replacements={"#": "##"})
                rule_b = make_rule(RULE_ID_2, replacements={"#": "***"})
                rule_b.target_chat_id = TARGET_2
                await seed(db, rule_a, rule_b)
                pipeline = build(db, queue)
                await pipeline.process(payload(706, text="#gold"), trigger="NEW_MESSAGE")
                by_rule = {j[0]: j[3] for j in queue.jobs}
                assert by_rule[RULE_ID] == "##gold"
                assert by_rule[RULE_ID_2] == "***gold"
            finally:
                db.close()

        asyncio.run(run())

    def test_the_rules_survive_a_persistence_round_trip_in_order(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "rep7.db"))
            try:
                rule = make_rule(replacements={"one": "1", "two": "2", "three": "3"})
                await seed(db, rule)
                loaded = await SqliteForwardRuleRepository(db).get_by_id(rule.id)
                assert loaded is not None
                assert list(loaded.replacements.items()) == [
                    ("one", "1"), ("two", "2"), ("three", "3")
                ]
            finally:
                db.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# 4. The editor belongs to the owner, and a stale click is not a delete
# --------------------------------------------------------------------------- #
class TestTheReplaceEditor:
    def _handlers(self):
        src = BOT_UI_PATH.read_text()
        tree = ast.parse(src)
        out = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                out.setdefault(node.name, (node, src))
        return out

    def test_no_rule_handler_is_admin_gated_or_reads_a_rule_unscoped(self):
        """Any handler that resolves a rule id from callback data must fetch it
        through the ownership guard — never through `self._rules.get(...)`, and
        never behind an installation-admin gate."""
        offenders = []
        for name, (fn, src) in self._handlers().items():
            if not name.startswith("_cb_"):
                continue
            body = ast.get_source_segment(src, fn) or ""
            if "rule_id" not in body:
                continue
            unscoped = "self._rules.get(" in body
            admin = "_is_admin(cq" in body
            if unscoped or admin:
                offenders.append(name)
        assert offenders == [], f"owner-facing rule handlers must be owner-scoped: {offenders}"

    def test_the_replace_entry_points_are_owner_scoped(self):
        handlers = self._handlers()
        for name in ("_cb_rule_replace_menu", "_cb_rule_replace_add", "_cb_rule_replace_del"):
            fn, src = handlers[name]
            body = ast.get_source_segment(src, fn) or ""
            assert "_owned_rule(" in body, f"{name} must fetch through the ownership guard"
            assert "_is_admin(" not in body, f"{name} must not require admin rights"

    def test_the_delete_click_carries_the_list_generation(self):
        """A positional delete is only safe while the list it was rendered from
        is still current."""
        src = BOT_UI_PATH.read_text()
        assert re.search(r'\^rtxd:\(\.\+\):\(\\d\+\):\(\\d\+\)\$', src), (
            "the delete callback must carry the rule version as well as the index"
        )
        fn, _ = self._handlers()["_cb_rule_replace_del"]
        body = ast.get_source_segment(src, fn) or ""
        assert "_resolve_replace_click(" in body

    def test_a_current_click_resolves_to_its_key(self):
        ui = _ui()
        rule = make_rule(replacements={"one": "1", "two": "2"})
        assert ui._resolve_replace_click(rule, 0, str(rule.version)) == "one"
        assert ui._resolve_replace_click(rule, 1, str(rule.version)) == "two"

    def test_a_stale_click_is_refused_instead_of_deleting_a_neighbour(self):
        """Two menus are open; the other one already deleted index 0. The stale
        click must not delete `two` — it must be refused."""
        ui = _ui()
        rule = make_rule(replacements={"one": "1", "two": "2"})
        stale_version = rule.version
        rule.replacements = {"two": "2"}      # the other menu already applied
        rule.version += 1                      # ... through a real save
        assert ui._resolve_replace_click(rule, 0, str(stale_version)) is None

    def test_an_out_of_range_click_resolves_to_nothing(self):
        ui = _ui()
        rule = make_rule(replacements={"one": "1"})
        assert ui._resolve_replace_click(rule, 7, str(rule.version)) is None
        assert ui._resolve_replace_click(rule, -1, str(rule.version)) is None

    def test_the_stale_alert_exists_in_every_supported_language(self):
        import json

        from core.infrastructure.telegram.i18n import SUPPORTED_LANGUAGES

        path = pathlib.Path(__file__).resolve().parents[1] / "infrastructure" / "telegram" / "locales" / "translations.json"
        catalog = json.loads(path.read_text())
        missing = [lang for lang in SUPPORTED_LANGUAGES if not catalog.get(lang, {}).get("ui_rep_list_changed_alert")]
        assert missing == [], f"missing ui_rep_list_changed_alert for: {missing}"


def _ui():
    """A bot-ui instance with only the pure helpers this test needs.

    The module needs a current event loop at import time; a preceding
    `asyncio.run(...)` leaves none behind, so make sure one exists.
    """
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI

    return ProBotUI.__new__(ProBotUI)
