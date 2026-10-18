"""Per-user task lifecycle: the state machine the API drives.

Design invariants (API-first phase 2):

* PostgreSQL/SQLite is the authoritative store; Redis holds **no** task
  state. A task's result is never announced before it is committed — the
  terminal state happens in the same write that records the executor's outcome.
* Ownership is part of every read and mutation: the caller's verified
  identity flows in from the Go API as ``owner_user_id`` and the repository
  predicates filter on it. 0 means "no identity" and fails closed.
* Idempotent submission: a client retrying with the same idempotency key
  gets the same task object back (``already_existed``), never a duplicate.
* Bounded retries with capped backoff; a task's ``max_attempts`` is clamped
  server-side so one caller cannot launch unbounded work.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .repositories import ITaskRepository

logger = logging.getLogger(__name__)

# The public state machine.
PENDING = "PENDING"
RUNNING = "RUNNING"
RETRY_WAIT = "RETRY_WAIT"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

ACTIVE_STATES: Set[str] = {PENDING, RUNNING, RETRY_WAIT}
TERMINAL_STATES: Set[str] = {SUCCEEDED, FAILED, CANCELLED}

# Valid transitions. Any transition not listed here is refused, not guessed.
_TRANSITIONS: Dict[str, Set[str]] = {
    PENDING: {RUNNING, CANCELLED},
    RUNNING: {SUCCEEDED, FAILED, CANCELLED, RETRY_WAIT},
    RETRY_WAIT: {PENDING, RUNNING, CANCELLED},
    FAILED: {PENDING},   # explicit user retry re-queues a failed task
    SUCCEEDED: set(),
    CANCELLED: set(),
}

# Known operation kinds. The worker registry validates against this list,
# so an unknown kind is refused at creation, not discovered at execution.
KINDS: Set[str] = {
    "session.backup",
    "dlq.replay",
    "dlq.purge",
    "backup.export",
}

DEFAULT_MAX_ATTEMPTS = 3
MAX_MAX_ATTEMPTS = 8
# exponential backoff: 5s, 20s, 80s, capped at 5 minutes.
RETRY_BACKOFF_BASE = 5.0
RETRY_BACKOFF_FACTOR = 4.0
RETRY_BACKOFF_CAP = 300.0

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_ERROR_BYTES = 8 * 1024


class TaskError(Exception):
    """Domain refusal: carries the wire-visible error code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def can_transition(current: str, new: str) -> bool:
    return new in _TRANSITIONS.get(current, set())


def backoff_run_at(attempt: int) -> int:
    delay = min(RETRY_BACKOFF_BASE * (RETRY_BACKOFF_FACTOR ** max(0, attempt - 1)),
                RETRY_BACKOFF_CAP)
    return int(time.time() + delay)


def _bounded_json(raw: str, cap: int, label: str) -> str:
    if not raw:
        return "{}" if label == "payload" else ""
    if len(raw) > cap:
        raise TaskError("payload_too_large", f"{label} exceeds {cap} bytes")
    try:
        parsed = json.loads(raw or ("{}" if label == "payload" else ""))
    except (TypeError, ValueError):
        raise TaskError("invalid_json", f"{label} is not valid JSON")
    if not isinstance(parsed, (dict, list)):
        raise TaskError("invalid_json", f"{label} must be a JSON object")
    # Re-serialise compactly and canonically so the stored form is stable.
    try:
        out = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise TaskError("invalid_json", f"{label} is not serialisable")
    if len(out) > cap:
        raise TaskError("payload_too_large", f"{label} exceeds {cap} bytes")
    return out


class UserTaskUseCases:
    """All task operations go through this object: servicers, the worker and
    tests share one validated lifecycle."""

    def __init__(self, repo: ITaskRepository) -> None:
        self._repo = repo

    # ------------------------------------------------------------------ #
    # Client-facing operations
    # ------------------------------------------------------------------ #

    async def create(
        self, owner_user_id: int, kind: str, payload_json: str = "",
        idempotency_key: str = "", max_attempts: int = 0,
    ) -> Tuple[Optional[Any], bool]:
        """Create (or idempotently re-find) a task. Returns (task, created).

        Ownership is mandatory and positive: 0 or a negative identity is a
        refusal, an empty kind or an unknown kind is one too.
        """
        owner = int(owner_user_id or 0)
        if owner <= 0:
            raise TaskError("unauthorized", "owner identity is required")
        kind = str(kind or "").strip()
        if kind not in KINDS:
            raise TaskError("unknown_task_kind", f"kind '{kind}' is not supported")
        idem = str(idempotency_key or "").strip()
        if len(idem) > 200:
            raise TaskError("invalid_idempotency_key", "idempotency_key too long")
        bounded_payload = _bounded_json(payload_json, MAX_PAYLOAD_BYTES, "payload")
        attempts = int(max_attempts or 0)
        if attempts and attempts < 1:
            raise TaskError("invalid_max_attempts", "attempts must be >= 1")
        clamped = min(attempts or DEFAULT_MAX_ATTEMPTS, MAX_MAX_ATTEMPTS)

        from core.domain.entities import UserTask  # local: avoids import cycle at module load
        task = UserTask(
            owner_user_id=owner,
            kind=kind,
            status=PENDING,
            idempotency_key=idem,
            max_attempts=clamped,
            payload_json=bounded_payload,
        )
        owned = await self._repo.insert_if_absent(task)
        # insert_if_absent returns the row that owns the key: the freshly
        # inserted task (same id) on the create path, the pre-existing row on
        # the idempotent-duplicate path.
        created = bool(owned is not None and owned.id == task.id)
        return owned or task, created

    async def get_owned(self, task_id: str, owner_user_id: int) -> Optional[Any]:
        owner = int(owner_user_id or 0)
        if owner <= 0:
            return None
        task = await self._repo.get(str(task_id or ""))
        if not task or int(task.owner_user_id) != owner:
            # Another tenant's task is deliberately the same shape as a
            # nonexistent one: no existence oracle for probing ids.
            return None
        return task

    async def list_for_owner(
        self, owner_user_id: int, status: str = "", kind: str = "",
        limit: int = 50, offset: int = 0,
    ) -> Tuple[List[Any], int]:
        owner = int(owner_user_id or 0)
        if owner <= 0:
            raise TaskError("unauthorized", "owner identity is required")
        status = str(status or "").strip()
        kind = str(kind or "").strip()
        if status and status not in (ACTIVE_STATES | TERMINAL_STATES):
            raise TaskError("invalid_status", f"status '{status}' is not a task state")
        if kind and kind not in KINDS:
            raise TaskError("unknown_task_kind", f"kind '{kind}' is not supported")
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        tasks = await self._repo.list_for_owner(
            owner, status=status, kind=kind, limit=limit, offset=offset)
        total = await self._repo.count_for_owner(owner, status=status, kind=kind)
        return tasks, int(total)

    async def cancel_owned(self, task_id: str, owner_user_id: int) -> Any:
        """Cancel while the task is still queue-bound.

        Queue-bound means PENDING or RETRY_WAIT: a task waiting for its next
        attempt has not run yet, so its owner can still stop it. A RUNNING task
        is not yanked mid-flight (its executor owns the row through its lease);
        the client sees the current state and must poll.

        The conditional UPDATE is the authority: if it commits nothing, the
        caller is told what actually happened instead of being handed a
        CANCELLED task that is still queued or already executing.
        """
        task = await self.get_owned(task_id, owner_user_id)
        if not task:
            raise TaskError("not_found")
        if task.status in TERMINAL_STATES:
            if task.status == CANCELLED:
                return task  # idempotent cancel
            raise TaskError("not_cancellable", f"task already {task.status}")
        if task.status == RUNNING:
            raise TaskError("running_not_cancellable",
                            "task is executing; poll until it settles")
        # One conditional UPDATE wins the CANCEL-vs-claim race: either the
        # task is cancelled (owner waited) or the worker's claim landed first
        # and the owner sees RUNNING on the next poll.
        committed = await self._repo.cancel_queued(task.id, int(task.owner_user_id),
                                                   int(time.time()))
        if not committed:
            # Nothing was cancelled. Reporting CANCELLED here would be a lie
            # the owner acts on: re-read and answer with what actually
            # happened — a worker claimed it between the read and the UPDATE,
            # or a concurrent operation settled it.
            current = await self.get_owned(task_id, owner_user_id)
            if current is None:
                raise TaskError("not_found")
            if current.status == RUNNING:
                raise TaskError("running_not_cancellable",
                                "task is executing; poll until it settles")
            if current.status in TERMINAL_STATES:
                raise TaskError("not_cancellable", f"task already {current.status}")
            raise TaskError("not_cancellable",
                            "task could not be cancelled in its current state")
        task.status = CANCELLED
        task.finished_at = task.finished_at or int(time.time())
        task.error_code = "cancelled"
        task.lease_token = ""
        task.lease_until = 0
        task.mark_updated()
        return task

    async def retry_owned(self, task_id: str, owner_user_id: int) -> Any:
        task = await self.get_owned(task_id, owner_user_id)
        if not task:
            raise TaskError("not_found")
        if task.status == RETRY_WAIT or task.status == PENDING or task.status == RUNNING:
            # Already queued/executing: return as-is (idempotent retry).
            return task
        if task.status != FAILED:
            raise TaskError("not_retryable", f"task is {task.status}")
        # One conditional UPDATE against the owner AND the FAILED state: the
        # retry either happens atomically or not at all. The old path mutated a
        # local snapshot and rewrote every column with no predicate, so a
        # concurrent writer's change could be silently overwritten and the
        # caller still saw a requeued task.
        requeued = await self._repo.retry_failed(
            task_id, int(task.owner_user_id), int(time.time()))
        if requeued is None:
            current = await self.get_owned(task_id, owner_user_id)
            if current is None:
                raise TaskError("not_found")
            raise TaskError("not_retryable", f"task is {current.status}")
        return requeued

    # ------------------------------------------------------------------ #
    # Worker-side operations (NOT exposed to clients)
    # ------------------------------------------------------------------ #

    async def acquire_for_work(self, lease_seconds: float = 120.0,
                               kinds: Optional[Sequence[str]] = None) -> Optional[Any]:
        token = secrets.token_hex(16)
        until = int(time.time() + lease_seconds)
        return await self._repo.acquire_next(token, until, int(time.time()),
                                            kinds=kinds)

    async def renew_work_lease(self, task: Any, lease_seconds: float = 120.0) -> bool:
        return await self._repo.renew_lease(
            task.id, task.lease_token, int(time.time() + lease_seconds))

    async def complete(self, task: Any, result_json: str = "") -> Any:
        self._assert_holder(task)
        bounded = _bounded_json(result_json, MAX_RESULT_BYTES, "result")
        # The terminal state is written by ONE conditional UPDATE that also
        # re-verifies the lease: nothing is "complete" until the result and
        # the state commit together, and a stolen/expired lease commits
        # nothing.
        ok = await self._repo.finish_succeeded(
            task.id, task.lease_token, bounded, int(time.time()))
        if not ok:
            raise TaskError("lease_lost",
                            "task lease no longer held; completion refused")
        task.status = SUCCEEDED
        task.result_json = bounded
        task.finished_at = int(time.time())
        task.lease_token = ""
        task.lease_until = 0
        task.mark_updated()
        return task

    async def fail(self, task: Any, message: str, code: str = "task_failed",
                   retryable: bool = True) -> Any:
        self._assert_holder(task)
        text = str(message or "")[:MAX_ERROR_BYTES]
        short = str(code or "task_failed")[:64]
        if retryable and task.attempt < task.max_attempts:
            ok = await self._repo.pause_for_retry(
                task.id, task.lease_token, backoff_run_at(task.attempt),
                text, short, int(time.time()))
            if not ok:
                raise TaskError("lease_lost",
                                "task lease no longer held; retry pause refused")
            task.status = RETRY_WAIT
            task.run_at = backoff_run_at(task.attempt)
            task.error = text
            task.error_code = short
            task.lease_token = ""
            task.lease_until = 0
            task.mark_updated()
            return task
        ok = await self._repo.finish_failed(task.id, task.lease_token,
                                            text, short, int(time.time()))
        if not ok:
            raise TaskError("lease_lost",
                            "task lease no longer held; failure refused")
        task.status = FAILED
        task.error = text
        task.error_code = short
        task.finished_at = int(time.time())
        task.lease_token = ""
        task.lease_until = 0
        task.mark_updated()
        return task

    async def _transition(self, task: Any, new_status: str, finished: bool = False) -> Any:
        if not can_transition(task.status, new_status):
            raise TaskError(
                "invalid_transition",
                f"{task.status} -> {new_status} is not a valid task transition")
        task.status = new_status
        if new_status == CANCELLED:
            task.error_code = "cancelled"
        if finished:
            task.finished_at = int(time.time())
        task.lease_token = ""
        task.lease_until = 0
        task.mark_updated()
        return await self._repo.replace(task)

    @staticmethod
    def _assert_holder(task: Any) -> None:
        if not task or not task.lease_token:
            raise TaskError("not_claimed", "task is not currently claimed for work")
