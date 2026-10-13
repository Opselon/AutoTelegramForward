"""Periodic channel polling — the reliable replacement for missing push updates.

Why this exists
---------------
Telegram only pushes `UpdateNewChannelMessage` for a channel while that channel
shows `unread_count > 0` for the session. The moment a session's read-marker
catches up to the top message of a channel, Telegram stops pushing that
channel entirely and expects the client to poll.

ATF's sessions routinely have their channel read-state fully caught up
(probes, status dashes, health checks all read the dialog list), which
silently flips every forwarding source channel to unread=0 and stops all push
delivery. The rule, the pipeline, the queue and the client all stay healthy —
no error is ever logged — yet nothing is forwarded.

This service polls each active source channel on a short interval and replays
any post the session has not seen yet through the same dispatcher path a live
push would use. It is the delivery mechanism that does not depend on
Telegram's read-state mood, and it also covers restarts: whatever arrived
while the core was down is picked up on the first sweep.

Idempotency: every replay goes through the pipeline's dedup layer
(processed_messages), so overlap with push or with a previous sweep is
harmless.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("atf.boot")

# How often to sweep every monitored source channel.
POLL_INTERVAL_SECONDS = 15.0

# Never pull more than this many posts per channel per sweep.
MAX_POSTS_PER_SWEEP = 100


class ChannelPollingService:
    """Poll source channels for new posts when Telegram will not push them."""

    def __init__(
        self,
        pool,
        rules,
        dispatcher,
        state_store=None,
        interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._pool = pool
        self._rules = rules
        self._dispatcher = dispatcher
        # Per-channel cursor: chat_id -> highest message id already replayed.
        # Kept out of the DB on purpose — the pipeline's dedup layer is the
        # authority; this only avoids re-reading the same tail every sweep.
        self._state: Dict[str, int] = {}
        self._interval = max(2.0, float(interval))
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_top: Dict[str, int] = {}
        self._state_store = state_store

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        await self._restore_cursors()
        self._task = asyncio.create_task(self._run(), name="atf-channel-poll")
        logger.info(
            "channel polling started (interval=%.0fs)", self._interval
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The loop must survive a bad sweep; the next one will retry.
                logger.warning("channel poll sweep failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------ #
    # Sweeps
    # ------------------------------------------------------------------ #
    async def sweep_all(self) -> Tuple[int, int]:
        """One pass over every live session's active source channels."""
        sessions = self._pool.list_sessions() if hasattr(self._pool, "list_sessions") else []
        if not sessions:
            return (0, 0)

        replayed = 0
        checked = 0
        for session_id in sessions:
            client = self._pool.get(session_id)
            if client is None:
                continue
            try:
                channels = await self._source_channels(session_id)
            except Exception as exc:
                logger.debug("poll: cannot list channels for %s: %s", session_id[:8], exc)
                continue

            for chat_id, chat_name in channels:
                checked += 1
                try:
                    n = await self._poll_channel(client, session_id, chat_id, chat_name)
                except Exception as exc:
                    logger.warning("poll: channel check failed for %s (%s): %s", chat_name or chat_id, chat_id, exc)
                    continue
                replayed += n
                if n:
                    logger.info(
                        "poll: replayed %d new post(s) from %s (%s)", n, chat_name or chat_id, chat_id
                    )
        return (replayed, checked)

    async def _source_channels(self, session_id: str) -> List[Tuple[str, str]]:
        """Active source channels for this session's rules."""
        try:
            all_rules = await self._rules.list_all()
        except Exception as exc:
            logger.debug("poll: cannot list rules: %s", exc)
            return []

        seen: Dict[str, str] = {}
        for rule in all_rules:
            if not getattr(rule, "is_active", False):
                continue
            if getattr(rule, "is_paused", False):
                continue
            rid = getattr(rule, "session_id", "") or ""
            if rid and rid != session_id:
                continue
            src = str(getattr(rule, "source_chat_id", "") or "")
            if src and src not in seen:
                seen[src] = getattr(rule, "source_chat_name", "") or src
        return list(seen.items())

    async def _poll_channel(
        self, client, session_id: str, chat_id: str, chat_name: str
    ) -> int:
        """Pull posts newer than the channel cursor and replay them."""
        from pyrogram.raw.functions.updates import GetChannelDifference
        from pyrogram.raw.types import ChannelMessagesFilter, MessageRange

        try:
            peer = await client.resolve_peer(chat_id)
        except Exception as exc:
            logger.warning("poll: cannot resolve peer for %s (%s): %s", chat_name or chat_id, chat_id, exc)
            return 0

        # The channel pts is required by getChannelDifference.
        try:
            from pyrogram.raw.functions.channels import GetFullChannel

            full = await client.invoke(GetFullChannel(channel=peer))
        except Exception as exc:
            logger.warning("poll: GetFullChannel for %s (%s) failed: %s", chat_name or chat_id, chat_id, exc)
            return 0
        pts = getattr(getattr(full, "full_chat", None), "pts", 0) or 0

        cursor = self._state.get(chat_id, 0)
        try:
            top = await self._channel_top_message(client, chat_id)
        except Exception as exc:
            logger.warning("poll: cannot read top of %s (%s): %s", chat_name or chat_id, chat_id, exc)
            return 0

        logger.info(
            "[POLL TRACE] channel=%s (%s) cursor=%s top_msg_id=%s pts=%s",
            chat_name or chat_id, chat_id, cursor, top, pts,
        )

        if top is None or top <= cursor:
            # Nothing newer than what we already replayed.
            return 0

        lo = max(1, cursor + 1)
        hi = top
        logger.info(
            "[POLL TRACE] requesting difference for %s: range=[%d..%d] (pts=%s)",
            chat_name or chat_id, lo, hi, pts,
        )
        msg_filter = ChannelMessagesFilter(ranges=[MessageRange(min_id=lo, max_id=hi)])
        try:
            diff = await client.invoke(
                GetChannelDifference(
                    channel=peer, filter=msg_filter, pts=pts, limit=MAX_POSTS_PER_SWEEP
                )
            )
        except Exception as exc:
            logger.warning("poll: GetChannelDifference for %s (%s) failed: %s", chat_name or chat_id, chat_id, exc)
            return 0

        raw_messages = getattr(diff, "new_messages", None) or []
        messages = sorted(raw_messages, key=lambda m: getattr(m, "id", 0) or 0)
        logger.info(
            "[POLL TRACE] channel=%s difference returned %d raw message(s)",
            chat_name or chat_id, len(messages),
        )
        if not messages:
            # Keep the cursor moving so an empty difference does not refetch.
            self._state[chat_id] = top
            await self._persist_cursors()
            return 0

        users = {u.id: u for u in (getattr(diff, "users", None) or [])}
        chats = {c.id: c for c in (getattr(diff, "chats", None) or [])}

        replayed = 0
        for raw in messages:
            logger.info(
                "[POLL REPLAY] dispatching post id=%s from %s (%s) through pipeline",
                getattr(raw, "id", "?"), chat_name or chat_id, chat_id,
            )
            if self._replay_one(client, session_id, raw, users, chats):
                replayed += 1
        if replayed:
            self._state[chat_id] = max(self._state.get(chat_id, 0), messages[-1].id)
            await self._persist_cursors()
        return replayed

    async def _channel_top_message(self, client, chat_id: str) -> Optional[int]:
        """Newest post id of a channel via a one-shot history read.

        Uses get_chat_history rather than the dialog list: the read-marker in
        the dialog list is exactly what Telegram uses to decide whether to
        push, so it is not a reliable indicator of what exists.
        """
        try:
            async for m in client.get_chat_history(chat_id, limit=1):
                return int(getattr(m, "id", 0) or 0)
        except Exception as exc:
            logger.warning("poll: history read of %s failed: %s", chat_id, exc)
        return None

    # ------------------------------------------------------------------ #
    # Cursor persistence (survives restarts)
    # ------------------------------------------------------------------ #
    async def _restore_cursors(self) -> None:
        if self._state_store is None:
            return
        try:
            loaded = await self._state_store.load_channel_cursors()
            self._state.update({str(k): int(v) for k, v in (loaded or {}).items()})
        except Exception as exc:
            logger.warning("poll: cannot restore cursors: %s", exc)

    async def _persist_cursors(self) -> None:
        if self._state_store is None:
            return
        try:
            await self._state_store.save_channel_cursors(dict(self._state))
        except Exception as exc:
            logger.warning("poll: cannot persist cursors: %s", exc)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def _replay_one(self, client, session_id: str, raw_message, users: dict, chats: dict) -> bool:
        """Schedule one replay on the loop; True when a task was queued."""

        async def _one():
            handler = None
            getter = getattr(self._dispatcher, "message_handler", None)
            if callable(getter):
                try:
                    handler = getter(session_id)
                except Exception as h_err:
                    logger.warning("poll: getter(session_id) error for %s: %s", session_id[:8], h_err)
                    handler = None
            if handler is None:
                logger.warning(
                    "poll: no message handler registered for session %s — dropping post %s",
                    session_id[:8], getattr(raw_message, "id", "?"),
                )
                return
            try:
                from pyrogram.types import Message as PyroMessage

                # pyrogram's Message._parse is async in this version.
                msg = await PyroMessage._parse(client, raw_message, users, chats)  # noqa: SLF001
            except Exception as exc:
                logger.warning("poll: failed to parse post %s: %s", getattr(raw_message, "id", "?"), exc)
                return
            if msg is None:
                logger.warning("poll: parsed post %s returned None", getattr(raw_message, "id", "?"))
                return
            try:
                await handler(client, msg)
            except Exception as exc:
                logger.error(
                    "poll: dispatch handler failed for post %s: %s",
                    getattr(raw_message, "id", "?"), exc, exc_info=True,
                )

        try:
            asyncio.ensure_future(_one())
            return True
        except Exception as exc:
            logger.error("poll: dispatch schedule failed for post %s: %s", getattr(raw_message, "id", "?"), exc)
            return False
