"""Pyrogram client pool: manages user-account MTProto sessions dynamically.

DIP: the rest of the app depends on this adapter's narrow public surface,
never on pyrogram types directly.
"""

import logging
from typing import Dict, Optional

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired

from ...domain.entities import MessagePayload
from ...domain.value_objects import MediaType

logger = logging.getLogger(__name__)

MEDIA_TYPE_MAP = {
    "photo": MediaType.PHOTO,
    "video": MediaType.VIDEO,
    "document": MediaType.DOCUMENT,
    "voice": MediaType.VOICE,
    "audio": MediaType.AUDIO,
    "animation": MediaType.ANIMATION,
    "sticker": MediaType.STICKER,
    "poll": MediaType.POLL,
    "location": MediaType.LOCATION,
    "contact": MediaType.CONTACT,
}


class ClientPool:
    """Holds live pyrogram.Client instances keyed by session_id."""

    def __init__(self, api_id: int, api_hash: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._clients: Dict[str, Client] = {}

    @property
    def api_id(self) -> int:
        return self._api_id

    @property
    def api_hash(self) -> str:
        return self._api_hash

    async def start_with_session_string(self, session_id: str, session_string: str) -> Client:
        if session_id in self._clients:
            return self._clients[session_id]
        client = Client(
            name=f"atf_{session_id[:8]}",
            api_id=self._api_id,
            api_hash=self._api_hash,
            session_string=session_string,
            in_memory=True,
        )
        await client.start()
        self._clients[session_id] = client
        return client

    async def create_login_client(self, phone_number: str) -> Client:
        """A temporary in-memory client used for the interactive login flow."""
        client = Client(
            name=f"atf_login_{phone_number}",
            api_id=self._api_id,
            api_hash=self._api_hash,
            in_memory=True,
        )
        await client.connect()
        return client

    def get(self, session_id: str) -> Optional[Client]:
        return self._clients.get(session_id)

    def all_clients(self) -> Dict[str, Client]:
        return dict(self._clients)

    async def stop(self, session_id: str) -> None:
        client = self._clients.pop(session_id, None)
        if client:
            try:
                await client.stop()
            except Exception:  # pragma: no cover
                logger.warning("Failed to stop client %s", session_id)

    async def stop_all(self) -> None:
        for sid in list(self._clients):
            await self.stop(sid)


def payload_from_pyrogram(message) -> MessagePayload:
    """Translate a pyrogram message into the domain MessagePayload."""
    chat = message.chat
    chat_type = getattr(chat, "type", None)
    type_name = chat_type.name.lower() if chat_type is not None else "unknown"
    media_name = (
        message.media.name.lower()
        if getattr(message, "media", None) is not None
        else "text"
    )
    media_type = MEDIA_TYPE_MAP.get(media_name, MediaType.OTHER if media_name != "text" else MediaType.TEXT)
    return MessagePayload(
        message_id=message.id,
        chat_id=str(message.chat.id),
        chat_type=type_name,
        sender_id=str(message.from_user.id) if message.from_user else None,
        text=message.text or "",
        caption=message.caption or "",
        media_type=media_type,
        has_media=media_type is not MediaType.TEXT,
        is_service=getattr(message, "service", None) is not None,
        reply_to_message_id=message.reply_to_message_id,
    )
