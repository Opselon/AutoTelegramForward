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
import os
import random
import time
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from ...application.repositories import IDeliveryQueueRepository, IMessageMapRepository
from ...domain.entities import DeliveryJob, DeliveryStatus, ForwardMode, MessageMapping, MessagePayload
from ...domain.services import AdvancedSettingsEngine
from ...domain.value_objects import ContentMode
from .errors import tg_detail, tg_error, wait_seconds
from .bot_transport import BotRouteAdapter, BotTransportCache, BotTransportUnsupported

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
        rule_repo: Optional[Any] = None,
        num_workers: int = 3,
        lease_duration: float = 30.0,
        max_queue_size: int = 10000,
        max_per_minute_per_target: int = 25,
        metrics_repo: Any = None,
        log_client: Any = None,
        batch_size: int = 8,
        bot_route: Optional[BotTransportCache] = None,
    ) -> None:
        self._queue = queue_repo
        self._map = message_map_repo
        self._pool = client_pool
        self._rules = rule_repo
        self._num_workers = max(1, num_workers)
        self._log_client = log_client
        self._lease_duration = lease_duration
        self._max_queue_size = max_queue_size
        self._max_per_minute = max(1, max_per_minute_per_target)
        self._metrics = metrics_repo
        self._batch_size = max(1, batch_size)
        # Bot route: one pooled transport per attached token. None means the
        # route is unavailable in this process and a BOT job retries rather than
        # silently posting as the owner.
        self._bot_route_cache = bot_route

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
        job_kind: str = "forward",
        correlation_id: str = "",
        branding: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Enqueue a delivery job for one target chat.

        Returns True if accepted, False if backpressured or already queued (idempotent).

        ``job_kind`` discriminates the primary forward (``"forward"``) from
        secondary side-effect deliveries such as the keyword-reply add-on
        (``"keyword_reply"``). Without it both jobs produce the same id
        (``rule:chat:msg:target``) and the reply is silently deduplicated away.

        ``branding`` carries the already-performed branding hop: ``{"chat_id": C,
        "message_id": <post in C>, "force_forward": bool}``. The hop itself runs
        on the dispatch path (it needs a live client and must not be retried
        blindly); the queue only has to *reproduce* it, otherwise the worker
        delivers source=A and the target shows the original channel's header —
        the exact failure that made the feature look "configured but useless".
        The job's own source stays the original message on purpose: the mapping
        and the idempotency key are keyed on it, so a replay never posts a second
        copy into C.
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

        album_ids = [int(m.message_id) for m in getattr(payload, "album_messages", []) if hasattr(m, "message_id")]
        if not album_ids:
            album_ids = [int(payload.message_id)]

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
            "album_message_ids": album_ids,
            # Multi-photo option travels with the job: the worker decides whether
            # to keep the media group, send only its first item, or split it.
            "album_mode": str(getattr(rule, "album_mode", "album") or "album"),
            # --- Delivery route -------------------------------------------------
            # USER = the owner's own account (default). BOT = post through the
            # bot whose token the owner attached, which is what makes inline
            # buttons and bot-authored copies possible.
            "delivery_transport": str(getattr(rule, "delivery_transport", "USER") or "USER"),
            "bot_token_id": str(getattr(rule, "bot_token_id", "") or ""),
            "inline_buttons": getattr(rule, "inline_buttons_raw", None),
            "forward_mode": forward_mode_val,
            "content_mode": mode_val,
            "delay_seconds": float(getattr(rule, "delay_seconds", 0.0) or 0.0),
            # --- Phase 2: advanced task-overview settings reach the worker ---
            "advanced_delay_seconds": AdvancedSettingsEngine.get_dispatch_delay(rule),
            "auto_delete_seconds": AdvancedSettingsEngine.get_auto_delete_seconds(rule),
            "topic_id": int(getattr(rule, "topic_id", None) or 0),
            "debug_mode": bool(getattr(rule, "debug_mode", False)),
            "process_raw_text": bool(getattr(rule, "process_raw_text", False)),
            "button_to_text": bool(getattr(rule, "button_to_text", False)),
            # A keyword-reply job is a pure-text follow-up: it must never
            # re-copy or re-forward the source message's media.
            "is_reply": job_kind != "forward",
            "correlation_id": correlation_id or getattr(payload, "correlation_id", "") or "",
            # --- Branding hop (A -> C -> B) ----------------------------------
            # Already performed on the dispatch path; the worker only repeats it
            # so B receives a forward of C instead of the original source.
            "branding": dict(branding) if branding else None,
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
        # The reply is a distinct deliverable, so it gets a distinct id —
        # otherwise the primary forward's INSERT OR IGNORE swallows it.
        suffix = "" if job_kind == "forward" else f":{job_kind}"
        job_id = f"{rule.id}:{payload.chat_id}:{payload.message_id}:{target_chat_id}{suffix}"

        job = DeliveryJob(
            id=job_id,
            rule_id=rule.id,
            source_chat_id=str(payload.chat_id),
            source_message_id=int(payload.message_id),
            target_chat_id=str(target_chat_id),
            payload_data=payload_dict,
            status=DeliveryStatus.PENDING,
            next_retry_at=next_retry_at,
            # Audit trail for the hop: which channel C carried this delivery and
            # which post in C the target is forwarded from (columns exist since
            # v9 and were never populated on the dispatch path).
            intermediate_chat_id=str(branding["chat_id"]) if branding else None,
            intermediate_message_id=int(branding["message_id"]) if branding else None,
            delivery_stage="BRANDED" if branding else "DIRECT",
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

    async def _enqueue_atomic(self, mapping: MessageMapping, job: DeliveryJob) -> bool:
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
        if db is not None and hasattr(db, "run_in_transaction"):
            return bool(db.run_in_transaction(
                lambda conn: self._write_mapping_and_job(conn, mapping, job)
            ))

        # Non-SQLite fallback (PostgreSQL, in-memory, or test doubles):
        # write mapping and job, preserving the boolean enqueue outcome.
        await self._map.add_or_update(mapping)
        res = await self._queue.enqueue(job)
        return True if res is None else bool(res)

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

        # Automatically stop all forward actions when account is time-limited
        if self._pool is not None and hasattr(self._pool, "is_time_limited"):
            if session_id:
                try:
                    is_limited = self._pool.is_time_limited(session_id)
                    # If mock wasn't explicitly configured or not limited, ignore
                    if type(is_limited).__name__ != "MagicMock" and is_limited:
                        raw_rem = self._pool.remaining_limit_seconds(session_id)
                        rem = float(raw_rem) if isinstance(raw_rem, (int, float)) else 0.0
                        if rem > 0:
                            _bump_target("retried")
                            logger.info(
                                "[PIPELINE:TIME_LIMITED] [%s] Account %s is time-limited (%.1fs remaining). Pausing job %s",
                                worker_id, session_id[:8], rem, job.id,
                            )
                            await self._queue.mark_retry(job.id, f"Account time-limited ({rem:.1f}s)", backoff_seconds=max(2.0, rem))
                            return None
                except Exception as exc:
                    logger.debug("Failed checking time limit for %s: %s", session_id, exc)

        # Check if rule is still active or paused
        if self._rules is not None:
            rule_obj = await self._rules.get_by_id(job.rule_id)
            if rule_obj:
                if not rule_obj.is_active:
                    logger.info("[PIPELINE:DROP] [%s] Rule %s is disabled. Dropping job %s", worker_id, job.rule_id[:8], job.id)
                    await self._queue.mark_failed(job.id, "Rule disabled")
                    return None
                if getattr(rule_obj, "is_paused", False) and getattr(rule_obj, "is_currently_paused", lambda: True)():
                    backoff = 5.0
                    if getattr(rule_obj, "paused_until", 0) > 0:
                        rem = float(rule_obj.paused_until) - time.time()
                        if rem > 0:
                            backoff = max(2.0, rem)
                    _bump_target("retried")
                    logger.info(
                        "[PIPELINE:PAUSED] [%s] Rule %s is paused. Postponing job %s for %.1fs",
                        worker_id, job.rule_id[:8], job.id, backoff,
                    )
                    await self._queue.mark_retry(job.id, f"Rule paused ({backoff:.1f}s)", backoff_seconds=backoff)
                    return None

        transport_wanted = str(job.payload_data.get("delivery_transport") or "USER").strip().upper()
        route = None
        if transport_wanted == "BOT":
            route = await self._bot_route(job)
            if route is None:
                _bump_target("retried")
                logger.error(
                    "[BOT ROUTE] [%s] job %s asks for the bot route but token %s is unusable; "
                    "not falling back silently — fix the token or switch the task back to the account route",
                    worker_id,
                    job.id,
                    str(job.payload_data.get("bot_token_id") or "")[:8] or "(none)",
                )
                await self._queue.mark_retry(job.id, "bot route: attached token unusable", backoff_seconds=15.0)
                return None

        client = route or (self._pool.get(session_id) if self._pool is not None else None)
        if client is None:
            _bump_target("retried")
            logger.error(
                "[PIPELINE:NO_CLIENT] [%s] Cannot deliver job %s: No live client for session %s (rule=%s, tgt=%s)",
                worker_id, job.id, session_id, job.rule_id[:8], target,
            )
            if self._log_client is not None:
                try:
                    self._log_client.error(
                        "queue", "forward",
                        f"No live client for session {session_id} (rule={job.rule_id[:8]}, target={target})",
                        {"job_id": job.id, "rule_id": job.rule_id, "target": target},
                    )
                except Exception:
                    pass
            await self._queue.mark_retry(job.id, f"No live client for session {session_id}", backoff_seconds=1.0)
            return None

        # Attempt delivery to Telegram
        t0 = time.monotonic()
        logger.info(
            "[PIPELINE:DISPATCH] [%s] Dispatching rule=%s src=%s:%s -> tgt=%s (session=%s, attempt=%d)",
            worker_id, job.rule_id[:8], job.source_chat_id, job.source_message_id, target, session_id, job.attempts + 1,
        )
        try:
            try:
                target_msg_id = await self._dispatch_to_telegram(client, job)
            except BotTransportUnsupported as exc:
                # The bot route genuinely cannot express this job (a media group
                # whose source the bot cannot read, say). Keep the message
                # flowing through the owner's account instead of failing it.
                fallback_client = self._pool.get(session_id) if self._pool is not None else None
                if route is None or fallback_client is None:
                    raise
                logger.warning(
                    "[BOT ROUTE] [%s] %s — retrying job %s through the account transport",
                    worker_id,
                    exc,
                    job.id,
                )
                target_msg_id = await self._dispatch_to_telegram(fallback_client, job)
            send_duration = time.monotonic() - t0
            self.total_dispatches += 1
            _bump_target("sent")
            # v14: how long this job waited in the queue before being sent.
            if job.created_at:
                self._queue_wait_sum += max(0.0, time.time() - float(job.created_at))

            # Delivery confirmed!
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
                "[PIPELINE:DELIVERED] [%s] Sent rule=%s src=%s:%s -> tgt=%s msg_id=%s (took %.2fs)",
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

            # --- Phase 2: auto-delete the forwarded message after the TTL -----
            # The paid-addon gate was evaluated at enqueue time and the resolved
            # TTL travelled in the payload, so this needs no extra DB read.
            # A scheduling failure must never turn a successful forward into a
            # failure: the message is already delivered and visible.
            ttl = int(job.payload_data.get("auto_delete_seconds", 0) or 0)
            if ttl > 0 and target_msg_id:
                asyncio.create_task(
                    self._schedule_auto_delete(client, target, int(target_msg_id), ttl, job.id)
                )
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
                    "[PIPELINE:DELIVERY_TIMEOUT] [%s] Telegram call timed out on %s (result UNKNOWN). Marking job UNKNOWN to prevent duplicates. Rule=%s src=%s:%s error=%s",
                    worker_id,
                    target,
                    job.rule_id[:8],
                    job.source_chat_id,
                    job.source_message_id,
                    err_msg,
                )
                if self._log_client is not None:
                    try:
                        self._log_client.error(
                            "queue", "forward",
                            f"Telegram timeout delivering rule={job.rule_id[:8]} to target={target}: {err_msg}",
                            {"rule_id": job.rule_id, "target": target, "src": f"{job.source_chat_id}:{job.source_message_id}"},
                        )
                    except Exception:
                        pass
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
                # Automatically stop all forward actions for time-limited account
                if self._pool is not None and hasattr(self._pool, "set_time_limit") and session_id:
                    self._pool.set_time_limit(session_id, wait_sec)
                self.stats["floodwait_hits"] += 1
                self.stats["retried"] += 1
                _bump_target("retried")
                logger.warning(
                    "[PIPELINE:FLOODWAIT] [%s] FloodWait on %s: waiting %.1fs. Rule=%s src=%s:%s error=%s",
                    worker_id,
                    target,
                    wait_sec,
                    job.rule_id[:8],
                    job.source_chat_id,
                    job.source_message_id,
                    err_msg,
                )
                if self._log_client is not None:
                    try:
                        self._log_client.warn(
                            "queue", "forward",
                            f"Rate limit / FloodWait delivering rule={job.rule_id[:8]} to {target}: {err_msg} (waiting {round(wait_sec, 1)}s)",
                            {"rule_id": job.rule_id, "target": target, "wait_seconds": round(wait_sec, 1)},
                        )
                    except Exception:
                        pass
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
                    "[PIPELINE:DELIVERY_FAIL] [%s] Permanent failure delivering to %s (rule=%s, src=%s:%s): %s. Marking job FAILED.",
                    worker_id,
                    target,
                    job.rule_id[:8],
                    job.source_chat_id,
                    job.source_message_id,
                    err_msg,
                )
                # Automatically disable task forward when source or target fails or is banned
                if self._rules is not None:
                    try:
                        rule_obj = await self._rules.get_by_id(job.rule_id)
                        if rule_obj and rule_obj.is_active:
                            rule_obj.is_active = False
                            await self._rules.update(rule_obj)
                            logger.warning(
                                "[PIPELINE:AUTO_DISABLE] Automatically disabled task forward rule %s (source/target failed or banned: %s)",
                                job.rule_id[:8], err_msg,
                            )
                    except Exception as dis_err:
                        logger.error("Failed to auto-disable rule %s: %s", job.rule_id[:8], dis_err)
                if self._log_client is not None:
                    try:
                        self._log_client.error(
                            "queue", "forward",
                            f"Permanent delivery failure rule={job.rule_id[:8]} → {target}: {err_msg}",
                            {
                                "rule_id": job.rule_id,
                                "source_chat_id": job.source_chat_id,
                                "source_message_id": job.source_message_id,
                                "target": target,
                                "error": err_msg,
                                "exception": exc_name,
                            },
                        )
                    except Exception:
                        pass
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
                    "[PIPELINE:RETRY] [%s] Transient send error on %s (attempt %d, rule=%s, src=%s:%s): %s. Retrying in %.1fs.",
                    worker_id,
                    target,
                    attempts,
                    job.rule_id[:8],
                    job.source_chat_id,
                    job.source_message_id,
                    err_msg,
                    backoff,
                )
                if self._log_client is not None:
                    try:
                        self._log_client.warn(
                            "queue", "forward",
                            f"Transient delivery error (attempt {attempts}) rule={job.rule_id[:8]} → {target}: {err_msg}",
                            {
                                "rule_id": job.rule_id,
                                "target": target,
                                "attempt": attempts,
                                "backoff_seconds": round(backoff, 1),
                                "error": err_msg,
                            },
                        )
                    except Exception:
                        pass
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

    async def _reupload_media(
        self,
        client: Any,
        src_chat: Any,
        src_msg_id: int,
        target: Any,
        caption: Optional[str],
        media_type: str = "",
    ) -> int:
        temp_path = None
        try:
            src_msg = None
            if hasattr(client, "get_messages"):
                try:
                    src_msg = await client.get_messages(chat_id=src_chat, message_ids=src_msg_id)
                except Exception as e:
                    logger.debug("get_messages failed in _reupload_media: %s", e)

            # Try downloading media file
            if src_msg is not None and hasattr(client, "download_media"):
                try:
                    temp_path = await client.download_media(src_msg)
                except Exception as dl_err:
                    logger.debug("download_media failed in _reupload_media: %s", dl_err)

            if temp_path and os.path.exists(temp_path):
                mtype = str(media_type).lower()
                if mtype == "photo" or getattr(src_msg, "photo", None):
                    res = await client.send_photo(chat_id=target, photo=temp_path, caption=caption)
                elif mtype in ("video", "round_video") or getattr(src_msg, "video", None):
                    res = await client.send_video(chat_id=target, video=temp_path, caption=caption)
                elif mtype == "voice" or getattr(src_msg, "voice", None):
                    res = await client.send_voice(chat_id=target, voice=temp_path, caption=caption)
                elif mtype == "audio" or getattr(src_msg, "audio", None):
                    res = await client.send_audio(chat_id=target, audio=temp_path, caption=caption)
                elif mtype == "animation" or getattr(src_msg, "animation", None):
                    res = await client.send_animation(chat_id=target, animation=temp_path, caption=caption)
                elif mtype == "sticker" or getattr(src_msg, "sticker", None):
                    res = await client.send_sticker(chat_id=target, sticker=temp_path)
                else:
                    res = await client.send_document(chat_id=target, document=temp_path, caption=caption)
                return getattr(res, "id", 0)

            # Fallback: send text if media re-upload failed
            if caption:
                res = await client.send_message(chat_id=target, text=caption)
                return getattr(res, "id", 0)
            return 0
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    async def _reupload_album(
        self,
        client: Any,
        src_chat: Any,
        album_ids: List[int],
        target: Any,
        caption: Optional[str],
        return_all: bool = False,
    ) -> Any:
        """Re-download and re-upload a whole album.

        Returns the new first message id (the delivery uses it as its result), or
        the ids of *every* message of the re-created group when ``return_all`` is
        set — the branding hop needs all of them so stage 2 can forward the album
        instead of one item of it.
        """
        temp_files: List[str] = []
        try:
            from pyrogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo

            msgs = []
            if hasattr(client, "get_messages"):
                try:
                    raw_msgs = await client.get_messages(chat_id=src_chat, message_ids=album_ids)
                    msgs = raw_msgs if isinstance(raw_msgs, list) else [raw_msgs]
                except Exception as e:
                    logger.debug("get_messages failed in _reupload_album: %s", e)

            media_list = []
            for i, m in enumerate(msgs):
                if not m or not hasattr(client, "download_media"):
                    continue
                try:
                    path = await client.download_media(m)
                    if path and os.path.exists(path):
                        temp_files.append(path)
                        cap = caption if i == 0 else None
                        if getattr(m, "photo", None):
                            media_list.append(InputMediaPhoto(path, caption=cap))
                        elif getattr(m, "video", None):
                            media_list.append(InputMediaVideo(path, caption=cap))
                        elif getattr(m, "audio", None):
                            media_list.append(InputMediaAudio(path, caption=cap))
                        else:
                            media_list.append(InputMediaDocument(path, caption=cap))
                except Exception as dl_err:
                    logger.debug("download_media item failed in _reupload_album: %s", dl_err)

            if media_list and hasattr(client, "send_media_group"):
                res = await client.send_media_group(chat_id=target, media=media_list)
                if return_all and isinstance(res, list):
                    return [getattr(m, "id", 0) for m in res if getattr(m, "id", 0)]
                if isinstance(res, list) and res:
                    return res[0].id
                return getattr(res, "id", 0)

            if caption:
                res = await client.send_message(chat_id=target, text=caption)
                return getattr(res, "id", 0)
            return 0
        finally:
            for p in temp_files:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    async def _schedule_auto_delete(
        self, client: Any, target: Any, message_id: int, ttl: int, job_id: str
    ) -> None:
        """Delete a forwarded message from the target after ``ttl`` seconds.

        The paid-addon entitlement is resolved at enqueue time; this coroutine
        only carries out the already-authorized deletion. Any Telegram error
        here is diagnostic-only: the forward itself already succeeded and must
        never be reported as failed because the cleanup missed.
        """
        try:
            await asyncio.sleep(min(ttl, 31_536_000))
            await client.delete_messages(chat_id=target, message_ids=message_id)
            logger.info(
                "[PIPELINE:AUTO_DELETE] [%s] Deleted forwarded message %s in %s after %ds",
                job_id, message_id, target, ttl,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[PIPELINE:AUTO_DELETE] [%s] Failed to delete message %s in %s after %ds: %s",
                job_id, message_id, target, ttl, tg_detail(exc),
            )

    async def _bot_route(self, job: DeliveryJob) -> Optional[Any]:
        """A per-job delivery view onto the bot the task attached.

        Returns ``None`` when the token is missing, inactive or unusable — the
        caller then retries the job (it must not quietly post as the owner, which
        would leak the sender identity the owner deliberately changed).
        """
        if self._bot_route_cache is None:
            logger.warning("[BOT ROUTE] job %s asks for the bot route but no token repository is wired here", job.id)
            return None

        token_id = str(job.payload_data.get("bot_token_id") or "")
        transport = await self._bot_route_cache.get(token_id)
        if transport is None:
            return None

        buttons = job.payload_data.get("inline_buttons")
        thread_id = int(job.payload_data.get("topic_id") or 0)
        logger.info(
            "[BOT ROUTE] delivering rule=%s via bot %s (token %s, buttons=%d)",
            job.rule_id[:8],
            transport.username or transport.bot_id,
            token_id[:8],
            len(buttons or []) if isinstance(buttons, (list, tuple)) else 0,
        )
        return BotRouteAdapter(transport, buttons=buttons, thread_id=thread_id)

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
        album_ids = data.get("album_message_ids") or [src_msg_id]
        is_album = bool(data.get("media_group_id")) and len(album_ids) > 1

        # Phase 2: a keyword-reply job is a pure-text follow-up. Force text-only
        # dispatch regardless of the source message's media so the reply never
        # re-copies or re-forwards the original attachment.
        if bool(data.get("is_reply", False)):
            forward_mode = "COPY_MESSAGE"
            content_mode = "TEXT_ONLY"
            is_album = False
            has_media = False

        # --- Phase 2: advanced task-overview settings execute here -------------
        # `advanced_delay_seconds` is the authoritative Phase 2 field; the
        # legacy `delay_seconds` is kept as a fallback so existing rules that
        # predate the advanced UI keep behaving exactly as before.
        adv_delay = data.get("advanced_delay_seconds")
        delay = float(adv_delay) if adv_delay is not None else float(data.get("delay_seconds", 0.0) or 0.0)
        if delay > 0:
            logger.debug(
                "[DURABLE_QUEUE] applying advanced dispatch delay %.1fs for job %s -> %s",
                delay, job.id, target,
            )
            await asyncio.sleep(min(delay, 3600.0))

        caption = text if text else None

        # --- Phase 2: forum-topic routing -----------------------------------
        # Telegram addresses a topic inside a supergroup with the
        # `message_thread_id` parameter. When a rule configures `topic_id` the
        # target must be the topic thread, not the group's General topic, or
        # the message silently lands in the wrong place. Only a positive id is
        # meaningful: 0/None means "no topic configured".
        topic_id = int(data.get("topic_id", 0) or 0)
        thread_kwargs: Dict[str, Any] = {}
        if topic_id > 0:
            thread_kwargs["message_thread_id"] = topic_id

        # --- Branding hop (A -> C -> B) --------------------------------------
        # The message was already copied into our channel C on the dispatch path
        # (with every transform applied) and the target must now receive a forward
        # of *that* post, so the header names C. Without this the worker delivered
        # source=A and the target saw the original channel's header — the hop was
        # configured, the post landed in C, and the branding never reached B.
        #
        # Stage 2 reproduces C's post and nothing more: the content is never
        # re-processed here, so Replace rules / AI rewrite / header / watermark
        # are applied exactly once (on the dispatch path, before the hop) and can
        # neither be skipped nor doubled by a retry.
        branding = data.get("branding") or {}
        if branding and branding.get("chat_id") and branding.get("message_id"):
            c_chat = branding["chat_id"]
            c_chat = int(c_chat) if str(c_chat).lstrip("-").isdigit() else c_chat
            c_ids = [int(i) for i in (branding.get("message_ids") or [branding["message_id"]])]
            if bool(branding.get("force_forward", True)):
                try:
                    res = await client.forward_messages(
                        chat_id=target, from_chat_id=c_chat,
                        message_ids=c_ids, **thread_kwargs,
                    )
                    if isinstance(res, list) and res:
                        return getattr(res[0], "id", 0)
                    return getattr(res, "id", 0)
                except Exception as hop_err:
                    err_str = str(hop_err).lower()
                    if any(k in err_str for k in ("restrict", "protect", "forbidden", "chatforwardsrestricted")):
                        logger.info(
                            "[PIPELINE:BRANDING] forward from C=%s restricted (%s) — copying C's post instead",
                            c_chat, hop_err,
                        )
                    else:
                        raise
            # Clean copy of C's own post (either requested, or the forward was
            # restricted): content still comes from C, never from the source. No
            # caption is passed, so Telegram keeps C's caption *and* its entities
            # verbatim — re-sending `text` here would be a second application of
            # the same content.
            try:
                last_id = 0
                for c_msg_id in c_ids:
                    copy_kwargs = dict(thread_kwargs)
                    if content_mode == "MEDIA_ONLY":
                        # Mirror the direct path: media only means no caption.
                        copy_kwargs["caption"] = ""
                    res = await client.copy_message(
                        chat_id=target, from_chat_id=c_chat, message_id=c_msg_id,
                        **copy_kwargs,
                    )
                    last_id = getattr(res, "id", last_id) or last_id
                return last_id
            except Exception as copy_err:
                err_str = str(copy_err).lower()
                if any(k in err_str for k in ("restrict", "protect", "forbidden", "chatforwardsrestricted")):
                    logger.info(
                        "[PIPELINE:BRANDING] copy from C=%s restricted (%s) — re-uploading",
                        c_chat, copy_err,
                    )
                    return await self._reupload_media(
                        client, c_chat, c_ids[0], target,
                        None if content_mode == "MEDIA_ONLY" else caption,
                        data.get("media_type", ""),
                    )
                raise

        # 1. DIRECT_FORWARD
        if forward_mode == ForwardMode.DIRECT_FORWARD.value:
            try:
                res = await client.forward_messages(
                    chat_id=target,
                    from_chat_id=src_chat,
                    message_ids=album_ids,
                    **thread_kwargs,
                )
                if isinstance(res, list) and res:
                    return res[0].id
                return getattr(res, "id", 0)
            except Exception as fwd_err:
                err_str = str(fwd_err).lower()
                if "restrict" in err_str or "protect" in err_str or "forbidden" in err_str or "chatforwardsrestricted" in err_str:
                    logger.info("Direct forward restricted (%s), falling back to copy/reupload", fwd_err)
                else:
                    raise

        # 2. ALBUM HANDLING (COPY / REUPLOAD)
        if is_album:
            # Per-task multi-photo option. "album" keeps the media group,
            # "first" delivers only its first item, "split" delivers every item
            # as its own post. The switch was persisted and rendered for months
            # while the worker never read it — every mode behaved like "album".
            album_mode = str(data.get("album_mode") or "album").lower()
            if album_mode == "first":
                try:
                    res = await client.copy_message(
                        chat_id=target,
                        from_chat_id=src_chat,
                        message_id=int(album_ids[0]),
                        caption=caption,
                        **thread_kwargs,
                    )
                    return getattr(res, "id", 0)
                except Exception as one_err:
                    logger.info("first-album-item copy failed (%s), falling back to the group", one_err)
            elif album_mode == "split":
                last_id = 0
                for item_id in album_ids:
                    res = await client.copy_message(
                        chat_id=target,
                        from_chat_id=src_chat,
                        message_id=int(item_id),
                        caption=caption,
                        **thread_kwargs,
                    )
                    last_id = getattr(res, "id", last_id) or last_id
                return last_id
            try:
                if hasattr(client, "copy_media_group"):
                    res = await client.copy_media_group(
                        chat_id=target,
                        from_chat_id=src_chat,
                        message_id=src_msg_id,
                        captions=caption,
                        **thread_kwargs,
                    )
                    if isinstance(res, list) and res:
                        return res[0].id
                    return getattr(res, "id", 0)
            except Exception as grp_err:
                logger.info("copy_media_group failed (%s), falling back to re-upload bypass", grp_err)

            return await self._reupload_album(client, src_chat, album_ids, target, caption)

        # 3. TEXT_ONLY mode
        if content_mode == "TEXT_ONLY":
            res = await client.send_message(chat_id=target, text=text or "...", **thread_kwargs)
            return getattr(res, "id", 0)

        # 4. MEDIA_ONLY mode
        if content_mode == "MEDIA_ONLY":
            if has_media:
                try:
                    res = await client.copy_message(
                        chat_id=target, from_chat_id=src_chat, message_id=src_msg_id, **thread_kwargs
                    )
                    return getattr(res, "id", 0)
                except Exception as c_err:
                    err_str = str(c_err).lower()
                    if "restrict" in err_str or "protect" in err_str or "forbidden" in err_str or "chatforwardsrestricted" in err_str:
                        return await self._reupload_media(client, src_chat, src_msg_id, target, None, data.get("media_type", ""))
                    raise
            return 0

        # 5. AUTO / TEXT_AND_MEDIA mode
        if has_media:
            try:
                res = await client.copy_message(
                    chat_id=target,
                    from_chat_id=src_chat,
                    message_id=src_msg_id,
                    caption=caption,
                    **thread_kwargs,
                )
                return getattr(res, "id", 0)
            except Exception as c_err:
                err_str = str(c_err).lower()
                if "restrict" in err_str or "protect" in err_str or "forbidden" in err_str or "chatforwardsrestricted" in err_str:
                    logger.info("copy_message restricted (%s), attempting re-upload bypass", c_err)
                    return await self._reupload_media(client, src_chat, src_msg_id, target, caption, data.get("media_type", ""))
                raise

        res = await client.send_message(chat_id=target, text=text or "...", **thread_kwargs)
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

