"""The infrastructure half of chat discovery: scan connected sessions.

Everything in this module needs a live client pool, so it is deliberately the
only place that knows how sessions are chosen, reconnected and read. It returns
raw rows — ``id``, ``title``, ``username``, ``type``, ``is_self`` — and leaves
classification, ordering and paging to
:class:`core.application.discovery.ListDialogsUseCase`.

The pipeline log lines (``[PIPELINE:DIALOG_FETCH:*]``) are part of the contract
with operators: a failing discovery is diagnosed from them, so every exit path
records why it produced no chats.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: A dialog scan is bounded: a slow session must not hold the bot's reply open.
SCAN_TIMEOUT = 12.0


class SessionDialogGateway:
    """Read a user's dialogs through the sessions that user owns."""

    def __init__(
        self,
        sessions: Any,
        pool: Any,
        is_admin: Optional[Callable[[int], bool]] = None,
    ) -> None:
        self._sessions = sessions
        self._pool = pool
        self._is_admin = is_admin or (lambda _uid: False)

    async def fetch(self, owner_user_id: int) -> List[Dict[str, Any]]:
        uid = int(owner_user_id or 0)
        logger.info("[PIPELINE:DIALOG_FETCH:START] owner_user_id=%s", uid)

        sessions = await self._owned_sessions(uid)
        active = self._active_sessions(sessions)
        logger.info(
            "[PIPELINE:DIALOG_FETCH:SESSIONS] active=%d total=%d owner_user_id=%s",
            len(active), len(sessions), uid,
        )
        if not active:
            logger.warning("[PIPELINE:DIALOG_FETCH:EMPTY] no active sessions owner_user_id=%s", uid)
            return []

        chats: List[Dict[str, Any]] = []
        seen: set = set()
        for session in active:
            client = await self._client_for(session)
            if client is None:
                continue
            chats.extend(await self.scan(client, seen))
        logger.info("[PIPELINE:DIALOG_FETCH:COMPLETE] rows=%d owner_user_id=%s", len(chats), uid)
        return chats

    # ------------------------------------------------------------------ #
    # Session selection
    # ------------------------------------------------------------------ #
    async def _owned_sessions(self, uid: int) -> List[Any]:
        sessions: List[Any] = []
        if uid:
            try:
                sessions = list(await self._sessions.list_by_owner(uid) or [])
                logger.info("[PIPELINE:DIALOG_FETCH:BY_OWNER] count=%d owner_user_id=%s", len(sessions), uid)
            except Exception as exc:
                logger.warning("[PIPELINE:DIALOG_FETCH:WARN] list_by_owner(%s) failed: %s", uid, exc)
        if sessions:
            return sessions
        # Admin/system scope only. A failed owner lookup must never widen the
        # query for a normal user — that is the tenant boundary.
        if uid and not self._is_admin(uid):
            return []
        try:
            sessions = list(await self._sessions.list_all() or [])
            logger.info("[PIPELINE:DIALOG_FETCH:ADMIN_FALLBACK] count=%d", len(sessions))
        except Exception as exc:
            logger.warning("[PIPELINE:DIALOG_FETCH:WARN] list_all() failed: %s", exc)
        return sessions

    @staticmethod
    def _active_sessions(sessions: List[Any]) -> List[Any]:
        active = [
            s for s in sessions
            if getattr(s, "is_active", True) and getattr(s, "is_authorized", False)
        ]
        if not active and sessions:
            active = [s for s in sessions if getattr(s, "is_active", True)] or list(sessions)
        return active

    async def _client_for(self, session: Any):
        """Return a connected client, reconnecting the stored session once."""
        client = self._pool.get(session.id)
        if client is not None and getattr(client, "is_connected", False):
            return client
        session_string = (
            getattr(session, "session_string", "")
            or getattr(session, "session_string_encrypted", "")
        )
        if not session_string:
            logger.warning("[PIPELINE:DIALOG_FETCH:SESSION_DISCONNECTED] session=%s", str(session.id)[:8])
            return None
        try:
            logger.info("[PIPELINE:DIALOG_FETCH:RECONNECT] session=%s", str(session.id)[:8])
            await self._pool.start_with_session_string(
                session.id,
                session_string,
                credential_id=getattr(session, "credential_id", "default"),
            )
        except Exception as exc:
            logger.warning(
                "[PIPELINE:DIALOG_FETCH:RECONNECT_FAIL] session=%s error=%s",
                str(session.id)[:8], exc,
            )
            return None
        client = self._pool.get(session.id)
        if client is None or not getattr(client, "is_connected", False):
            logger.warning("[PIPELINE:DIALOG_FETCH:SESSION_DISCONNECTED] session=%s", str(session.id)[:8])
            return None
        return client

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #
    async def scan(self, client: Any, seen: set) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            async with asyncio.timeout(SCAN_TIMEOUT):
                async for dialog in client.get_dialogs(limit=100):
                    chat = getattr(dialog, "chat", None)
                    if chat is None:
                        continue
                    chat_id = getattr(chat, "id", None)
                    if chat_id is None or chat_id in seen:
                        continue
                    seen.add(chat_id)
                    rows.append(self._row(client, chat, dialog))
            logger.info(
                "[PIPELINE:DIALOG_FETCH:SUCCESS] scanned=%d pinned=%d",
                len(rows), sum(1 for row in rows if row.get("pinned")),
            )
        except Exception as exc:
            logger.warning("[PIPELINE:DIALOG_FETCH:ERROR] scan failed: %s", exc)
        return rows

    @staticmethod
    def _row(client: Any, chat: Any, dialog: Any = None) -> Dict[str, Any]:
        me = getattr(client, "me", None)
        my_id = getattr(me, "id", None) if me is not None else None
        return {
            "id": str(getattr(chat, "id", "")),
            "title": getattr(chat, "title", None) or getattr(chat, "first_name", None) or "",
            "username": getattr(chat, "username", "") or "",
            "type": getattr(getattr(chat, "type", ""), "value", getattr(chat, "type", "")),
            "is_self": bool(my_id is not None and getattr(chat, "id", None) == my_id),
            "pinned": bool(getattr(dialog, "pinned", False)),
        }
