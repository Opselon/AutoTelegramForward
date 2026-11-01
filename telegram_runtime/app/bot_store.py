"""Read-only, owner-scoped access to the Core's stored bot tokens.

The Telegram Runtime must resolve a bot token **by id and for a specific
tenant**, and it must do so without the Go Worker ever seeing the token. This
module is the runtime's window into the Core's ``bot_tokens`` table — the
same table ``SqliteBotTokenRepository`` owns — with two properties preserved
exactly:

* **the owner is in the WHERE clause, not compared after the read.** A
  transplanted token id then never round-trips the secret at all: the SELECT
  for a tenant that is not the owner simply finds no row.
* **decryption happens here, and only here.** The stored column is
  ``token_encrypted``; the ``CryptoService`` that unlocks it lives in the
  Core, and the resolved plaintext never leaves this process (never in a
  log, never in a response field, never on the wire).

This is a deliberate read-only *adapter*, not a second repository: writes
(add/validate/delete/defaults) stay with the Core's own servicer and bot UI.
The runtime only answers one question on the Worker's behalf: "may this
tenant use this stored token, and if so, what does it look like?"
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("atf.telegram_runtime.bot_store")

__all__ = ["standalone_bot_repo", "ResolvedBotToken"]


class ResolvedBotToken:
    """One stored bot token as the runtime needs to see it.

    Deliberately tiny: the adapter reads ``token``, ``is_active`` and
    ``owner_user_id`` and nothing else, so a change to the table's other
    columns cannot leak into the runtime's world.
    """

    __slots__ = ("id", "token", "is_active", "owner_user_id")

    def __init__(self, id: str, token: str, is_active: bool, owner_user_id: int) -> None:
        self.id = id
        self.token = token
        self.is_active = is_active
        self.owner_user_id = owner_user_id


class _StandaloneBotTokenRepo:
    """Owner-scoped reads over the shared SQLite ``bot_tokens`` table.

    Implements exactly the surface :class:`BotApiAdapter` calls:
    ``get_owned(token_id, owner)`` and ``get_by_id(token_id)``. Both return
    a :class:`ResolvedBotToken` (with the token decrypted) or ``None``.
    """

    def __init__(self, db: Any, crypto: Any) -> None:
        self._db = db
        self._crypto = crypto

    def _row(self, sql: str, params: tuple) -> Optional[ResolvedBotToken]:
        row = self._db.query_one(sql, params) if hasattr(self._db, "query_one") else None
        if row is None:
            return None
        # Columns: id, label, token_encrypted, is_active, created_at,
        # updated_at, owner_user_id, defaults_json. Read positionally-tolerant:
        # a row from a database that predates the owner/defaults columns must
        # still resolve rather than crash.
        try:
            token_encrypted = row["token_encrypted"] if isinstance(row, dict) else row[2]
        except (KeyError, IndexError):
            return None
        is_active = self._col(row, "is_active", 3, 1)
        owner = self._col(row, "owner_user_id", 6, 0)
        token = self._decrypt(token_encrypted)
        return ResolvedBotToken(
            id=str(row["id"] if isinstance(row, dict) else row[0]),
            token=token,
            is_active=bool(is_active),
            owner_user_id=int(owner or 0),
        )

    @staticmethod
    def _col(row: Any, name: str, index: int, default: Any) -> Any:
        if isinstance(row, dict):
            return row.get(name, default)
        try:
            return row[index]
        except IndexError:
            return default

    def _decrypt(self, token_encrypted: str) -> str:
        """Decrypt one stored token, never raising.

        An unreadable ciphertext answers as an empty token, which the adapter
        treats as "unusable" — a bot whose stored secret cannot be decrypted
        must never forward through a half-built client, but it must also not
        kill the runtime's loop.
        """
        if not token_encrypted:
            return ""
        try:
            return str(self._crypto.decrypt(token_encrypted) or "")
        except Exception as exc:
            logger.warning("[BOT STORE] stored bot token could not be decrypted: %s", exc)
            return ""

    # -- the two reads the adapter makes ----------------------------------- #

    def get_owned(self, token_id: str, owner_user_id: int) -> Optional[ResolvedBotToken]:
        """The token when it is this tenant's; ``None`` otherwise.

        The owner is part of the predicate — "not yours" and "does not
        exist" answer identically, so a token id cannot be probed for
        existence by an unrelated tenant.
        """
        return self._row(
            "SELECT * FROM bot_tokens WHERE id=? AND owner_user_id=?",
            (str(token_id or ""), int(owner_user_id or 0)),
        )

    def get_by_id(self, token_id: str) -> Optional[ResolvedBotToken]:
        """The token regardless of owner (used only when no tenant was given).

        The adapter only calls this when the caller supplied no tenant id —
        which is itself a refused route for a job. It exists so the same
        adapter can answer availability probes in contexts where ownership
        is established differently (the Core's own in-process wiring).
        """
        return self._row(
            "SELECT * FROM bot_tokens WHERE id=?",
            (str(token_id or ""),),
        )


class _SyncDb:
    """A minimal read-only handle over the shared SQLite file.

    The Core owns the file and its connection; the standalone runtime just
    needs to read rows without disturbing the writer's lock. A fresh
    read-only connection per call would be wasteful, so the handle keeps one
    connection with a busy timeout and reads through it.
    """

    def __init__(self, path: str) -> None:
        import sqlite3

        self._conn = sqlite3.connect(
            path, timeout=10.0, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

    def query_one(self, sql: str, params: tuple):
        cur = self._conn.execute(sql, params)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - shutdown best effort
            pass


def standalone_bot_repo(db_path: str) -> _StandaloneBotTokenRepo:
    """Build the owner-scoped token repo over the shared atf.db.

    Uses the Core's own ``CryptoService`` for decryption — the same key
    (``ATF_MASTER_KEY``, falling back to the Core's default) that the write
    path used — so the resolved plaintext is exactly what the owner stored.
    """
    from core.infrastructure.security.crypto import CryptoService

    db = _SyncDb(db_path)
    # The Core's Config resolves ATF_MASTER_KEY into cfg.master_key; the same
    # resolution here means the runtime decrypts what the Core encrypted. The
    # default is reproduced rather than imported so the runtime never has to
    # construct a full Core Config (and pull its whole validation surface)
    # just to read one column.
    master_key = os.environ.get("ATF_MASTER_KEY") or "change-me"
    crypto = CryptoService(master_key)
    return _StandaloneBotTokenRepo(db, crypto)
