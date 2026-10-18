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

#: How often the terminal-task retention sweep runs, and how long to back off
#: after a failed sweep. Housekeeping is slow on purpose: the sweep is a
#: bounded batch, so a backlog drains over several passes rather than in one
#: long write lock.
RETENTION_INTERVAL = 3600.0
RETENTION_RETRY_INTERVAL = 300.0


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
        retention_seconds: int = 0,
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
        # Terminal-task retention. 0 = keep terminal tasks forever (the
        # pre-existing behavior); the sweep is then never attempted.
        self._retention = max(0, int(retention_seconds or 0))
        self._next_retention = time.monotonic()
        # Distinguishes "the claim found nothing" from "the claim is broken":
        # both used to look like an empty queue.
        self._claim_failed = False

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
                self._claim_failed = False
            except asyncio.TimeoutError:
                claimed = None
            except Exception as exc:
                # A broken claim and an empty queue are not the same thing, and
                # the old `except (asyncio.TimeoutError, Exception): claimed =
                # None` reported them identically: a storage fault looked like
                # "no work to do" forever. Record the first failure of a run,
                # then stay quiet until a claim succeeds, so the fault is
                # visible without one log line per poll cycle.
                if not self._claim_failed:
                    self._claim_failed = True
                    logger.exception("task claim failed (treated as no work): %s", exc)
                claimed = None
            if claimed is None:
                # Reclaim tasks whose worker lease expired while nothing else
                # changed: keeps a crashed worker's tasks from being stuck.
                try:
                    await self._tasks._repo.reclaim_expired(int(time.time()))
                except Exception:
                    logger.exception("reclaiming expired task leases failed")
                await self._maybe_purge()
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

    async def _maybe_purge(self) -> None:
        """Delete terminal tasks outside the retention window, at most hourly.

        Self-gated by time rather than scheduled so every replica can call it
        from its idle path: the sweep is a bounded (TASK_PURGE_BATCH),
        terminal-only, idempotent DELETE — whoever gets there first drains the
        batch, and a replica that runs it twice does nothing the second time.
        """
        if self._retention <= 0:
            return
        now = time.monotonic()
        if now < self._next_retention:
            return
        try:
            deleted = await self._tasks.purge_terminal(self._retention)
        except Exception:
            # Back off rather than retrying every poll cycle; a failed sweep is
            # housekeeping, not a reason to spin.
            self._next_retention = now + RETENTION_RETRY_INTERVAL
            logger.exception("task retention sweep failed")
            return
        self._next_retention = now + RETENTION_INTERVAL
        if deleted:
            logger.info(
                "task retention sweep deleted %d terminal task(s) older than %ds",
                deleted, self._retention)

    async def _claim_one(self) -> Optional[tuple]:
        for _ in range(8):
            # Only claim what this replica can actually execute: a replica
            # without an executor for a kind used to claim it anyway, rewrite
            # the row to push it back, and repeat that every poll cycle.
            task = await self._tasks.acquire_for_work(
                LeaseSeconds, kinds=sorted(self._available))
            if task is None:
                return None
            if task.kind not in self._available:
                # Defensive: an executor set that changed after the claim, or a
                # backend that ignored the kind filter. Hand the task back with
                # a holder-checked UPDATE that also clears the lease — the old
                # path rewrote every column from a stale entity and left this
                # worker's lease_token on a row it had given up.
                handed_back = await self._tasks._repo.requeue_unclaimed(
                    task.id, task.lease_token,
                    int(time.time() + RETRY_BACKOFF_CAP / 10), int(time.time()))
                if not handed_back:
                    logger.warning(
                        "task %s could not be handed back (lease no longer held)",
                        task.id)
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
            lease_lost = False
            holder = asyncio.current_task()

            async def _watchdog() -> None:
                nonlocal last_renew, lease_lost
                while holder is not None and not holder.done():
                    await asyncio.sleep(LEASE_RENEW_INTERVAL)
                    renew_error = False
                    try:
                        renewed = await self._tasks.renew_work_lease(task, LeaseSeconds)
                    except Exception as exc:
                        # The store is briefly unreachable: this says nothing
                        # about who holds the lease, so do not abort on it.
                        logger.warning("lease renewal failed for task %s: %s",
                                       task.id, type(exc).__name__)
                        renewed, renew_error = False, True
                    if not renewed:
                        if renew_error and (time.monotonic() - last_renew) < LeaseSeconds:
                            # Still inside the lease window another worker would
                            # have to honor: keep trying, keep executing.
                            continue
                        # Definitive (a holder-checked UPDATE affected no row) or
                        # a full lease period with no successful renewal: the
                        # lease is no longer ours — reclaimed as expired, or
                        # another worker owns the row. The executor must STOP:
                        # continuing applies side effects for a task someone
                        # else owns, and its later complete()/fail() is refused
                        # anyway, leaving a duplicated run recorded as
                        # RETRY_WAIT/FAILED.
                        lease_lost = True
                        logger.error(
                            "task %s lost its lease (%s); aborting the executor",
                            task.id, task.kind)
                        holder.cancel()
                        return
                    last_renew = time.monotonic()

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
            except asyncio.CancelledError:
                # Either our own watchdog (lease lost) or worker shutdown. In
                # both cases the lease is not ours to write, so no terminal
                # state is attempted; a lease loss is recorded as such.
                if lease_lost:
                    await self._audit(task, "lease_lost", reason_code="lease_lost",
                                      severity="WARN",
                                      result_summary="execution aborted: lease lost")
                    logger.warning("task %s aborted after losing its lease", task.id)
                else:
                    logger.info("task %s cancelled during shutdown", task.id)
                raise
            except Exception as exc:
                await self._tasks.fail(task, str(exc)[:2000], "executor_error")
                await self._audit(task, "failed", reason_code="executor_error",
                                  severity="ERROR", result_summary=str(exc)[:400])
            finally:
                watchdog.cancel()
                logger.info("task %s (%s) finished in %.3fs", task.id, task.kind,
                            time.monotonic() - started)
