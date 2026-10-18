"""Core-side worker for the per-user task lifecycle.

The worker claims runnable tasks from the authoritative database with a
bounded lease (so a crashed worker cannot hold a task hostage) and executes
them through the SAME domain code paths the synchronous gRPC methods already
use — there is exactly one implementation of every operation, and the task
path reuses it.

Bounded concurrency: at most ``poll.max_concurrent`` tasks execute at once;
the pool is a fixed-size asyncio semaphore, never an unbounded queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from .user_tasks import (
    UserTaskUseCases,
    KINDS,
    RETRY_BACKOFF_CAP,
)

logger = logging.getLogger(__name__)

LeaseSeconds = 120.0
POLL_INTERVAL = 1.0
# A task whose execution exceeds its lease is renewed at this interval while
# running (the executor calls the use-case's renew through the runner).
LEASE_RENEW_INTERVAL = 60.0


def build_executors(container: Dict[str, Any]) -> Dict[str, Callable[[int, Dict[str, Any]], Awaitable[Dict[str, Any]]]]:
    """Map each task kind to the existing domain implementation.

    Every executor receives the verified (owner_user_id, payload) and returns
    a JSON-safe result dict. Ownership checks stay inside the domain calls —
    the executor adds none and bypasses none.
    """
    executors: Dict[str, Callable[..., Awaitable[Dict[str, Any]]]] = {}

    backup = container.get("backup")
    if backup is not None:
        async def _export(owner: int, payload: Dict[str, Any]) -> Dict[str, Any]:
            snapshot = await backup.create_snapshot(owner_user_id=owner or None)
            counts = {k: len(v) for k, v in snapshot.items() if isinstance(v, list)}
            # The bundle itself is credential material: it is delivered to the
            # same tenant through the synchronous endpoint, never stored in
            # the task result. The task records the counts only.
            return {"counts": counts, "bundle": "__delivered_synchronously__"}

        executors["backup.export"] = _export

    sessions = container.get("sessions")
    if sessions is not None:
        async def _backup_session(owner: int, payload: Dict[str, Any]) -> Dict[str, Any]:
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                return {"ok": False, "code": "invalid_payload", "message": "session_id required"}
            session = await sessions.get_owned(session_id, owner)
            if not session:
                # Same shape as the synchronous refusal — no existence oracle.
                return {"ok": False, "code": "session_not_found"}
            return {"ok": True, "session_id": session.id,
                    "exported": bool(session.session_string_encrypted)}

        executors["session.backup"] = _backup_session

    queue_repo = container.get("queue_repo")
    if queue_repo is not None:
        async def _scope(owner: int):
            rules = container.get("rules")
            if rules is None or not owner:
                return None
            owned = await rules.list_by_owner(owner)
            return [r.id for r in owned]

        async def _replay(owner: int, payload: Dict[str, Any]) -> Dict[str, Any]:
            delivery_id = str(payload.get("delivery_id") or "")
            if not delivery_id:
                return {"ok": False, "code": "invalid_payload", "message": "delivery_id required"}
            scope = await _scope(owner)
            result = await queue_repo.replay_dead_letter(delivery_id, list(scope) if scope is not None else None)
            if result == "requeued":
                return {"ok": True, "delivery_id": delivery_id, "state": "requeued"}
            return {"ok": False, "code": result or "not_found"}

        async def _purge(owner: int, payload: Dict[str, Any]) -> Dict[str, Any]:
            scope = await _scope(owner)
            removed = await queue_repo.purge_dead_letters(
                list(scope) if scope is not None else None,
                rule_id=str(payload.get("rule_id") or ""),
                older_than=int(payload.get("older_than") or 0),
            )
            return {"ok": True, "removed": int(removed)}

        executors["dlq.replay"] = _replay
        executors["dlq.purge"] = _purge

    missing = KINDS - set(executors)
    if missing:
        logger.warning("task executors unavailable for kinds: %s (creation stays enabled; "
                       "create refuses unknown kinds via the KINDS set)", sorted(missing))
    return executors


class UserTaskWorker:
    """Bounded, cancellable worker loop. Multiple Core replicas are safe:
    the database claim is atomic and each executor holds a unique lease."""

    def __init__(
        self,
        tasks: UserTaskUseCases,
        executors: Dict[str, Callable[[int, Dict[str, Any]], Awaitable[Dict[str, Any]]]],
        container: Optional[Dict[str, Any]] = None,
        max_concurrent: int = 4,
        poll_interval: float = POLL_INTERVAL,
    ) -> None:
        self._tasks = tasks
        self._executors = executors
        self._sem = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._poll = max(0.2, float(poll_interval))
        # Durable audit events (PostgreSQL outbox); None keeps SQLite deployments
        # identical to before because there the central store does not exist.
        self._audit_repo = (
            (container or {}).get("audit_repo") if isinstance(container, dict) else None
        )
        self._available: set = {k for k in executors}
        self._stopping = asyncio.Event()
        self._children: set = set()

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        logger.info("UserTaskWorker started (concurrency=%d, kinds=%s)",
                    self._sem._value if hasattr(self._sem, "_value") else 0,
                    sorted(self._available))
        while not self._stopping.is_set():
            try:
                claimed = await asyncio.wait_for(
                    self._claim_one(), timeout=self._poll)
            except (asyncio.TimeoutError, Exception):
                claimed = None
            if claimed is None:
                # Reclaim tasks whose worker lease expired while nothing else
                # changed: keeps a crashed worker's tasks from being stuck.
                try:
                    await self._tasks._repo.reclaim_expired(int(time.time()))
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll)
                    break
                except asyncio.TimeoutError:
                    continue
            task, payload = claimed
            t = asyncio.create_task(self._run_one(task, payload))
            self._children.add(t)
            t.add_done_callback(self._children.discard)
        for child in list(self._children):
            child.cancel()

    async def _claim_one(self) -> Optional[tuple]:
        for _ in range(8):
            task = await self._tasks.acquire_for_work(LeaseSeconds)
            if task is None:
                return None
            if task.kind not in self._available:
                # No executor for this kind on this process: hand it back
                # instead of spinning. Run-at is nudged so the claim loop
                # does not pick it again immediately.
                task.status = "RETRY_WAIT"
                task.attempt = max(0, task.attempt - 1)
                task.run_at = int(time.time() + RETRY_BACKOFF_CAP / 10)
                await self._tasks._repo.replace(task)
                continue
            try:
                payload = json.loads(task.payload_json or "{}")
            except (TypeError, ValueError):
                payload = {}
            return task, payload if isinstance(payload, dict) else {}
        return None

    async def _audit(self, task: Any, outcome: str, reason_code: str = "",
                     severity: str = "INFO", result_summary: str = "") -> None:
        """Record the task's lifecycle milestone into the durable audit store.

        `audit_repo` is the PostgreSQL transactional-outbox store; SQLite
        deployments have no central audit store (repo is None), where the
        lifecycle stays INFO-logged. Never raises: audit must not kill a task.
        """
        repo = self._audit_repo
        if repo is None:
            return
        try:
            from core.infrastructure.persistence.audit_events import AuditEvent
            evt = AuditEvent(
                event_type=f"user_task.{outcome}",
                service="core",
                outcome=outcome,
                event_category="feature_execution",
                tenant_id=str(task.owner_user_id),
                actor_type="system",
                task_id=task.id,
                feature_id="user_tasks",
                action=task.kind,
                reason_code=reason_code or "",
                severity=severity,
                result_summary=result_summary,
                metadata={"kind": task.kind, "attempt": int(task.attempt),
                          "max_attempts": int(task.max_attempts)},
            )
            await repo.record(evt)
        except Exception:
            logger.info("audit write skipped for task %s", task.id)

    async def _run_one(self, task: Any, payload: Dict[str, Any]) -> None:
        async with self._sem:
            executor = self._executors[task.kind]
            started = time.monotonic()
            last_renew = started
            holder = asyncio.current_task()

            async def _watchdog() -> None:
                nonlocal last_renew
                while holder is not None and not holder.done():
                    await asyncio.sleep(LEASE_RENEW_INTERVAL)
                    try:
                        await self._tasks.renew_work_lease(task, LeaseSeconds)
                        last_renew = time.monotonic()
                    except Exception:
                        logger.warning("lease renewal failed for task %s", task.id)

            watchdog = asyncio.ensure_future(_watchdog())
            try:
                result = await executor(int(task.owner_user_id), payload)
                if result and result.get("ok") is False:
                    code = str(result.get("code") or "task_failed")
                    retryable = code not in ("invalid_payload", "session_not_found",
                                             "not_found")
                    outcome = "outcome_unconfirmed" if retryable else "failed"
                    await self._tasks.fail(
                        task, str(result.get("message") or code), code,
                        retryable=retryable)
                    await self._audit(
                        task, outcome,
                        reason_code=code, severity="WARN",
                        result_summary=str(result.get("message") or ""))
                else:
                    await self._tasks.complete(task, json.dumps(result or {}))
                    await self._audit(task, "succeeded")
            except Exception as exc:
                await self._tasks.fail(task, str(exc)[:2000], "executor_error")
                await self._audit(task, "failed", reason_code="executor_error",
                                  severity="ERROR", result_summary=str(exc)[:400])
            finally:
                watchdog.cancel()
                logger.info("task %s (%s) finished in %.3fs", task.id, task.kind,
                            time.monotonic() - started)
