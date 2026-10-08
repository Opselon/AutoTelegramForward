"""Message dispatcher: bridges pyrogram events into the forwarding pipeline.

This is the real-time heart of the forwarder. For every live session it
registers four pyrogram handlers (new message, edited message, channel post,
edited channel post) plus media-group handling. Outbound traffic goes through
an async queue with per-rule delay and rate limiting, and every send fan-outs
to all targets of the matched rule.

Design notes:
  * The use case is pure and testable: `sender` port = this dispatcher.
  * Skipping logic (since_ts / ignore_edits / already-processed) lives in the
    use case; the dispatcher only translates events -> payload -> use case.
  * Multi-target fan-out and caption templates are handled here so the use
    case stays transport-agnostic (a single `sender(payload, rule, text,
    evaluation)` port).
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any

from ...domain.entities import MessagePayload
from ...domain.value_objects import ContentMode, ForwardMode
from .client_pool import ClientPool, payload_from_pyrogram
from .errors import tg_detail, tg_error, wait_seconds

logger = logging.getLogger(__name__)


class MessageDispatcher:
    """Registers handlers on live clients and executes sends per rule."""

    def __init__(
        self, pool: ClientPool, use_case, send_queue_size: int = 2000,
        error_log=None, metrics=None, rule_stats=None, log_client=None,
    ) -> None:
        self._pool = pool
        self._use_case = use_case
        # The pipeline's sender port is this class's `_send` coroutine.
        self._use_case.sender = self._send
        # (session_id, chat_id, message_id) -> asyncio.Event for album dedupe
        self._album_pending: dict = {}
        self._album_lock = asyncio.Lock()
        # send-rate bookkeeping: target -> deque of timestamps
        self._rate: dict = defaultdict(deque)
        self._max_per_minute = 25  # Telegram-friendly default per target
        self._bound_sessions: set = set()
        self._album_window = 2.5  # seconds to collect an album before sending
        self._error_log = error_log
        self._metrics = metrics
        self._rule_stats = rule_stats
        self._log_client = log_client

    # ------------------------------------------------------------------ #
    # Error + metrics sinks
    # ------------------------------------------------------------------ #
    async def _record_send_error(self, payload, rule, target, exc, severity: str) -> None:
        """Persist the exact Telegram error and bump the counters."""
        key, _sev, recoverable = tg_error(exc)
        if self._error_log is not None:
            try:
                await self._error_log.record(
                    category="forward", error_name=type(exc).__name__,
                    severity=severity, detail=tg_detail(exc),
                    recoverable=recoverable,
                    session_id=getattr(rule, "session_id", None),
                    rule_id=getattr(rule, "id", None),
                    chat_id=str(target),
                )
            except Exception:
                logger.debug("error_log write failed", exc_info=True)
        if self._rule_stats is not None:
            try:
                await self._rule_stats.bump(getattr(rule, "id", ""), errors=1)
            except Exception:
                pass
        if self._metrics is not None:
            try:
                await self._metrics.bump(errors=1)
            except Exception:
                pass
        if self._log_client is not None:
            try:
                self._log_client.error(
                    "core", "forward",
                    f"{type(exc).__name__}: {tg_detail(exc)}",
                )
            except Exception:
                pass

    async def _record_success(self, payload, rule, target) -> None:
        """Counters for a successful forward (message dedupe stays in the UC)."""
        if self._rule_stats is not None:
            try:
                await self._rule_stats.bump(
                    getattr(rule, "id", ""), forwarded=1)
            except Exception:
                pass
        if self._metrics is not None:
            try:
                await self._metrics.bump(forwarded=1)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Binding
    # ------------------------------------------------------------------ #
    async def bind_session(self, session_id: str) -> bool:
        """Attach the four real-time handlers to one live session client.

        Idempotent: calling twice for the same session is a no-op.
        """
        if session_id in self._bound_sessions:
            return True
        client = self._pool.get(session_id)
        if client is None:
            logger.warning("bind_session: no live client for %s", session_id[:8])
            return False

        handlers = [
            # pyrogram's MessageHandler already fires for private chats, groups,
            # supergroups AND channel posts — one registration covers every
            # chat type. EditedMessageHandler covers message/channel edits.
            (self._make_handler("NEW_MESSAGE"), "message"),
            (self._make_handler("EDITED_MESSAGE"), "edited_message"),
        ]
        for handler, event_type in handlers:
            try:
                self._pool.add_handler(session_id, handler, event_type)
            except Exception as exc:
                logger.warning("bind %s/%s failed: %s", session_id[:8], event_type, exc)
                return False
        self._bound_sessions.add(session_id)
        logger.info("real-time handlers bound for session %s", session_id[:8])
        return True

    async def unbind_session(self, session_id: str) -> None:
        self._bound_sessions.discard(session_id)
        try:
            self._pool.remove_handlers(session_id)
        except Exception:
            pass

    def is_bound(self, session_id: str) -> bool:
        return session_id in self._bound_sessions

    def set_rate_limit(self, per_minute: int) -> None:
        self._max_per_minute = max(1, int(per_minute))

    # ------------------------------------------------------------------ #
    # Incoming events
    # ------------------------------------------------------------------ #
    def _make_handler(self, trigger: str):
        async def _handler(client, message):
            try:
                payload: MessagePayload = payload_from_pyrogram(message)
            except Exception:
                logger.exception("payload translate failed")
                return
            # Album: only the first message of a media_group triggers the
            # pipeline; the rest are swallowed as duplicates of the same send.
            if payload.media_group_id:
                key = (str(client), payload.chat_id, payload.media_group_id)
                async with self._album_lock:
                    if key in self._album_pending and trigger == "NEW_MESSAGE":
                        return  # already queued by the first media of the album
                    self._album_pending[key] = time.monotonic()
                # schedule cleanup of the album marker
                asyncio.create_task(self._release_album(key))
            try:
                await self._use_case.process_message(payload, trigger=trigger)
            except Exception:
                logger.exception("Failed to process incoming message")
        _handler.__name__ = f"atf_{trigger.lower()}_handler"
        return _handler

    async def _release_album(self, key) -> None:
        await asyncio.sleep(self._album_window)
        async with self._album_lock:
            self._album_pending.pop(key, None)

    # Back-compat: the original API returned a single handler. Keep it.
    def register_handler(self, filter_cb=None):
        """Returns a pyrogram-compatible async handler (legacy API)."""
        return self._make_handler("NEW_MESSAGE")

    # ------------------------------------------------------------------ #
    # Outgoing sends (implements the use-case `sender` port)
    # ------------------------------------------------------------------ #
    async def _send(self, payload: MessagePayload, rule, text: str, evaluation) -> bool:
        targets = list(getattr(rule, "all_targets", [rule.target_chat_id]) or [rule.target_chat_id])
        if not targets:
            logger.warning("rule %s has no targets", getattr(rule, "id", "?")[:8])
            return False
        client = self._pool.get(rule.session_id)
        if client is None:
            logger.warning("No live client for session %s", rule.session_id)
            return False
        delay = float(getattr(rule, "delay_seconds", 0) or 0)
        if delay > 0:
            await asyncio.sleep(min(delay, 3600))
        ok_any = False
        for target in targets:
            await self._throttle(target)
            try:
                if await self._deliver(client, payload, rule, text, target):
                    ok_any = True
                    await self._record_success(payload, rule, target)
            except Exception as exc:
                # Exact Telegram error: keep the exception class + RPC message
                # in the log so the debug panel shows the real cause.
                key, sev, recoverable = tg_error(exc)
                logger.warning(
                    "send → %s failed: %s (severity=%s recoverable=%s)",
                    target, tg_detail(exc), sev, recoverable,
                )
                await self._record_send_error(payload, rule, target, exc, sev)
                # FloodWait is not a rule problem: respect the wait before retry.
                if recoverable:
                    await asyncio.sleep(min(wait_seconds(exc, default=5) + 1, 300))
                continue  # a multi-target rule must still try its other targets
        return ok_any

    async def _throttle(self, target: Any) -> None:
        """Token-bucket-ish: cap sends per target to N/minute."""
        now = time.monotonic()
        window = self._rate[str(target)]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self._max_per_minute:
            wait = 60 - (now - window[0]) + 0.5
            logger.debug("rate-limit: waiting %.1fs for %s", wait, target)
            await asyncio.sleep(min(wait, 65))
        window.append(time.monotonic())

    async def _deliver(self, client, payload: MessagePayload, rule, text: str, target: Any) -> bool:
        """Send one copy of the message to one target, honoring content mode."""
        mode = getattr(rule, "content_mode", ContentMode.AUTO)
        mode_val = getattr(mode, "value", mode)
        caption_tmpl = rule.custom_caption_template or None

        if rule.forward_mode == ForwardMode.DIRECT_FORWARD:
            await client.forward_messages(
                chat_id=target, from_chat_id=int(payload.chat_id),
                message_ids=[payload.message_id],
            )
            return True

        # COPY_MESSAGE path
        if mode_val == "TEXT_ONLY":
            if text:
                await client.send_message(chat_id=target, text=text)
                return True
            return False
        if mode_val == "MEDIA_ONLY":
            if payload.has_media:
                await client.copy_message(
                    chat_id=target, from_chat_id=int(payload.chat_id),
                    message_id=payload.message_id,
                )
                return True
            return False
        if payload.has_media:
            await client.copy_message(
                chat_id=target, from_chat_id=int(payload.chat_id),
                message_id=payload.message_id,
                caption=(text or None) if mode_val != "MEDIA_ONLY" else None,
            )
            if mode_val == "TEXT_AND_MEDIA" and text:
                await client.send_message(chat_id=target, text=text)
            return True
        body = text or caption_tmpl
        if not body:
            return False
        await client.send_message(chat_id=target, text=body)
        return True
