"""STEER 10 — Filters: every toggle in the screen must gate the real pipeline.

The Filters screen (`rfl:` …) owns six toggles — link, video, audio, sticker,
text, voice — plus the advanced filter rule the web dashboard attaches through
`filter_rule_id` (`FilterRule`: keyword lists, media allow/deny lists, length
bounds, regex patterns). The bot's `rfl_lnk:` / `rfl_vid:` / `rfl_aud:` /
`rfl_stk:` / `rfl_txt:` / `rfl_voc:` callbacks write the flags and
`DurableMessagePipeline`'s FILTER stage enforces them; `FilterEngine.evaluate`
enforces the attached rule.

Measured on the pre-fix code, driving the real pipeline against the real SQLite
store:

* each toggle **did** gate delivery — the gates themselves were sound;
* the `ui_rule_btn_filters` badge, however, counted two different wrong subsets:
  the rule-list site counted `duplicate_filters + block_links + block_voice +
  block_stickers` (video, audio, text missing, and a duplicate-filter toggle that
  belongs to another screen added), and the rule-detail site counted only
  `filter_rule_id` (which the bot never sets), so the same task showed two
  different numbers and neither matched the six toggles the screen owns;
* the attached `FilterRule` was fetched with `get_by_id(rule.filter_rule_id)` —
  by id alone, with no owner check, so a task could be filtered by another
  tenant's rule;
* `FilterEngine` compiled user patterns with a bare `re.compile`, so a
  nested-quantifier pattern (`(a+)+$`) executed against a 4096-character body
  could occupy a worker for minutes.

These tests pin the fixed behaviour: each toggle changes what is delivered, the
badge equals the engine's count, a foreign filter rule is ignored, and a
pathological pattern is refused instead of executed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import FilterRule, ForwardRule, MessagePayload, TelegramSession
from core.domain.value_objects import MediaType
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

OWNER = 5094837833
OTHER_OWNER = 6278059256
RULE_ID = "rule-filter-1"
SOURCE = "-1004400615270"
TARGET = "-1003915313919"
LANGUAGES = ("en", "fa", "pt", "uz", "ru", "id", "es", "fr", "it", "zh")


class FakeQueue:
    """Records accepted deliveries."""

    def __init__(self):
        self.jobs: list[tuple[str, int, str, str]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        return True


def make_rule(rule_id: str = RULE_ID, owner: int = OWNER, **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=owner,
    )
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
        TelegramSession(id="sess-a", phone_number="+100****0000", is_active=True,
                        is_authorized=True, owner_user_id=OWNER)
    )
    repo = SqliteForwardRuleRepository(db)
    for rule in rules:
        await repo.add(rule)


def text_message(text: str = "hello", message_id: int = 1) -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text=text)


def media_message(media: MediaType, caption: str = "", message_id: int = 1) -> MessagePayload:
    return MessagePayload(
        message_id=message_id, chat_id=SOURCE, text="", caption=caption,
        media_type=media, has_media=True,
    )


async def delivered(db, queue, *messages) -> list[tuple]:
    queue.jobs.clear()
    pipe = build(db, queue)
    for message in messages:
        await pipe.process(message, trigger="NEW_MESSAGE")
    return list(queue.jobs)


def run(coro_factory, tmp_path):
    async def runner():
        db = SqliteDatabase(str(tmp_path / "f.db"))
        try:
            await coro_factory(db)
        finally:
            db.close()

    asyncio.run(runner())


# --------------------------------------------------------------------------- #
# The six toggles the screen owns
# --------------------------------------------------------------------------- #
class TestEachToggleGatesWhatTheEngineDelivers:
    """One test per toggle: on = the message is dropped, off = it goes out."""

    def test_by_default_nothing_is_filtered(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await seed(db, make_rule())
            jobs = await delivered(db, queue, text_message("hello"), media_message(MediaType.VIDEO, message_id=2))
            assert len(jobs) == 2

        run(body, tmp_path)

    def test_link_toggle_drops_only_links(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await seed(db, make_rule(block_links=True))
            jobs = await delivered(
                db, queue,
                text_message("buy now https://t.me/ad"),
                text_message("plain words only", message_id=2),
                text_message("join t.me/other here", message_id=3),
                text_message("see www.example.com", message_id=4),
            )
            assert [job[1] for job in jobs] == [2]

        run(body, tmp_path)

    def test_text_toggle_drops_text_and_keeps_media(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await seed(db, make_rule(block_text=True))
            jobs = await delivered(
                db, queue,
                text_message("a plain post"),
                media_message(MediaType.PHOTO, caption="a photo post", message_id=2),
            )
            assert [job[1] for job in jobs] == [2]

        run(body, tmp_path)

    def test_video_toggle_drops_video_and_video_notes(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await seed(db, make_rule(block_video=True))
            jobs = await delivered(
                db, queue,
                media_message(MediaType.VIDEO),
                media_message(MediaType.VIDEO_NOTE, message_id=2),
                media_message(MediaType.PHOTO, message_id=3),
                text_message("words", message_id=4),
            )
            assert [job[1] for job in jobs] == [3, 4]

        run(body, tmp_path)

    def test_audio_toggle_drops_audio(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await seed(db, make_rule(block_audio=True))
            jobs = await delivered(
                db, queue,
                media_message(MediaType.AUDIO),
                media_message(MediaType.PHOTO, message_id=2),
            )
            assert [job[1] for job in jobs] == [2]

        run(body, tmp_path)

    def test_sticker_toggle_drops_stickers(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await seed(db, make_rule(block_stickers=True))
            jobs = await delivered(
                db, queue,
                media_message(MediaType.STICKER),
                media_message(MediaType.ANIMATION, message_id=2),
            )
            assert [job[1] for job in jobs] == [2]

        run(body, tmp_path)

    def test_voice_toggle_drops_voice_notes(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await seed(db, make_rule(block_voice=True))
            jobs = await delivered(
                db, queue,
                media_message(MediaType.VOICE),
                media_message(MediaType.PHOTO, message_id=2),
            )
            assert [job[1] for job in jobs] == [2]

        run(body, tmp_path)

    def test_the_toggles_survive_a_restart(self, tmp_path):
        """The flags are persisted, and the engine honours them after a reload."""
        async def body(db):
            queue = FakeQueue()
            await seed(db, make_rule(block_links=True))
            # Re-read the rule the way a restarted core does.
            stored = await SqliteForwardRuleRepository(db).get_by_id(RULE_ID)
            assert stored.block_links is True
            pipe = build(db, queue)
            await pipe.process(text_message("ad https://t.me/x"), trigger="NEW_MESSAGE")
            assert queue.jobs == []

        run(body, tmp_path)

    def test_one_tasks_filters_never_touch_another_task(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            blocking = make_rule(RULE_ID, block_links=True)
            allowing = make_rule("rule-filter-2")
            allowing.multi_route = True
            blocking.multi_route = True
            await seed(db, blocking, allowing)
            jobs = await delivered(db, queue, text_message("ad https://t.me/x"))
            assert [job[0] for job in jobs] == ["rule-filter-2"], jobs

        run(body, tmp_path)


# --------------------------------------------------------------------------- #
# The attached advanced filter rule
# --------------------------------------------------------------------------- #
class TestTheAttachedFilterRuleReachesTheEngine:
    async def _with_rule(self, db, filter_rule: FilterRule, **rule_kwargs):
        await SqliteFilterRuleRepository(db).add(filter_rule)
        rule = make_rule(**rule_kwargs)
        rule.filter_rule_id = filter_rule.id
        await seed(db, rule)
        return rule

    def test_blacklist_keyword_blocks_the_message(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f1", blacklist_keywords=["spam"], owner_user_id=OWNER))
            jobs = await delivered(db, queue, text_message("buy spam now"), text_message("hello", message_id=2))
            assert [job[1] for job in jobs] == [2]

        run(body, tmp_path)

    def test_whitelist_keyword_requires_a_match(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f2", whitelist_keywords=["gold"], owner_user_id=OWNER))
            jobs = await delivered(db, queue, text_message("gold only"), text_message("silver only", message_id=2))
            assert [job[1] for job in jobs] == [1]

        run(body, tmp_path)

    def test_length_bounds_are_enforced(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f3", min_message_length=5, max_message_length=10, owner_user_id=OWNER))
            jobs = await delivered(
                db, queue,
                text_message("ab", message_id=1),
                text_message("just right", message_id=2),
                text_message("x" * 40, message_id=3),
            )
            assert [job[1] for job in jobs] == [2]

        run(body, tmp_path)

    def test_blocked_and_allowed_media_lists(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f4", blocked_media_types=["photo"], owner_user_id=OWNER))
            jobs = await delivered(db, queue, media_message(MediaType.PHOTO), media_message(MediaType.VIDEO, message_id=2))
            assert [job[1] for job in jobs] == [2]

        run(body, tmp_path)

    def test_a_matching_regex_lets_the_message_through_and_a_mismatch_drops_it(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f5", regex_patterns=[r"^Deal:\s"], owner_user_id=OWNER))
            jobs = await delivered(db, queue, text_message("Deal: 50% off"), text_message("no deal here", message_id=2))
            assert [job[1] for job in jobs] == [1]

        run(body, tmp_path)

    def test_a_malformed_regex_is_ignored_and_never_drops(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f6", regex_patterns=["([unclosed"], owner_user_id=OWNER))
            jobs = await delivered(db, queue, text_message("anything"))
            assert [job[1] for job in jobs] == [1]

        run(body, tmp_path)

    def test_a_pathological_regex_is_refused_instead_of_executed(self, tmp_path):
        """`(a+)+$` against a long body is minutes of backtracking if executed.

        The guard lives in `compile_user_regex` (shared with the content policy),
        so the pattern is refused and the message is delivered; the elapsed time
        assertion is what proves it was never run.
        """
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f7", regex_patterns=[r"(a+)+$"], owner_user_id=OWNER))
            started = time.monotonic()
            jobs = await delivered(db, queue, text_message("a" * 4000 + "b"))
            elapsed = time.monotonic() - started
            assert [job[1] for job in jobs] == [1]
            assert elapsed < 5.0, f"pattern was executed: {elapsed:.2f}s"

        run(body, tmp_path)

    def test_an_oversized_pattern_is_refused(self, tmp_path):
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f8", regex_patterns=["a" * 5000], owner_user_id=OWNER))
            jobs = await delivered(db, queue, text_message("anything"))
            assert [job[1] for job in jobs] == [1]

        run(body, tmp_path)

    def test_another_tenants_filter_rule_is_ignored(self, tmp_path):
        """A task must never be filtered by a rule another account owns."""
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(
                db, FilterRule(id="f9", blacklist_keywords=["spam"], owner_user_id=OTHER_OWNER)
            )
            jobs = await delivered(db, queue, text_message("buy spam now"))
            assert [job[1] for job in jobs] == [1], "a foreign filter rule was applied"

        run(body, tmp_path)

    def test_a_legacy_filter_row_without_an_owner_still_applies(self, tmp_path):
        """Rows written before ownership existed (owner 0) stay the task's own."""
        async def body(db):
            queue = FakeQueue()
            await self._with_rule(db, FilterRule(id="f10", blacklist_keywords=["spam"], owner_user_id=0))
            jobs = await delivered(db, queue, text_message("buy spam now"))
            assert jobs == []

        run(body, tmp_path)


# --------------------------------------------------------------------------- #
# The badge and the screen
# --------------------------------------------------------------------------- #
class TestTheFiltersBadgeAndScreenAgreeWithTheEngine:
    def test_the_count_follows_the_six_toggles_the_screen_owns(self):
        rule = make_rule()
        assert rule.filter_active_option_count == 0
        for index, flag in enumerate(ForwardRule.FILTER_OPTION_FLAGS, start=1):
            setattr(rule, flag, True)
            assert rule.filter_active_option_count == index, flag
        assert set(ForwardRule.FILTER_OPTION_FLAGS) == {
            "block_links", "block_video", "block_audio",
            "block_stickers", "block_text", "block_voice",
        }

    def test_an_attached_advanced_rule_counts_once(self):
        rule = make_rule()
        rule.block_links = True
        rule.filter_rule_id = "f1"
        assert rule.filter_active_option_count == 2

    def test_both_render_sites_use_the_same_count(self, tmp_path):
        """The rule screen and the rule list must not disagree about the number."""
        rule = make_rule()
        rule.block_video = True
        rule.block_text = True
        ui = ProBotUI.__new__(ProBotUI)
        ui.i18n = I18n()
        label = ui._t("ui_rule_btn_filters", lang="en", fl_count=rule.filter_active_option_count)
        assert "2" in label, label
        # The badge value and the screen's own flags come from one list.
        assert rule.filter_active_option_count == sum(
            1 for flag in ForwardRule.FILTER_OPTION_FLAGS if getattr(rule, flag)
        )

    def test_the_screen_names_the_advanced_rule_state(self, tmp_path):
        rule = make_rule()
        ui = ProBotUI.__new__(ProBotUI)
        ui.i18n = I18n()
        text, _kbd = ui._render_filters_menu(rule)
        assert "Advanced filter rule" in text
        assert "No" in text  # nothing attached yet
        rule.filter_rule_id = "f1"
        text_on, _kbd = ui._render_filters_menu(rule)
        assert "Yes" in text_on

    def test_the_new_label_exists_in_every_language_and_is_not_english(self):
        path = Path("core/infrastructure/telegram/locales/translations.json")
        if not path.exists():
            path = Path(__file__).resolve().parents[1] / "infrastructure/telegram/locales/translations.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        english = catalog["en"]["ui_fl_advanced"]
        for lang in LANGUAGES:
            value = (catalog.get(lang) or {}).get("ui_fl_advanced")
            assert value, (lang, "missing ui_fl_advanced")
            assert "{status}" in value, (lang, value)
            if lang != "en":
                assert value != english, (lang, "English placeholder")
        assert "{__" not in english


def test_the_badge_never_counts_a_duplicate_filter():
    """`duplicate_filters` belongs to the duplicate-filter toggle, not this screen."""
    rule = make_rule()
    rule.duplicate_filters = True
    assert rule.filter_active_option_count == 0
