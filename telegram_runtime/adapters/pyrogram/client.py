"""Pyrogram adapter — the MTProto execution layer of the Telegram Runtime.

Every Telegram side effect the Go Worker can request lands here. Each method
maps ONE business-neutral contract call onto the corresponding pyrogram
client call, then reports WHAT Telegram actually said:

* a confirmed result (message id, ids, size) on success;
* a typed ``TelegramOperationError`` on failure — never a bare exception.

The adapter performs no business decisions: it does not choose targets, it
does not evaluate rules, it does not retry, it does not sleep for a business
policy. Go decides WHAT should happen; this executes the Telegram-specific
operation and reports back.

Media never sits fully in memory: downloads write to the shared scratch path
the Go Worker owns, and uploads are staged from that same path.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

from telegram_runtime import errors

logger = logging.getLogger("atf.telegram_runtime.pyrogram")

__all__ = ["PyrogramAdapter"]


def _chat(value: Any) -> Any:
    """Coerce a chat id to int where Telegram expects an int.

    Chat ids are kept as strings across the boundary (channel ids exceed the
    32-bit range and a numeric round-trip is how precision is lost); the
    transport needs the number.
    """
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return value
    try:
        return int(text)
    except ValueError:
        return text  # a @username is a valid chat specifier for pyrogram


class PyrogramAdapter:
    """One pyrogram client behind the runtime's typed contract."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # --- message reads --------------------------------------------------- #

    async def get_message(self, chat_id: Any, message_id: int) -> Tuple[Optional[Any], Optional[errors.TelegramOperationError]]:
        try:
            msg = await self._client.get_messages(chat_id=_chat(chat_id), message_ids=int(message_id))
        except Exception as exc:  # normalized, never re-raised raw
            return None, errors.normalize(exc, operation="get_messages")
        return msg, None

    async def get_album_message_ids(self, chat_id: Any, message_id: int) -> Tuple[List[int], Optional[errors.TelegramOperationError]]:
        """Resolve every member of the album a message belongs to.

        Pyrogram exposes the media group through the message's
        ``media_group_id``; the members are found in the chat's recent
        history window around the known id (albums are always posted as one
        consecutive burst).
        """
        try:
            msg, ferr = await self.get_message(chat_id, message_id)
            if ferr is not None:
                return [], ferr
            group = getattr(msg, "media_group_id", None)
            if not group:
                return [int(message_id)], None
            chat = _chat(chat_id)
            # A bounded window around the known member: albums never exceed
            # 10 items, so ±20 is far more than enough and stays cheap.
            messages = await self._client.get_messages(chat_id=chat, limit=40)
            ids = [
                m.id for m in (messages or [])
                if getattr(m, "media_group_id", None) == group
            ]
            if not ids:
                ids = [int(message_id)]
            return sorted(set(int(i) for i in ids)), None
        except Exception as exc:
            return [], errors.normalize(exc, operation="get_album_message_ids")

    async def get_file_extension(self, chat_id: Any, message_id: int) -> Tuple[str, Optional[errors.TelegramOperationError]]:
        try:
            msg, ferr = await self.get_message(chat_id, message_id)
            if ferr is not None:
                return "", ferr
            for attr in ("document", "audio", "video", "animation", "voice", "video_note"):
                media = getattr(msg, attr, None)
                file_name = getattr(media, "file_name", None) if media is not None else None
                if file_name:
                    _, ext = os.path.splitext(str(file_name))
                    return ext, None
            photo = getattr(msg, "photo", None)
            if photo is not None:
                return ".jpg", None
            return "", None
        except Exception as exc:
            return "", errors.normalize(exc, operation="get_file_extension")

    # --- sends (the Telegram side effects) -------------------------------- #

    async def forward_messages(
        self, target_chat_id: Any, from_chat_id: Any, message_ids: List[int],
        message_thread_id: int = 0,
    ) -> Tuple[List[int], Optional[errors.TelegramOperationError]]:
        """Native Telegram forward (original author header preserved)."""
        kwargs: dict = {}
        if message_thread_id and message_thread_id > 0:
            kwargs["message_thread_id"] = int(message_thread_id)
        try:
            res = await self._client.forward_messages(
                chat_id=_chat(target_chat_id),
                from_chat_id=_chat(from_chat_id),
                message_ids=[int(i) for i in message_ids],
                **kwargs,
            )
        except Exception as exc:
            return [], errors.normalize(exc, operation="forward_messages")
        return [int(getattr(m, "id", 0) or 0) for m in (res or [])], None

    async def copy_message(
        self, target_chat_id: Any, from_chat_id: Any, message_id: int,
        caption: Optional[str] = None, message_thread_id: int = 0,
    ) -> Tuple[int, Optional[errors.TelegramOperationError]]:
        """A clean copy of one message (no original author header)."""
        kwargs: dict = {}
        if message_thread_id and message_thread_id > 0:
            kwargs["message_thread_id"] = int(message_thread_id)
        # An empty string means "no caption" (MEDIA_ONLY); None means "keep
        # whatever the source carries" — the two are different instructions.
        if caption is not None:
            kwargs["caption"] = caption
        try:
            res = await self._client.copy_message(
                chat_id=_chat(target_chat_id),
                from_chat_id=_chat(from_chat_id),
                message_id=int(message_id),
                **kwargs,
            )
        except Exception as exc:
            return 0, errors.normalize(exc, operation="copy_message")
        return int(getattr(res, "id", 0) or 0), None

    async def copy_messages(
        self, target_chat_id: Any, from_chat_id: Any, message_ids: List[int],
        caption: Optional[str] = None, message_thread_id: int = 0,
    ) -> Tuple[List[int], Optional[errors.TelegramOperationError]]:
        ids: List[int] = []
        for message_id in message_ids:
            sent, ferr = await self.copy_message(
                target_chat_id, from_chat_id, message_id, caption, message_thread_id,
            )
            if ferr is not None:
                return ids, ferr
            ids.append(sent)
        return ids, None

    async def copy_media_group(
        self, target_chat_id: Any, from_chat_id: Any, message_id: int,
        caption: Optional[str] = None, message_thread_id: int = 0,
    ) -> Tuple[List[int], Optional[errors.TelegramOperationError]]:
        kwargs: dict = {}
        if message_thread_id and message_thread_id > 0:
            kwargs["message_thread_id"] = int(message_thread_id)
        if caption is not None:
            kwargs["captions"] = caption
        try:
            res = await self._client.copy_media_group(
                chat_id=_chat(target_chat_id),
                from_chat_id=_chat(from_chat_id),
                message_id=int(message_id),
                **kwargs,
            )
        except Exception as exc:
            return [], errors.normalize(exc, operation="copy_media_group")
        return [int(getattr(m, "id", 0) or 0) for m in (res or [])], None

    async def send_message(
        self, target_chat_id: Any, text: str, message_thread_id: int = 0,
    ) -> Tuple[int, Optional[errors.TelegramOperationError]]:
        """Send plain (already-transformed) text."""
        kwargs: dict = {}
        if message_thread_id and message_thread_id > 0:
            kwargs["message_thread_id"] = int(message_thread_id)
        try:
            res = await self._client.send_message(
                chat_id=_chat(target_chat_id), text=text or "...", **kwargs,
            )
        except Exception as exc:
            return 0, errors.normalize(exc, operation="send_message")
        return int(getattr(res, "id", 0) or 0), None

    # --- media staging ---------------------------------------------------- #

    async def download_media(
        self, chat_id: Any, message_id: int, destination: str,
    ) -> Tuple[int, Optional[errors.TelegramOperationError]]:
        """Download a message's file to the shared scratch path.

        The path is Go-owned (the worker owns the scratch volume's lifecycle
        and cleanup); the runtime is only told where to write.
        """
        try:
            msg, ferr = await self.get_message(chat_id, message_id)
            if ferr is not None:
                return 0, ferr
            if msg is None:
                return 0, errors.failed(errors.MESSAGE_NOT_FOUND, "source message not found")
            path = await self._client.download_media(msg, file_name=destination)
            if not path or not os.path.exists(path):
                return 0, errors.failed(
                    errors.MEDIA_DOWNLOAD_FAILED,
                    f"download did not produce a file at {destination}",
                )
            try:
                return int(os.path.getsize(path)), None
            except OSError as exc:
                return 0, errors.failed(errors.MEDIA_DOWNLOAD_FAILED, str(exc))
        except Exception as exc:
            return 0, errors.normalize(exc, operation="download_media")

    async def reload_media(
        self, chat_id: Any, message_id: int, destination: str, media_type: str = "",
    ) -> Tuple[int, Optional[errors.TelegramOperationError]]:
        """Download raw bytes for the re-upload fallback."""
        return await self.download_media(chat_id, message_id, destination)

    async def prepare_media_upload(
        self, source_path: str, source_chat_id: Any, source_message_id: int,
        media_type: str = "",
    ) -> Tuple[str, Optional[errors.TelegramOperationError]]:
        """Stage a file for upload, returning an opaque reference token.

        The token is only meaningful to the runtime (it wraps the staged
        path); the Go Worker never parses it. An empty token means "no file
        reference available — fall back to plain text".
        """
        if not source_path or not os.path.exists(source_path):
            return "", errors.failed(
                errors.MEDIA_UPLOAD_FAILED,
                f"staged media is missing at {source_path}",
            )
        # The staged path IS the token: the send path re-reads it, so no
        # second copy of the bytes exists anywhere.
        return source_path, None

    async def send_media(
        self, target_chat_id: Any, token: str, caption: Optional[str] = None,
        media_type: str = "", message_thread_id: int = 0,
    ) -> Tuple[int, Optional[errors.TelegramOperationError]]:
        """Send media from a staged reference token.

        Dispatches by the ingest-side media classification, defaulting to a
        document send (the lossless fallback)._caption semantics mirror the
        Core's re-upload path.
        """
        if not token or not os.path.exists(token):
            return 0, errors.failed(errors.MEDIA_UPLOAD_FAILED, "staged media vanished before upload")
        target = _chat(target_chat_id)
        kwargs: dict = {}
        if message_thread_id and message_thread_id > 0:
            kwargs["message_thread_id"] = int(message_thread_id)
        kind = str(media_type or "").lower()
        try:
            if kind == "photo":
                res = await self._client.send_photo(photo=token, caption=caption, chat_id=target, **kwargs)
            elif kind in ("video", "round_video"):
                res = await self._client.send_video(video=token, caption=caption, chat_id=target, **kwargs)
            elif kind == "voice":
                res = await self._client.send_voice(voice=token, chat_id=target, **kwargs)
            elif kind == "audio":
                res = await self._client.send_audio(audio=token, caption=caption, chat_id=target, **kwargs)
            elif kind == "animation":
                res = await self._client.send_animation(animation=token, caption=caption, chat_id=target, **kwargs)
            elif kind == "sticker":
                res = await self._client.send_sticker(sticker=token, chat_id=target, **kwargs)
            else:
                res = await self._client.send_document(document=token, caption=caption, chat_id=target, **kwargs)
        except Exception as exc:
            return 0, errors.normalize(exc, operation="send_media")
        return int(getattr(res, "id", 0) or 0), None
