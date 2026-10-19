"""Durable account-level suspensions (Telegram flood waits / restrictions).

`ClientPool` kept the ``session -> until`` map in memory. That is enough for
one process and nothing else: a Core restart forgot a 20-minute restriction
(and immediately hammered Telegram again, extending it), a second replica had
its own empty map and kept sending, and nothing outside the worker could see
why an account had gone quiet.

The row is the coordination point. Every process reads it before it decides an
account is free (``ClientPool.is_time_limited``), the duration is Telegram's
own, and the row is deleted the moment it expires so "resumed" is a fact in the
database rather than a guess in one process's dict.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("atf.account_limits")

__all__ = ["AccountLimit", "AccountLimitStore"]


class AccountLimit:
    __slots__ = ("session_id", "until", "code", "reason", "updated_at")

    def __init__(self, session_id: str, until: float, code: str = "", reason: str = "",
                 updated_at: float = 0.0) -> None:
        self.session_id = session_id
        self.until = float(until or 0)
        self.code = code or ""
        self.reason = reason or ""
        self.updated_at = float(updated_at or 0)

    @property
    def remaining(self) -> float:
        return max(0.0, self.until - time.time())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "until": self.until,
            "code": self.code,
            "reason": self.reason,
            "remaining": round(self.remaining, 1),
        }


class AccountLimitStore:
    """The `account_limits` table, written only from a real Telegram wait.

    Writes are idempotent and monotonic: a shorter wait never shortens an
    existing suspension (Telegram's later answer is not a pardon), while a
    longer one extends it.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    def set(self, session_id: str, until: float, *, code: str = "", reason: str = "") -> None:
        if not session_id or until <= 0:
            return
        now = int(time.time())
        try:
            current = self.get(session_id)
            if current is not None and current.until >= until:
                return
            self._db.execute(
                "INSERT INTO account_limits (session_id, until, code, reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "until = excluded.until, code = excluded.code, "
                "reason = excluded.reason, updated_at = excluded.updated_at",
                (session_id, float(until), code, reason[:300], now),
            )
            self._db.commit() if hasattr(self._db, "commit") else None
        except Exception as exc:
            logger.warning("[ACCOUNT:TIME_LIMIT] could not persist suspension for %s: %s",
                           session_id[:8], exc)

    def get(self, session_id: str) -> Optional[AccountLimit]:
        try:
            row = self._db.query_one(
                "SELECT session_id, until, code, reason, updated_at "
                "FROM account_limits WHERE session_id = ?", (session_id,),
            )
        except Exception:
            return None
        if not row:
            return None
        return AccountLimit(row["session_id"], row["until"], row["code"], row["reason"], row["updated_at"])

    def clear(self, session_id: str) -> None:
        try:
            self._db.execute("DELETE FROM account_limits WHERE session_id = ?", (session_id,))
            self._db.commit() if hasattr(self._db, "commit") else None
        except Exception as exc:
            logger.debug("could not clear suspension for %s: %s", session_id[:8], exc)

    def list_active(self, now: Optional[float] = None) -> List[AccountLimit]:
        cutoff = float(now if now is not None else time.time())
        try:
            rows = self._db.query_all(
                "SELECT session_id, until, code, reason, updated_at "
                "FROM account_limits WHERE until > ?", (cutoff,),
            )
        except Exception:
            return []
        return [AccountLimit(r["session_id"], r["until"], r["code"], r["reason"], r["updated_at"])
                for r in rows]

    def purge_expired(self, now: Optional[float] = None) -> int:
        cutoff = float(now if now is not None else time.time())
        try:
            cur = self._db.execute("DELETE FROM account_limits WHERE until <= ?", (cutoff,))
            self._db.commit() if hasattr(self._db, "commit") else None
            return int(getattr(cur, "rowcount", 0) or 0)
        except Exception:
            return 0
