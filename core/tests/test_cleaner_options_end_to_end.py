"""STEER 09 — Cleaner: every option in the menu must change the outgoing message.

The cleaner menu (`rcl:` ...) offers a master switch plus eight options. The
master switch and the options are stored on the rule (`cleaner_enabled`,
`remove_links`, `clean_usernames`, `clean_hashtags`, `remove_emojis`,
`clean_custom_words`, `clean_phones`, `clean_buttons`, `clean_empty_lines`) and
the forwarding engine materialises them through `ForwardRule.build_cleaner()` in
`DurableMessagePipeline._apply_transforms`.

Measured on the pre-fix code, driving the real pipeline against the real SQLite
store:

* `clean_phones`, `clean_buttons` and `clean_empty_lines` were stored, rendered
  and counted, and changed nothing in the outgoing message — they are not part of
  `build_cleaner()`'s config and have no branch in `CleanerRule.clean()`;
* the `Links: Remove` option (`remove_links`) and any non-default `link_policy`
  **dropped every message**: `_apply_transforms` called
  `LinkManagementEngine.apply_to_payload`, a method that does not exist, and the
  surrounding `except Exception` turned the `AttributeError` into a dropped
  message with no reason code. `CleanerRule`'s own docstring promises the
  cleaner "never drops a message".

These tests pin the fixed behaviour: every documented option has an observable
effect, the cleaner never drops a message, an unknown link policy fails closed to
"preserve", and each task's cleaner only touches its own messages.
"""

from __future__ import annotations

import asyncio
import logging
import re

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import ForwardRule, MessagePayload, TelegramSession
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService

# Imported at module scope on purpose: importing `pro_bot_ui` pulls in pyrogram,
# which calls `asyncio.get_event_loop()` while it imports, and by the time a test
# body runs an earlier `asyncio.run()` has already closed that loop.
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

OWNER = 5094837833
RULE_ID = "rule-clean-1"
RULE_ID_2 = "rule-clean-2"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"
PHONE = "+989123456789"


class FakeQueue:
    """Records accepted deliveries, keeping the payload for later inspection."""

    def __init__(self):
        self.jobs: list[tuple[str, int, str, str]] = []
        self.payloads: list[MessagePayload] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        self.payloads.append(payload)
        return True


class FakeMarkup:
    """Stands in for an inline keyboard attached to the source message."""

    def __init__(self):
        self.inline_keyboard = [[{"text": "Join", "url": "https://t.me/x"}]]


def make_rule(rule_id: str = RULE_ID, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=OWNER,
    )
    # One gate per test: a duplicate-filter match would hide the cleaner's effect.
    rule.duplicate_filters = False
    for key, value in kwargs.items():
        setattr(rule, key, value)
    return rule


def build(db, queue) -> DurableMessagePipeline:
    return DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(db),
        filter_repo=SqliteFilterRuleRepository(db),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(db),
        processed_repo=SqliteProcessedMessageRepository(db),
    )


async def seed(db, *rules) -> None:
    await SqliteSessionRepository(db, CryptoService("test-key")).add(
        TelegramSession(id="sess-a", phone_number="+10000000000", is_active=True,
                        is_authorized=True, owner_user_id=OWNER)
    )
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def payload(message_id: int = 1, text: str = "", chat_id: str = SOURCE, **kwargs) -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=chat_id, text=text, **kwargs)


async def delivered(db, queue, message) -> list[str]:
    queue.jobs.clear()
    queue.payloads.clear()
    pipe = build(db, queue)
    await pipe.process(message, trigger="NEW_MESSAGE")
    return [job[3] for job in queue.jobs]


async def delivered_text(db, queue, text: str) -> str:
    jobs = await delivered(db, queue, payload(text=text))
    return jobs[0] if jobs else "<DROPPED>"


def _write_toggle_through_the_menu(rule: ForwardRule, name: str, value) -> None:
    """The exact write the bot's cleaner toggles perform (``rcl:`` callbacks)."""
    setattr(rule, name, value)


# --------------------------------------------------------------------------- #
# Every option the menu offers must change the outgoing message
# --------------------------------------------------------------------------- #
class TestEachDocumentedOptionReachesTheOutgoingMessage:
    def test_without_the_cleaner_nothing_is_removed(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                await seed(db, make_rule())
                text = f"Buy now https://t.me/ad @seller #gold \N{CHART WITH UPWARDS TREND} {PHONE}"
                assert await delivered_text(db, FakeQueue(), text) == text
            finally:
                db.close()

        asyncio.run(run())

    def test_mentions_are_removed(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_usernames", True)
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "hey @seller see this")
                assert "@seller" not in out
                assert "hey" in out and "see this" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_hashtags_are_removed(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_hashtags", True)
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "gold #signal #vip now")
                assert "#" not in out
                assert "gold" in out and "now" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_emojis_are_removed(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "remove_emojis", True)
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "gold \N{CHART WITH UPWARDS TREND}\N{FIRE} now")
                assert "\N{CHART WITH UPWARDS TREND}" not in out
                assert "\N{FIRE}" not in out
                assert "gold" in out and "now" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_custom_words_are_removed(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_custom_words", ["Buy now", "promo"])
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "Buy now this promo deal")
                assert "Buy now" not in out and "promo" not in out
                assert "this" in out and "deal" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_phone_numbers_are_removed(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_phones", True)
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), f"call me on {PHONE} today")
                assert PHONE not in out
                assert "call me on" in out and "today" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_extra_empty_lines_are_removed(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_empty_lines", True)
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "first\n\n\n\nsecond")
                assert "\n\n" not in out
                assert "first" in out and "second" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_extra_empty_lines_are_kept_when_the_option_is_off(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_usernames", True)  # cleaner on, option off
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "first\n\n\n\nsecond")
                assert "\n" in out  # a paragraph break survives
                assert "first" in out and "second" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_inline_buttons_are_removed_and_the_message_still_goes_out(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_buttons", True)
                await seed(db, rule)
                queue = FakeQueue()
                message = payload(text="deal inside", reply_markup=FakeMarkup())
                jobs = await delivered(db, queue, message)
                assert len(jobs) == 1
                assert queue.payloads[0].reply_markup is None
            finally:
                db.close()

        asyncio.run(run())

    def test_buttons_survive_when_the_option_is_off(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_usernames", True)
                await seed(db, rule)
                queue = FakeQueue()
                markup = FakeMarkup()
                await delivered(db, queue, payload(text="deal inside", reply_markup=markup))
                assert queue.payloads[0].reply_markup is markup
            finally:
                db.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# The master switch, and the links option that used to drop every message
# --------------------------------------------------------------------------- #
class TestTheMasterSwitchAndTheLinksOption:
    def test_an_explicitly_disabled_cleaner_stays_off(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                rule.cleaner_enabled = False  # the user turned the master switch off
                _write_toggle_through_the_menu(rule, "clean_usernames", True)
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "hey @seller")
                assert "@seller" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_an_option_on_without_the_master_switch_still_cleans(self, tmp_path):
        """Legacy rows only carry the per-option flags — they must keep working."""
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_hashtags", True)
                await seed(db, rule)
                stored = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
                assert stored is not None and "cleaner_enabled" not in stored.metadata
                out = await delivered_text(db, FakeQueue(), "gold #vip")
                assert "#vip" not in out
            finally:
                db.close()

        asyncio.run(run())

    def test_removing_links_strips_them_and_still_forwards(self, tmp_path):
        """The defect: this option dropped 100% of the task's messages."""
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "remove_links", True)
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "Deal here https://t.me/ad now")
                assert "https://t.me/ad" not in out
                assert "Deal here" in out and "now" in out
            finally:
                db.close()

        asyncio.run(run())

    @pytest.mark.parametrize("policy", ["REMOVE_ALL_URLS", "REMOVE_EXTERNAL_URLS"])
    def test_a_remove_url_policy_strips_the_link_and_forwards(self, tmp_path, policy):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                rule.link_policy = policy
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "Deal here https://example.com/ad now")
                assert "example.com" not in out
                assert "Deal here" in out and "now" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_preserve_all_keeps_the_link(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                await seed(db, make_rule())
                out = await delivered_text(db, FakeQueue(), "Deal here https://example.com/ad now")
                assert "https://example.com/ad" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_an_unknown_link_policy_fails_closed_to_preserve(self, tmp_path):
        """A malformed policy must not silently strip (or drop) content."""
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                rule.link_policy = "REMOVE_ALL"  # not a supported policy value
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "Deal here https://example.com/ad now")
                assert "https://example.com/ad" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_stripping_the_only_content_records_a_reason_instead_of_dropping_silently(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "remove_links", True)
                await seed(db, rule)
                records: list[str] = []

                class Capture(logging.Handler):
                    def emit(self, record):
                        msg = record.getMessage()
                        if "[PIPELINE:" in msg:
                            records.append(msg)

                handler = Capture()
                root = logging.getLogger()
                root.addHandler(handler)
                root.setLevel(logging.INFO)
                try:
                    jobs = await delivered(db, FakeQueue(), payload(text="https://only-links.example/ad"))
                finally:
                    root.removeHandler(handler)

                # Nothing is delivered as an empty message...
                assert jobs == []
                # ...and the drop is explained, not swallowed by a bare except.
                assert any(
                    "empty_after_clean" in r or "EMPTY_AFTER_CLEAN" in r for r in records
                ), records
            finally:
                db.close()

        asyncio.run(run())

    def test_entity_offsets_follow_the_stripped_text(self, tmp_path):
        """A bold run that sits after a removed link must keep covering the text.

        This is the link engine's own contract (UTF-16 offset adjustment): the
        policy removes the URL and reports entities that match the new body.
        """
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                rule.link_policy = "REMOVE_ALL_URLS"  # engine strips; no cleaner involved
                await seed(db, rule)
                queue = FakeQueue()
                text = "https://example.com/ad gold"
                entities = [FakeEntity("bold", offset=23, length=4)]
                message = payload(text=text, entities=entities)
                jobs = await delivered(db, queue, message)
                assert len(jobs) == 1
                kept = queue.payloads[0].entities or []
                assert kept, "the formatting entity must survive link removal"
                body = jobs[0]
                start = kept[0].offset
                assert body[start:start + kept[0].length] == "gold"
            finally:
                db.close()

        asyncio.run(run())

    def test_an_entity_left_past_the_end_of_a_cleaned_body_is_dropped(self, tmp_path):
        """A text-only strip can strand an entity; an out-of-range one must not ship."""
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "remove_links", True)
                await seed(db, rule)
                queue = FakeQueue()
                # The entity points at the URL the cleaner removes.
                message = payload(
                    text="https://example.com/ad",
                    entities=[FakeEntity("bold", offset=0, length=22)],
                )
                jobs = await delivered(db, queue, message)
                if jobs:  # a body survived -> every entity must be in range
                    body = jobs[0]
                    for ent in queue.payloads[0].entities or []:
                        assert ent.offset + ent.length <= len(body)
            finally:
                db.close()

        asyncio.run(run())


class FakeEntity:
    """Minimal stand-in for a pyrogram MessageEntity (UTF-16 offsets)."""

    def __init__(self, type_, offset: int, length: int, url: str = ""):
        self.type = type_
        self.offset = offset
        self.length = length
        self.url = url


# --------------------------------------------------------------------------- #
# Isolation, persistence, failure handling
# --------------------------------------------------------------------------- #
class TestTheCleanerIsScopedAndSurvives:
    def test_one_tasks_cleaner_never_touches_another_task(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                cleaning = make_rule(RULE_ID)
                cleaning.multi_route = True
                _write_toggle_through_the_menu(cleaning, "clean_custom_words", ["secret"])
                untouched = make_rule(RULE_ID_2)
                untouched.multi_route = True
                await seed(db, cleaning, untouched)

                queue = FakeQueue()
                await delivered(db, queue, payload(text="the secret deal"))
                by_rule = {job[0]: job[3] for job in queue.jobs}
                assert set(by_rule) == {RULE_ID, RULE_ID_2}
                assert "secret" not in by_rule[RULE_ID]        # task A cleans
                assert "secret" in by_rule[RULE_ID_2]          # task B keeps it
            finally:
                db.close()

        asyncio.run(run())

    def test_the_options_survive_a_restart(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                for name, value in [("clean_phones", True), ("clean_buttons", True),
                                    ("clean_empty_lines", True), ("remove_links", True)]:
                    _write_toggle_through_the_menu(rule, name, value)
                await seed(db, rule)

                # A fresh repository (a restart) must load the same cleaner.
                reloaded = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
                assert reloaded is not None
                assert reloaded.clean_phones and reloaded.clean_buttons
                assert reloaded.clean_empty_lines and reloaded.remove_links
                cleaner = reloaded.build_cleaner()
                assert cleaner is not None
                assert cleaner.remove_phone_numbers and cleaner.remove_empty_lines
                assert cleaner.strip_buttons

                out = await delivered_text(
                    db, FakeQueue(), f"call {PHONE}\n\n\n\nhttps://t.me/ad now"
                )
                assert PHONE not in out and "t.me/ad" not in out and "\n\n" not in out
            finally:
                db.close()

        asyncio.run(run())

    def test_clearing_every_option_disables_the_cleaner(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_hashtags", False)
                await seed(db, rule)
                stored = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
                assert stored is not None and stored.build_cleaner() is None
            finally:
                db.close()

        asyncio.run(run())

    def test_a_malformed_regex_pattern_cannot_break_forwarding(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                rule.cleaner_config = {"patterns": ["(unclosed"], "use_regex": True}
                rule.cleaner_enabled = True
                await seed(db, rule)
                out = await delivered_text(db, FakeQueue(), "keep (unclosed me")
                assert out != "<DROPPED>"
                assert "keep" in out
            finally:
                db.close()

        asyncio.run(run())

    def test_a_failing_cleaner_delivers_the_message_uncleaned(self, tmp_path, monkeypatch):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_hashtags", True)
                await seed(db, rule)

                from core.domain.entities import CleanerRule

                def explode(self, text):
                    raise RuntimeError("cleaner exploded")

                monkeypatch.setattr(CleanerRule, "clean", explode)
                out = await delivered_text(db, FakeQueue(), "gold #vip")
                assert out != "<DROPPED>"
                assert "#vip" in out  # delivered as-is rather than lost
            finally:
                db.close()

        asyncio.run(run())

    def test_messages_without_text_are_left_alone(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_phones", True)
                await seed(db, rule)
                queue = FakeQueue()
                jobs = await delivered(db, queue, payload(text=""))
                # An empty body is not the cleaner's business; it must not crash.
                assert isinstance(jobs, list)
            finally:
                db.close()

        asyncio.run(run())

    def test_captions_are_cleaned_like_bodies(self, tmp_path):
        async def run():
            db = SqliteDatabase(str(tmp_path / "c.db"))
            try:
                from core.domain.value_objects import MediaType

                rule = make_rule()
                _write_toggle_through_the_menu(rule, "clean_phones", True)
                await seed(db, rule)
                message = payload(
                    text="", caption=f"call {PHONE}", media_type=MediaType.PHOTO,
                    has_media=True,
                )
                jobs = await delivered(db, FakeQueue(), message)
                assert len(jobs) == 1
                assert PHONE not in jobs[0]
            finally:
                db.close()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# i18n — the menu the user sees must be translated everywhere
# --------------------------------------------------------------------------- #
class TestTheCleanerMenuIsTranslated:
    @staticmethod
    def _catalog() -> dict:
        import json
        from pathlib import Path

        path = Path("core/infrastructure/telegram/locales/translations.json")
        if not path.exists():
            path = Path(__file__).resolve().parents[1] / "infrastructure/telegram/locales/translations.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_cleaner_label_exists_in_all_supported_languages(self):
        keys = [
            "ui_clean_btn_urls", "ui_clean_btn_phones", "ui_clean_btn_buttons",
            "ui_clean_btn_empty_lines", "ui_clean_btn_usernames",
            "ui_clean_btn_hashtags", "ui_clean_btn_emojis",
            "ui_clean_btn_custom_words",
            "ui_clean_phones_on", "ui_clean_phones_off",
            "ui_clean_buttons_on", "ui_clean_buttons_off",
            "ui_clean_empty_lines_on", "ui_clean_empty_lines_off",
        ]
        catalog = self._catalog()
        missing = {
            lang: [k for k in keys if not (catalog.get(lang) or {}).get(k)]
            for lang in ("en", "fa", "pt", "uz", "ru", "id", "es", "fr", "it", "zh")
        }
        assert {lang: miss for lang, miss in missing.items() if miss} == {}

    def test_the_new_option_labels_are_not_english_placeholders(self):
        catalog = self._catalog()
        english = catalog["en"]
        for lang in ("fa", "ru", "zh", "pt", "es", "fr", "it", "id", "uz"):
            for key in ("ui_clean_btn_phones", "ui_clean_btn_buttons", "ui_clean_btn_empty_lines"):
                value = catalog[lang][key]
                assert isinstance(value, str) and value.strip()
                if lang == "fa":
                    # Persian must actually be Persian, never the English body.
                    assert re.search(r"[\u0600-\u06FF]", value), (lang, key, value)
                else:
                    assert value != english[key], (lang, key, value)


# --------------------------------------------------------------------------- #
# The badge — the screen's count must be the engine's count
# --------------------------------------------------------------------------- #
class TestTheBadgeCountsWhatTheEngineRuns:
    """The rule list renders `ui_rule_btn_cleaner` with a count of active options.

    Both render sites used to carry their own hand-written subset (neither
    listing phones, buttons, usernames, hashtags or empty lines, and one of them
    not even the master switch), so a task that stripped five things could show
    "cleaner: 1". The count now comes from `ForwardRule.cleaner_active_option_count`
    and this pins it to the engine: the number grows by one per option the
    pipeline really applies, and the master switch still wins.
    """

    def test_the_count_matches_the_options_the_engine_applies(self):
        rule = ForwardRule(id=RULE_ID, owner_user_id=OWNER, name="cleaner badge")
        # Nothing configured -> the engine materialises no cleaner at all.
        assert rule.cleaner_active_option_count == 0
        assert rule.build_cleaner() is None

        rule.cleaner_enabled = True
        # Master switch on, no option chosen -> still nothing to clean.
        assert rule.cleaner_active_option_count == 0

        for index, flag in enumerate(ForwardRule.CLEANER_OPTION_FLAGS, start=1):
            setattr(rule, flag, True)
            assert rule.cleaner_active_option_count == index, flag
            assert rule.build_cleaner() is not None, flag

        rule.clean_custom_words = ["spam", "scam"]
        assert rule.cleaner_active_option_count == len(ForwardRule.CLEANER_OPTION_FLAGS) + 1

        # ...and the master switch still wins over every option.
        rule.cleaner_enabled = False
        assert rule.cleaner_active_option_count == 0
        assert rule.build_cleaner() is None

    def test_the_rendered_button_shows_the_engine_count(self):
        """The label the user reads carries the number, not a stale subset."""
        rule = ForwardRule(id=RULE_ID, owner_user_id=OWNER, name="cleaner badge")
        rule.cleaner_enabled = True
        for flag in ("remove_links", "clean_phones", "clean_buttons", "clean_empty_lines"):
            setattr(rule, flag, True)

        ui = ProBotUI.__new__(ProBotUI)
        ui.i18n = I18n()
        label = ui._t("ui_rule_btn_cleaner", lang="en", cl_count=rule.cleaner_active_option_count)
        assert "4" in label, label
