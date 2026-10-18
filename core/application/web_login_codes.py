"""Single-use dashboard login codes.

A dashboard link is a bearer secret, and a link in a Telegram chat outlives the
click: it sits in the chat history, in backups and in forwarded messages. So the
link must not carry a session token. It carries a *code* instead — random,
short-lived and burned on first use — and only the core that minted it can
exchange it for a session.

Two properties are load-bearing:

* **Single use.** ``burn`` marks the row used in the same UPDATE that matches
  it, and refuses when the update matched nothing. Two tabs racing on one link
  produce exactly one session, not two.
* **Only the code names the account.** There is no "mint for user N" path: a
  Telegram user id is public, so accepting one unauthenticated would let anyone
  sign in as anyone. That was the defect this module exists to remove.

Codes are stored hashed (SHA-256): a database read must not yield a working
link. The plaintext exists only in the bot's reply.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Long enough for a slow human, short enough that a leaked link dies quickly.
LOGIN_CODE_TTL_SECONDS = 600

# Rows past this age are worthless (expired or already used) and are removed
# opportunistically when a new code is issued.
_PRUNE_AFTER_SECONDS = 3600


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class WebLoginCodeService:
    """Issues and burns single-use login codes against the accounts database."""

    def __init__(self, db, ttl_seconds: int = LOGIN_CODE_TTL_SECONDS, clock=time.time) -> None:
        self._db = db
        self._ttl = int(ttl_seconds)
        self._clock = clock

    # ------------------------------------------------------------------ issue
    def issue(self, user_id: int, purpose: str = "dashboard") -> tuple[str, int]:
        """Return ``(code, expires_at)`` for one login. Never logged, never stored."""
        uid = int(user_id)
        if uid <= 0:
            raise ValueError("user_id is required")
        now = int(self._clock())
        expires_at = now + self._ttl
        code = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO web_login_codes "
            "(code_hash, user_id, created_at, expires_at, used_at, purpose) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (_digest(code), uid, now, expires_at, purpose),
        )
        logger.info(
            "[LOGIN-CODE] issued user_id=%s expires_at=%s purpose=%s", uid, expires_at, purpose
        )
        self._prune(now)
        return code, expires_at

    # ------------------------------------------------------------------- burn
    def burn(self, code: str) -> Optional[int]:
        """Consume ``code`` and return its user id, or ``None`` if it is not usable.

        A code is usable exactly once, before it expires. Anything else — unknown,
        expired, already used, blank — answers ``None`` so the caller can refuse
        without revealing which case it was.
        """
        raw = (code or "").strip()
        if not raw:
            return None
        digest = _digest(raw)
        now = int(self._clock())
        row = self._db.query_one(
            "SELECT user_id, expires_at, used_at FROM web_login_codes WHERE code_hash=?",
            (digest,),
        )
        if row is None:
            logger.warning("[LOGIN-CODE] refused reason=unknown")
            return None
        if row["used_at"] is not None:
            logger.warning("[LOGIN-CODE] refused reason=already_used user_id=%s", row["user_id"])
            return None
        if int(row["expires_at"] or 0) <= now:
            logger.warning("[LOGIN-CODE] refused reason=expired user_id=%s", row["user_id"])
            return None
        cur = self._db.execute(
            "UPDATE web_login_codes SET used_at=? "
            "WHERE code_hash=? AND used_at IS NULL AND expires_at>?",
            (now, digest, now),
        )
        # The UPDATE is the gate: a concurrent exchange of the same link gets
        # rowcount 0 here and is refused, so one link cannot open two sessions.
        if getattr(cur, "rowcount", 0) != 1:
            logger.warning("[LOGIN-CODE] refused reason=raced user_id=%s", row["user_id"])
            return None
        uid = int(row["user_id"])
        logger.info("[LOGIN-CODE] exchanged user_id=%s", uid)
        return uid

    # ------------------------------------------------------------------ prune
    def _prune(self, now: int) -> None:
        cutoff = now - self._ttl - _PRUNE_AFTER_SECONDS
        try:
            self._db.execute("DELETE FROM web_login_codes WHERE created_at < ?", (cutoff,))
        except Exception as exc:  # pruning must never fail an issue
            logger.warning("[LOGIN-CODE] prune failed: %s", exc)
