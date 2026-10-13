"""Deep MTProto raw API tracer and diagnostic instrumentation.

Hooks into pyrogram Client instances to trace:
  1. Outgoing MTProto RPC queries via client.invoke(query)
  2. Incoming MTProto raw updates via client.handle_updates(updates)
  3. Latency, payload summaries, PTS numbers, and error traces.

Zero secret leakage:
  * Uses redact() on all free-text fields and attributes.
  * Masks auth keys, session strings, hashes, and 2FA credentials.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .errors import redact

logger = logging.getLogger("atf.raw_api")

_SENSITIVE_ATTRS = frozenset({
    "api_hash", "phone_code", "phone_code_hash", "password", "session",
    "auth_key", "secret", "token", "bot_token", "hash", "password_hash",
})


def _summarize_raw_object(obj: Any, depth: int = 0) -> str:
    """Safe, bounded string summary of an MTProto raw object."""
    if obj is None:
        return "None"
    if depth > 2:
        return type(obj).__name__

    cls_name = type(obj).__name__

    # Primitives
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, str):
        if len(obj) > 80:
            return repr(redact(obj[:77]) + "...")
        return repr(redact(obj))
    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes len={len(obj)}>"

    # Lists / iterables
    if isinstance(obj, (list, tuple)):
        if not obj:
            return "[]"
        if len(obj) > 5:
            items = [_summarize_raw_object(x, depth + 1) for x in obj[:3]]
            return f"[{', '.join(items)}, ... ({len(obj)} items)]"
        items = [_summarize_raw_object(x, depth + 1) for x in obj]
        return f"[{', '.join(items)}]"

    # Dicts
    if isinstance(obj, dict):
        parts = []
        for k, v in list(obj.items())[:5]:
            if str(k).lower() in _SENSITIVE_ATTRS:
                parts.append(f"{k}=[REDACTED]")
            else:
                parts.append(f"{k}={_summarize_raw_object(v, depth + 1)}")
        return "{" + ", ".join(parts) + "}"

    # Generic MTProto object
    if hasattr(obj, "__dict__"):
        parts = []
        for k, v in vars(obj).items():
            if k.startswith("_"):
                continue
            if str(k).lower() in _SENSITIVE_ATTRS:
                parts.append(f"{k}=[REDACTED]")
            else:
                parts.append(f"{k}={_summarize_raw_object(v, depth + 1)}")
        if not parts:
            return cls_name
        return f"{cls_name}({', '.join(parts[:6])})"

    return redact(str(obj))


def _summarize_raw_update(update: Any) -> str:
    """Extract key diagnostic fields from an incoming MTProto update."""
    name = type(update).__name__
    parts = []

    # Common update fields
    msg = getattr(update, "message", None)
    if msg is not None:
        mid = getattr(msg, "id", None)
        if mid:
            parts.append(f"msg_id={mid}")
        peer = getattr(msg, "peer_id", None)
        if peer:
            cid = getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None) or getattr(peer, "user_id", None)
            if cid:
                parts.append(f"peer={cid}")
        date = getattr(msg, "date", None)
        if date:
            parts.append(f"date={date}")

    for attr in ("channel_id", "chat_id", "user_id", "pts", "pts_count", "qts", "max_id"):
        val = getattr(update, attr, None)
        if val is not None:
            parts.append(f"{attr}={val}")

    if not parts:
        return name
    return f"{name}({', '.join(parts)})"


class RawTelegramApiTracer:
    """Interceptors attached to a Pyrogram Client instance."""

    def __init__(self, client: Any, session_id: str = "", log_client: Any = None) -> None:
        self.client = client
        self.session_id = session_id[:8] if session_id else "default"
        self._log_client = log_client
        self._orig_invoke = None
        self._orig_handle_updates = None
        self._attached = False

    def attach(self) -> None:
        """Wrap client.invoke and client.handle_updates for live tracing."""
        if self._attached:
            return

        # 1. Wrap invoke (all outbound MTProto RPC calls)
        orig_invoke: Any = getattr(self.client, "invoke", None)
        if callable(orig_invoke):
            self._orig_invoke = orig_invoke

            async def _traced_invoke(query, *args, **kwargs):
                q_name = type(query).__name__
                summary = _summarize_raw_object(query)
                logger.info(
                    "[RAW MTPROTO >> %s] %s %s",
                    self.session_id, q_name, summary,
                )
                t0 = time.monotonic()
                try:
                    res = await orig_invoke(query, *args, **kwargs)
                    elapsed = (time.monotonic() - t0) * 1000
                    res_summary = _summarize_raw_object(res)
                    logger.info(
                        "[RAW MTPROTO << %s] %s OK (%.1fms): %s",
                        self.session_id, q_name, elapsed, res_summary,
                    )
                    return res
                except Exception as exc:
                    elapsed = (time.monotonic() - t0) * 1000
                    logger.warning(
                        "[RAW MTPROTO << %s] %s FAILED (%.1fms): %s: %s",
                        self.session_id, q_name, elapsed, type(exc).__name__, redact(str(exc)),
                    )
                    if self._log_client is not None:
                        try:
                            self._log_client.warn(
                                "core", "raw_api",
                                f"MTProto {q_name} failed: {type(exc).__name__}",
                                detail={"query": q_name, "error": redact(str(exc)), "ms": round(elapsed, 1)},
                            )
                        except Exception:
                            pass
                    raise

            self.client.invoke = _traced_invoke

        # 2. Wrap handle_updates (all inbound MTProto updates from Telegram)
        orig_handle_updates: Any = getattr(self.client, "handle_updates", None)
        if callable(orig_handle_updates):
            self._orig_handle_updates = orig_handle_updates

            async def _traced_handle_updates(updates, *args, **kwargs):
                try:
                    raw_list = getattr(updates, "updates", None)
                    if isinstance(raw_list, list):
                        for upd in raw_list:
                            logger.info(
                                "[RAW UPDATE >> %s] %s",
                                self.session_id, _summarize_raw_update(upd),
                            )
                    else:
                        logger.info(
                            "[RAW UPDATE >> %s] %s",
                            self.session_id, _summarize_raw_update(updates),
                        )
                except Exception as trace_err:
                    logger.debug("Failed logging raw update trace: %s", trace_err)

                return await orig_handle_updates(updates, *args, **kwargs)

            self.client.handle_updates = _traced_handle_updates

        self._attached = True
        logger.info("Raw MTProto API tracer attached to session %s", self.session_id)

    def detach(self) -> None:
        """Restore original invoke and handle_updates methods."""
        if not self._attached:
            return
        if self._orig_invoke is not None:
            self.client.invoke = self._orig_invoke
        if self._orig_handle_updates is not None:
            self.client.handle_updates = self._orig_handle_updates
        self._attached = False
