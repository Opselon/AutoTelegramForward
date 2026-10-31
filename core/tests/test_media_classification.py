"""Regression tests for unified media-type classification (E2E-1, E2E-2, E2E-7).

Before the fix the two normalizers of the same pyrogram message drifted apart:

* the live path mapped 14 ``message.media`` names but named ``venue``, ``dice``
  and ``game`` ``OTHER`` and never mapped ``video_note``;
* the replay path scanned 7 attributes, so ``video_note``, ``poll``,
  ``contact``, ``location``, ``venue``, ``dice`` and ``game`` were all
  classified ``text`` with ``has_media=False`` — invisible to every content
  policy decision;
* ``web_page`` (a link preview attached to a text message) was ``OTHER``, so an
  ``allowed_media_types`` gate could drop a plain text post for carrying one.

The consequence was path-dependent: the same message behaved differently
depending on whether it arrived live or through catch-up/replay — the defining
symptom of the reported incident class.
"""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

import pytest

from core.domain.media_kind import media_kind_from_name, resolve_media_type
from core.domain.value_objects import MediaType


class FakeMedia(Enum):
    """Stand-in for pyrogram's MessageMedia enum (by name, as pyrogram uses it)."""

    PHOTO = "photo"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    ANIMATION = "animation"
    DOCUMENT = "document"
    AUDIO = "audio"
    VOICE = "voice"
    STICKER = "sticker"
    POLL = "poll"
    CONTACT = "contact"
    LOCATION = "location"
    VENUE = "venue"
    DICE = "dice"
    GAME = "game"
    WEB_PAGE = "web_page"
    STORY = "story"


def msg(media=None, **attrs):
    """A pyrogram-like message carrying a media enum and/or media attributes."""
    base = dict(text="", caption="", media_group_id=None, service=False,
                edit_date=None, date=0, reply_to_message_id=0)
    base.update(attrs)
    return SimpleNamespace(media=media, **base)


#: Every kind that must be namable, with the attribute that carries it on the
#: replay path (``media`` unset).
KINDS = [
    ("photo", "photo"),
    ("video", "video"),
    ("video_note", "video_note"),
    ("animation", "animation"),
    ("document", "document"),
    ("audio", "audio"),
    ("voice", "voice"),
    ("sticker", "sticker"),
    ("poll", "poll"),
    ("contact", "contact"),
    ("location", "location"),
    ("venue", "venue"),
    ("dice", "dice"),
    ("game", "game"),
]


# --------------------------------------------------------------------------- #
# The shared resolver
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind,attr", KINDS)
def test_the_resolver_names_every_media_kind_from_the_media_enum(kind, attr):
    """The live path's signal: ``message.media`` is already classified."""
    media = FakeMedia[kind.upper().replace("-", "_")]
    assert resolve_media_type(msg(media=media)) == MediaType[kind.upper().replace("-", "_")]


@pytest.mark.parametrize("kind,attr", KINDS)
def test_the_resolver_names_every_media_kind_from_the_attribute(kind, attr):
    """The replay path's signal: the object carries the media attribute only."""
    resolved = resolve_media_type(msg(**{attr: object()}))
    assert resolved == MediaType[kind.upper().replace("-", "_")], (
        f"{kind} was classified as {resolved} when the message carries {attr}"
    )


@pytest.mark.parametrize("kind,attr", KINDS)
def test_both_signals_agree_on_the_same_kind(kind, attr):
    """The drift contract: the enum and the attribute must never disagree."""
    by_enum = resolve_media_type(msg(media=FakeMedia[kind.upper().replace("-", "_")]))
    by_attr = resolve_media_type(msg(**{attr: object()}))
    assert by_enum == by_attr


def test_a_message_without_media_is_text():
    assert resolve_media_type(msg()) == MediaType.TEXT


def test_a_message_with_no_media_enum_but_attributes_is_not_text():
    """has_media follows the resolution, so an unknown kind is never TEXT."""
    assert resolve_media_type(msg(invoice=object())) != MediaType.TEXT


def test_a_link_preview_is_not_a_content_category():
    """E2E-7: a web_page preview rides on a text message; it is TEXT."""
    assert resolve_media_type(msg(media=FakeMedia.WEB_PAGE)) == MediaType.TEXT
    assert media_kind_from_name("web_page") == MediaType.TEXT


def test_text_with_a_preview_keeps_text_classification_both_ways():
    """The replay path has no preview signal at all: a text message stays text."""
    from core.application.pipeline import Normalizer

    payload = Normalizer.normalize(msg(media=FakeMedia.WEB_PAGE, text="hello"))
    assert payload.media_type == MediaType.TEXT
    assert payload.has_media is False


def test_an_unnameable_media_kind_is_other_never_text():
    """Honest degradation: a real but unknown media kind is not silently text."""
    class Future(Enum):
        NEW_MEDIA = "new_media"

    assert resolve_media_type(msg(media=Future.NEW_MEDIA)) == MediaType.OTHER


# --------------------------------------------------------------------------- #
# Both normalizers agree (the actual E2E-2 contract)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind,attr", KINDS)
def test_both_normalizers_agree_on_every_media_kind(kind, attr):
    """The end-to-end contract: live and replay classify identically."""
    from core.application.pipeline import Normalizer
    from core.infrastructure.telegram.client_pool import payload_from_pyrogram

    chat = SimpleNamespace(id=-100, type=SimpleNamespace(name="channel"),
                           title="t", username=None)
    media = FakeMedia[kind.upper().replace("-", "_")]

    live = payload_from_pyrogram(SimpleNamespace(
        id=1, chat=chat, media=media, text="", caption="",
        from_user=None, sender_chat=None, media_group_id=None, service=False,
        edit_date=None, date=0, reply_to_message_id=0, entities=None,
        caption_entities=None, forward_from_chat=None, forward_from=None,
        forward_date=None, via_bot=None, has_media_spoiler=None,
        author_signature=None,
    ))
    replay = Normalizer.normalize(msg(media=media))

    live_kind = live.media_type.value if hasattr(live.media_type, "value") else str(live.media_type)
    replay_kind = replay.media_type.value if hasattr(replay.media_type, "value") else str(replay.media_type)
    assert live_kind == replay_kind == kind, (
        f"live={live_kind} replay={replay_kind} for {kind}"
    )
    assert live.has_media == replay.has_media is True


def test_the_replay_normalizer_flags_media_for_every_kind():
    """E2E-1: has_media must be True for every namable kind on the replay path."""
    from core.application.pipeline import Normalizer

    for kind, attr in KINDS:
        payload = Normalizer.normalize(msg(**{attr: object()}))
        assert payload.has_media is True, f"{kind} is not flagged as media"
        resolved = payload.media_type.value if hasattr(payload.media_type, "value") else str(payload.media_type)
        assert resolved == kind, f"{kind} resolved as {resolved}"


# --------------------------------------------------------------------------- #
# The media gate reads the corrected classification
# --------------------------------------------------------------------------- #

def test_a_task_blocking_video_now_blocks_a_round_video():
    """Before the fix a video_note was TEXT, so block_video let it through
    *when it arrived through catch-up* even while blocking it live."""
    from core.domain.media_filter import media_block_reason

    class Rule:
        block_video = True
        allowed_media_types = ()

    assert media_block_reason(Rule, "video_note") == "video_blocked"
    assert media_block_reason(Rule, "video") == "video_blocked"


def test_a_photo_allow_list_no_longer_admits_a_poll():
    """A poll classified as TEXT passed an allow-list of photos on replay."""
    from core.domain.media_filter import media_block_reason

    class Rule:
        allowed_media_types = ("photo",)

    assert media_block_reason(Rule, "poll") == "media_not_allowed:poll"
    assert media_block_reason(Rule, "photo") is None
