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
from typing import Any, Callable, Dict, List, Optional

from ...application.repositories import IDeliveryQueueRepository, IMessageMapRepository
from ...domain.entities import DeliveryJob, DeliveryStatus, ForwardMode, MessageMapping, MessagePayload
from ...domain.value_objects import ContentMode
from .errors import tg_detail, tg_error, wait_seconds

logger = logging.getLogger("atf.durable_queue")


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
    ) -> None:
        self._queue = queue_repo
        self._map = message_map_repo
        self._pool = client_pool
        self._num_workers = max(1, num_workers)
        self._lease_duration = lease_duration
        self._max_queue_size = max_queue_size
        self._max_per_minute = max_per_minute_per_target
        self._metrics = metrics_repo

        self._workers: List[asyncio.Task] = []
        self._watchdog_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._notify_event = asyncio.Event()
        self._running = False

        # Per-target token-bucket rate limiter: target -> list of timestamps
        self._target_windows: Dict[str, List[float]] = {}
        self._rate_lock = asyncio.Lock()

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

    async def start(self) -> None:
        """Start worker tasks and lease watchdog."""
        if self._running:
            return
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
            return
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
        await self._map.add_or_update(mapping)

        # 4. Enqueue durable job
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
        enqueued = await self._queue.enqueue(job)
        if enqueued:
            self.stats["enqueued"] += 1
            self._notify_event.set()

        return enqueued

    def _notify_worker(self) -> None:
        """Signal waiting workers that new work or retry is available."""
        self._notify_event.set()

    # ------------------------------------------------------------------ #
    # Worker Loop
    # ------------------------------------------------------------------ #
    async def _worker_loop(self, worker_id: str) -> None:
        """Continuous non-blocking worker polling for claimed delivery jobs."""
        logger.debug("[%s] Worker started", worker_id)
        while not self._stop_event.is_set():
            try:
                jobs = await self._queue.claim_batch(
                    worker_id=worker_id,
                    batch_size=5,
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

                for job in jobs:
                    if self._stop_event.is_set():
                        break
                    await self._process_job(worker_id, job)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[%s] Unexpected error in worker loop", worker_id)
                await asyncio.sleep(0.5)

    async def _process_job(self, worker_id: str, job: DeliveryJob) -> None:
        """Process a single delivery job with rate limiting, error classification, and mapping confirmation."""
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
            return

        session_id = job.payload_data.get("session_id")
        client = self._pool.get(session_id)
        if client is None:
            _bump_target("retried")
            await self._queue.mark_retry(job.id, f"No live client for session {session_id}", backoff_seconds=1.0)
            return

        # Attempt delivery to Telegram
        t0 = time.monotonic()
        try:
            target_msg_id = await self._dispatch_to_telegram(client, job)
            send_duration = time.monotonic() - t0
            self.total_dispatch_duration_ms += send_duration * 1000
            self.total_dispatches += 1
            _bump_target("sent")

            # Delivery confirmed!
            await self._queue.mark_sent(job.id, target_msg_id)
            await self._map.update_delivery(
                rule_id=job.rule_id,
                source_chat_id=job.source_chat_id,
                source_message_id=job.source_message_id,
                target_chat_id=job.target_chat_id,
                target_message_id=target_msg_id,
                status=DeliveryStatus.SENT,
            )
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
            if self._metrics is not None:
                try:
                    await self._metrics.bump(forwarded=1)
                except Exception:
                    pass

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
                return

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
                return

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
        """Check if target exceeded rate limit. If so, return seconds to wait."""
        now = time.monotonic()
        async with self._rate_lock:
            window = self._target_windows.setdefault(target, [])
            # Prune timestamps older than 60s
            while window and now - window[0] > 60:
                window.pop(0)

            if len(window) >= self._max_per_minute:
                wait_time = max(0.5, 60.0 - (now - window[0]) + 0.5)
                return wait_time

            window.append(now)
            return 0.0

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
        """Extract structured observability metrics per P1 specification."""
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

        return {
            "pending_jobs": db_stats.get(DeliveryStatus.PENDING.value, 0),
            "processing_jobs": db_stats.get(DeliveryStatus.CLAIMED.value, 0),
            "retry_wait_jobs": db_stats.get(DeliveryStatus.RETRY_WAIT.value, 0),
            "sent_jobs": db_stats.get(DeliveryStatus.SENT.value, 0),
            "failed_jobs": db_stats.get(DeliveryStatus.FAILED.value, 0),
            "unknown_jobs": db_stats.get(DeliveryStatus.UNKNOWN.value, 0),
            "queue_depth": age_metrics.get("pending_count", 0),
            "oldest_job_age_seconds": age_metrics.get("oldest_job_age_seconds", 0.0),
            "recovered_after_restart": self.recovered_after_restart_count,
            "avg_dispatch_duration_ms": round(avg_dispatch_ms, 2),
            "floodwait_hits": self.stats["floodwait_hits"],
            "backpressure_drops": self.stats["backpressure_drops"],
            "targets": per_target,
        }

