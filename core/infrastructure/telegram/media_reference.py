"""Protected-content media bypass primitives shared by every delivery path.

Channels that set "restrict forwarding/saving" make server-side
forward_messages/copy_message fail with ChatForwardsRestricted. Telegram
still hands the *content itself* to any member session, so the message can
be rebuilt from its parts. The InputMedia reference (photo/file id) is the
highest-fidelity path: same quality, no disk I/O, and it survives when
downloading is also blocked.

The dispatcher (live path) and the durable queue (worker path) previously
kept private copies of this logic and drifted — the worker lost the
media-reference layer entirely, so protected media degraded to a download
that could fail, and then to nothing. One module, both callers.
"""

from __future__ import annotations

from typing import Any, Optional

_MEDIA_ATTRIBUTES = (
    ("photo", "photo", "send_photo"),
    ("video_note", "video", "send_video"),
    ("video", "video", "send_video"),
    ("audio", "audio", "send_audio"),
    ("voice", "voice", "send_voice"),
    ("animation", "animation", "send_animation"),
    ("document", "document", "send_document"),
    ("sticker", "sticker", "send_sticker"),
)

_REFERENCE_SENDERS = {
    "photo": "send_photo",
    "video": "send_video",
    "audio": "send_audio",
    "voice": "send_voice",
    "animation": "send_animation",
    "document": "send_document",
    "sticker": "send_sticker",
}


def extract_media_reference(msg: Any) -> Optional[dict]:
    """Return ``{kind, ref, caption?, file_name?}`` for a message's media.

    ``ref`` is the raw ``InputMedia``/file-id object straight off the pyrogram
    message — it keeps original resolution and unique-ids. ``None`` when the
    message carries no usable media (pure text, service messages, polls...).
    """
    if msg is None:
        return None
    for attr, kind, _send in _MEDIA_ATTRIBUTES:
        media = getattr(msg, attr, None)
        if media is None:
            continue
        # pyrogram exposes ``file_id`` on the media object; the file-id string
        # is accepted directly by every ``send_*`` call.
        ref = getattr(media, "file_id", None)
        if not ref:
            continue
        out = {"kind": kind, "ref": ref}
        file_name = getattr(media, "file_name", None)
        if file_name:
            out["file_name"] = file_name
        caption = getattr(msg, "caption", None)
        if caption:
            out["caption"] = caption
        return out
    return None


async def send_media_reference(client: Any, target: Any, ref: dict, caption: Optional[str]) -> Any:
    """Send a media reference (file-id) to ``target``. ``None`` if unsupported."""
    kind = ref.get("kind")
    cap = caption if caption is not None else ref.get("caption")
    method_name = _REFERENCE_SENDERS.get(kind) if isinstance(kind, str) else None
    method = getattr(client, method_name, None) if method_name else None
    if method is None:
        return None
    kw: dict = {}
    if kind == "photo":
        kw["photo"] = ref["ref"]
    elif kind == "sticker":
        kw["sticker"] = ref["ref"]
    else:
        kw[kind] = ref["ref"]
    if kind != "sticker":
        kw["caption"] = cap
    return await method(chat_id=target, **kw)


# Backward-compatible aliases: the dispatcher module re-exports these names and
# existing tests import them from there.
_extract_media_reference = extract_media_reference
_send_media_reference = send_media_reference
