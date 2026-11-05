"""Session lifecycle for the Telegram Runtime.

Python remains the authoritative owner of Telegram session *connectivity*;
the Go Worker references sessions through a stable session identifier and
never sees a session string, an api hash or a token.

This manager is deliberately a thin adapter over the Core's existing
``ClientPool``: the pool already carries the connection lifecycle, the per-
session locks, the watchdogs and the time-limit (FloodWait suspension) state
the whole product depends on. Reimplementing it here would duplicate session
ownership across two services — exactly the "second source of truth" the
migration must not create.

What lives here vs the Core:

* the Core's ClientPool owns the pyrogram clients, the session store and the
  connection state;
* this manager exposes the *readiness* and *lookup* operations the runtime's
  gRPC handlers need, with the fail-closed semantics the contract demands
  (a session without a live client is reported unavailable, never guessed).
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from telegram_runtime import errors

logger = logging.getLogger("atf.telegram_runtime.sessions")

__all__ = ["SessionManager"]


class SessionManager:
    """Readiness and client lookup over the Core's ClientPool."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # -- readiness -------------------------------------------------------- #

    def is_ready(self, session_id: str) -> tuple[bool, str]:
        """True only when the pool can serve MTProto operations right now.

        A session that is *connecting* is NOT ready — the caller must defer,
        not fail. A time-limited (FloodWait-suspended) session is reported
        with its remaining window so the caller can pass it to Go verbatim.

        An empty ``session_id`` asks whether the runtime can serve MTProto
        work for **any** session: the Go Worker's connectivity probe has no
        single session of its own (it delivers for all of them), so it probes
        with an empty id and must not be answered "not ready" by a validation
        error on a question the contract asks on purpose.
        """
        if not session_id:
            return self._any_ready()
        client = self._pool.get(session_id) if hasattr(self._pool, "get") else None
        if client is None:
            return False, "session_offline"
        if self._is_time_limited(session_id):
            remaining = self._remaining_limit(session_id)
            return False, f"session_time_limited:{remaining:.0f}s"
        is_connected = getattr(client, "is_connected", False)
        if not is_connected:
            return False, "session_connecting"
        return True, "ready"

    def _any_ready(self) -> tuple[bool, str]:
        """Pool-wide readiness: ready when at least one session is connected."""
        ids = self._session_ids()
        if not ids:
            return False, "no sessions"
        ready = [sid for sid in ids if self._session_is_connected(sid)]
        if not ready:
            return False, "session_connecting"
        return True, "ready"

    def _session_ids(self) -> List[str]:
        get_ids = getattr(self._pool, "list_sessions", None) or getattr(
            self._pool, "session_ids", None
        )
        if callable(get_ids):
            try:
                return [str(s) for s in get_ids()]  # type: ignore[arg-type]
            except Exception:
                pass
        sessions = getattr(self._pool, "sessions", None)
        if isinstance(sessions, dict):
            return [str(s) for s in sessions.keys()]
        return []

    def _session_is_connected(self, session_id: str) -> bool:
        client = self._pool.get(session_id) if hasattr(self._pool, "get") else None
        if client is None:
            return False
        if self._is_time_limited(session_id):
            return False
        return bool(getattr(client, "is_connected", False))

    def _is_time_limited(self, session_id: str) -> bool:
        check = getattr(self._pool, "is_time_limited", None)
        if check is None:
            return False
        try:
            return bool(check(session_id))
        except Exception as exc:  # never let a probe break a delivery
            logger.debug("time-limit probe failed for %s: %s", session_id[:8] if session_id else "?", exc)
            return False

    def _remaining_limit(self, session_id: str) -> float:
        remaining = getattr(self._pool, "remaining_limit_seconds", None)
        if remaining is None:
            return 0.0
        try:
            value = remaining(session_id)
            return float(value) if isinstance(value, (int, float)) else 0.0
        except Exception:
            return 0.0

    # -- client lookup ----------------------------------------------------- #

    def client_for(self, session_id: str) -> tuple[Optional[Any], Optional[errors.TelegramOperationError]]:
        """Resolve the live pyrogram client for a session id.

        Returns ``(client, None)`` on success and ``(None, typed_error)``
        when the session cannot serve — never a client that is not usable,
        and never an untyped exception.
        """
        if not session_id:
            return None, errors.failed(errors.INTERNAL_ERROR, "empty session id")
        ready, state = self.is_ready(session_id)
        if not ready:
            # A FloodWait suspension is not a failure of the job: report it
            # as the shared FLOOD_WAIT vocabulary so Go defers rather than
            # retrying against a suspended account.
            if state.startswith("session_time_limited"):
                return None, errors.failed(
                    errors.FLOOD_WAIT, f"session time-limited ({state})",
                    error_type="SESSION_TIME_LIMITED",
                )
            if state == "session_connecting":
                return None, errors.failed(
                    errors.SESSION_UNAVAILABLE, "session is connecting",
                    error_type="CONNECTING",
                )
            return None, errors.failed(
                errors.SESSION_UNAVAILABLE, f"no live client for session {session_id[:8]}",
                error_type="OFFLINE",
            )
        client = self._pool.get(session_id)
        if client is None:  # raced with a disconnect between the two calls
            return None, errors.failed(
                errors.SESSION_UNAVAILABLE, f"session {session_id[:8]} went offline",
                error_type="OFFLINE",
            )
        return client, None
