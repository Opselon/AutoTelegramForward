"""Telegram-backed chat probe: can this account reach a chat, and post to it?

The picker accepts ids a user typed, so "it looks like an id" cannot be the
whole check. This adapter asks the account's own connected client — the only
place that knows — and reports three outcomes per chat:

``ok``           the account can post there (channel admin with post rights,
                 a group where members may send, a private chat);
``read_only``    reachable but posting is not allowed (plain channel member,
                 muted group, admin without post rights);
``unknown``      Telegram refused the lookup (no such chat, no access);
``unchecked``    no connected client, so the question cannot be answered.

It never guesses: a failure is reported as unverified, and the confirmation
step decides what to do with that.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core.application.chat_access import Probe
from core.application.chat_selection import (
    STATE_OK,
    STATE_READ_ONLY,
    STATE_UNCHECKED,
    STATE_UNKNOWN,
)

logger = logging.getLogger(__name__)

#: One lookup must not hold the whole picker hostage.
PROBE_TIMEOUT = 6.0

#: Chat types where an ordinary member can post.
_POSTABLE_TYPES = ("group", "supergroup", "private", "bot")

#: Member statuses that can post in a channel (supergroups get the group rule).
_POSTING_STATUSES = ("creator", "administrator", "owner")


def _chat_type(chat: Any) -> str:
    kind = str(getattr(chat, "type", "") or "")
    return kind.split(".")[-1].lower()


def _member_can_post(member: Any) -> Optional[bool]:
    """True/False when the member object says so, None when it does not."""
    if member is None:
        return None
    status = str(getattr(member, "status", "") or "").split(".")[-1].lower()
    if status in ("creator", "owner"):
        return True
    if status == "administrator":
        privileges = getattr(member, "privileges", None)
        if privileges is None:
            return None
        return bool(getattr(privileges, "can_post_messages", False))
    if status == "member":
        return True
    if status in ("restricted", "left", "kicked", "banned"):
        return False
    return None


class SessionChatProbe:
    """The production :class:`ChatAccessProbe`: the account's own client."""

    def __init__(self, sessions: Any, pool: Any, timeout: float = PROBE_TIMEOUT) -> None:
        self._sessions = sessions
        self._pool = pool
        self._timeout = timeout

    async def _client(self, owner_user_id: int) -> Any:
        sessions = await self._sessions.list_by_owner(int(owner_user_id or 0))
        if not sessions:
            return None
        session = next(
            (item for item in sessions if getattr(item, "is_active", True)), sessions[0]
        )
        client = self._pool.get(session.id) if self._pool is not None else None
        if client is None or not getattr(client, "is_connected", False):
            return None
        return client

    async def probe(self, owner_user_id: int,
                    chat_ids: Sequence[str]) -> Dict[str, Probe]:
        results: Dict[str, Probe] = {}
        wanted: List[str] = [str(item).strip() for item in chat_ids if str(item).strip()]
        if not wanted:
            return results
        try:
            client = await self._client(owner_user_id)
        except Exception as exc:  # noqa: BLE001 - a probe failure is an answer, not a crash
            logger.warning("[PIPELINE:SELECT] session lookup failed for %s: %s",
                           owner_user_id, exc)
            client = None
        if client is None:
            # Honest answer: nobody asked Telegram, so nothing is proven.
            return {chat_id: Probe(chat_id=chat_id, state=STATE_UNCHECKED) for chat_id in wanted}

        for chat_id in wanted:
            results[chat_id] = await self._probe_one(client, chat_id)
        return results

    async def _probe_one(self, client: Any, chat_id: str) -> Probe:
        try:
            chat = await asyncio.wait_for(client.get_chat(chat_id), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.info("[PIPELINE:SELECT] probe timeout chat=%s", chat_id)
            return Probe(chat_id=chat_id, state=STATE_UNCHECKED, reason="timeout")
        except Exception as exc:  # noqa: BLE001 - Telegram's refusal is the answer
            logger.info("[PIPELINE:SELECT] probe failed chat=%s: %s", chat_id, exc)
            return Probe(chat_id=chat_id, state=STATE_UNKNOWN, reason=str(exc)[:120])

        title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or chat_id
        kind = _chat_type(chat)
        if kind == "channel":
            verdict = await self._channel_verdict(client, chat_id, chat)
        elif kind in _POSTABLE_TYPES:
            verdict = self._group_verdict(chat)
        else:
            verdict = None
        if verdict is True:
            return Probe(chat_id=chat_id, state=STATE_OK, title=str(title))
        if verdict is False:
            return Probe(chat_id=chat_id, state=STATE_READ_ONLY, title=str(title))
        return Probe(chat_id=chat_id, state=STATE_UNCHECKED, title=str(title))

    async def _channel_verdict(self, client: Any, chat_id: str, chat: Any) -> Optional[bool]:
        """Channels: the bot's membership decides, not the chat's default."""
        try:
            member = await asyncio.wait_for(
                client.get_chat_member(chat_id, "me"), timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("[PIPELINE:SELECT] member lookup failed chat=%s: %s", chat_id, exc)
            member = None
        verdict = _member_can_post(member)
        if verdict is not None:
            return verdict
        permissions = getattr(chat, "permissions", None)
        if permissions is not None:
            return bool(getattr(permissions, "can_send_messages", False))
        return None

    @staticmethod
    def _group_verdict(chat: Any) -> Optional[bool]:
        permissions = getattr(chat, "permissions", None)
        if permissions is not None:
            return bool(getattr(permissions, "can_send_messages", True))
        return True


class StaticChatProbe:
    """A probe with fixed answers — for wiring a caller that already knows."""

    def __init__(self, answers: Optional[Dict[str, str]] = None,
                 default: str = STATE_UNCHECKED) -> None:
        self._answers = {str(k): str(v) for k, v in (answers or {}).items()}
        self._titles: Dict[str, str] = {}
        self._default = default

    def seed(self, chat_id: str, state: str, title: str = "") -> "StaticChatProbe":
        """Give one chat a fixed answer and optionally a fixed title."""
        self._answers[str(chat_id)] = str(state)
        if title:
            self._titles[str(chat_id)] = title
        return self

    async def probe(self, owner_user_id: int,
                    chat_ids: Sequence[str]) -> Dict[str, Probe]:
        out: Dict[str, Probe] = {}
        for chat_id in chat_ids:
            state = self._answers.get(str(chat_id), self._default)
            out[str(chat_id)] = Probe(
                chat_id=str(chat_id), state=state, title=self._titles.get(str(chat_id), "")
            )
        return out
