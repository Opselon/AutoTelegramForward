"""STEER — message-type filters must be enforced identically on every path.

The bot's Filters screen (`rfl:`) writes six block flags and the REST API can
set an allow-list, but before this change only two of the six flags were
enforced by the durable pipeline and only two by the reply/PV use case — so a
task whose owner blocked video, audio or text kept forwarding them, and the
bot's own "test rule" preview disagreed with the engine.

These tests pin one decision (`core.domain.media_filter`) shared by the
pipeline, the use case and the preview, and drive the real pipeline against the
real SQLite store for each type.
"""

from __future__ import annotations

import asyncio

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import ForwardRule, MessagePayload, TelegramSession
from core.domain.media_filter import media_block_reason, normalise_media_type
from core.domain.services import SmartRoutingEngine
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

OWNER = 5094837833
SOURCE = "-1004400615270"
TARGET = "-1003915313919"


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, payload.message_id, str(target_chat_id), text))
        return True


def make_rule(rule_id: str = "rule-media-1", **kwargs) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id,
        session_id="sess-a",
        source_chat_id=SOURCE,
        target_chat_id=TARGET,
        is_active=True,
        owner_user_id=OWNER,
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


def payload(message_id: int = 1, text: str = "", media_type: MediaType = MediaType.TEXT,
            **kwargs) -> MessagePayload:
    return MessagePayload(message_id=message_id, chat_id=SOURCE, text=text,
                          media_type=media_type, **kwargs)


async def delivered(db, queue, message) -> list[str]:
    queue.jobs.clear()
    await build(db, queue).process(message, trigger="NEW_MESSAGE")
    return [job[3] for job in queue.jobs]


# --------------------------------------------------------------------------
# The decision itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag,media,expected", [
    ("block_video", MediaType.VIDEO, "video_blocked"),
    ("block_video", MediaType.VIDEO_NOTE, "video_blocked"),
    ("block_audio", MediaType.AUDIO, "audio_blocked"),
    ("block_audio", MediaType.VOICE, "audio_blocked"),
    ("block_voice", MediaType.VOICE, "voice_blocked"),
    ("block_voice", MediaType.AUDIO, "voice_blocked"),
    ("block_stickers", MediaType.STICKER, "sticker_blocked"),
    ("block_text", MediaType.TEXT, "text_blocked"),
])
def test_each_flag_blocks_its_category(flag, media, expected):
    rule = make_rule(**{flag: True})
    assert media_block_reason(rule, media) == expected


@pytest.mark.parametrize("flag,media", [
    ("block_video", MediaType.PHOTO),
    ("block_audio", MediaType.DOCUMENT),
    ("block_stickers", MediaType.ANIMATION),
    ("block_text", MediaType.PHOTO),
])
def test_a_flag_does_not_block_other_types(flag, media):
    rule = make_rule(**{flag: True})
    assert media_block_reason(rule, media) is None


def test_allow_list_is_exact_and_wins_over_flags():
    rule = make_rule(allowed_media_types=["photo"])
    assert media_block_reason(rule, MediaType.VIDEO) == "media_not_allowed:video"
    assert media_block_reason(rule, MediaType.PHOTO) is None
    # A block flag on an allowed type does not fire: the owner said "only photo".
    rule.block_video = True
    assert media_block_reason(rule, MediaType.PHOTO) is None


def test_allow_list_accepts_the_enum_and_plain_strings():
    rule = make_rule(allowed_media_types=[MediaType.TEXT, "PHOTO"])
    assert media_block_reason(rule, "text") is None
    assert media_block_reason(rule, MediaType.PHOTO) is None


def test_allow_list_tolerates_a_hand_edited_string():
    rule = make_rule()
    rule.metadata["allowed_media_types"] = "text,photo"
    # The column, not metadata, is authoritative.
    rule.allowed_media_types = []
    assert media_block_reason(rule, MediaType.VIDEO) is None
    rule.allowed_media_types = "text,photo"
    assert media_block_reason(rule, MediaType.VIDEO) == "media_not_allowed:video"


def test_preview_agrees_with_the_gate():
    """`FilterEngine.evaluate_rule` is what the bot and gRPC previews call."""
    engine = SmartRoutingEngine()
    for flag, media, reason in [
        ("block_video", MediaType.VIDEO, "video_blocked"),
        ("block_audio", MediaType.AUDIO, "audio_blocked"),
        ("block_text", MediaType.TEXT, "text_blocked"),
    ]:
        rule = make_rule(**{flag: True})
        matched, got = engine.evaluate_rule(payload(media_type=media), rule)
        assert matched is False and got == reason


# --------------------------------------------------------------------------
# The real pipeline
# --------------------------------------------------------------------------

def test_pipeline_drops_every_blocked_type_and_keeps_the_rest(tmp_path):
    db = SqliteDatabase(str(tmp_path / "atf.db"))
    queue = FakeQueue()
    rule = make_rule(
        block_video=True, block_audio=True, block_stickers=True, block_text=True,
    )
    asyncio.run(seed(db, rule))

    assert asyncio.run(delivered(db, queue, payload(1, media_type=MediaType.VIDEO))) == []
    assert asyncio.run(delivered(db, queue, payload(2, media_type=MediaType.VIDEO_NOTE))) == []
    assert asyncio.run(delivered(db, queue, payload(3, media_type=MediaType.AUDIO))) == []
    assert asyncio.run(delivered(db, queue, payload(4, media_type=MediaType.VOICE))) == []
    assert asyncio.run(delivered(db, queue, payload(5, media_type=MediaType.STICKER))) == []
    assert asyncio.run(delivered(db, queue, payload(6, media_type=MediaType.TEXT))) == []
    # A photo is not in any of the four categories.
    assert asyncio.run(delivered(db, queue, payload(7, media_type=MediaType.PHOTO))) != []


def test_pipeline_allow_list_forwards_only_listed_types(tmp_path):
    db = SqliteDatabase(str(tmp_path / "atf.db"))
    queue = FakeQueue()
    rule = make_rule(allowed_media_types=["text", "photo"])
    asyncio.run(seed(db, rule))

    assert asyncio.run(delivered(db, queue, payload(1, media_type=MediaType.TEXT))) != []
    assert asyncio.run(delivered(db, queue, payload(2, media_type=MediaType.PHOTO))) != []
    assert asyncio.run(delivered(db, queue, payload(3, media_type=MediaType.VIDEO))) == []
    assert asyncio.run(delivered(db, queue, payload(4, media_type=MediaType.DOCUMENT))) == []


def test_one_tasks_filters_do_not_leak_to_another(tmp_path):
    db = SqliteDatabase(str(tmp_path / "atf.db"))
    queue = FakeQueue()
    blocking = make_rule(rule_id="rule-blocking", block_video=True)
    allowing = make_rule(rule_id="rule-allowing")
    asyncio.run(seed(db, blocking, allowing))

    # The allow-list task sees the same message; only the blocking task drops it.
    jobs = asyncio.run(delivered(db, queue, payload(media_type=MediaType.VIDEO)))
    assert len(jobs) == 1


def test_filters_survive_a_draft_round_trip():
    rule = make_rule(
        block_video=True, block_audio=True, block_text=True, block_links=True,
    )
    rule.allowed_media_types = ["text"]
    draft = rule.to_draft()

    restored = ForwardRule(id="rule-restored", session_id="sess-a", owner_user_id=OWNER)
    restored.apply_draft(draft)

    assert restored.block_video and restored.block_audio and restored.block_text
    assert restored.block_links
    assert restored.allowed_media_types == ["text"]
    assert restored.to_draft()["allowed_media_types"] == ["text"]


def test_registry_round_trips_the_allow_list():
    from core.domain.rule_settings import apply_settings, read_settings

    rule = make_rule()
    changed = apply_settings(rule, {"allowed_media_types": ["text", "photo"]})
    assert "allowed_media_types" in changed
    assert read_settings(rule)["allowed_media_types"] == ["text", "photo"]
