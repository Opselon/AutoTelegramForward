"""Standalone session bootstrap for the Telegram Runtime.

The authoritative owner of Telegram session connectivity is the Core's
``ClientPool``; when the runtime runs *inside* the Core process it reuses
that pool and this module is not used. It exists for the standalone path
(integration tests, local development) so the runtime can still start the
configured sessions from the same session store.

Session STRINGS never leave this process: only the stable session ids are
exposed across the boundary, which is the whole point of the ownership
rule. A runtime restart therefore never destroys persisted session state,
and a worker restart never asks a Telegram user to authenticate again.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

logger = logging.getLogger("atf.telegram_runtime.bootstrap")

__all__ = ["bootstrap_pool"]


class _StandalonePool:
    """A minimal ClientPool-compatible shim.

    Exposes the small surface ``SessionManager`` reads (``get``,
    ``is_time_limited``, ``remaining_limit_seconds``) over the sessions the
    store holds. It is NOT a reimplementation of the Core pool's lifecycle:
    it starts the clients, keeps them alive, and stops them on shutdown.
    """

    def __init__(self) -> None:
        self._clients: Dict[str, Any] = {}

    def get(self, session_id: str):
        return self._clients.get(session_id)

    def is_time_limited(self, session_id: str) -> bool:
        return False

    def remaining_limit_seconds(self, session_id: str) -> float:
        return 0.0

    async def start(self, sessions: list) -> None:
        for session in sessions:
            client = await self._start_one(session)
            if client is not None:
                self._clients[session.get("id") or session.get("session_id") or ""] = client

    async def _start_one(self, session: Any) -> Any:
        try:
            from pyrogram import Client
        except Exception as exc:  # pragma: no cover - pyrogram is a hard dep
            logger.error("pyrogram is not installed: %s", exc)
            return None
        try:
            session_string = session.get("session_string") if isinstance(session, dict) else getattr(session, "session_string", None)
            api_id = int(session.get("api_id") or os.environ.get("ATF_API_ID") or 0)
            api_hash = str(session.get("api_hash") or os.environ.get("ATF_API_HASH") or "")
            if not session_string or not api_id or not api_hash:
                logger.warning("session %s is missing credentials — skipping", str(session.get("id"))[:8])
                return None
            client = Client(
                name=f"runtime-{session.get('id', 'x')}",
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
            )
            await client.start()
            return client
        except Exception as exc:
            logger.warning("session %s failed to start: %s", str(session.get("id"))[:8], exc)
            return None

    async def stop(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.stop()
            except Exception:
                pass
        self._clients.clear()


async def bootstrap_pool(db_path: str) -> _StandalonePool:
    """Start a standalone pool from the Core's session store."""
    pool = _StandalonePool()
    sessions = _read_sessions(db_path)
    if not sessions:
        logger.info("no sessions found in %s — runtime online with zero sessions", db_path)
        return pool
    await pool.start(sessions)
    logger.info("standalone pool started %d/%d session(s)", len(pool._clients), len(sessions))
    return pool


def _read_sessions(db_path: str) -> list:
    """Read the encrypted session rows from the Core's store.

    Decryption is delegated to the Core's crypto module when importable, so
    the runtime never holds a second implementation of the master-key
    handling. A store that cannot be read yields no sessions rather than an
    exception: an empty runtime is a valid, observable state.
    """
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT id, session_string_encrypted FROM sessions WHERE is_active = 1").fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("session store %s could not be read: %s", db_path, exc)
        return []

    out = []
    for session_id, encrypted in rows:
        try:
            session_string = _decrypt(encrypted)
        except Exception as exc:
            logger.warning("session %s could not be decrypted: %s", str(session_id)[:8], exc)
            continue
        if session_string:
            out.append({"id": session_id, "session_string": session_string})
    return out


def _decrypt(encrypted: str) -> str:
    """Decrypt a session string through the Core's CryptoService.

    The runtime never re-implements key handling: a second crypto path is a
    second way to lose a secret. A runtime that cannot reach the Core module
    runs with zero sessions instead of guessing.
    """
    master_key = os.environ.get("ATF_MASTER_KEY", "")
    try:
        from core.infrastructure.security.crypto import CryptoService
        service = CryptoService(master_key)
        return service.decrypt(encrypted) or ""
    except Exception:
        raise RuntimeError("session decryption requires the Core crypto module")
