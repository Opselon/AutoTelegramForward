"""Pyrogram client pool: manages user-account MTProto sessions dynamically.

DIP: the rest of the app depends on this adapter's narrow public surface,
never on pyrogram types directly.

Supports multiple api_id/api_hash credentials (multi-account), per-client
handler registration (auto re-installed after reconnect), a watchdog that
auto-reconnects with exponential back-off, and per-client health state.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pyrogram.client import Client
from pyrogram.errors import (
    AuthKeyInvalid,
    FloodWait,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
    UserDeactivated,
)

from ...domain.entities import MessagePayload
from ...domain.value_objects import MediaType

# Compat: Telegram now issues channel IDs beyond pyrogram 2.0.106's
# hardcoded bounds (e.g. -1002826947877, -1003898966857). With the stock
# constants get_peer_type() raises ValueError("Peer id invalid"), which
# escapes Client.handle_updates and permanently kills that client's
# update-receiver task — after which NO new messages are forwarded at
# all. Widen the range once, process-wide (same module object every
# pyrogram Client in this process uses).
try:
    from pyrogram import utils as _pyro_utils
    _pyro_utils.MIN_CHANNEL_ID = -10099999999999
except Exception:
    pass

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
    "video_note": MediaType.VIDEO,
    "web_page": MediaType.OTHER,
}


@dataclass
class ClientState:
    """Live state of one managed user-account client."""

    client: Client
    session_id: str
    api_cred_id: str
    connected_since: float = field(default_factory=time.time)
    reconnects: int = 0
    healthy: bool = True
    last_error: str = ""
    # pyrogram dispatcher handler objects returned by add_handler.
    handler_ids: List[Any] = field(default_factory=list)
    watchdog: Optional[asyncio.Task] = field(default=None, compare=False)


class ClientPool:
    """Holds live pyrogram.Client instances keyed by session_id.

    Each client may use its own api_id/api_hash pair, selected by the
    `credential_id` stored alongside the session. A single default credential
    (the one from config.yaml) is used when a session has none.
    """

    def __init__(self, api_id: int = 0, api_hash: str = "") -> None:
        self._default = (api_id, api_hash)
        self._clients: Dict[str, ClientState] = {}
        self._credentials: Dict[str, tuple] = {}
        if api_id and api_hash:
            self._credentials["default"] = self._default
        # session_id -> [{handler, event_type}] specs re-installed on reconnect.
        self._handlers: Dict[str, List[dict]] = {}
        self._stopped = False
        self._starting: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Credentials
    # ------------------------------------------------------------------ #
    def register_credentials(self, credential_id: str, api_id: int, api_hash: str) -> None:
        """Register an api_id/api_hash pair usable by sessions."""
        if not api_id or not api_hash:
            raise ValueError("api_id and api_hash are required")
        self._credentials[credential_id] = (int(api_id), api_hash)
        if credential_id == "default":
            self._default = (int(api_id), api_hash)

    def list_credentials(self) -> Dict[str, tuple]:
        return dict(self._credentials)

    def _creds_for(self, credential_id: Optional[str]) -> tuple:
        if credential_id and credential_id in self._credentials:
            return self._credentials[credential_id]
        if "default" in self._credentials:
            return self._credentials["default"]
        return self._default

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def api_id(self) -> int:
        return self._default[0]

    @property
    def api_hash(self) -> str:
        return self._default[1]

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._starting.setdefault(session_id, asyncio.Lock())

    async def start_with_session_string(
        self,
        session_id: str,
        session_string: str,
        credential_id: Optional[str] = None,
        auto_reconnect: bool = True,
    ) -> Client:
        """Start (or reuse) a client for an already-authorized session."""
        async with self._lock_for(session_id):
            existing = self._clients.get(session_id)
            if existing and existing.client.is_connected:
                return existing.client
            if existing:
                # stale (disconnected) entry: drop before recreating
                self._clients.pop(session_id, None)
            api_id, api_hash = self._creds_for(credential_id)
            client = Client(
                name=f"atf_{session_id[:8]}",
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                in_memory=True,
                no_updates=False,
                device_model="Desktop",
                system_version="Windows 11",
                app_version="5.6.3 x64",
                lang_code="en",
            )
            t0 = time.monotonic()
            await client.start()
            state = ClientState(
                client=client, session_id=session_id,
                api_cred_id=credential_id or "default",
            )
            self._clients[session_id] = state
            await self._install_handlers(state)
            if auto_reconnect and state.watchdog is None:
                state.watchdog = asyncio.create_task(
                    self._watchdog(session_id), name=f"atf-watchdog-{session_id[:8]}"
                )
            logger.info(
                "client started: session=%s cred=%s in %.2fs",
                session_id[:8], state.api_cred_id, time.monotonic() - t0,
            )
            return client

    async def create_login_client(
        self, phone_number: str, credential_id: Optional[str] = None
    ) -> Client:
        """A temporary in-memory client used for the interactive login flow."""
        api_id, api_hash = self._creds_for(credential_id)
        client = Client(
            name=f"atf_login_{phone_number}",
            api_id=api_id,
            api_hash=api_hash,
            in_memory=True,
            device_model="Desktop",
            system_version="Windows 11",
            app_version="5.6.3 x64",
            lang_code="en",
        )
        await client.connect()
        return client

    def get(self, session_id: str) -> Optional[Client]:
        state = self._clients.get(session_id)
        return state.client if state else None

    def get_state(self, session_id: str) -> Optional[ClientState]:
        return self._clients.get(session_id)

    def all_clients(self) -> Dict[str, Client]:
        return {sid: st.client for sid, st in self._clients.items()}

    def all_states(self) -> Dict[str, ClientState]:
        return dict(self._clients)

    async def stop(self, session_id: str) -> None:
        state = self._clients.pop(session_id, None)
        if state:
            if state.watchdog is not None and not state.watchdog.done():
                state.watchdog.cancel()
            try:
                await state.client.stop()
            except Exception:  # pragma: no cover
                logger.warning("Failed to stop client %s", session_id)
            logger.info("client stopped: session=%s", session_id[:8])

    async def stop_all(self) -> None:
        self._stopped = True
        for sid in list(self._clients):
            await self.stop(sid)

    # ------------------------------------------------------------------ #
    # Handler registry (auto re-installed after reconnect)
    # ------------------------------------------------------------------ #
    def add_handler(
        self,
        session_id: str,
        handler: Callable,
        event_type: str = "message",
    ) -> None:
        """Register a pyrogram handler for a session.

        Handlers are stored so they can be re-installed automatically whenever
        the underlying client reconnects. Accepted event types: "message"
        (MessageHandler) and "edited_message" (EditedMessageHandler). Any
        other name (e.g. legacy "channel_post") maps onto "message", because
        pyrogram already delivers channel posts to MessageHandler.
        """
        if event_type not in ("message", "edited_message", "deleted_messages"):
            event_type = "message"  # channel posts arrive via MessageHandler
        self._handlers.setdefault(session_id, []).append(
            {"handler": handler, "event_type": event_type}
        )
        state = self._clients.get(session_id)
        if state:
            self._attach(state, handler, event_type)

    def remove_handlers(self, session_id: str) -> None:
        handlers = self._handlers.pop(session_id, None)
        state = self._clients.get(session_id)
        if state and handlers:
            for spec in handlers:
                try:
                    state.client.remove_handler(spec["handler_obj"])
                except Exception:
                    pass
            state.handler_ids.clear()

    def _attach(self, state: ClientState, handler: Callable, event_type: str) -> None:
        if event_type == "deleted_messages":
            from pyrogram.handlers.deleted_messages_handler import DeletedMessagesHandler
            cls = DeletedMessagesHandler
        elif event_type == "edited_message":
            from pyrogram.handlers.edited_message_handler import EditedMessageHandler
            cls = EditedMessageHandler
        else:
            from pyrogram.handlers.message_handler import MessageHandler
            cls = MessageHandler
        try:
            handler_obj = cls(handler)
            state.client.add_handler(handler_obj)
            spec = None
            for s in self._handlers.get(state.session_id, []):
                if s.get("handler") is handler and s.get("event_type") == event_type and "handler_obj" not in s:
                    spec = s
                    break
            if spec is not None:
                spec["handler_obj"] = handler_obj
            state.handler_ids.append(handler_obj)
        except Exception as exc:  # pragma: no cover - pyrogram API drift
            logger.warning("failed to attach %s handler: %s", event_type, exc)

    async def _install_handlers(self, state: ClientState) -> None:
        state.handler_ids = []
        for spec in self._handlers.get(state.session_id, []):
            self._attach(state, spec["handler"], spec["event_type"])

    # ------------------------------------------------------------------ #
    # Watchdog: auto-reconnect with exponential back-off
    # ------------------------------------------------------------------ #
    async def _watchdog(self, session_id: str) -> None:
        backoff = 5
        while not self._stopped:
            await asyncio.sleep(2)
            state = self._clients.get(session_id)
            if state is None:
                return
            client = state.client
            try:
                if client.is_connected:
                    backoff = 5
                    state.healthy = True
                    continue
            except Exception:
                pass
            state.healthy = False
            state.last_error = "disconnected"
            logger.warning(
                "client %s disconnected — reconnecting in %ds (attempt %d)",
                session_id[:8], backoff, state.reconnects + 1,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
            try:
                await client.start()
                if await ensure_authorized(client):
                    state.reconnects += 1
                    state.healthy = True
                    state.connected_since = time.time()
                    await self._install_handlers(state)
                    logger.info("client %s reconnected", session_id[:8])
                else:
                    logger.error(
                        "client %s no longer authorized after reconnect", session_id[:8]
                    )
                    return
            except FloodWait as exc:
                wait = int(getattr(exc, "value", 60) or 60)
                backoff = max(backoff, wait + 5)
                logger.warning(
                    "flood wait %ds on reconnect of %s", wait, session_id[:8]
                )
            except (AuthKeyInvalid, UserDeactivated) as exc:
                logger.error("client %s auth lost: %s — giving up", session_id[:8], exc)
                return
            except Exception as exc:
                logger.warning("reconnect failed for %s: %s", session_id[:8], exc)


async def ensure_authorized(client: Client) -> bool:
    """Return True if `client` still holds a valid authorization."""
    try:
        return bool(await client.get_me())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
def payload_from_pyrogram(message) -> MessagePayload:
    """Translate a pyrogram message into the domain MessagePayload.

    Handles all chat types (channel / supergroup / group / private / bot),
    service messages, edits, forwards and albums.
    """
    chat = message.chat
    chat_type = getattr(chat, "type", None)
    type_name = chat_type.name.lower() if chat_type is not None else "unknown"
    media_name = (
        message.media.name.lower()
        if getattr(message, "media", None) is not None
        else "text"
    )
    media_type = MEDIA_TYPE_MAP.get(
        media_name, MediaType.OTHER if media_name != "text" else MediaType.TEXT
    )
    sender = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)

    sender_name = None
    if sender:
        name_parts = [p for p in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if p]
        sender_name = getattr(sender, "username", "") or " ".join(name_parts) or str(sender.id)
    elif sender_chat:
        sender_name = getattr(sender_chat, "username", "") or getattr(sender_chat, "title", "") or str(sender_chat.id)

    chat_username = getattr(chat, "username", None) if chat else None
    chat_title = getattr(chat, "title", None) if chat else None

    return MessagePayload(
        message_id=message.id,
        chat_id=str(message.chat.id),
        chat_type=type_name,
        sender_id=str(sender.id) if sender else (str(sender_chat.id) if sender_chat else None),
        sender_name=sender_name,
        chat_username=chat_username,
        chat_title=chat_title,
        text=message.text or "",
        caption=message.caption or "",
        media_type=media_type,
        has_media=media_type is not MediaType.TEXT,
        is_service=getattr(message, "service", None) is not None,
        reply_to_message_id=message.reply_to_message_id,
        date=int(message.date.timestamp()) if getattr(message, "date", None) else 0,
        is_edit=bool(getattr(message, "edit_hide", False)) or _is_edit_message(message),
        forward_origin=_forward_origin(message),
        media_group_id=getattr(message, "media_group_id", None),
        views=getattr(message, "views", 0) or 0,
        entities=getattr(message, "entities", None),
        caption_entities=getattr(message, "caption_entities", None),
        reply_markup=getattr(message, "reply_markup", None),
    )


def _is_edit_message(message) -> bool:
    """pyrogram fires edited_message events as Message objects with an
    internal flag; fall back to the raw TL layer hint when available."""
    return bool(getattr(message, "_edit", False)) or bool(
        getattr(getattr(message, "raw", None), "edit", False)
    )


def _forward_origin(message) -> Optional[dict]:
    fwd = getattr(message, "forward_origin", None)
    if fwd is not None:
        name = ""
        sender = getattr(fwd, "sender_user", None)
        chat = getattr(fwd, "sender_chat_name", getattr(fwd, "chat", None))
        if sender is not None:
            name = (
                getattr(sender, "username", None)
                or getattr(sender, "first_name", "")
                or str(getattr(sender, "id", ""))
            )
        elif chat is not None:
            name = (
                getattr(chat, "username", None)
                or getattr(chat, "title", "")
                or str(getattr(chat, "id", ""))
            )
        return {
            "type": type(fwd).__name__,
            "from_chat_id": str(getattr(fwd, "from_chat_id", "") or getattr(getattr(fwd, "chat", None), "id", "") or ""),
            "from_message_id": getattr(fwd, "from_message_id", 0),
            "date": int(getattr(fwd, "date", 0) or 0),
            "sender_name": name,
        }

    # Standard Pyrogram forward attributes
    fwd_chat = getattr(message, "forward_from_chat", None)
    fwd_user = getattr(message, "forward_from", None)
    fwd_name = getattr(message, "forward_sender_name", None)
    fwd_msg_id = getattr(message, "forward_from_message_id", 0) or 0
    fwd_date = getattr(message, "forward_date", None)
    date_ts = int(fwd_date.timestamp()) if (fwd_date is not None and hasattr(fwd_date, "timestamp")) else (int(fwd_date) if fwd_date else 0)

    if fwd_chat is not None:
        name = getattr(fwd_chat, "username", "") or getattr(fwd_chat, "title", "") or str(fwd_chat.id)
        return {
            "type": "channel",
            "from_chat_id": str(fwd_chat.id),
            "from_chat_username": getattr(fwd_chat, "username", "") or "",
            "from_chat_title": getattr(fwd_chat, "title", "") or "",
            "from_message_id": fwd_msg_id,
            "date": date_ts,
            "sender_name": name,
        }

    if fwd_user is not None:
        name = getattr(fwd_user, "username", "") or f"{getattr(fwd_user, 'first_name', '')} {getattr(fwd_user, 'last_name', '')}".strip() or str(fwd_user.id)
        return {
            "type": "user",
            "from_chat_id": str(fwd_user.id),
            "from_chat_username": getattr(fwd_user, "username", "") or "",
            "from_chat_title": "",
            "from_message_id": fwd_msg_id,
            "date": date_ts,
            "sender_name": name,
        }

    if fwd_name:
        return {
            "type": "hidden_user",
            "from_chat_id": "",
            "from_chat_username": "",
            "from_chat_title": "",
            "from_message_id": fwd_msg_id,
            "date": date_ts,
            "sender_name": str(fwd_name),
        }

    return None
