"""The single media/content gate every forwarding path applies.

A task can say "don't forward video" in two ways: the six legacy block flags
the bot panel and the dashboard have always written (voice, audio, video,
stickers, text, links), or an explicit allow-list (``allowed_media_types`` —
a real ``forward_rules`` column the REST API and backups already carry, which
until now only the *filter-rule* engine read).

Keeping the checks inline in ``DurableMessagePipeline`` made the two paths
disagree: the reply/PV use case enforced voice and stickers and silently
forwarded video, audio, text and links the owner had blocked. One decision
function is what stops that drift, so every path calls ``media_block_reason``
and logs the code it returns.
"""

from typing import Any, Iterable, Optional

from .content_policy import has_link

__all__ = [
    "MEDIA_TYPES",
    "normalise_media_type",
    "media_block_reason",
    "link_block_reason",
]

#: Message types an allow-list may name. ``video`` covers ``video_note`` and
#: ``voice``/``audio`` are distinct on Telegram but both mean "sound" to an
#: owner ticking a box; the allow-list is exact, so it lists them separately.
MEDIA_TYPES = (
    "text",
    "photo",
    "video",
    "video_note",
    "document",
    "audio",
    "voice",
    "animation",
    "sticker",
    "poll",
    "location",
    "contact",
    "venue",
    "dice",
    "game",
    "invoice",
    "story",
    "giveaway",
    "service",
    "other",
)

#: flag -> (media types it forbids, reason code). A voice note is audio and a
#: round video is a video: the owner ticked a category, not a Telegram enum, so
#: each flag covers the pair that looks like that category in the app.
_BLOCK_FLAGS = (
    ("block_voice", ("voice", "audio"), "voice_blocked"),
    ("block_audio", ("audio", "voice"), "audio_blocked"),
    ("block_video", ("video", "video_note"), "video_blocked"),
    ("block_stickers", ("sticker",), "sticker_blocked"),
    ("block_text", ("text",), "text_blocked"),
)


def normalise_media_type(media: Any) -> str:
    """``'VIDEO'``, ``MediaType.VIDEO`` and ``MediaType`` all become ``video``."""
    return str(getattr(media, "value", media) or "").strip().lower()


def _allowed_types(rule: Any) -> Iterable[str]:
    raw = getattr(rule, "allowed_media_types", None) or ()
    if isinstance(raw, str):  # a hand-edited row, not the JSON list the API writes
        raw = [part for part in raw.replace("\n", ",").split(",")]
    return tuple(normalise_media_type(item) for item in raw if str(item).strip())


def media_block_reason(rule: Any, media: Any) -> Optional[str]:
    """Reason code when this task forbids this message type, else ``None``.

    The allow-list is checked first: "forward only photos" is a stronger
    statement than "don't forward video", and the reason it returns names the
    type so the log says why a particular message was skipped.
    """
    m_type = normalise_media_type(media)
    allowed = _allowed_types(rule)
    if allowed and m_type not in allowed:
        return f"media_not_allowed:{m_type}"
    for flag, covered, reason in _BLOCK_FLAGS:
        if getattr(rule, flag, False) and m_type in covered:
            return reason
    return None


def link_block_reason(rule: Any, text: Optional[str]) -> Optional[str]:
    """Reason code when the task blocks link-bearing messages, else ``None``."""
    if getattr(rule, "block_links", False) and has_link(text or ""):
        return "link_blocked"
    return None
