"""Unified Authorization Policy and Administrator Management Service.

Enforces strict fail-closed authorization across Telegram bot updates,
gRPC servicers, API middleware, and web dashboard sessions.
Primary identity is Telegram's immutable numeric 64-bit integer ID.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set

from ..infrastructure.persistence.database import SqliteDatabase

logger = logging.getLogger("atf.authorization")


class AdminService:
    """Authoritative administrative and tenant security boundary service."""

    def __init__(
        self,
        db: SqliteDatabase,
        initial_admin_ids: Optional[List[int]] = None,
        owner_user_id: int = 0,
    ) -> None:
        self._db = db
        self._owner_user_id = int(owner_user_id or 0)
        self._in_memory_admins: Set[int] = set()

        if self._owner_user_id > 0:
            self._in_memory_admins.add(self._owner_user_id)

        for admin_id in initial_admin_ids or []:
            try:
                aid = int(admin_id)
                if aid > 0:
                    self._in_memory_admins.add(aid)
            except (ValueError, TypeError):
                continue

        self._bootstrap()

    def _bootstrap(self) -> None:
        """Persist configured owner and initial admins into the database idempotently."""
        now = int(time.time())
        try:
            if self._owner_user_id > 0:
                self._db.execute(
                    "INSERT INTO administrators (user_id, role, added_by, created_at) "
                    "VALUES (?, 'owner', 0, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET role='owner'",
                    (self._owner_user_id, now),
                )
                self._db.execute(
                    "UPDATE users SET is_admin=1, updated_at=? WHERE user_id=?",
                    (now, self._owner_user_id),
                )

            for aid in self._in_memory_admins:
                role = "owner" if aid == self._owner_user_id else "admin"
                self._db.execute(
                    "INSERT OR IGNORE INTO administrators (user_id, role, added_by, created_at) "
                    "VALUES (?, ?, 0, ?)",
                    (aid, role, now),
                )
                self._db.execute(
                    "UPDATE users SET is_admin=1, updated_at=? WHERE user_id=?",
                    (now, aid),
                )
        except Exception as exc:
            logger.warning("Admin bootstrap error (continuing with in-memory set): %s", exc)

    def _clean_uid(self, user_id: Any) -> Optional[int]:
        """Convert arbitrary identity representations into a positive numeric Telegram ID.

        Strict fail-closed: returns None for 0, None, booleans, negative numbers,
        or malformed non-numeric values.
        """
        if user_id is None or isinstance(user_id, bool):
            return None
        try:
            if isinstance(user_id, (int, float)):
                val = int(user_id)
                return val if val > 0 else None
            s = str(user_id).strip()
            if s.isdigit():
                val = int(s)
                return val if val > 0 else None
            return None
        except Exception:
            return None

    def is_admin(self, user_id: Any) -> bool:
        """Return True only if user_id is an authenticated, authorized system administrator."""
        uid = self._clean_uid(user_id)
        if not uid:
            return False

        if uid == self._owner_user_id or uid in self._in_memory_admins:
            return True

        try:
            row = self._db.query_one(
                "SELECT 1 FROM administrators WHERE user_id=?", (uid,)
            )
            if row:
                self._in_memory_admins.add(uid)
                return True

            user_row = self._db.query_one(
                "SELECT is_admin FROM users WHERE user_id=?", (uid,)
            )
            if user_row and bool(user_row["is_admin"]):
                self._in_memory_admins.add(uid)
                return True
        except Exception as exc:
            logger.error("Admin check database lookup failed for uid=%s: %s", uid, exc)

        return False

    def is_owner(self, user_id: Any) -> bool:
        """Return True only if user_id is the primary system owner."""
        uid = self._clean_uid(user_id)
        if not uid:
            return False
        if uid == self._owner_user_id and self._owner_user_id > 0:
            return True
        try:
            row = self._db.query_one(
                "SELECT role FROM administrators WHERE user_id=?", (uid,)
            )
            if row and row["role"] == "owner":
                return True
        except Exception:
            pass
        return False

    def is_suspended(self, user_id: Any) -> bool:
        """Return True if user_id is currently suspended from accessing the service.

        Admins and the owner can never be suspended (fail-safe).
        """
        uid = self._clean_uid(user_id)
        if not uid:
            return False
        if self.is_admin(uid):
            return False

        try:
            row = self._db.query_one(
                "SELECT status FROM users WHERE user_id=?", (uid,)
            )
            if row and row["status"] == "suspended":
                return True
        except Exception:
            pass
        return False

    def add_admin(self, user_id: Any, added_by: int = 0, role: str = "admin") -> bool:
        """Grant administrator role to user_id."""
        uid = self._clean_uid(user_id)
        if not uid:
            raise ValueError(f"Invalid administrator user_id: {user_id!r}")

        now = int(time.time())
        role_clean = "owner" if role == "owner" else "admin"
        self._db.execute(
            "INSERT INTO administrators (user_id, role, added_by, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",
            (uid, role_clean, int(added_by or 0), now),
        )
        self._db.execute(
            "UPDATE users SET is_admin=1, updated_at=? WHERE user_id=?",
            (now, uid),
        )
        self._in_memory_admins.add(uid)
        logger.info("Admin granted to uid=%s by uid=%s (role=%s)", uid, added_by, role_clean)
        return True

    def remove_admin(self, user_id: Any, removed_by: int = 0) -> bool:
        """Revoke administrator role from user_id."""
        uid = self._clean_uid(user_id)
        if not uid:
            return False

        if self.is_owner(uid):
            raise ValueError("The primary system owner administrator cannot be removed.")

        now = int(time.time())
        self._db.execute(
            "DELETE FROM administrators WHERE user_id=? AND role!='owner'", (uid,)
        )
        self._db.execute(
            "UPDATE users SET is_admin=0, updated_at=? WHERE user_id=?",
            (now, uid),
        )
        self._in_memory_admins.discard(uid)
        logger.info("Admin revoked from uid=%s by uid=%s", uid, removed_by)
        return True

    def list_admins(self) -> List[Dict[str, Any]]:
        """List all registered administrators with their roles and metadata."""
        out: Dict[int, Dict[str, Any]] = {}
        now = int(time.time())

        try:
            rows = self._db.query_all(
                "SELECT user_id, role, added_by, created_at FROM administrators ORDER BY created_at ASC"
            )
            for r in rows or []:
                aid = int(r["user_id"])
                out[aid] = {
                    "user_id": aid,
                    "role": r["role"] or "admin",
                    "added_by": int(r["added_by"] or 0),
                    "created_at": int(r["created_at"] or now),
                }
        except Exception as exc:
            logger.warning("Could not list administrators from database: %s", exc)

        for aid in self._in_memory_admins:
            if aid not in out:
                role = "owner" if aid == self._owner_user_id else "admin"
                out[aid] = {
                    "user_id": aid,
                    "role": role,
                    "added_by": 0,
                    "created_at": now,
                }

        return list(out.values())
