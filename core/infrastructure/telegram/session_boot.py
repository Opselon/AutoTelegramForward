"""Session boot manager: restores saved sessions at startup, binds handlers,
watches health, and reconciles new/disabled sessions while running.

Runs alongside core.main: after every client is started the real-time
handlers are bound, so forwarding works immediately after a restart without
any manual intervention.
"""

import asyncio
import logging
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger("atf.boot")

#: hard cap on parallel client starts (Telegram throttles aggressive logins)
BOOT_CONCURRENCY = 3


class SessionBootManager:
    """Owns boot-time restore + runtime health reconciliation."""

    def __init__(
        self,
        sessions,
        rules,
        pool,
        dispatcher,
        log_client=None,
    ) -> None:
        self._sessions = sessions
        self._rules = rules
        self._pool = pool
        self._dispatcher = dispatcher
        self._log = log_client
        # session_id -> last status string surfaced in /sessions + gRPC
        self.statuses: Dict[str, str] = {}
        self._reconcile_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Boot
    # ------------------------------------------------------------------ #
    async def boot(
        self,
        on_status: Optional[Callable[[str, str], None]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, str]:
        """Start every active session, bind real-time handlers, return statuses."""
        sessions = await self._sessions.list_active()
        total = len(sessions)
        self._emit(f"restoring {total} session(s)…", progress=None)
        sem = asyncio.Semaphore(BOOT_CONCURRENCY)

        async def _one(index: int, session) -> None:
            async with sem:
                status = await self.restore_session(session.id)
                if progress:
                    try:
                        progress(index + 1, total)
                    except Exception:
                        pass
                if on_status:
                    try:
                        on_status(session.id, status)
                    except Exception:
                        pass

        await asyncio.gather(*(_one(i, s) for i, s in enumerate(sessions)))
        self._emit(f"boot complete: {total} session(s) processed")
        return dict(self.statuses)

    async def restore_session(self, session_id: str) -> str:
        """Start one session client + bind handlers. Returns a status string."""
        session = await self._sessions.get(session_id)
        if session is None:
            self.statuses[session_id] = "not_found"
            return "not_found"
        if not session.is_active or not session.is_authorized:
            self.statuses[session_id] = "inactive"
            logger.info("skip inactive session %s", session_id[:8])
            return "inactive"
        if not session.session_string_encrypted:
            self.statuses[session_id] = "no_session_string"
            return "no_session_string"
        try:
            client = await self._pool.start_with_session_string(
                session.id,
                session.session_string_encrypted,
                credential_id=getattr(session, "api_credential_id", "default"),
            )
            me = await client.get_me()
            bound = await self._dispatcher.bind_session(session.id)
            status = "live" if bound else "live_no_bind"
            self.statuses[session_id] = status
            label = getattr(me, "username", "") or getattr(me, "first_name", "") or session.phone_number
            self._emit(f"session live: {label} (rules bound: {bound})")
            logger.info("session %s live as @%s", session_id[:8], label)
            return status
        except Exception as exc:
            from pyrogram.errors import AuthKeyInvalid, AuthKeyDuplicated, UserDeactivated
            if isinstance(exc, (AuthKeyInvalid, AuthKeyDuplicated, UserDeactivated)):
                self.statuses[session_id] = f"auth_revoked:{type(exc).__name__}"
                try:
                    session.deactivate()
                    await self._sessions._repo.update(session)
                except Exception:
                    pass
                logger.error("session %s auth revoked (%s) — deactivated", session_id[:8], exc)
            else:
                self.statuses[session_id] = f"error:{type(exc).__name__}"
                logger.error("restore of session %s failed: %s", session_id[:8], exc)
            return self.statuses[session_id]

    async def drop_session(self, session_id: str) -> None:
        """Unbind handlers and stop the client (called on delete/deactivate)."""
        try:
            await self._dispatcher.unbind_session(session_id)
        except Exception:
            pass
        try:
            await self._pool.stop(session_id)
        except Exception:
            pass
        self.statuses.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # Runtime reconciliation: every N seconds make sure every active
    # session has a live, healthy client with handlers bound.
    # ------------------------------------------------------------------ #
    async def start_reconciler(self, interval: int = 60) -> None:
        if self._reconcile_task and not self._reconcile_task.done():
            return
        self._stop.clear()
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(interval), name="atf-session-reconciler"
        )

    async def stop_reconciler(self) -> None:
        self._stop.set()
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reconcile_task = None

    async def _reconcile_loop(self, interval: int) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(interval)
                await self.reconcile_once()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("reconciler error")

    async def reconcile_once(self) -> Dict[str, str]:
        """Ensure all active sessions are live; stop clients of dead ones."""
        try:
            sessions = await self._sessions.list_active()
        except Exception:
            logger.exception("reconcile: session list failed")
            return {}
        live_ids = {s.id for s in sessions}
        for session in sessions:
            state = self._pool.get_state(session.id)
            needs_restore = (
                state is None
                or not getattr(state.client, "is_connected", True)
                or not self._dispatcher.is_bound(session.id)
            )
            if self.statuses.get(session.id, "").startswith("auth_revoked"):
                continue
            if needs_restore:
                await self.restore_session(session.id)
        # stop clients whose sessions were deleted/deactivated meanwhile
        for sid in list(self._pool.all_states()):
            if sid not in live_ids and not sid.startswith("login"):
                logger.info("reconcile: stopping orphan client %s", sid[:8])
                await self.drop_session(sid)
        return dict(self.statuses)

    # ------------------------------------------------------------------ #
    def _emit(self, message: str, progress=None) -> None:
        logger.info(message)
        if self._log is not None:
            try:
                self._log.info("core", "boot", message, {"ts": int(time.time())})
            except Exception:
                pass


async def restore_all_sessions(container: dict, progress=None) -> Dict[str, str]:
    """Convenience: build a boot manager from a DI container and boot."""
    mgr = SessionBootManager(
        container["sessions"], container["rules"], container["pool"],
        container["dispatcher"], container.get("log_client"),
    )
    container["boot_manager"] = mgr
    statuses = await mgr.boot(progress=progress)
    await mgr.start_reconciler()
    return statuses
