"""Post the *processed* message into our branding channel C (the A -> C -> B hop).

The hop has two halves and they must agree:

* **the decision** — whether this message takes the hop at all — lives in
  :mod:`core.domain.branding` (:func:`branding.should_hop`), so the dashboard,
  the bot preview and the engine can never disagree, and
* **the content** — what actually lands in C — is what this module owns.

The content half is where the hop composes with every other message-processing
feature. Transforms (cleaner, link engine, emoji strip, Replace rules, content
policies, AI rewrite, header/footer, watermark) all run *before* the hop, and the
hop posts their **output** — the same string the target would have received on a
direct delivery. The hop therefore never changes what B sees; it only changes
whose forward header B sees.

Two rules follow from that and are enforced here:

1. **One post per deliverable.** C receives exactly what the destination would
   have received, per ``content_mode``: ``TEXT_ONLY`` puts bare text in C (no
   media, even when the source message had some), ``MEDIA_ONLY`` puts the media
   with no caption, ``AUTO``/``TEXT_AND_MEDIA`` put media + the processed caption.
   Without this a hop would silently *add* media to a text-only task, or *add* a
   caption to a media-only one — i.e. the hop would change the payload.
2. **Entities survive only while their offsets are valid.** Caption entities are
   Telegram UTF-16 offsets into the text they belong to. When no transform
   changed the text, the source entities are shifted and forwarded so bold/links
   keep working in C (and therefore in B's forward of C). When a Replace rule, AI
   rewrite, header/footer or truncation did change the text, the old offsets
   point at the wrong characters, so they are dropped instead of corrupting the
   post.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..domain import branding
from ..domain.services import CaptionManagementEngine

logger = logging.getLogger(__name__)

__all__ = ["MODE_TEXT_ONLY", "MODE_MEDIA_ONLY", "dot", "post_to_channel", "source_entities"]

MODE_TEXT_ONLY = "TEXT_ONLY"
MODE_MEDIA_ONLY = "MEDIA_ONLY"

#: ``await reupload(target_chat, caption, mode_val)`` — infrastructure supplies a
#: media re-uploader for channels whose content protection refuses a copy.
ReuploadFn = Callable[[Any, Optional[str], str], Awaitable[Any]]


def dot(rule: Any, name: str, default: Any = None) -> Any:
    """Read a rule attribute that may hold an enum, falling back to ``default``."""
    value = getattr(rule, name, None)
    if value is None:
        return default
    return getattr(value, "value", value)


def _mode_of(rule: Any) -> str:
    return str(dot(rule, "content_mode", "AUTO") or "AUTO").upper()


def _is_protection_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        key in text
        for key in ("restrict", "protect", "forbidden", "chatforwardsrestricted",
                    "not_allowed", "have no rights")
    )


def source_entities(payload: Any, text: str) -> Optional[List[Any]]:
    """Entities of the source message, but only while they still fit ``text``.

    Returns ``None`` when a transform changed the text: the stored offsets are
    UTF-16 positions into the *original* string, so reusing them after a Replace
    rule or an AI rewrite would bold the wrong characters.
    """
    has_media = bool(getattr(payload, "has_media", False))
    caption = getattr(payload, "caption", "") or ""
    if has_media and caption:
        entities = getattr(payload, "caption_entities", None)
        original = caption
    else:
        entities = getattr(payload, "entities", None)
        original = getattr(payload, "text", "") or getattr(payload, "effective_text", "") or ""
    if not entities:
        return None
    if str(original).strip() != str(text or "").strip():
        return None
    return list(entities)


def _message_id(sent: Any) -> int:
    if sent is None:
        return 0
    if isinstance(sent, list):
        sent = sent[0] if sent else None
        if sent is None:
            return 0
    return int(getattr(sent, "id", None) or getattr(sent, "message_id", None) or 0)


def _plan(inter_id: str, ids: List[int], rule: Any, mode_val: str, via: str) -> Dict[str, Any]:
    """Job state handed to the durable queue so the worker reproduces stage 2."""
    return {
        "chat_id": inter_id,
        "message_id": int(ids[0]),
        "message_ids": [int(i) for i in ids],
        "force_forward": bool(branding.force_forward(rule)),
        "content_mode": mode_val,
        "via": via,
    }


async def post_to_channel(
    client: Any,
    payload: Any,
    rule: Any,
    text: str,
    *,
    caption_template: Optional[str] = None,
    thread_kwargs: Optional[Dict[str, Any]] = None,
    reupload: Optional[ReuploadFn] = None,
) -> Optional[Dict[str, Any]]:
    """Post the processed message into channel C and return the hop plan.

    ``text`` must be the **final** processed text (the one the target would have
    received directly). Returns ``None`` when there is nothing to post, which the
    callers treat as "deliver directly" — never as "drop the message".
    """
    inter_id = branding.channel_id(rule)
    if not inter_id:
        return None
    if str(getattr(payload, "chat_id", "")).strip() == inter_id.strip():
        logger.debug("[PIPELINE:BRANDING] hop skipped: C is the message's own source (%s)", inter_id)
        return None

    mode_val = _mode_of(rule)
    has_media = bool(getattr(payload, "has_media", False))
    kwargs: Dict[str, Any] = dict(thread_kwargs or {})
    target = int(inter_id) if inter_id.lstrip("-").isdigit() else inter_id
    src_chat = getattr(payload, "chat_id", None)
    src_msg_id = int(getattr(payload, "message_id", 0) or 0)

    # --- text-only deliverable ------------------------------------------- #
    if not has_media or mode_val == MODE_TEXT_ONLY:
        body = str(text or caption_template or "").strip()
        if not body:
            return None
        send_kwargs = dict(kwargs)
        entities = source_entities(payload, text) if not has_media else None
        if entities:
            send_kwargs["entities"] = entities
        sent = await client.send_message(chat_id=target, text=body, **send_kwargs)
        mid = _message_id(sent)
        if not mid:
            return None
        return _plan(inter_id, [mid], rule, mode_val, "text")

    # --- media deliverable ------------------------------------------------ #
    caption: Optional[str] = None
    entities: Optional[List[Any]] = None
    overflow: Optional[str] = None
    if mode_val != MODE_MEDIA_ONLY:
        raw = str(text or caption_template or "")
        caption, entities, overflow = CaptionManagementEngine.format_caption(
            raw,
            caption_entities=source_entities(payload, text),
            split_overflow=bool(getattr(rule, "split_long_caption", True)),
        )
        caption = caption or None
        entities = entities or None

    copy_kwargs = dict(kwargs)
    if mode_val == MODE_MEDIA_ONLY:
        # "" drops the source caption: the direct path would have sent media only.
        copy_kwargs["caption"] = ""
    elif caption is not None:
        copy_kwargs["caption"] = caption
    if entities:
        copy_kwargs["caption_entities"] = entities

    try:
        sent = await client.copy_message(
            chat_id=target, from_chat_id=src_chat, message_id=src_msg_id, **copy_kwargs,
        )
    except Exception as exc:
        if not _is_protection_error(exc) or reupload is None:
            raise
        logger.info(
            "[PIPELINE:BRANDING] copy into C=%s blocked (%s) — re-uploading the media",
            inter_id, exc,
        )
        sent = await reupload(target, caption if mode_val != MODE_MEDIA_ONLY else None, mode_val)

    mid = _message_id(sent)
    if not mid:
        return None
    ids = [mid]

    # A caption longer than Telegram's media limit cannot live in one post: the
    # tail is posted as a follow-up in C and both posts travel to the target, so
    # the hop still carries the whole message.
    if overflow and mode_val != MODE_MEDIA_ONLY:
        try:
            extra = await client.send_message(chat_id=target, text=overflow, **dict(thread_kwargs or {}))
            extra_id = _message_id(extra)
            if extra_id:
                ids.append(extra_id)
        except Exception as exc:  # a lost tail must not lose the post itself
            logger.warning("[PIPELINE:BRANDING] C=%s caption overflow failed: %s", inter_id, exc)

    return _plan(inter_id, ids, rule, mode_val, "media")
