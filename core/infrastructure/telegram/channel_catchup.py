"""Active channel catch-up for user sessions.

Why this exists
---------------
Telegram does not push channel post updates to a client whose update state is
far behind. When a session has a large backlog (`unread_count` in the tens of
thousands), the server stops sending `UpdateNewChannelMessage` for channels
and expects the client to fetch the gap itself with
`updates.getChannelDifference`.

Pyrogram only calls `updates.GetState()` on start and then waits for push
updates. For a channel-forwarding service this means every channel post that
arrives while the core is restarting — or while a session is briefly away —
is silently lost forever. That is the root cause of rules that look correct,
are active, produce no errors, and still forward nothing.

This module closes the gap: after a session goes live it iterates the source
channels of that session's active rules, computes the unread slice via
`channels.GetFullChannel`, and replays the missed posts through the normal
dispatcher path. It is bounded (no history beyond the backlog), idempotent
(the pipeline's dedup layer drops anything already processed) and never
raises into the boot path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger("atf.boot")

# Never replay more than this many posts per channel per catch-up pass.
# Telegram returns them newest-first; we only care about the recent tail.
MAX_POSTS_PER_CHANNEL = 200

# A channel gap larger than this is skipped rather than replayed in full:
# the backlog is old news and a huge slice risks a floodwait. The fresh
# catch-up window still applies on the next pass once the pts is aligned.
MAX_GAP_FOR_REPLAY = 5000


class ChannelCatchUpService:
    """Replay channel posts a session missed while it was offline."""

    def __init__(self, pool, rules, dispatcher, log_client=None) -> None:
        self._pool = pool
        self._rules = rules
        self._dispatcher = dispatcher
        self._log = log_client

    async def catch_up_session(self, session_id: str) -> Tuple[int, int]:
        """Fetch missed posts for every active source channel of a session.

        Returns ``(replayed, skipped)`` counts. Never raises: catch-up is a
        best-effort enhancement and must never break boot.
        """
        client = self._pool.get(session_id)
        if client is None:
            return (0, 0)

        try:
            channels = await self._source_channels(session_id)
        except Exception as exc:
            logger.warning("catch-up: could not list source channels for %s: %s", session_id[:8], exc)
            return (0, 0)

        if not channels:
            return (0, 0)

        replayed = 0
        skipped = 0
        for chat_id, chat_name in channels:
            try:
                n = await self._catch_up_channel(client, session_id, chat_id, chat_name)
            except Exception as exc:
                # A single channel failing must not stop the others.
                logger.warning("catch-up failed for %s (%s): %s", chat_name or chat_id, type(exc).__name__, exc)
                skipped += 1
                continue
            replayed += n
            if n:
                logger.info("catch-up: replayed %d post(s) for %s", n, chat_name or chat_id)
                self._emit(
                    "catch-up: replayed %d missed post(s) from %s" % (n, chat_name or chat_id),
                )

        return (replayed, skipped)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _source_channels(self, session_id: str) -> List[Tuple[str, str]]:
        """Distinct source chats of active rules belonging to this session."""
        try:
            all_rules = await self._rules.list_all()
        except Exception as exc:
            logger.warning("catch-up: cannot list rules: %s", exc)
            return []

        seen: dict[str, str] = {}
        for rule in all_rules:
            if not getattr(rule, "is_active", False):
                continue
            if getattr(rule, "is_paused", False):
                continue
            # A rule only belongs to a session if it references it. Older rows
            # may carry an empty session id; match those too so a legacy
            # single-session install still gets caught up.
            rid = getattr(rule, "session_id", "") or ""
            if rid and rid != session_id:
                continue
            src = str(getattr(rule, "source_chat_id", "") or "")
            if src and src not in seen:
                seen[src] = getattr(rule, "source_chat_name", "") or src
        return list(seen.items())

    async def _catch_up_channel(
        self, client, session_id: str, chat_id: str, chat_name: str
    ) -> int:
        from pyrogram.raw.functions.channels import GetFullChannel
        from pyrogram.raw.functions.messages import GetDialogs
        from pyrogram.raw.functions.updates import GetChannelDifference
        from pyrogram.raw.types import ChannelMessagesFilter, InputPeerEmpty, MessageRange

        # top_message / read_inbox_max_id live on the Dialog row, not on
        # ChannelFull. Fetch the dialog entry to learn what push never sent.
        try:
            dialogs = await client.invoke(
                GetDialogs(offset_date=0, offset_id=0, offset_peer=InputPeerEmpty(), limit=200, hash=0)
            )
        except Exception as exc:
            logger.debug("catch-up: GetDialogs failed: %s", exc)
            return 0

        raw_id = _raw_channel_id(chat_id)
        target_dialog = None
        for d in getattr(dialogs, "dialogs", []) or []:
            peer = getattr(d, "peer", None)
            if getattr(peer, "channel_id", None) == raw_id:
                target_dialog = d
                break
        if target_dialog is None:
            # The channel is not in the dialog list, so Telegram will not
            # track read state for it here — nothing to catch up.
            return 0

        top = getattr(target_dialog, "top_message", 0) or 0
        seen = getattr(target_dialog, "read_inbox_max_id", 0) or 0
        if top <= seen:
            # Nothing unread: the channel is already aligned.
            return 0

        try:
            peer = await client.resolve_peer(chat_id)
        except Exception as exc:
            logger.debug("catch-up: cannot resolve %s: %s", chat_id, exc)
            return 0

        try:
            full = await client.invoke(GetFullChannel(channel=peer))
        except Exception as exc:
            logger.debug("catch-up: GetFullChannel %s failed: %s", chat_id, exc)
            return 0
        pts = getattr(getattr(full, "full_chat", None), "pts", 0) or 0

        gap = top - seen
        if gap > MAX_GAP_FOR_REPLAY:
            logger.info(
                "catch-up: %s has %d unread posts (> %d); skipping bulk replay",
                chat_name or chat_id, gap, MAX_GAP_FOR_REPLAY,
            )
            return 0

        # Ask Telegram for the unread slice. Ranges are inclusive.
        lo = max(1, seen + 1)
        hi = top
        msg_filter = ChannelMessagesFilter(ranges=[MessageRange(min_id=lo, max_id=hi)])
        try:
            diff = await client.invoke(
                GetChannelDifference(channel=peer, filter=msg_filter, pts=pts, limit=MAX_POSTS_PER_CHANNEL)
            )
        except Exception as exc:
            logger.debug("catch-up: GetChannelDifference %s failed: %s", chat_id, exc)
            return 0

        raw_messages: Iterable[Any] = getattr(diff, "new_messages", None) or []
        # newest-first → replay in chronological order so targets read top-down
        messages = sorted(raw_messages, key=lambda m: getattr(m, "id", 0) or 0)

        replayed = 0
        # users/chats maps come with the difference payload so that the raw
        # posts can be turned into full pyrogram Message objects.
        users = {u.id: u for u in (getattr(diff, "users", None) or [])}
        chats = {c.id: c for c in (getattr(diff, "chats", None) or [])}
        for raw in messages:
            if self._replay_one(client, session_id, raw, users, chats):
                replayed += 1
        return replayed

    def _replay_one(self, client, session_id: str, raw_message: Any, users: dict, chats: dict) -> bool:
        """Feed one raw channel post through the bound dispatcher handler."""
        handler = self._dispatcher.message_handler(session_id) if hasattr(
            self._dispatcher, "message_handler"
        ) else None
        if handler is None:
            return False
        try:
            from pyrogram.types import Message as PyroMessage
            msg = PyroMessage._parse(client, raw_message, users, chats)  # noqa: SLF001
        except Exception as exc:
            logger.debug("catch-up: could not parse post: %s", exc)
            return False
        if msg is None:
            return False
        try:
            asyncio.ensure_future(handler(client, msg))
            return True
        except Exception as exc:
            logger.debug("catch-up: dispatch failed: %s", exc)
            return False

    def _emit(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log.info("core", "catch-up", message)
        except Exception:
            pass


def _raw_channel_id(chat_id: str) -> int:
    """Strip the -100 mark from a marked channel/supergroup id.

    Dialog peers report the bare channel id (e.g. 1957768371) while rules
    store the marked form (-1001957768371). New-style ids longer than ten
    digits carry no legacy -100 prefix, so they are returned unchanged.
    """
    text = str(chat_id).lstrip("-")
    if len(text) > 10 and text.startswith("100"):
        return int(text[3:])
    try:
        return int(text)
    except ValueError:
        return 0
