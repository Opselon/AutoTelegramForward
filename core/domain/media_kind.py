"""One authoritative media-type resolver for every ingestion path.

Two normalizers of the same pyrogram message existed and drifted apart — the
live path (``payload_from_pyrogram``) mapped 14 ``message.media`` names, the
replay path (``Normalizer.normalize``) scanned 7 attributes — so 7 media kinds
were invisible on one path or the other, and 3 were mis-named as ``OTHER``.
Which copy of a message got through then depended on *how it arrived*, the
defining signature of the reported incident.

This module is the single resolution point. Both paths call it; nothing else
decides a media type.

Resolution order, deliberately:

1. ``message.media.name`` when pyrogram already classified it — the library's
   own view of the MTProto message, which covers every kind including ones no
   attribute scan would find (``web_page``, ``story``, ``giveaway`` …).
2. An attribute scan over the *full* table, for callers that carry a raw or
   partially-built object where ``media`` is unset but the attribute is present.

``web_page`` is a link preview attached to a **text** message, not a content
category the owner ever decided on: it resolves to TEXT so an
``allowed_media_types`` gate cannot drop a plain text post for carrying a
preview (audit finding E2E-7).
"""

from __future__ import annotations

from typing import Any, Optional

from .value_objects import MediaType

__all__ = ["resolve_media_type", "MEDIA_KINDS"]

#: pyrogram ``message.media`` name -> domain type. This is the authoritative
#: table; there is no second copy anywhere.
_NAME_TO_TYPE = {
    "photo": MediaType.PHOTO,
    "video": MediaType.VIDEO,
    "video_note": MediaType.VIDEO_NOTE,
    "document": MediaType.DOCUMENT,
    "audio": MediaType.AUDIO,
    "voice": MediaType.VOICE,
    "animation": MediaType.ANIMATION,
    "sticker": MediaType.STICKER,
    "poll": MediaType.POLL,
    "location": MediaType.LOCATION,
    "contact": MediaType.CONTACT,
    "venue": MediaType.VENUE,
    "dice": MediaType.DICE,
    "game": MediaType.GAME,
    "story": MediaType.STORY,
    "giveaway": MediaType.GIVEAWAY,
    "invoice": MediaType.INVOICE,
    "service": MediaType.SERVICE,
}

#: Attribute scan, in specificity order. Every kind the live path knows is
#: here, so the two paths cannot disagree again.
_ATTRIBUTE_TO_TYPE = (
    ("photo", MediaType.PHOTO),
    ("video_note", MediaType.VIDEO_NOTE),
    ("animation", MediaType.ANIMATION),
    ("video", MediaType.VIDEO),
    ("audio", MediaType.AUDIO),
    ("voice", MediaType.VOICE),
    ("document", MediaType.DOCUMENT),
    ("sticker", MediaType.STICKER),
    ("poll", MediaType.POLL),
    ("contact", MediaType.CONTACT),
    ("venue", MediaType.VENUE),
    ("location", MediaType.LOCATION),
    ("dice", MediaType.DICE),
    ("game", MediaType.GAME),
    ("invoice", MediaType.INVOICE),
    ("story", MediaType.STORY),
    ("giveaway", MediaType.GIVEAWAY),
)

#: A link preview is not a content decision; it rides on a text message.
PREVIEW_NAMES = frozenset({"web_page"})


def resolve_media_type(message: Any) -> MediaType:
    """Classify one pyrogram message into exactly one :class:`MediaType`.

    Never raises on a shape it does not recognise: an unclassifiable message
    is ``TEXT`` only when it carries no media attribute at all, and ``OTHER``
    when it carries one this table cannot name — the distinction the media
    gate needs, so a genuine-but-unknown media kind is never silently treated
    as text.
    """
    media = getattr(message, "media", None)
    if media is not None:
        name = str(getattr(media, "name", "") or "").strip().lower()
        if name:
            if name in PREVIEW_NAMES:
                return MediaType.TEXT
            found = _NAME_TO_TYPE.get(name)
            if found is not None:
                return found
            # A real media kind the table cannot name: report it honestly
            # rather than guessing text.
            return MediaType.OTHER

    saw_any_attribute = False
    for attr, media_type in _ATTRIBUTE_TO_TYPE:
        if getattr(message, attr, None) is not None:
            return media_type
        saw_any_attribute = saw_any_attribute or attr in (
            "photo", "video", "video_note", "animation", "audio", "voice",
            "document", "sticker", "poll", "contact", "location", "venue",
            "dice", "game", "invoice",
        )
    if getattr(message, "media", None) is not None:
        return MediaType.OTHER
    return MediaType.TEXT


def has_any_media(message: Any) -> bool:
    """True when the message carries any media at all, by either signal.

    ``resolve_media_type`` returning non-TEXT is the same answer; this helper
    exists for the call sites that only need the boolean.
    """
    return resolve_media_type(message) not in (MediaType.TEXT,)


def media_kind_from_name(name: Optional[str]) -> Optional[MediaType]:
    """Resolve a bare media kind name (``message.media.name``).

    Kept separate from :func:`resolve_media_type` because some call sites read
    only the name; the preview rule applies here too.
    """
    if not name:
        return None
    cleaned = str(name).strip().lower()
    if cleaned in PREVIEW_NAMES:
        return MediaType.TEXT
    return _NAME_TO_TYPE.get(cleaned)
