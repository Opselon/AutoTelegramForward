"""Bridge between Pyrogram and the auto-reply engine.

Two responsibilities, both deliberately small:

* translate one inbound private message into :class:`IncomingMessage`
  (no model, no database, no decisions), and
* hand the worker a way to type and send on the session the message arrived on.

Everything else — targeting, rules, pacing, generation — lives in the
application layer, which is why this file never imports the engine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ...domain.autoreply import IncomingMessage, normalize_chat_id

logger = logging.getLogger("atf.autoreply.bridge")


def _epoch_seconds(value: Any) -> int:
    """Epoch seconds for a Telegram timestamp, whatever shape it arrives in.

    Pyrogram hands ``Message.date`` over as a naive UTC ``datetime`` while the
    raw MTProto layer uses an int. ``int(datetime)`` raises TypeError, which
    used to abort the whole auto-reply hand-off for every private message
    ("int() argument must be ... not 'datetime.datetime'") — the bridge is the
    one place that has to accept both representations.
    """
    if value is None:
        return 0
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0



def _chat_type_name(chat: Any) -> str:
    raw = getattr(chat, "type", None)
    if raw is None:
        return "private"
    name = getattr(raw, "name", None) or str(raw)
    return str(name).split(".")[-1].lower()


def _media_name(message: Any) -> str:
    media = getattr(message, "media", None)
    if media is None:
        return "text"
    name = getattr(media, "name", None) or str(media)
    return str(name).split(".")[-1].lower()


def inbound_from_pyrogram(message: Any, *, session_id: str = "",
                          owner_user_id: int = 0) -> Optional[IncomingMessage]:
    """Return the domain view of a message, or None when it is not a candidate.

    A private message with no text (a sticker, a voice note) still reaches the
    engine so the rules can decide; it is not the bridge's job to guess.
    """
    chat = getattr(message, "chat", None)
    if chat is None:
        return None
    chat_type = _chat_type_name(chat)
    if chat_type != "private":
        return None
    sender = getattr(message, "from_user", None)
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    if not isinstance(text, str):
        text = str(text)
    sender_name = ""
    sender_username = ""
    if sender is not None:
        parts = [p for p in (getattr(sender, "first_name", "") or "",
                             getattr(sender, "last_name", "") or "") if p]
        sender_name = " ".join(parts) or (getattr(sender, "username", "") or "")
        sender_username = getattr(sender, "username", "") or ""
    return IncomingMessage(
        chat_id=normalize_chat_id(getattr(chat, "id", "")),
        message_id=int(getattr(message, "id", 0) or 0),
        text=text.strip(),
        chat_type="private",
        sender_id=str(getattr(sender, "id", "") or "") or None,
        sender_name=sender_name,
        sender_username=sender_username,
        is_bot=bool(getattr(sender, "is_bot", False)),
        is_self=bool(getattr(sender, "is_self", False)),
        is_forwarded=bool(getattr(message, "forward_date", None)
                          or getattr(message, "forward_origin", None)),
        media_type=_media_name(message),
        date=_epoch_seconds(getattr(message, "date", 0)),
        session_id=session_id,
        owner_user_id=int(owner_user_id or 0),
    )


def make_sender(client: Any) -> Callable[[str, str], Any]:
    """A ``send(chat_id, text) -> message_id`` callable bound to one client."""

    async def _send(chat_id: str, text: str) -> Optional[int]:
        sent = await client.send_message(chat_id=chat_id, text=text)
        return int(getattr(sent, "id", 0) or 0)

    return _send


def make_typing(client: Any) -> Callable[[str], Any]:
    async def _typing(chat_id: str) -> None:
        from pyrogram.enums import ChatAction

        await client.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    return _typing


def register_pool_senders(pool: Any, worker: Any) -> int:
    """Bind every live session's transport to the worker at start-up.

    A restart between a job being queued and the next inbound message would
    otherwise leave a runnable reply with no way to send it: this wires the
    already-connected clients in before the worker's first claim.
    """
    from core.application.autoreply.worker import OutboundSender  # local: avoids a cycle

    registered = 0
    try:
        session_ids = list(pool.list_sessions())
    except Exception:
        return 0
    for sid in session_ids:
        try:
            client = pool.get(sid)
            if client is None:
                continue
            worker.register_session_sender(
                str(sid), OutboundSender(make_sender(client), make_typing(client)),
            )
            registered += 1
        except Exception:
            continue
    return registered


async def resolve_private_chat(client: Any, query: str) -> Optional[Dict[str, Any]]:
    """Resolve a user-supplied identifier to a private chat this account can message.

    Accepts a numeric id, an ``@username``, or a name fragment searched across
    the account's own recent dialogs. Returns nothing when the search finds no
    private chat, so the UI can say "not found" instead of adding a wrong id.
    """
    raw = (query or "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        try:
            peer = await client.get_users(int(raw))
            if peer is not None:
                return {
                    "chat_id": normalize_chat_id(peer.id),
                    "title": " ".join(p for p in (getattr(peer, "first_name", ""),
                                                   getattr(peer, "last_name", "")) if p) or raw,
                    "username": getattr(peer, "username", "") or "",
                    "chat_type": "private",
                }
        except Exception:
            return None
    username = raw.lstrip("@")
    try:
        peer = await client.get_users(username)
        if peer is not None:
            return {
                "chat_id": normalize_chat_id(peer.id),
                "title": " ".join(p for p in (getattr(peer, "first_name", ""),
                                               getattr(peer, "last_name", "")) if p) or username,
                "username": getattr(peer, "username", "") or username,
                "chat_type": "private",
            }
    except Exception:
        pass
    # A name fragment: search the account's own dialogs, never the global index.
    needle = raw.casefold()
    try:
        async for dialog in client.get_dialogs(limit=200):
            chat = getattr(dialog, "chat", None)
            if chat is None or _chat_type_name(chat) != "private":
                continue
            title = " ".join(p for p in (getattr(chat, "first_name", ""),
                                          getattr(chat, "last_name", "")) if p)
            title = title or getattr(chat, "title", "") or ""
            uname = (getattr(chat, "username", "") or "").casefold()
            if needle in title.casefold() or (uname and needle in uname):
                return {
                    "chat_id": normalize_chat_id(chat.id),
                    "title": title or uname,
                    "username": getattr(chat, "username", "") or "",
                    "chat_type": "private",
                }
    except Exception:
        return None
    return None


__all__ = ["inbound_from_pyrogram", "make_sender", "make_typing", "resolve_private_chat"]
