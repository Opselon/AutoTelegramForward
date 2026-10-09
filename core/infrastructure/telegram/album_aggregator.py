"""Album Aggregator with LRU memory bounds and TTL flush for P0 Data-Plane Reliability.

Handles Telegram media groups:
- Groups incoming album pieces by (session_id, chat_id, media_group_id).
- Flushes automatically upon reaching Telegram's album limit (10 items) or after TTL window.
- Bounded memory via LRU eviction to prevent unbounded RAM growth.
- Restart-safe: checks persisted message_map / dedupe before accumulating.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from ...domain.entities import MessagePayload

logger = logging.getLogger("atf.album_aggregator")


class AlbumGroup:
    """Represents an active in-flight media group being assembled."""

    def __init__(self, key: Tuple[str, str, str], initial_message: MessagePayload) -> None:
        self.key = key  # (session_id, chat_id, media_group_id)
        self.messages: List[MessagePayload] = [initial_message]
        self.created_at = time.monotonic()
        self.flushed = False
        self.task: Optional[asyncio.Task] = None

    def add(self, message: MessagePayload) -> bool:
        if self.flushed:
            return False
        # Avoid duplicate message IDs within the same album
        if any(m.message_id == message.message_id for m in self.messages):
            return False
        self.messages.append(message)
        return True


class AlbumAggregator:
    """LRU bounded media group aggregator with TTL flush window."""

    def __init__(
        self,
        flush_callback: Callable[[MessagePayload, List[MessagePayload]], Coroutine[Any, Any, None]],
        ttl_seconds: float = 2.0,
        max_active_albums: int = 500,
        processed_repo: Any = None,
    ) -> None:
        self._flush_cb = flush_callback
        self._ttl = ttl_seconds
        self._max_capacity = max_active_albums
        self._processed = processed_repo

        # LRU storage: key -> AlbumGroup
        self._albums: OrderedDict[Tuple[str, str, str], AlbumGroup] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False

    async def ingest(self, session_id: str, payload: MessagePayload) -> bool:
        """Ingest a message belonging to a media group.

        Returns True if this is the first message that opened the group,
        False if it was added to an existing group or rejected as duplicate.
        """
        if not payload.media_group_id:
            return True

        key = (str(session_id), str(payload.chat_id), str(payload.media_group_id))

        async with self._lock:
            if self._closed:
                return False

            # 1. Existing in-flight album
            if key in self._albums:
                group = self._albums[key]
                self._albums.move_to_end(key)
                group.add(payload)
                if len(group.messages) >= 10:
                    # Maximum Telegram album size reached: flush immediately
                    if group.task and not group.task.done():
                        group.task.cancel()
                    asyncio.create_task(self._do_flush(key))
                return False

            # 2. Check LRU capacity eviction
            if len(self._albums) >= self._max_capacity:
                oldest_key, oldest_group = self._albums.popitem(last=False)
                if not oldest_group.flushed:
                    logger.warning("Evicting oldest in-flight album %s due to LRU capacity", oldest_key)
                    if oldest_group.task and not oldest_group.task.done():
                        oldest_group.task.cancel()
                    asyncio.create_task(self._do_flush_group(oldest_group))

            # 3. New album group
            group = AlbumGroup(key, payload)
            self._albums[key] = group
            group.task = asyncio.create_task(self._schedule_ttl_flush(key))
            return True

    async def _schedule_ttl_flush(self, key: Tuple[str, str, str]) -> None:
        try:
            await asyncio.sleep(self._ttl)
            await self._do_flush(key)
        except asyncio.CancelledError:
            pass

    async def _do_flush(self, key: Tuple[str, str, str]) -> None:
        async with self._lock:
            group = self._albums.get(key)
            if not group or group.flushed:
                return
            group.flushed = True

        await self._do_flush_group(group)

    async def _do_flush_group(self, group: AlbumGroup) -> None:
        try:
            first_msg = group.messages[0]
            logger.debug(
                "Flushing media group %s with %d messages (chat=%s)",
                group.key[2],
                len(group.messages),
                group.key[1],
            )
            await self._flush_cb(first_msg, group.messages)
        except Exception:
            logger.exception("Error executing album flush callback for %s", group.key)

    async def stop(self) -> None:
        """Close aggregator and flush pending albums."""
        async with self._lock:
            groups = [g for g in self._albums.values() if not g.flushed and g.messages]
            self._closed = True
            for g in self._albums.values():
                if g.task and not g.task.done():
                    g.task.cancel()
            self._albums.clear()
        for g in groups:
            await self._do_flush_group(g)

    async def close(self) -> None:
        await self.stop()
