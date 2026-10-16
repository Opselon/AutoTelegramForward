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

# How long a sweep waits for a channel's replay lane to hand every post to the
# pipeline before giving up. The cursor only advances once the lane is drained,
# so a timeout is safe: the same posts are read again next sweep and the
# pipeline's dedup layer drops the ones already accepted.
REPLAY_DRAIN_TIMEOUT_SECONDS = 120.0


class _OrderedReplayLane:
    """A per-channel FIFO that hands replayed posts to the pipeline in order.

    A channel difference is a *sequence*: posts carry a total order (ascending
    message id) and the target channel must receive them in exactly that order.
    Scheduling every post as its own task — the previous behaviour — let a
    50-post burst race through parsing and the pipeline concurrently, so the
    durable queue was filled in completion order and the target channel received
    the burst shuffled.

    One consumer per channel keeps the order exact (submit order is message-id
    order) while leaving the sweep loop free to keep polling other channels.
    """

    def __init__(self, key: Tuple[str, str], worker) -> None:
        self.key = key
        self._worker = worker
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self.submitted = 0
        self.completed = 0
        self.failed = 0

    def submit(self, item) -> None:
        self._queue.put_nowait(item)
        self.submitted += 1
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._worker(item)
                self.completed += 1
            except Exception:
                # One bad post must never kill the lane: the posts behind it are
                # still ordered work that has to go out.
                self.failed += 1
                logger.exception("poll: ordered replay failed for %r", self.key)
            finally:
                self._queue.task_done()

    async def join(self, timeout: Optional[float] = None) -> bool:
        """Wait until every submitted post has been handed to the pipeline.

        Returns True when the queue drained, False on timeout.
        """
        if self._queue.empty():
            return True
        if timeout is None:
            timeout = REPLAY_DRAIN_TIMEOUT_SECONDS
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "poll: replay lane %r still has %d post(s) pending after %.0fs",
                self.key, self._queue.qsize(), timeout,
            )
            return False

    def idle(self) -> bool:
        return self._queue.empty() and (self._task is None or self._task.done())

    def pending(self) -> int:
        return self._queue.qsize()


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
        # Ordered replay lanes, keyed by (session_id, chat_id). One live lane per
        # polled channel is what guarantees a burst leaves in source order.
        self._lanes: Dict[Tuple[str, str], _OrderedReplayLane] = {}

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

        target_peer = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        try:
            peer = await client.resolve_peer(target_peer)
        except Exception:
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
            if self._replay_one(client, session_id, raw, users, chats, chat_id=chat_id):
                replayed += 1
        if replayed:
            # Wait for the lane to hand every post to the pipeline *before*
            # advancing the cursor. Advancing first would let a crash (or a
            # restart) skip a tail that was never accepted; the cursor is the
            # only record of what has been seen.
            lane = self._lane_for(session_id, chat_id)
            if await lane.join():
                self._state[chat_id] = max(self._state.get(chat_id, 0), messages[-1].id)
                await self._persist_cursors()
            else:
                logger.warning(
                    "poll: not advancing cursor for %s (%s) — %d post(s) still in the replay lane",
                    chat_name or chat_id, chat_id, lane.pending(),
                )
        self._prune_lanes()
        return replayed

    async def _channel_top_message(self, client, chat_id: str) -> Optional[int]:
        """Newest post id of a channel via a one-shot history read.

        Uses get_chat_history rather than the dialog list: the read-marker in
        the dialog list is exactly what Telegram uses to decide whether to
        push, so it is not a reliable indicator of what exists.
        """
        target = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
        try:
            async for m in client.get_chat_history(target, limit=1):
                return int(getattr(m, "id", 0) or 0)
        except Exception:
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

    def _lane_for(self, session_id: str, chat_id: str) -> _OrderedReplayLane:
        key = (str(session_id), str(chat_id))
        lane = self._lanes.get(key)
        if lane is None:
            lane = _OrderedReplayLane(key, self._dispatch_one)
            self._lanes[key] = lane
        return lane

    def _prune_lanes(self) -> None:
        """Drop lanes that have nothing pending so a long-lived process does not
        accumulate one lane object per channel ever polled."""
        for key in [k for k, lane in self._lanes.items() if lane.idle()]:
            self._lanes.pop(key, None)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def _replay_one(
        self, client, session_id: str, raw_message, users: dict, chats: dict,
        chat_id: str = "",
    ) -> bool:
        """Submit one replayed post to its channel's ordered lane.

        True when the post was accepted. Posts for one channel are handed to the
        pipeline strictly in ascending message-id order, one at a time — the
        sweep itself does not block on them.
        """
        try:
            lane = self._lane_for(session_id, chat_id or str(getattr(raw_message, "chat_id", "") or ""))
            lane.submit((client, session_id, raw_message, users, chats))
            return True
        except Exception as exc:
            logger.error("poll: dispatch schedule failed for post %s: %s", getattr(raw_message, "id", "?"), exc)
            return False

    async def _dispatch_one(self, item) -> None:
        """Lane worker: parse and hand one post to the registered handler."""
        client, session_id, raw_message, users, chats = item
        handler = None
        getter = getattr(self._dispatcher, "message_handler", None)
        if callable(getter):
            try:
                handler = getter(session_id)
            except Exception as h_err:
                logger.warning("poll: getter(session_id) error for %s: %s", session_id[:8], h_err)
                handler = None
        if handler is None:
            raise RuntimeError(
                f"no message handler registered for session {session_id[:12]} — "
                f"cannot dispatch post {getattr(raw_message, 'id', '?')}"
            )
        from pyrogram.types import Message as PyroMessage

        # pyrogram's Message._parse is async in this version.
        msg = await PyroMessage._parse(client, raw_message, users, chats)  # noqa: SLF001
        if msg is None:
            raise RuntimeError(f"parsed post {getattr(raw_message, 'id', '?')} returned None")
        await handler(client, msg)
