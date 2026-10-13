"""Durable Queue and non-blocking delivery worker pool for P0 Data-Plane Reliability.

Implements:
Ingest -> Deduplicate -> Durable Queue -> Workers -> Dispatch -> Confirm -> Message Map

Key invariants:
1. Zero blocking `asyncio.sleep` on the hot path (Rate limits and FloodWait handled via `next_retry_at`).
2. Per-target delivery isolation (FloodWait on target A never stalls target B).
3. Lease claiming with automatic crash recovery.
4. Transient retry with exponential backoff + jitter.
5. Telegram timeout / network ambiguity marked as UNKNOWN (prevents blind duplicate retries).
6. Target message IDs saved to message_map for edit/delete synchronization.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from ...application.repositories import IDeliveryQueueRepository, IMessageMapRepository
from ...domain.entities import DeliveryJob, DeliveryStatus, ForwardMode, MessageMapping, MessagePayload
from ...domain.value_objects import ContentMode
from .errors import tg_detail, tg_error, wait_seconds

logger = logging.getLogger("atf.durable_queue")


def _rss_mb() -> float:
    """Resident set size of this process in MB (best effort)."""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def _dumps(value) -> str:
    """Serialize job payload the same way the SQLite repositories do."""
    from ...infrastructure.persistence.database import SqliteDatabase
    return SqliteDatabase.dumps(value)




class DurableQueueManager:
    """Manages background delivery workers, atomic job claiming, and lease recovery."""

    def __init__(
        self,
        queue_repo: IDeliveryQueueRepository,
        message_map_repo: IMessageMapRepository,
        client_pool: Any,
        num_workers: int = 3,
        lease_duration: float = 30.0,
        max_queue_size: int = 10000,
        max_per_minute_per_target: int = 25,
        metrics_repo: Any = None,
        log_client: Any = None,
        batch_size: int = 8,
    ) -> None:
        self._queue = queue_repo
        self._map = message_map_repo
        self._pool = client_pool
        self._num_workers = max(1, num_workers)
        self._log_client = log_client
        self._lease_duration = lease_duration
        self._max_queue_size = max_queue_size
        self._max_per_minute = max(1, max_per_minute_per_target)
        self._metrics = metrics_repo
        self._batch_size = max(1, batch_size)

        self._workers: List[asyncio.Task] = []
        self._watchdog_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._notify_event = asyncio.Event()
        self._running = False

        # Per-target token-bucket rate limiter: target -> deque of timestamps.
        # Bounded (v14): a deque with maxlen drops the oldest timestamps
        # automatically and caps bookkeeping at max_per_minute entries per
        # target, so a fleet of targets can no longer grow this dict without
        # limit. Idle targets are pruned by _prune_rate_windows.
        from collections import deque
        self._target_windows: Dict[str, "deque[float]"] = {}
        self._rate_lock = asyncio.Lock()
        self._max_tracked_targets = 4096

        # Operational metrics counters
        self.stats = {
            "enqueued": 0,
            "sent": 0,
            "retried": 0,
            "failed": 0,
            "unknown": 0,
            "floodwait_hits": 0,
            "backpressure_drops": 0,
        }
        self.recovered_after_restart_count: int = 0
        self.target_stats: Dict[str, Dict[str, int]] = {}
        self.total_dispatch_duration_ms: float = 0.0
        self.total_dispatches: int = 0
        # v14 observability
        self._started_at: float = time.time()
        self._queue_wait_sum: float = 0.0

    async def start(self) -> None:
        """Start worker tasks and lease watchdog."""
        if self._running:
            return None
        self._running = True
        self._stop_event.clear()
        self._notify_event.clear()

        # Immediate recovery of orphaned jobs on startup / restart
        try:
            initial_recovered = await self._queue.recover_expired_leases()
            if initial_recovered > 0:
                self.recovered_after_restart_count += initial_recovered
                logger.info("Startup recovery: restored %d claimed jobs from prior run.", initial_recovered)
        except Exception as exc:
            logger.warning("Startup recovery failed: %s", exc)

        for i in range(self._num_workers):
            task = asyncio.create_task(self._worker_loop(f"worker-{i+1}"))
            self._workers.append(task)

        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info(
            "Durable queue started with %d workers (lease=%.1fs, max_capacity=%d)",
            self._num_workers,
            self._lease_duration,
            self._max_queue_size,
        )

    async def stop(self) -> None:
        """Gracefully shut down all workers and watchdog."""
        if not self._running:
            return None
        self._running = False
        self._stop_event.set()
        self._notify_event.set()

        for w in self._workers:
            w.cancel()
        if self._watchdog_task:
            self._watchdog_task.cancel()

        await asyncio.gather(*self._workers, return_exceptions=True)
        if self._watchdog_task:
            await asyncio.gather(self._watchdog_task, return_exceptions=True)

        self._workers.clear()
        self._watchdog_task = None
        logger.info("Durable queue stopped cleanly.")

    # ------------------------------------------------------------------ #
    # Ingestion & Enqueueing (Non-blocking)
    # ------------------------------------------------------------------ #
    async def enqueue_delivery(
        self,
        rule: Any,
        payload: MessagePayload,
        target_chat_id: str,
        text: str,
        evaluation: Any = None,
    ) -> bool:
        """Enqueue a delivery job for one target chat.

        Returns True if accepted, False if backpressured or already queued (idempotent).
        """
        # 1. Backpressure guard
        pending_count = await self._queue.get_pending_count()
        if pending_count >= self._max_queue_size:
            self.stats["backpressure_drops"] += 1
            logger.warning(
                "Durable queue full (%d pending >= %d max). Backpressure triggered for target %s",
                pending_count,
                self._max_queue_size,
                target_chat_id,
            )
            return False

        # 2. Prepare serializable payload data
        mode = getattr(rule, "content_mode", ContentMode.AUTO)
        mode_val = getattr(mode, "value", str(mode))
        forward_mode_val = getattr(rule.forward_mode, "value", str(rule.forward_mode))

        payload_dict = {
            "session_id": rule.session_id,
            "source_chat_id": str(payload.chat_id),
            "source_message_id": int(payload.message_id),
            "target_chat_id": str(target_chat_id),
            "text": text,
            "effective_text": payload.effective_text,
            "has_media": bool(payload.has_media),
            "media_type": getattr(payload.media_type, "value", str(payload.media_type)),
            "media_group_id": payload.media_group_id,
            "forward_mode": forward_mode_val,
            "content_mode": mode_val,
            "delay_seconds": float(getattr(rule, "delay_seconds", 0.0) or 0.0),
        }

        # 3. Insert or update initial mapping as PENDING
        mapping = MessageMapping(
            rule_id=rule.id,
            source_chat_id=str(payload.chat_id),
            source_message_id=int(payload.message_id),
            target_chat_id=str(target_chat_id),
            target_message_id=None,
            media_group_id=payload.media_group_id,
            delivery_status=DeliveryStatus.PENDING,
        )
        # 4. Enqueue durable job + mapping as ONE atomic unit.
        # v12.3 (ACID atomicity): previously these were two separate auto-commits.
        # A crash between them left a PENDING mapping with no delivery job — an
        # orphan row no watchdog reconciles (it scans jobs, not mappings) and a
        # permanent source-of-truth inconsistency between rule state and the
        # delivery table. The job insert stays INSERT OR IGNORE, so a replay
        # after a mid-unit crash is a harmless no-op rather than a duplicate.
        delay = float(getattr(rule, "delay_seconds", 0.0) or 0.0)
        next_retry_at = time.time() + delay if delay > 0 else 0.0
        job_id = f"{rule.id}:{payload.chat_id}:{payload.message_id}:{target_chat_id}"

        job = DeliveryJob(
            id=job_id,
            rule_id=rule.id,
            source_chat_id=str(payload.chat_id),
            source_message_id=int(payload.message_id),
            target_chat_id=str(target_chat_id),
            payload_data=payload_dict,
            status=DeliveryStatus.PENDING,
            next_retry_at=next_retry_at,
        )

        enqueued = await self._enqueue_atomic(mapping, job)
        if enqueued:
            self.stats["enqueued"] += 1
            # v14: the atomic unit writes via the raw connection, bypassing
            # SqliteDeliveryQueueRepository.enqueue(), so the repository's
            # pending counter is bumped here to keep the backpressure guard
            # exact. On a replay (INSERT OR IGNORE no-op) enqueued is False.
            bump = getattr(self._queue, "_pending_estimate", None)
            if isinstance(bump, int):
                self._queue._pending_estimate = bump + 1
            self._notify_event.set()

        return enqueued

    async def _enqueue_atomic(self, mapping: MessageMapping, job: DeliveryJob) -> None:
        """v12.3 (ACID atomicity): write the delivery job and its message mapping
        in ONE transaction.

        Before v12.3 these were two separate auto-commits; a crash between them
        left a PENDING mapping with no delivery job — an orphan row no watchdog
        reconciles (it scans jobs, not mappings), a permanent inconsistency
        between the rule state and the delivery table. The job insert stays
        INSERT OR IGNORE, so a replay after a mid-unit crash is a harmless
        no-op rather than a duplicate delivery.
        """
        db = getattr(self._queue, "_db", None) or getattr(self._map, "_db", None)
        if db is None or not hasattr(db, "run_in_transaction"):
            # In-memory or fake repos (tests): no transactional boundary to
            # borrow — write sequentially. The unit guarantee is a
            # real-database property; forcing it on mocks would break tests
            # that drive the repos directly.
            await self._map.add_or_update(mapping)
            await self._queue.enqueue(job)
            return None
        # The repository writes are plain SQL with no awaits internally, so they
        # can run under run_in_transaction on the running loop via a sync unit
        # helper — we drive them directly (the async wrappers only call execute).
        return db.run_in_transaction(
            lambda conn: self._write_mapping_and_job(conn, mapping, job)
        )

    def _write_mapping_and_job(self, conn, mapping: MessageMapping, job: DeliveryJob) -> bool:
        """Both durable writes on one connection: commit together or roll back together.

        Returns True only when the delivery job row was newly created. A replay
        (job id already present) hits INSERT OR IGNORE and returns False, which
        is what makes re-enqueueing an already-processed event a clean no-op
        instead of a duplicate delivery.
        """
        now = int(time.time())
        status_str = getattr(mapping.delivery_status, "value", str(mapping.delivery_status))
        conn.execute(
            """
            INSERT INTO message_map (
                id, rule_id, source_chat_id, source_message_id, target_chat_id,
                target_message_id, media_group_id, delivery_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id, source_chat_id, source_message_id, target_chat_id) DO UPDATE SET
                target_message_id = COALESCE(excluded.target_message_id, message_map.target_message_id),
                media_group_id = COALESCE(excluded.media_group_id, message_map.media_group_id),
                delivery_status = excluded.delivery_status,
                updated_at = excluded.updated_at
            """,
            (
                mapping.id, mapping.rule_id,
                str(mapping.source_chat_id), int(mapping.source_message_id),
                str(mapping.target_chat_id), mapping.target_message_id,
                mapping.media_group_id or "", status_str, mapping.created_at, now,
            ),
        )
        payload_str = _dumps(job.payload_data)
        job_status = getattr(job.status, "value", str(job.status))
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO delivery_jobs (
                    id, rule_id, source_chat_id, source_message_id, target_chat_id,
                    payload_json, status, attempts, max_attempts, next_retry_at,
                    lease_until, worker_id, error_detail, created_at, updated_at,
                    intermediate_chat_id, intermediate_message_id, delivery_stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id, job.rule_id, str(job.source_chat_id),
                    int(job.source_message_id), str(job.target_chat_id),
                    payload_str, job_status, job.attempts, job.max_attempts,
                    job.next_retry_at, job.lease_until, job.worker_id or "",
                    job.error_detail or "", job.created_at, job.updated_at,
                    str(job.intermediate_chat_id or "") if job.intermediate_chat_id else None,
                    job.intermediate_message_id, job.delivery_stage or "DIRECT",
                ),
            )
            return cur.rowcount > 0
        except sqlite3.OperationalError:
            # Legacy table without the v9 intermediate/delivery_stage columns
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO delivery_jobs (
                    id, rule_id, source_chat_id, source_message_id, target_chat_id,
                    payload_json, status, attempts, max_attempts, next_retry_at,
                    lease_until, worker_id, error_detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id, job.rule_id, str(job.source_chat_id),
                    int(job.source_message_id), str(job.target_chat_id),
                    payload_str, job_status, job.attempts, job.max_attempts,
                    job.next_retry_at, job.lease_until, job.worker_id or "",
                    job.error_detail or "", job.created_at, job.updated_at,
                ),
            )
            return cur.rowcount > 0

    def _notify_worker(self) -> None:
        """Signal waiting workers that new work or retry is available."""
        self._notify_event.set()

    # ------------------------------------------------------------------ #
    # Worker Loop
    # ------------------------------------------------------------------ #
    async def _worker_loop(self, worker_id: str) -> None:
        """Continuous non-blocking worker polling for claimed delivery jobs.

        v14: successful sends are accumulated and committed once per batch via
        mark_sent_batch instead of one UPDATE+commit per job — a commit is an
        fsync under SQLite WAL, which is the dominant cost per delivered
        message. Failures are still marked individually because each one
        carries its own retry/backoff state.
        """
        logger.debug("[%s] Worker started", worker_id)
        while not self._stop_event.is_set():
            try:
                jobs = await self._queue.claim_batch(
                    worker_id=worker_id,
                    batch_size=self._batch_size,
                    lease_duration=self._lease_duration,
                )
                if not jobs:
                    # Wait for enqueue signal or 1s fallback
                    try:
                        await asyncio.wait_for(self._notify_event.wait(), timeout=1.0)
                        self._notify_event.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue

                sent_batch: List[tuple] = []
                for job in jobs:
                    if self._stop_event.is_set():
                        break
                    result = await self._process_job(worker_id, job)
                    if result is not None:
                        # (job_id, target_message_id) — deferred commit
                        sent_batch.append(result)

                # Commit all confirmed sends for this batch in one transaction.
                if sent_batch:
                    try:
                        await self._queue.mark_sent_batch(sent_batch)
                    except Exception:
                        logger.exception(
                            "[%s] mark_sent_batch failed (%d jobs); falling back",
                            worker_id, len(sent_batch),
                        )
                        for jid, msg_id in sent_batch:
                            try:
                                await self._queue.mark_sent(jid, msg_id)
                            except Exception:
                                logger.error("[%s] per-job mark_sent failed for %s", worker_id, jid)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[%s] Unexpected error in worker loop", worker_id)
                await asyncio.sleep(0.5)

    async def _process_job(self, worker_id: str, job: DeliveryJob) -> Optional[tuple]:
        """Process a single delivery job with rate limiting, error classification, and mapping confirmation.

        Returns ``(job_id, target_message_id)`` when the send was confirmed, so
        the worker loop can commit the whole batch in one transaction (v14).
        Returns ``None`` when the job was retried, failed, or marked UNKNOWN —
        those transitions already carry their own state and are written here.
        """
        target = job.target_chat_id
        def _bump_target(k: str) -> None:
            if target not in self.target_stats:
                self.target_stats[target] = {"sent": 0, "failed": 0, "retried": 0, "unknown": 0}
            self.target_stats[target][k] += 1

        # Check per-target throttling
        rate_wait = await self._check_target_rate(target)
        if rate_wait > 0:
            # Target is throttled: schedule next attempt for this job and yield worker immediately
            _bump_target("retried")
            await self._queue.mark_retry(job.id, "Target rate-limited", backoff_seconds=rate_wait)
            return None

        session_id = job.payload_data.get("session_id")
        client = self._pool.get(session_id)
        if client is None:
            _bump_target("retried")
            await self._queue.mark_retry(job.id, f"No live client for session {session_id}", backoff_seconds=1.0)
            return None

        # Attempt delivery to Telegram
        t0 = time.monotonic()
        try:
            target_msg_id = await self._dispatch_to_telegram(client, job)
            send_duration = time.monotonic() - t0
            self.total_dispatch_duration_ms += send_duration * 1000
            self.total_dispatches += 1
            _bump_target("sent")
            # v14: how long this job waited in the queue before being sent.
            if job.created_at:
                self._queue_wait_sum += max(0.0, time.time() - float(job.created_at))

            # Delivery confirmed!
            # v12.3 (ACID atomicity/durability): record the mapping BEFORE
            # marking the job. Both are separate commits, so a crash between
            # them leaves a window — mark_sent first leaves a SENT job with a
            # PENDING mapping forever (the job is never replayed, so nothing
            # ever fixes the mapping). Mapping first leaves a SENT mapping with
            # a CLAIMED job, which the next worker/lease-recovery pass settles
            # normally. Either way the message is not re-sent (Telegram already
            # has it); this only decides which half-state is self-healing.
            await self._map.update_delivery(
                rule_id=job.rule_id,
                source_chat_id=job.source_chat_id,
                source_message_id=job.source_message_id,
                target_chat_id=job.target_chat_id,
                target_message_id=target_msg_id,
                status=DeliveryStatus.SENT,
            )
            # v14: the SENT marker is committed once per batch by the worker
            # loop (mark_sent_batch) rather than here per job.
            self.stats["sent"] += 1
            logger.info(
                "[%s] Sent rule=%s src=%s:%s -> tgt=%s msg_id=%s (%.2fs)",
                worker_id,
                job.rule_id[:8],
                job.source_chat_id,
                job.source_message_id,
                target,
                target_msg_id,
                send_duration,
            )
            if self._log_client is not None:
                try:
                    self._log_client.info(
                        "pipeline", "forward",
                        f"delivered rule={job.rule_id[:8]} {job.source_chat_id}:{job.source_message_id} → {target}",
                        {"msg_id": target_msg_id, "ms": round(send_duration * 1000)},
                    )
                except Exception:
                    pass
            if self._metrics is not None:
                try:
                    await self._metrics.bump(forwarded=1)
                except Exception:
                    pass
            return (job.id, target_msg_id)

        except Exception as exc:
            send_duration = time.monotonic() - t0
            self.total_dispatch_duration_ms += send_duration * 1000
            self.total_dispatches += 1
            key, sev, recoverable = tg_error(exc)
            err_msg = tg_detail(exc)

            # 1. Ambiguous timeout / connection drop: evaluated FIRST to prevent blind duplicates
            is_timeout = (
                isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                or "timeout" in err_msg.lower()
                or "timeout" in str(type(exc).__name__).lower()
            )
            if is_timeout:
                self.stats["unknown"] += 1
                _bump_target("unknown")
                logger.error(
                    "[%s] Telegram call timed out on %s (result UNKNOWN). Marking job UNKNOWN to prevent duplicates.",
                    worker_id,
                    target,
                )
                await self._queue.mark_unknown(job.id, f"Telegram timeout: {err_msg}")
                await self._map.update_delivery(
                    rule_id=job.rule_id,
                    source_chat_id=job.source_chat_id,
                    source_message_id=job.source_message_id,
                    target_chat_id=job.target_chat_id,
                    target_message_id=None,
                    status=DeliveryStatus.UNKNOWN,
                )
                return None

            # 2. FloodWait / recoverable retry
            if "FloodWait" in str(type(exc).__name__) or recoverable:
                wait_sec = float(wait_seconds(exc, default=5) + random.uniform(0.5, 2.0))
                self.stats["floodwait_hits"] += 1
                self.stats["retried"] += 1
                _bump_target("retried")
                logger.warning(
                    "[%s] FloodWait on %s: waiting %.1fs. Rule=%s src=%s:%s",
                    worker_id,
                    target,
                    wait_sec,
                    job.rule_id[:8],
                    job.source_chat_id,
                    job.source_message_id,
                )
                await self._queue.mark_retry(job.id, err_msg, backoff_seconds=wait_sec)
                await self._map.update_delivery(
                    rule_id=job.rule_id,
                    source_chat_id=job.source_chat_id,
                    source_message_id=job.source_message_id,
                    target_chat_id=job.target_chat_id,
                    target_message_id=None,
                    status=DeliveryStatus.RETRY_WAIT,
                )
                return None

            # 3. Permanent or other transient error
            exc_name = str(type(exc).__name__)
            is_permanent = (
                sev == "CRITICAL"
                or any(
                    p in err_msg.upper()
                    for p in (
                        "CHAT_WRITE_FORBIDDEN",
                        "USER_BANNED_IN_CHANNEL",
                        "CHAT_ADMIN_REQUIRED",
                        "PEER_ID_INVALID",
                        "CHANNEL_PRIVATE",
                        "CHAT_RESTRICTED",
                        "CHANNEL_INVALID",
                        "CHAT_ID_INVALID",
                        "USERNAME_INVALID",
                        "USERNAME_NOT_OCCUPIED",
                    )
                )
                or any(
                    p in exc_name
                    for p in (
                        "ChatWriteForbidden",
                        "UserBannedInChannel",
                        "ChatAdminRequired",
                        "PeerIdInvalid",
                        "ChannelPrivate",
                        "ChatRestricted",
                        "ChannelInvalid",
                        "UsernameInvalid",
                        "UsernameNotOccupied",
                    )
                )
            )
            if is_permanent:
                self.stats["failed"] += 1
                _bump_target("failed")
                logger.error(
                    "[%s] Permanent failure delivering to %s: %s. Marking job FAILED.",
                    worker_id,
                    target,
                    err_msg,
                )
                await self._queue.mark_failed(job.id, err_msg)
                await self._map.update_delivery(
                    rule_id=job.rule_id,
                    source_chat_id=job.source_chat_id,
                    source_message_id=job.source_message_id,
                    target_chat_id=job.target_chat_id,
                    target_message_id=None,
                    status=DeliveryStatus.FAILED,
                )
            else:
                # Transient retry with exponential backoff + jitter
                attempts = job.attempts + 1
                backoff = min(60.0, (2.0 ** attempts) + random.uniform(0.5, 3.0))
                self.stats["retried"] += 1
                _bump_target("retried")
                logger.warning(
                    "[%s] Transient send error on %s (attempt %d): %s. Retrying in %.1fs.",
                    worker_id,
                    target,
                    attempts,
                    err_msg,
                    backoff,
                )
                await self._queue.mark_retry(job.id, err_msg, backoff_seconds=backoff)
                await self._map.update_delivery(
                    rule_id=job.rule_id,
                    source_chat_id=job.source_chat_id,
                    source_message_id=job.source_message_id,
                    target_chat_id=job.target_chat_id,
                    target_message_id=None,
                    status=DeliveryStatus.RETRY_WAIT,
                )
            return None

    async def _dispatch_to_telegram(self, client: Any, job: DeliveryJob) -> int:
        """Call pyrogram client methods and return the sent message ID."""
        data = job.payload_data
        target = int(job.target_chat_id) if job.target_chat_id.lstrip("-").isdigit() else job.target_chat_id
        src_chat = int(job.source_chat_id) if job.source_chat_id.lstrip("-").isdigit() else job.source_chat_id
        src_msg_id = int(job.source_message_id)

        forward_mode = data.get("forward_mode", "COPY_MESSAGE")
        content_mode = data.get("content_mode", "AUTO")
        text = data.get("text", "")
        has_media = data.get("has_media", False)

        if forward_mode == ForwardMode.DIRECT_FORWARD.value:
            res = await client.forward_messages(
                chat_id=target,
                from_chat_id=src_chat,
                message_ids=[src_msg_id],
            )
            if isinstance(res, list):
                return res[0].id if res else 0
            return getattr(res, "id", 0)

        # COPY_MESSAGE path
        if content_mode == "TEXT_ONLY":
            res = await client.send_message(chat_id=target, text=text or "...")
            return getattr(res, "id", 0)

        if content_mode == "MEDIA_ONLY":
            if has_media:
                res = await client.copy_message(chat_id=target, from_chat_id=src_chat, message_id=src_msg_id)
                return getattr(res, "id", 0)
            return 0

        # AUTO / TEXT_AND_MEDIA
        if has_media:
            caption = text if text else None
            res = await client.copy_message(
                chat_id=target,
                from_chat_id=src_chat,
                message_id=src_msg_id,
                caption=caption,
            )
            return getattr(res, "id", 0)

        res = await client.send_message(chat_id=target, text=text or "...")
        return getattr(res, "id", 0)

    # ------------------------------------------------------------------ #
    # Rate Limiting & Leases
    # ------------------------------------------------------------------ #
    async def _check_target_rate(self, target: str) -> float:
        """Check if target exceeded rate limit. If so, return seconds to wait.

        v14: the window is a bounded deque — it cannot grow past
        max_per_minute entries per target, and idle targets are pruned by
        _prune_rate_windows so a large destination fleet cannot leak memory.
        """
        from collections import deque
        now = time.monotonic()
        async with self._rate_lock:
            window = self._target_windows.get(target)
            if window is None:
                if len(self._target_windows) >= self._max_tracked_targets:
                    # v14: evict stale targets BEFORE inserting the new one so
                    # the map can never exceed the cap by one entry.
                    self._prune_rate_windows(now)
                if len(self._target_windows) >= self._max_tracked_targets:
                    # Everything is still active: refuse to track this target
                    # and let it send (the account-level Telegram limit is the
                    # real bound) rather than evict an active window.
                    return 0.0
                window = deque(maxlen=self._max_per_minute)
                self._target_windows[target] = window
            # Prune timestamps older than 60s
            while window and now - window[0] > 60:
                window.popleft()

            if len(window) >= self._max_per_minute:
                wait_time = max(0.5, 60.0 - (now - window[0]) + 0.5)
                return wait_time

            window.append(now)
            return 0.0

    def _prune_rate_windows(self, now: float) -> None:
        """Drop targets whose window is entirely stale (v14 memory bound).

        Called under _rate_lock when the tracked-target cap is reached. Keeps
        targets that sent anything in the last 60s, evicts the rest so a burst
        of one-off destinations does not accumulate forever.
        """
        stale = [
            tgt for tgt, win in self._target_windows.items()
            if not win or now - win[-1] > 60
        ]
        for tgt in stale:
            self._target_windows.pop(tgt, None)

    async def _watchdog_loop(self) -> None:
        """Periodically recovers expired leases and cleans up message maps."""
        cleanup_counter = 0
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(10.0)
                recovered = await self._queue.recover_expired_leases()
                if recovered > 0:
                    logger.warning("Lease watchdog recovered %d abandoned jobs.", recovered)
                    self._notify_event.set()

                cleanup_counter += 1
                if cleanup_counter >= 360:  # Every 1 hour (360 * 10s)
                    cleanup_counter = 0
                    try:
                        await self._queue.cleanup()
                        await self._map.cleanup()
                    except Exception:
                        logger.debug("Periodic retention cleanup failed", exc_info=True)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Lease watchdog check failed", exc_info=True)

    async def get_observability_metrics(self) -> dict:
        """Extract structured observability metrics per P1 specification.

        v14: adds throughput (jobs/sec), average queue wait (latency from
        enqueue to send), lease-expiry recovery count and RSS so the operator
        can see backpressure forming before it drops messages.
        """
        db_stats = await self._queue.get_stats()
        age_metrics = await self._queue.get_queue_age_metrics()
        avg_dispatch_ms = (
            self.total_dispatch_duration_ms / self.total_dispatches
            if self.total_dispatches > 0
            else 0.0
        )

        per_target = {}
        for tgt, counts in self.target_stats.items():
            tot = counts["sent"] + counts["failed"] + counts["unknown"]
            rate = (counts["sent"] / tot) if tot > 0 else 1.0
            per_target[tgt] = {
                "sent": counts["sent"],
                "failed": counts["failed"],
                "retried": counts["retried"],
                "unknown": counts["unknown"],
                "success_rate": round(rate, 4),
            }

        # v14: throughput over the process lifetime and average time a job
        # spends waiting in the queue (created_at -> SENT).
        lifetime_s = max(1e-9, time.time() - self._started_at)
        throughput = round(self.stats["sent"] / lifetime_s, 2)
        avg_queue_wait_s = round(self._queue_wait_sum / self.stats["sent"], 3) if self.stats["sent"] else 0.0

        try:
            rss_mb = round(_rss_mb(), 1)
        except Exception:
            rss_mb = 0.0

        return {
            "pending_jobs": db_stats.get(DeliveryStatus.PENDING.value, 0),
            "processing_jobs": db_stats.get(DeliveryStatus.CLAIMED.value, 0),
            "retry_wait_jobs": db_stats.get(DeliveryStatus.RETRY_WAIT.value, 0),
            "sent_jobs": db_stats.get(DeliveryStatus.SENT.value, 0),
            "failed_jobs": db_stats.get(DeliveryStatus.FAILED.value, 0),
            "unknown_jobs": db_stats.get(DeliveryStatus.UNKNOWN.value, 0),
            "dead_letter_jobs": db_stats.get(DeliveryStatus.DEAD_LETTER.value, 0),
            "queue_depth": age_metrics.get("pending_count", 0),
            "oldest_job_age_seconds": age_metrics.get("oldest_job_age_seconds", 0.0),
            "recovered_after_restart": self.recovered_after_restart_count,
            "avg_dispatch_duration_ms": round(avg_dispatch_ms, 2),
            "floodwait_hits": self.stats["floodwait_hits"],
            "backpressure_drops": self.stats["backpressure_drops"],
            "throughput_per_second": throughput,
            "avg_queue_wait_seconds": avg_queue_wait_s,
            "rss_mb": rss_mb,
            "targets": per_target,
        }

