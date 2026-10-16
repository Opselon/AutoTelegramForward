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
import os
import time
from collections import defaultdict, deque
from typing import Any, Optional

from ...domain import branding
from ...domain.entities import MessagePayload
from ...domain.services import AdvancedSettingsEngine, CaptionManagementEngine, safe_text_preview
from ...domain.value_objects import ContentMode, ForwardMode
from .album_aggregator import AlbumAggregator
from .client_pool import ClientPool, payload_from_pyrogram
from .errors import tg_detail, tg_error, wait_seconds
from ..persistence.coercion import enum_str

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Protected-content bypass helpers (module level — no client state needed)
# --------------------------------------------------------------------------- #
# Channels that set "restrict forwarding/saving" make server-side
# forward_messages/copy_message fail with ChatForwardsRestricted. Telegram
# still hands the *content itself* to any member session, so we can rebuild
# the message from its parts. The InputMedia reference (photo/file id) is the
# highest-fidelity path: same quality, no disk I/O, and it survives when
# downloading is also blocked.
_MEDIA_ATTRIBUTES = (
    ("photo", "photo", "send_photo"),
    ("video", "video", "send_video"),
    ("audio", "audio", "send_audio"),
    ("voice", "voice", "send_voice"),
    ("animation", "animation", "send_animation"),
    ("document", "document", "send_document"),
    ("sticker", "sticker", "send_sticker"),
)


def _extract_media_reference(msg: Any) -> Optional[dict]:
    """Return ``{kind, ref, caption?, file_name?}`` for a message's media.

    ``ref`` is the raw ``InputMedia``/file-id object straight off the pyrogram
    message — it keeps original resolution and unique-ids. ``None`` when the
    message carries no usable media (pure text, service messages, polls...).
    """
    if msg is None:
        return None
    for attr, kind, _send in _MEDIA_ATTRIBUTES:
        media = getattr(msg, attr, None)
        if media is None:
            continue
        # pyrogram exposes ``file_id`` on the media object; the file-id string
        # is accepted directly by every ``send_*`` call.
        ref = getattr(media, "file_id", None)
        if not ref:
            continue
        out = {"kind": kind, "ref": ref}
        file_name = getattr(media, "file_name", None)
        if file_name:
            out["file_name"] = file_name
        caption = getattr(msg, "caption", None)
        if caption:
            out["caption"] = caption
        return out
    return None


async def _send_media_reference(client: Any, target: Any, ref: dict, caption: Optional[str]) -> Any:
    """Send a media reference (file-id) to ``target``. ``None`` if unsupported."""
    kind = ref.get("kind")
    cap = caption if caption is not None else ref.get("caption")
    senders = {
        "photo": "send_photo",
        "video": "send_video",
        "audio": "send_audio",
        "voice": "send_voice",
        "animation": "send_animation",
        "document": "send_document",
        "sticker": "send_sticker",
    }
    method_name = senders.get(kind) if isinstance(kind, str) else None
    method = getattr(client, method_name, None) if method_name else None
    if method is None:
        return None
    kw: dict = {}
    if kind == "photo":
        kw["photo"] = ref["ref"]
    elif kind == "sticker":
        kw["sticker"] = ref["ref"]
    else:
        kw[kind] = ref["ref"]
    if kind != "sticker":
        kw["caption"] = cap
    return await method(chat_id=target, **kw)


class MessageDispatcher:
    """Registers handlers on live clients and executes sends per rule."""

    def __init__(
        self, pool: ClientPool, use_case, send_queue_size: int = 2000,
        error_log=None, metrics=None, rule_stats=None, log_client=None,
        queue_manager=None, sync_engine=None, durable_pipeline=None,
        pv_responder=None,
    ) -> None:
        self._pool = pool
        self._use_case = use_case
        self._durable_pipeline = durable_pipeline
        # The pipeline's sender port is this class's `_send` coroutine.
        self._use_case.sender = self._send
        self._queue_manager = queue_manager
        self._sync_engine = sync_engine
        self._pv_responder = pv_responder
        # Album aggregation with bounded LRU & TTL
        self._album_aggregator = AlbumAggregator(flush_callback=self._flush_album, ttl_seconds=2.0, max_active_albums=500)
        # send-rate bookkeeping: target -> deque of timestamps
        self._rate: dict = defaultdict(deque)
        self._max_per_minute = 25  # Telegram-friendly default per target
        self._bound_sessions: set = set()
        # v15: session_id -> NEW_MESSAGE closure, exposed for channel catch-up
        self._message_handlers: dict = {}
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
                    f"Send error on rule {getattr(rule, 'id', '?')[:8]}: {type(exc).__name__} ({tg_detail(exc)})",
                    detail={
                        "rule_id": getattr(rule, "id", ""),
                        "source_chat_id": str(getattr(payload, "chat_id", "")),
                        "source_message_id": getattr(payload, "message_id", 0),
                        "target_chat_id": str(target),
                        "error_type": type(exc).__name__,
                        "error_detail": tg_detail(exc),
                        "severity": severity,
                        "recoverable": recoverable,
                    },
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
            (self._make_handler("NEW_MESSAGE", session_id=session_id), "message"),
            (self._make_handler("EDITED_MESSAGE", session_id=session_id), "edited_message"),
            (self._make_deleted_handler(), "deleted_messages"),
        ]
        for handler, event_type in handlers:
            try:
                self._pool.add_handler(session_id, handler, event_type)
            except Exception as exc:
                logger.warning("bind %s/%s failed: %s", session_id[:8], event_type, exc)
                return False
        self._bound_sessions.add(session_id)
        # v15: remember the NEW_MESSAGE closure so the channel catch-up
        # service can replay missed posts through the identical path.
        self._message_handlers[session_id] = handlers[0][0]
        logger.info("real-time handlers bound for session %s", session_id[:8])
        return True

    async def unbind_session(self, session_id: str) -> None:
        self._bound_sessions.discard(session_id)
        self._message_handlers.pop(session_id, None)
        try:
            self._pool.remove_handlers(session_id)
        except Exception:
            pass

    def is_bound(self, session_id: str) -> bool:
        return session_id in self._bound_sessions

    # ------------------------------------------------------------------ #
    # Catch-up support
    # ------------------------------------------------------------------ #
    def message_handler(self, session_id: str):
        """The NEW_MESSAGE closure used for this session.

        Returned so the channel catch-up service can replay missed channel
        posts through the exact same path as a live push update.
        """
        return self._message_handlers.get(session_id)

    def set_rate_limit(self, per_minute: int) -> None:
        self._max_per_minute = max(1, int(per_minute))

    # ------------------------------------------------------------------ #
    # Incoming events
    # ------------------------------------------------------------------ #
    def _make_deleted_handler(self):
        async def _del_handler(client, messages):
            if self._sync_engine is None or not messages:
                return
            try:
                chat_id = ""
                msg_ids = []
                for m in messages:
                    if hasattr(m, "chat") and m.chat:
                        chat_id = str(m.chat.id)
                    if hasattr(m, "id"):
                        msg_ids.append(int(m.id))
                if chat_id and msg_ids:
                    await self._sync_engine.handle_delete(chat_id, msg_ids)
            except Exception:
                logger.exception("Error in deleted_messages handler")
        return _del_handler

    def _make_handler(self, trigger: str, session_id: Optional[str] = None):
        async def _handler(client, message):
            try:
                payload: MessagePayload = payload_from_pyrogram(message)
            except Exception as tr_err:
                logger.error("payload translate failed: %s", tr_err, exc_info=True)
                return

            sid = str(session_id or getattr(client, "name", "") or getattr(client, "session_id", "session") or "session")
            eff_text = str(payload.effective_text or "")
            text_preview = safe_text_preview(eff_text, 60)
            logger.info(
                "[DISPATCH IN %s] %s chat=%s (%s) msg_id=%s media=%s text=%r",
                sid[:12], trigger, payload.chat_title or "", payload.chat_id, payload.message_id,
                getattr(payload.media_type, "value", payload.media_type),
                text_preview,
            )

            # AI PV Auto-Reply for direct messages on user accounts
            if trigger == "NEW_MESSAGE" and getattr(message, "chat", None) and str(getattr(message.chat, "type", "")).lower().endswith("private"):
                if self._pv_responder is not None:
                    try:
                        replied = await self._pv_responder.handle_message(client, message)
                        if replied:
                            return
                    except Exception as exc:
                        logger.warning("pv_responder handle_message failed: %s", exc)

            # Sync edits propagation
            if trigger == "EDITED_MESSAGE" and self._sync_engine is not None:
                try:
                    await self._sync_engine.handle_edit(payload, payload.effective_text)
                except Exception:
                    logger.exception("sync_engine handle_edit failed")

            # Album: bounded LRU aggregator with TTL
            if payload.media_group_id and trigger == "NEW_MESSAGE":
                sid = str(session_id or getattr(client, "session_id", str(client) or "session"))
                await self._album_aggregator.ingest(sid, payload)
                return

            try:
                if self._durable_pipeline is not None:
                    await self._durable_pipeline.process(payload, trigger=trigger)
                else:
                    await self._use_case.process_message(payload, trigger=trigger)
            except Exception:
                logger.exception("Failed to process incoming message")
        _handler.__name__ = f"atf_{trigger.lower()}_handler"
        return _handler

    async def _flush_album(self, first_msg: MessagePayload, all_msgs: list) -> None:
        try:
            first_msg.album_messages = list(all_msgs)
            if self._durable_pipeline is not None:
                await self._durable_pipeline.process(first_msg, trigger="NEW_MESSAGE")
            else:
                await self._use_case.process_message(first_msg, trigger="NEW_MESSAGE")
        except Exception:
            logger.exception("Failed to process flushed album")

    # Back-compat: the original API returned a single handler. Keep it.
    def register_handler(self, filter_cb=None):
        """Returns a pyrogram-compatible async handler (legacy API)."""
        return self._make_handler("NEW_MESSAGE")

    # ------------------------------------------------------------------ #
    # Outgoing sends (implements the use-case `sender` port)
    # ------------------------------------------------------------------ #
    async def _send(self, payload: MessagePayload, rule, text: str, evaluation) -> bool:
        # Phase 2: apply the advanced content transforms (watermark, link
        # buttons, affiliate kit) on the direct dispatch path too, so a rule
        # behaves identically whether or not the durable queue is configured.
        try:
            text, _applied = AdvancedSettingsEngine.apply_content_transforms(text, rule, payload)
        except Exception as exc:
            logger.warning("advanced content transform failed for rule %s: %s", getattr(rule, "id", "?")[:8], exc)

        # Phase 2: debug mode emits a structured diagnostic of exactly what the
        # worker is about to send, without ever logging session material.
        if bool(getattr(rule, "debug_mode", False)):
            logger.info(
                "[PIPELINE:DEBUG] rule=%s src=%s:%s tgt=%s topic=%s delay=%.1fs "
                "media=%s text_len=%d preview=%r",
                getattr(rule, "id", "?")[:8], payload.chat_id, payload.message_id,
                getattr(rule, "target_chat_id", "?"), int(getattr(rule, "topic_id", None) or 0),
                float(getattr(rule, "forward_delay_seconds", 0.0) or 0.0),
                bool(payload.has_media), len(text or ""),
                safe_text_preview(text, 60),
            )

        targets = list(getattr(rule, "all_targets", [rule.target_chat_id]) or [rule.target_chat_id])
        if not targets:
            logger.warning(
                "Rule %s (source %s) has no targets configured — message %s cannot be forwarded",
                getattr(rule, "id", "?")[:8],
                getattr(payload, "chat_id", "?"),
                getattr(payload, "message_id", "?"),
            )
            return False

        # Branding hop (A -> C -> B)
        # Our own channel C is posted first, then the final target receives a
        # *forward of C*, so its forward header names C — never the original
        # source channel. The decision is the domain's, not a local shortcut:
        # `should_brand_hop` is what the bot/dashboard preview shows, so the two
        # can never disagree about which messages take the hop.
        hop_msg_id = None
        branding_plan = None
        take_hop, hop_reason = self._brand_hop_decision(payload, rule)
        if take_hop:
            inter_id = branding.channel_id(rule)
            final_targets = [t for t in targets if str(t).strip() != inter_id]
            if final_targets and inter_id not in (str(payload.chat_id),):
                try:
                    hop_res = await self._hop_intermediate(payload, rule, text, inter_id)
                    if isinstance(hop_res, list) and hop_res:
                        hop_msg_id = getattr(hop_res[0], "id", None) or getattr(hop_res[0], "message_id", None)
                    elif hop_res is not None:
                        hop_msg_id = getattr(hop_res, "id", None) or getattr(hop_res, "message_id", None)
                    if hop_msg_id:
                        # Final delivery uses the intermediate as its source identity.
                        targets = final_targets
                        branding_plan = {
                            "chat_id": inter_id,
                            "message_id": int(hop_msg_id),
                            "force_forward": branding.force_forward(rule),
                            "reason": hop_reason,
                        }
                        logger.info(
                            "[PIPELINE:BRANDING] rule=%s %s -> C=%s msg=%s -> %d target(s) (%s)",
                            getattr(rule, "id", "?")[:8], payload.chat_id, inter_id,
                            hop_msg_id, len(targets), hop_reason,
                        )
                    else:
                        logger.warning(
                            "[PIPELINE:BRANDING] rule=%s hop to C=%s returned no message id — delivering directly",
                            getattr(rule, "id", "?")[:8], inter_id,
                        )
                except Exception as exc:
                    key, sev, recoverable = tg_error(exc)
                    logger.warning(
                        "intermediate hop → %s failed: %s (recoverable=%s) — delivering directly",
                        inter_id, tg_detail(exc), recoverable,
                    )
                    # Fall back to direct delivery without the intermediate hop.
            elif not final_targets:
                logger.warning(
                    "[PIPELINE:BRANDING] rule=%s has no target other than C=%s — delivering directly",
                    getattr(rule, "id", "?")[:8], inter_id,
                )

        # If durable queue manager is active, delegate delivery non-blockingly!
        if self._queue_manager is not None:
            any_queued = False
            for target in targets:
                # The hop is handed to the queue as *job state*, not as a local
                # variable: the worker is a different process pass and used to
                # receive source=A, so B was served a forward of A (the original
                # header) while an orphan copy stayed behind in C. Passing the
                # plan makes the queued delivery reproduce the hop exactly.
                ok = await self._queue_manager.enqueue_delivery(
                    rule, payload, str(target), text, evaluation, branding=branding_plan,
                )
                if ok:
                    any_queued = True
            return any_queued

        client = self._pool.get(rule.session_id)
        if client is None:
            logger.warning("No live client for session %s", rule.session_id)
            return False

        # Phase 2: the advanced dispatch delay is the authoritative setting.
        # The legacy `delay_seconds` field is preserved as a fallback so rules
        # created before the advanced UI keep their original behaviour.
        delay = float(getattr(rule, "forward_delay_seconds", None) or 0)
        if delay <= 0:
            delay = float(getattr(rule, "delay_seconds", 0) or 0)
        if delay > 0:
            await asyncio.sleep(min(delay, 3600))
        ok_any = False
        for target in targets:
            await self._throttle(target)
            try:
                if await self._deliver(client, payload, rule, text, target, intermediate_msg_id=hop_msg_id):
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

    def _brand_hop_decision(self, payload: MessagePayload, rule) -> tuple:
        """Should this message take the branding hop? (shared decision, see domain).

        Prefers the routing service the pipeline already holds; falls back to the
        module-level rule only when no service is wired (bare unit tests), so the
        production path and the preview path can never diverge.
        """
        routing = getattr(self._use_case, "_routing", None)
        if routing is not None and hasattr(routing, "should_brand_hop"):
            try:
                return routing.should_brand_hop(payload, rule)
            except Exception as exc:  # never let the decision break delivery
                logger.warning("branding decision failed (%s) — using the module rule", exc)
        return branding.should_hop(payload, rule)

    async def _hop_intermediate(self, payload: MessagePayload, rule, text: str, inter_id: str) -> Any:
        """Deliver the message to the intermediate (VIP hop) channel exactly once.

        Guarantees:
          * the message is never sent back to its own source chat,
          * idempotency is preserved by the queue/processed layer above,
          * the intermediate message is a COPY (no original author header),
            so the final forward header will read as the intermediate channel.
        """
        if str(payload.chat_id).strip() == inter_id.strip():
            logger.debug("intermediate hop skipped: target is the message source (%s)", inter_id)
            return None

        client = self._pool.get(rule.session_id)
        if client is None:
            logger.warning("intermediate hop: no live client for session %s", rule.session_id)
            raise RuntimeError(f"no_live_client:{rule.session_id}")

        await self._throttle(inter_id)
        try:
            res = await self._deliver_copy(
                client, payload, rule, text, inter_id,
                enum_str(getattr(rule, "content_mode", None), "AUTO"),
                rule.custom_caption_template or None,
            )
            logger.debug("intermediate hop → %s ok: %s", inter_id, bool(res))
            return res
        except Exception as exc:
            # record the exact Telegram error for the diagnostics panel
            key, sev, recoverable = tg_error(exc)
            await self._record_send_error(payload, rule, inter_id, exc, sev)
            raise

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

    async def _deliver(
        self, client, payload: MessagePayload, rule, text: str, target: Any,
        intermediate_msg_id: Optional[int] = None,
    ) -> Any:
        """Send one copy of the message to one target, honoring forward mode
        with a **definitive Copy fallback**. Never silently falls back to
        native forward with original author header."""
        mode = getattr(rule, "content_mode", ContentMode.AUTO)
        mode_val = getattr(mode, "value", mode)
        caption_tmpl = rule.custom_caption_template or None

        f_mode_val = getattr(getattr(rule, "forward_mode", None), "value", str(getattr(rule, "forward_mode", "COPY_MESSAGE")))

        # Phase 2: route into the configured forum topic. Only a positive id
        # addresses a real thread; 0/None means "no topic configured" and must
        # not be passed (Telegram would reject a zero thread id).
        topic_id = int(getattr(rule, "topic_id", None) or 0)
        thread_kwargs: dict = {}
        if topic_id > 0:
            thread_kwargs["message_thread_id"] = topic_id

        # If the branding hop ran, stage 2 starts from C — never from A.
        if intermediate_msg_id and branding.enabled(rule):
            inter_id_raw = branding.channel_id(rule)
            inter_chat = int(inter_id_raw) if inter_id_raw.lstrip("-").isdigit() else inter_id_raw
            if branding.force_forward(rule):
                try:
                    res = await client.forward_messages(
                        chat_id=target, from_chat_id=inter_chat,
                        message_ids=[int(intermediate_msg_id)],
                        **thread_kwargs,
                    )
                    return res[0] if isinstance(res, list) and res else (res or True)
                except Exception as exc:
                    key, sev, recoverable = tg_error(exc)
                    if not self._fallback_allowed(rule, key, recoverable):
                        logger.warning(
                            "forward from C (%s) → %s failed (%s) — fallback disabled",
                            inter_chat, target, tg_detail(exc),
                        )
                        raise
                    logger.warning(
                        "forward from C (%s) → %s failed (%s) — falling back to a clean copy of C's post",
                        inter_chat, target, tg_detail(exc),
                    )
            # A clean copy of C's own post: still branded content, and still never
            # the original source's forward header.
            return await self._deliver_copy(
                client, payload, rule, text, target, mode_val, caption_tmpl, thread_kwargs,
                src_chat=inter_chat, src_msg_id=int(intermediate_msg_id),
            )

        # A. NATIVE FORWARD — preserves Telegram's real forward header
        if f_mode_val == "DIRECT_FORWARD":
            try:
                res = await client.forward_messages(
                    chat_id=target, from_chat_id=int(payload.chat_id),
                    message_ids=[payload.message_id],
                    **thread_kwargs,
                )
                return res[0] if isinstance(res, list) and res else (res or True)
            except Exception as exc:
                # classify the real Telegram API error
                key, sev, recoverable = tg_error(exc)
                if not self._fallback_allowed(rule, key, recoverable):
                    logger.warning(
                        "send → %s: native forward failed (%s) — fallback disabled for this error class",
                        target, tg_detail(exc),
                    )
                    raise
                logger.warning(
                    "send → %s: native forward failed (%s, %s) — definitive COPY fallback",
                    target, tg_detail(exc), "recoverable" if recoverable else "permanent",
                )
                # fall through to copy path (never back to forward)

        # B. COPY MESSAGE path (also the fallback destination)
        try:
            return await self._deliver_copy(client, payload, rule, text, target, mode_val, caption_tmpl, thread_kwargs)
        except Exception as exc:
            key, sev, recoverable = tg_error(exc)
            if recoverable and f_mode_val == "DIRECT_FORWARD" and self._fallback_allowed(rule, key, True):
                # last-resort: bare text message without any header or media
                logger.warning("send → %s: copy failed (%s) — bare text fallback", target, tg_detail(exc))
                body = (payload.effective_text or caption_tmpl or "").strip()
                if body:
                    res = await client.send_message(chat_id=target, text=body)
                    return res or True
            raise

    @staticmethod
    def _fallback_allowed(rule, error_key: str, recoverable: bool) -> bool:
        """Copy fallback is allowed when native forward is blocked by channel permissions,
        protected content, or Telegram forward restrictions.
        Fallback destination is ALWAYS Copy Message (never native forward)."""
        # The explicit opt-out always wins: a rule with fallback_enabled=False must
        # never be rescued by copy/re-upload, even when fallback_mode was coerced
        # back to COPY_MESSAGE by ForwardRule.__post_init__.
        if not bool(getattr(rule, "fallback_enabled", True)):
            return False

        err_l = str(error_key or "").lower()

        # Destination write blocks — cannot deliver at all
        unrecoverable_target_errors = (
            "unauthorized", "authkeyunregistered", "inputuserdeactivated",
            "kicked", "banned", "channelprivate", "userbannedinchannel",
        )
        if any(m in err_l for m in unrecoverable_target_errors):
            return False

        # Forward-specific restrictions are primary triggers for clean copy fallback!
        forward_restriction_markers = (
            "forward", "restrict", "protected", "copy", "permission", "notallowed",
            "chatforwardsrestricted", "chatadminrequired",
        )
        if any(m in err_l for m in forward_restriction_markers):
            return True

        return recoverable

    async def _reupload_media(self, client, payload: MessagePayload, target: Any, caption: Optional[str], mode_val: str) -> Any:
        """Download and re-upload media when server-side copy is blocked by channel content protection.

        Bypass layers, in order of fidelity:
          1. server-side copy (already failed before reaching here),
          2. download + re-upload the original media file (loses nothing but
             the "forwarded from" header),
          3. extract the media's input-media reference (photo/file id) and send
             it directly — works when the channel forbids *forwarding* but the
             media itself is still publicly fetchable by the session,
          4. text-only delivery as the last resort so the message body and its
             caption are never silently dropped.
        """
        temp_path = None
        try:
            src_msg = None
            if hasattr(client, "get_messages"):
                try:
                    src_msg = await client.get_messages(chat_id=int(payload.chat_id), message_ids=payload.message_id)
                except Exception as get_err:
                    logger.debug("get_messages failed during media re-upload: %s", get_err)

            # Layer 3 first: media references work even when the download is
            # blocked, and they keep the original quality without touching disk.
            if src_msg is not None:
                ref = _extract_media_reference(src_msg)
                if ref is not None:
                    try:
                        sent = await _send_media_reference(client, target, ref, caption if mode_val != "MEDIA_ONLY" else None)
                        if sent is not None:
                            return sent
                    except Exception as ref_err:
                        logger.debug("media-reference send failed (%s) — falling back to download", ref_err)

            # Layer 2: download the original file and re-upload it.
            if src_msg is not None and hasattr(client, "download_media"):
                try:
                    temp_path = await client.download_media(src_msg)
                except Exception as dl_err:
                    logger.debug("download_media for protected message failed: %s", dl_err)

            if temp_path and os.path.exists(temp_path):
                mtype = payload.media_type.value if hasattr(payload.media_type, "value") else str(payload.media_type)
                m_caption = caption if mode_val != "MEDIA_ONLY" else None
                if mtype == "photo" and hasattr(client, "send_photo"):
                    return await client.send_photo(chat_id=target, photo=temp_path, caption=m_caption)
                elif mtype in ("video", "round_video") and hasattr(client, "send_video"):
                    return await client.send_video(chat_id=target, video=temp_path, caption=m_caption)
                elif mtype == "voice" and hasattr(client, "send_voice"):
                    return await client.send_voice(chat_id=target, voice=temp_path, caption=m_caption)
                elif mtype == "audio" and hasattr(client, "send_audio"):
                    return await client.send_audio(chat_id=target, audio=temp_path, caption=m_caption)
                elif mtype == "animation" and hasattr(client, "send_animation"):
                    return await client.send_animation(chat_id=target, animation=temp_path, caption=m_caption)
                elif hasattr(client, "send_document"):
                    return await client.send_document(chat_id=target, document=temp_path, caption=m_caption)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

        # Layer 4: nothing could be re-uploaded — still deliver the text body.
        if caption and mode_val != "MEDIA_ONLY":
            return await client.send_message(chat_id=target, text=caption)
        # A text-only protected message (no media at all) still deserves delivery.
        fallback_text = (caption or payload.effective_text or "").strip()
        if fallback_text:
            return await client.send_message(chat_id=target, text=fallback_text)
        raise RuntimeError("Protected content cannot be copied or re-uploaded")

    async def _deliver_copy(self, client, payload, rule, text, target, mode_val, caption_tmpl, thread_kwargs: Optional[dict] = None, src_chat: Any = None, src_msg_id: Optional[int] = None) -> Any:
        """Copy (or re-upload) one message to ``target``.

        ``src_chat``/``src_msg_id`` default to the incoming message, and are only
        overridden for the branding hop's stage 2 — where the content must be read
        back from our own channel C rather than from the original source.
        """
        thread_kwargs = thread_kwargs or {}
        src_chat = int(payload.chat_id) if src_chat is None else src_chat
        src_msg_id = int(payload.message_id) if src_msg_id is None else int(src_msg_id)
        # CUSTOM_HEADER_COPY / COPY_MESSAGE:
        if payload.has_media:
            raw_caption = text or caption_tmpl or ""
            split_long = bool(getattr(rule, "split_long_caption", True))
            formatted_caption, shifted_ents, overflow_text = CaptionManagementEngine.format_caption(
                raw_caption, split_overflow=split_long
            )
            res = None
            try:
                res = await client.copy_message(
                    chat_id=target, from_chat_id=src_chat,
                    message_id=src_msg_id,
                    caption=(formatted_caption or None) if mode_val != "MEDIA_ONLY" else None,
                    **thread_kwargs,
                )
            except Exception as copy_exc:
                err_str = str(copy_exc).lower()
                if any(k in err_str for k in ("forward", "restrict", "protected", "chatforwardsrestricted", "not_allowed")):
                    logger.info("copy_message blocked by channel content protection (%s) — downloading & re-uploading content", copy_exc)
                    res = await self._reupload_media(client, payload, target, formatted_caption, mode_val)
                else:
                    raise copy_exc

            if overflow_text and mode_val != "MEDIA_ONLY":
                try:
                    await client.send_message(chat_id=target, text=overflow_text, **thread_kwargs)
                except Exception as exc:
                    logger.debug("Failed sending caption overflow text: %s", exc)
            elif mode_val == "TEXT_AND_MEDIA" and text and not overflow_text:
                await client.send_message(chat_id=target, text=text, **thread_kwargs)
            return res or True

        if mode_val == "MEDIA_ONLY":
            return False

        body = text or caption_tmpl
        if not body:
            return False
        res = await client.send_message(chat_id=target, text=body, **thread_kwargs)
        return res or True
