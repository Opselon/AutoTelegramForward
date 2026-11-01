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

from ...application.repositories import (
    DeliveryAttemptKey,
    IDeliveryQueueRepository,
    IMessageMapRepository,
)
from ...domain.entities import DeliveryJob, DeliveryStatus, ForwardMode, MessageMapping, MessagePayload
from ...domain.services import AdvancedSettingsEngine
from ...domain.value_objects import ContentMode
from .errors import is_restricted_source_error, is_timeout_error, tg_detail, tg_error, wait_seconds
from .diagnostics_contract import (
    C_DELIVERY, C_MEDIA, C_PIPELINE, C_TELEGRAM_API, R_DELIVERY_OUTCOME_UNKNOWN,
    R_TELEGRAM_FLOOD_WAIT, ORIGIN_TELEGRAM, S_COPY, S_NATIVE_FORWARD,
    S_REUPLOAD, S_TEXT_RECONSTRUCT, build_diagnostic_event,
    log_diagnostic_event, strategy_of, strategy_rpc_method,
)
from .failure_policy import classify_failure, error_code, rate_limit_seconds
from .bot_transport import BotRouteAdapter, BotTransportCache, BotTransportUnsupported
from .media_reference import extract_media_reference, send_media_reference

logger = logging.getLogger("atf.durable_queue")


class DeliveryBlockedError(RuntimeError):
    """Every permitted delivery method failed and *nothing* was sent.

    Raised instead of returning ``0`` so the caller records an honest FAILED
    (or dead-letters) rather than reporting a success that never happened.
    Not a task defect: the task stays active, the message waits in the
    dead-letter queue with the full reason attached.
    """


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


def _dumps_decode(raw) -> dict:
    """Decode a stored job payload, tolerating either engine's row shape."""
    import json as _json
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            data = _json.loads(raw)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    return {}




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
        lease_coordinator: Any = None,
        owner_notifier: Optional[Callable[..., Any]] = None,
        attempts_repo: Any = None,
        diagnostics_repo: Any = None,
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
        # Redis lease coordinator (optional, see core/infrastructure/redis).
        # None means "this deployment is one replica": every maintenance action
        # runs here, exactly as it did before coordination existed. With a
        # coordinator, the periodic actions below run on one replica at a time.
        # The delivery claim stays in the database either way — a lease can be
        # lost while it is held, so it can never be what makes delivery
        # idempotent.
        self._lease = lease_coordinator

        # Send-side idempotency fence (audit finding #5). Optional: ``None``
        # keeps every existing embedding and unit test working unchanged; when
        # absent the worker falls back to the pre-fence behaviour (lease-only),
        # which is exactly the behaviour this store was introduced to close.
        self._attempts = attempts_repo
        # Failure intelligence (schema v32): the structured diagnostic events
        # store. Optional and None by default — every existing embedding and
        # unit test keeps working, and the send path simply skips persistence
        # when no store is wired.
        self._diagnostics = diagnostics_repo

        # Tells a task's owner that the engine had to pause it (dead source or
        # target) or that their account cannot send. Wired in main.py to the
        # bot; `None` keeps every unit test and embedding working unchanged.
        self._owner_notifier = owner_notifier
        # session_id -> the deadline we already told the owner about; a queue of
        # jobs hitting the same suspension must not send one message each.
        self._account_notices: Dict[str, float] = {}
        # session_id -> [consecutive no-client sweeps, next retry epoch, last
        # warn epoch]. A dead/offline session used to re-log and re-defer every
        # one of its queued jobs every 15s, which is how a single unregistered
        # session produced ~14k identical ERRORs and kept the queue spinning.
        # Backoff is per session, not per job, because every job on that
        # session is blocked by the same missing client.
        self._no_client_backoff: Dict[str, list] = {}

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
            # Deliveries that needed the copy/re-upload bypass because native
            # forwarding was refused (protected source, bot route, ...).
            "fallback_copies": 0,
        }
        self.recovered_after_restart_count: int = 0
        self.target_stats: Dict[str, Dict[str, int]] = {}
        self.total_dispatch_duration_ms: float = 0.0
        self.total_dispatches: int = 0
        # v14 observability
        self._started_at: float = time.time()
        self._queue_wait_sum: float = 0.0

    async def start(self) -> None:
        """Start worker tasks and lease watchdog.

        Strangler-fig migration switch: when ``ATF_WORKER_ENABLED`` is set,
        the Go Worker owns delivery-job execution and this loop deliberately
        does NOT start. The enqueue path (inserting into delivery_jobs)
        stays here — ingest is still Python's — only the consumption moves.
        That keeps exactly one authoritative execution path at a time, and
        rolling back to the Python loop is a config flip, not a redeploy.
        """
        if os.environ.get("ATF_WORKER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
            self._running = False
            logger.info(
                "[DURABLE_QUEUE] ATF_WORKER_ENABLED is set — the Go Worker owns delivery "
                "execution; the Python queue loop is standing down (enqueue still active)."
            )
            return None
        if self._running:
            return None
        self._running = True
        self._stop_event.clear()
        self._notify_event.clear()

        # Immediate recovery of orphaned jobs on startup / restart. On a
        # multi-replica deployment this is a singleton action: N replicas
        # recovering in parallel do N times the work and produce N conflicting
        # log lines for one event.
        try:
            initial_recovered = 0
            if await self._holds_singleton("queue-startup-recovery"):
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
            "Durable queue started with %d workers (lease=%.1fs, max_capacity=%d, coordination=%s)",
            self._num_workers,
            self._lease_duration,
            self._max_queue_size,
            "redis" if self._lease is not None else "single-replica",
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
            if self._log_client is not None:
                try:
                    self._log_client.error(
                        "queue", "forward",
                        f"Durable queue full ({pending_count} pending >= {self._max_queue_size} max); "
                        f"delivery for target {target_chat_id} refused",
                        {"target": str(target_chat_id), "pending": pending_count, "event_code": "QUEUE_BACKPRESSURE", "owner_user_id": int(getattr(rule, "owner_user_id", 0) or 0)},
                    )
                except Exception:
                    pass
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
            # The worker must honor the same fallback opt-out the live path
            # does: with this off, a refused forward fails the job instead of
            # being rescued by a copy/re-upload the owner did not ask for.
            "fallback_enabled": bool(getattr(rule, "fallback_enabled", True)),
            # The owner's log attribution: pipeline events ride into the shared
            # log store carrying this id, so a tenant's Log Center shows their
            # own deliveries (the API pins log reads to it) without the worker
            # re-resolving the rule on every emission.
            "owner_user_id": int(getattr(rule, "owner_user_id", 0) or 0),
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
        # The delay is a *durable scheduling* decision, so it is written into the
        # job's claim gate (`next_retry_at`) once, from the single authority the
        # worker also reads. Applying it here *and* sleeping in the worker made a
        # configured 300s wait 600s, and the sleep died with the process while the
        # gate survives a restart.
        delay = AdvancedSettingsEngine.get_dispatch_delay(rule)
        next_retry_at = time.time() + delay if delay > 0 else 0.0
        # The reply is a distinct deliverable, so it gets a distinct id. The id
        # alone is not enough: ``delivery_jobs`` carries a UNIQUE key on
        # (rule, source_chat, source_message, target, job_kind), and the kind is
        # part of that key so a forward and a keyword reply for the same event
        # are two rows rather than one — with the kind omitted from the key the
        # reply's INSERT OR IGNORE matched the forward's row, returned False and
        # the reply was dropped in silence.
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
            job_kind=job_kind,
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

    async def _record_diagnostic(self, event: Any) -> None:
        """Persist one structured diagnostic event, best effort.

        The send path must never wait on diagnostics: the write is deferred to
        a fire-and-forget task, and any failure is logged once and swallowed —
        the delivery's own state transition is the authoritative record, this
        is the forensic layer on top of it.

        The event also fans out to the Rust logger (§10): that stream is what
        the operational dashboards already read, so a failure is visible in
        both places — the persisted per-attempt row and the live log — without
        duplicating the business state.
        """
        if event is None:
            return
        log_diagnostic_event(self._log_client, event)
        if self._diagnostics is None:
            return
        try:
            await self._diagnostics.record_event(event)
        except Exception as exc:
            logger.debug("[PIPELINE:DIAGNOSTIC_WRITE_FAILED] %s", exc)

    def _diagnostic_for_send_failure(self, *, job: DeliveryJob, exc: BaseException,
                                     err_msg: str, owner_id: int, worker_id: str,
                                     strategy: str, category: str,
                                     reason: str = "", event_name: str = "") -> Any:
        """Build the canonical event for a failed Telegram send.

        The raw layer is extracted from the *actual* exception through
        ``errors.extract`` — never fabricated, never paraphrased away — and
        the normalized layer is the stable (category, reason) pair the
        monitoring facets aggregate on.
        """
        from .diagnostics_contract import build_diagnostic_event, ORIGIN_TELEGRAM
        from .failure_policy import classify_failure
        is_timeout = is_timeout_error(exc)
        decision = classify_failure(exc)
        return build_diagnostic_event(
            event_name=event_name or "delivery.send.failed",
            stage="SEND", task_id=job.rule_id,
            trace_id=str((job.payload_data or {}).get("correlation_id", "") or ""),
            correlation_id=str((job.payload_data or {}).get("correlation_id", "") or ""),
            owner_id=owner_id, job_id=job.id, worker_id=worker_id,
            source_peer_id=job.source_chat_id, source_message_id=job.source_message_id,
            target_peer_id=job.target_chat_id,
            destination_message_id=int((job.payload_data or {}).get("sent_message_id") or 0),
            album_id=str((job.payload_data or {}).get("media_group_id") or ""),
            attempt=int(job.attempts or 0), strategy=strategy,
            category=category, reason=reason, error_origin=ORIGIN_TELEGRAM,
            # Transient classification reads the same policy the retry
            # decision uses, so the diagnostic and the behaviour cannot disagree.
            retryable=bool(decision.kind == "transient"),
            fallback_eligible=is_restricted_source_error(exc),
            severity="WARN" if is_timeout else "ERROR",
            raw_exception=exc,
            raw_method=strategy_rpc_method(strategy),
            explanation=str(err_msg or exc)[:500],
            client=getattr(self, "_pool", None),
        )

    async def _record_send_diagnostic(self, *, job: DeliveryJob,
                                      exc: BaseException, strategy: str,
                                      context: str = "") -> None:
        """Record the raw Telegram error for one failed send attempt.

        Called at the exact call site that raised, so the event states what
        was *actually* attempted and what Telegram answered — before any
        fallback rewrites the outcome. The fallback's own success adds another
        event; this one is never overwritten, which is how a successful
        re-upload still shows the original restriction (mission §6.3).
        """
        if self._diagnostics is None:
            return
        from .diagnostics_contract import build_diagnostic_event, ORIGIN_TELEGRAM
        try:
            await self._record_diagnostic(build_diagnostic_event(
                event_name="delivery.strategy.failed", stage="SEND",
                task_id=job.rule_id,
                trace_id=str((job.payload_data or {}).get("correlation_id", "") or ""),
                correlation_id=str((job.payload_data or {}).get("correlation_id", "") or ""),
                owner_id=int((job.payload_data or {}).get("owner_user_id") or 0),
                job_id=job.id, worker_id=str(getattr(self, "_worker_tag", "") or ""),
                source_peer_id=job.source_chat_id, source_message_id=job.source_message_id,
                target_peer_id=job.target_chat_id,
                album_id=str((job.payload_data or {}).get("media_group_id") or ""),
                attempt=int(job.attempts or 0), strategy=strategy,
                category=C_TELEGRAM_API, error_origin=ORIGIN_TELEGRAM,
                severity="WARN", raw_exception=exc,
                raw_method=strategy_rpc_method(strategy),
                explanation=f"{context or strategy} failed: {type(exc).__name__}",
            ))
        except Exception:
            logger.debug("[PIPELINE:DIAGNOSTIC_BUILD_FAILED] %s", exc)

    async def _note_mapping(self, job: DeliveryJob, status, target_message_id=None) -> None:
        """Record a delivery outcome on the job's source→target mapping.

        Only the *primary forward* owns that row. ``message_map`` is keyed by
        (rule, source_chat, source_message, target), so a keyword reply — a
        second deliverable to the same target for the same source message —
        shares the key with the forward. Letting it write there replaced the
        forward's ``target_message_id`` with the reply's message id (so edit and
        delete sync acted on the reply post instead of the forwarded copy) and
        rewrote the forward's delivery status (a failed reply marked a delivered
        forward as RETRY_WAIT/FAILED). The reply has its own ``delivery_jobs``
        row; that is where its outcome is recorded.
        """
        kind = str(getattr(job, "job_kind", "forward") or "forward")
        is_reply = bool((job.payload_data or {}).get("is_reply", False))
        if kind != "forward" or is_reply:
            logger.debug(
                "[DURABLE_QUEUE] job %s is a %s deliverable — mapping row left to the forward",
                job.id, kind,
            )
            return
        await self._map.update_delivery(
            rule_id=job.rule_id,
            source_chat_id=job.source_chat_id,
            source_message_id=job.source_message_id,
            target_chat_id=job.target_chat_id,
            target_message_id=target_message_id,
            status=status,
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
        job_kind = str(getattr(job, "job_kind", "forward") or "forward")
        # The mapping row is the source→target relationship, established by the
        # primary forward. A reply shares its key, so writing it here would
        # refresh (`delivery_status`) a row that describes a different
        # deliverable — a replay of the event could flip a SENT forward back to
        # PENDING and make the source look undelivered.
        if job_kind == "forward":
            # Audit finding #3: a replay of an already-processed event must
            # never regress the mapping. The job insert below is INSERT OR
            # IGNORE, so when the job already exists the enqueue reports
            # "not newly created" — but the upsert above already ran and would
            # flip a SENT row back to PENDING with no job to drive it. The
            # mapping is therefore only written when the job row does not
            # exist yet, and only ever *promoted*: a replay that finds an
            # existing non-terminal row refreshes its bookkeeping, while a
            # terminal ( SENT / FAILED / DEAD_LETTER ) row is left alone.
            exists = conn.execute(
                "SELECT 1 FROM delivery_jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    """
                    INSERT INTO message_map (
                        id, rule_id, source_chat_id, source_message_id, target_chat_id,
                        target_message_id, media_group_id, delivery_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rule_id, source_chat_id, source_message_id, target_chat_id) DO UPDATE SET
                        target_message_id = COALESCE(excluded.target_message_id, message_map.target_message_id),
                        media_group_id = COALESCE(excluded.media_group_id, message_map.media_group_id),
                        delivery_status = CASE
                            WHEN message_map.delivery_status IN ('SENT') THEN message_map.delivery_status
                            ELSE excluded.delivery_status
                        END,
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
                    intermediate_chat_id, intermediate_message_id, delivery_stage,
                    job_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id, job.rule_id, str(job.source_chat_id),
                    int(job.source_message_id), str(job.target_chat_id),
                    payload_str, job_status, job.attempts, job.max_attempts,
                    job.next_retry_at, job.lease_until, job.worker_id or "",
                    job.error_detail or "", job.created_at, job.updated_at,
                    str(job.intermediate_chat_id or "") if job.intermediate_chat_id else None,
                    job.intermediate_message_id, job.delivery_stage or "DIRECT",
                    job_kind,
                ),
            )
            return cur.rowcount > 0
        except sqlite3.OperationalError:
            # Legacy table without the v9 intermediate/delivery_stage columns.
            # A non-forward deliverable cannot be stored there at all (the
            # dedup key has no room for it), so say so instead of reporting a
            # refusal as an ordinary replay.
            if job_kind != "forward":
                logger.warning(
                    "[DURABLE_QUEUE] delivery_jobs table cannot store a %s row "
                    "(legacy schema) — job %s not queued", job_kind, job.id,
                )
                return False
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

    async def _diagnose_failure_chat(
        self, client: Any, job: DeliveryJob, target: str,
    ) -> tuple:
        """Which chat is actually unreachable: the target, or the source?

        A delivery call reports one error for the whole operation, and
        ``CHANNEL_INVALID`` does not say which side it means. Resolving both
        chats costs one request per permanent failure (never on the hot path)
        and turns "something is broken" into the chat the owner must fix.
        """
        if client is None or not hasattr(client, "get_chat"):
            return target, "target"
        try:
            await client.get_chat(target)
        except Exception:
            return target, "target"
        try:
            await client.get_chat(job.source_chat_id)
        except Exception:
            return str(job.source_chat_id), "source"
        # Both resolve but Telegram still refuses the send (ChatWriteForbidden,
        # ChatAdminRequired): the target's permissions are the blocker.
        return target, "target"

    async def _pause_task_for_failure(
        self,
        worker_id: str,
        client: Any,
        job: DeliveryJob,
        decision: Any,
        target: str,
        err_msg: str,
    ) -> None:
        """Pause the one task that owns this chat, persist why, notify the owner."""
        if self._rules is None:
            return
        try:
            rule_obj = await self._rules.get_by_id(job.rule_id)
        except Exception as exc:
            logger.error("Failed to load rule %s for auto-pause: %s", job.rule_id[:8], exc)
            return
        if rule_obj is None:
            return
        chat_id, scope = await self._diagnose_failure_chat(client, job, target)
        code = decision.code or "CHAT_UNAVAILABLE"
        changed = False
        if hasattr(rule_obj, "mark_auto_paused"):
            changed = rule_obj.mark_auto_paused(
                code, code, chat_id=chat_id, scope=scope, detail=err_msg,
            )
        elif getattr(rule_obj, "is_active", False):
            # An older entity without the pause model: keep the task stopped
            # (the pre-existing behaviour) rather than forwarding into a void.
            rule_obj.is_active = False
            changed = True
        if not changed:
            return
        try:
            await self._rules.update(rule_obj)
        except Exception as exc:
            logger.error(
                "[PIPELINE:AUTO_PAUSE] could not persist the pause on rule %s: %s",
                job.rule_id[:8], exc,
            )
            return
        logger.warning(
            "[PIPELINE:AUTO_PAUSE] [%s] Task %s paused: %s on %s %s (owner=%s)",
            worker_id, job.rule_id[:8], code, scope, chat_id,
            getattr(rule_obj, "owner_user_id", "?"),
        )
        if self._log_client is not None:
            try:
                self._log_client.error(
                    "queue", "forward",
                    f"Task {job.rule_id[:8]} auto-paused: {code} on {scope} {chat_id} ({err_msg})",
                    {
                        "rule_id": job.rule_id,
                        "chat_id": chat_id,
                        "scope": scope,
                        "error": err_msg,
                        "event_code": "TASK_AUTO_PAUSED",
                        "owner_user_id": int(getattr(rule_obj, "owner_user_id", 0) or 0),
                    },
                )
            except Exception:
                pass
        await self._notify_owner(
            worker_id, job,
            {
                "kind": "task_paused",
                "code": code,
                "reason": code,
                "chat_id": chat_id,
                "scope": scope,
                "detail": err_msg,
                "rule": rule_obj,
            },
        )

    async def _notify_account_limited(
        self,
        worker_id: str,
        job: DeliveryJob,
        rule_obj: Any,
        session_id: str,
        seconds: int,
        code: str,
    ) -> None:
        """Tell the owner once that Telegram suspended their account.

        Keyed by the suspension deadline: the same wait is announced once, a
        *new* wait (a later error with a different deadline) is announced too.
        """
        if not session_id:
            return
        key = session_id
        deadline = time.time() + max(1, int(seconds))
        already = self._account_notices.get(key, 0.0)
        if abs(already - deadline) < 30.0:
            return
        self._account_notices[key] = deadline
        if len(self._account_notices) > 256:
            now = time.time()
            self._account_notices = {k: v for k, v in self._account_notices.items() if v > now}
        if rule_obj is None and self._rules is not None:
            try:
                rule_obj = await self._rules.get_by_id(job.rule_id)
            except Exception:
                rule_obj = None
        if rule_obj is None:
            return
        await self._notify_owner(
            worker_id, job,
            {
                "kind": "account_restricted",
                "code": code,
                "reason": "telegram_rate_limit",
                "chat_id": "",
                "scope": "account",
                "detail": f"{code} {int(seconds)}s",
                "minutes": max(1, int(round(seconds / 60.0))) if seconds >= 60 else 1,
                "rule": rule_obj,
            },
        )

    async def _notify_owner(self, worker_id: str, job: DeliveryJob, payload: Dict[str, Any]) -> None:
        """Best effort: a notification failure must never fail a delivery."""
        notifier = self._owner_notifier
        if notifier is None:
            return
        rule_obj = payload.pop("rule", None)
        owner = int(getattr(rule_obj, "owner_user_id", 0) or 0) if rule_obj is not None else 0
        if rule_obj is None and self._rules is not None:
            try:
                rule_obj = await self._rules.get_by_id(job.rule_id)
                owner = int(getattr(rule_obj, "owner_user_id", 0) or 0) if rule_obj is not None else 0
            except Exception:
                rule_obj = None
        if not owner or rule_obj is None:
            return
        try:
            result = notifier(owner, rule_obj, payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning(
                "[PIPELINE:NOTIFY] [%s] owner notification failed for rule %s: %s",
                worker_id, job.rule_id[:8], exc,
            )

    def _note_no_client(self, session_id: str) -> tuple:
        """Backoff for a session with no live client. Returns (delay, first, reminder).

        A session with no client blocks every job queued on it, so the counter
        lives per session rather than per job: the first miss is loud, repeats
        are quiet (the operator already knows), and a reminder lands every 15
        minutes so a permanently dead session is still visible in the log.
        """
        now = time.time()
        state = self._no_client_backoff.get(session_id)
        if not state:
            state = [0, 0.0, 0.0]
            self._no_client_backoff[session_id] = state
        state[0] += 1
        delay = min(300.0, 15.0 * (2 ** min(state[0] - 1, 5)))
        state[1] = now + delay
        first = state[0] == 1
        reminder = (now - float(state[2] or 0.0)) >= 900.0
        if first or reminder:
            state[2] = now
        return (delay, first, reminder)

    async def _defer_job(self, job: DeliveryJob, reason: str, backoff: float) -> None:
        """Postpone a job whose blocker is transient, without spending an attempt.

        Falling back to ``mark_retry`` keeps older queue adapters working, but it
        is logged because that fallback loses the guarantee: five transient
        deferrals would dead-letter a perfectly good message.
        """
        try:
            deferred = await self._queue.defer(job.id, reason, backoff)
        except Exception as exc:  # adapter predates defer()
            logger.debug("queue adapter has no working defer(): %s", exc)
            deferred = False
        if deferred:
            return
        logger.warning(
            "[PIPELINE:DEFER:UNSUPPORTED] queue adapter cannot postpone %s without "
            "spending an attempt; falling back to mark_retry (%s)",
            job.id, reason,
        )
        await self._queue.mark_retry(job.id, reason, backoff_seconds=backoff)

    async def _record_album_progress(self, job_id: str, sent_map: Dict[str, int]) -> None:
        """Persist a split album's per-item confirmations into the job payload.

        Audit finding E2E-5: a retry used to re-send every item, duplicating
        the ones Telegram had already accepted. The payload gains an
        ``album_sent`` map (source id -> target id); the split loop reads it to
        resume from the first unconfirmed item.

        The merge is *additive and version-less by construction*: only the
        entries the caller actually confirmed are written, and a row that is no
        longer a delivery job (sent, cleaned up) is left alone — updating a
        terminal row would resurrect its payload for nothing.
        """
        if not sent_map:
            return
        try:
            db = getattr(self._queue, "_db", None)
            if db is None:
                return
            row = None
            if hasattr(db, "query_one"):  # SQLite: sync connection
                row = db.query_one(
                    "SELECT payload_json FROM delivery_jobs WHERE id = ?", (job_id,)
                )
                if row is None:
                    return
                payload = row["payload_json"] if isinstance(row, dict) else row[0]
                data = _dumps_decode(payload)
                existing = dict(data.get("album_sent") or {})
                existing.update({str(k): int(v) for k, v in sent_map.items()})
                data["album_sent"] = existing
                db.execute(
                    "UPDATE delivery_jobs SET payload_json = ? WHERE id = ? AND status NOT IN ('SENT')",
                    (_dumps(data), job_id),
                )
            else:  # PostgreSQL: async pool — record via the repository's port
                await self._queue.record_album_progress(job_id, sent_map)
        except Exception as exc:
            # Progress bookkeeping must never fail a delivery that is otherwise
            # fine; the cost of missing it is a duplicated item on retry, not a
            # lost message — and the log says so.
            logger.warning(
                "[PIPELINE:ALBUM_SPLIT] could not record per-item progress for job %s: %s",
                job_id, exc,
            )

    def _attempt_key(self, job: DeliveryJob) -> Optional[DeliveryAttemptKey]:
        """The deterministic fence key for one job, or ``None`` when unknown.

        Derived purely from identifiers the job already carries, so the worker,
        the legacy direct path and a replay all generate the *same* key without
        needing to read anything else. ``None`` (a job whose payload lacks the
        routing fields) means the fence cannot be keyed and is skipped for that
        job rather than blocking delivery on a shape it cannot express.
        """
        try:
            return DeliveryAttemptKey(
                rule_id=str(job.rule_id),
                source_chat_id=str(job.source_chat_id),
                source_message_id=int(job.source_message_id),
                target_chat_id=str(job.target_chat_id),
                job_kind=str(getattr(job, "job_kind", "forward") or "forward"),
            )
        except (TypeError, ValueError):
            return None

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
            await self._defer_job(job, "Target rate-limited", rate_wait)
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
                            await self._defer_job(job, f"Account time-limited ({rem:.1f}s)", max(2.0, rem))
                            return None
                except Exception as exc:
                    logger.debug("Failed checking time limit for %s: %s", session_id, exc)

        # Check if rule is still active or paused
        rule_obj = None
        if self._rules is not None:
            rule_obj = await self._rules.get_by_id(job.rule_id)
            if rule_obj is None:
                # The task was deleted while this job was queued. Delivering it
                # would forward a message the owner has already removed, and
                # `update`'s being a no-op for a missing row is exactly how a
                # deleted task used to keep forwarding.
                logger.info(
                    "[PIPELINE:DROP] [%s] Rule %s no longer exists. Dropping job %s",
                    worker_id, job.rule_id[:8], job.id,
                )
                await self._queue.mark_failed(job.id, "Rule no longer exists")
                return None
            if not rule_obj.is_active:
                logger.info("[PIPELINE:DROP] [%s] Rule %s is disabled. Dropping job %s", worker_id, job.rule_id[:8], job.id)
                await self._queue.mark_failed(job.id, "Rule disabled")
                return None
            auto_pause = getattr(rule_obj, "auto_pause", None) or {}
            if isinstance(auto_pause, dict) and auto_pause.get("reason"):
                # The engine paused this task for a dead source/target. The
                # message cannot be delivered until the owner fixes the chat,
                # so failing it with the reason beats deferring it forever.
                reason = str(auto_pause.get("reason"))
                logger.info(
                    "[PIPELINE:DROP] [%s] Rule %s is auto-paused (%s). Dropping job %s",
                    worker_id, job.rule_id[:8], reason, job.id,
                )
                await self._queue.mark_failed(job.id, f"Task paused: {reason}")
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
                await self._defer_job(job, f"Rule paused ({backoff:.1f}s)", backoff)
                return None

        transport_wanted = str(job.payload_data.get("delivery_transport") or "USER").strip().upper()
        route = None
        if transport_wanted == "BOT":
            route = await self._bot_route(
                job,
                owner_user_id=int(getattr(rule_obj, "owner_user_id", 0) or 0) or None,
            )
            if route is None:
                _bump_target("retried")
                logger.error(
                    "[BOT ROUTE] [%s] job %s asks for the bot route but token %s is unusable "
                    "for this task; not falling back silently — fix the token or switch the "
                    "task back to the account route",
                    worker_id,
                    job.id,
                    str(job.payload_data.get("bot_token_id") or "")[:8] or "(none)",
                )
                await self._defer_job(job, "bot route: attached token unusable", 15.0)
                return None

        client = route or (self._pool.get(session_id) if self._pool is not None else None)
        if client is None:
            _bump_target("retried")
            backoff, first, reminder = self._note_no_client(str(session_id or ""))
            if first or reminder:
                logger.error(
                    "[PIPELINE:NO_CLIENT] [%s] Cannot deliver job %s: No live client for session %s "
                    "(rule=%s, tgt=%s); retrying in %.0fs",
                    worker_id, job.id, session_id, job.rule_id[:8], target, backoff,
                )
            else:
                logger.debug(
                    "[PIPELINE:NO_CLIENT] [%s] job %s deferred again for session %s (%.0fs)",
                    worker_id, job.id, session_id, backoff,
                )
            if self._log_client is not None and (first or reminder):
                try:
                    self._log_client.error(
                        "queue", "forward",
                        f"No live client for session {session_id} (rule={job.rule_id[:8]}, target={target})",
                        {"job_id": job.id, "rule_id": job.rule_id, "target": target},
                    )
                except Exception:
                    pass
            # A session that is offline is not a failed delivery. Deferring keeps
            # the message for when the account comes back, instead of
            # dead-lettering it after five one-second polls. The backoff grows
            # with consecutive misses so a session that is gone (rather than
            # momentarily reconnecting) stops costing an attempt every 15s.
            await self._defer_job(job, f"No live client for session {session_id}", backoff)
            return None

        # Attempt delivery to Telegram
        if session_id and self._no_client_backoff.pop(str(session_id), None) is not None:
            logger.info(
                "[PIPELINE:NO_CLIENT] session %s is live again; retry backoff cleared",
                str(session_id)[:8],
            )

        # --- Send-side idempotency fence (audit finding #5) ----------------- #
        # Telegram has no idempotency key for ordinary sends, so the only way to
        # stop ourselves double-dispatching one logical delivery (a lease
        # expiring mid-dispatch, a crash between Telegram accepting the send and
        # the batch commit, the legacy and worker paths racing) is to claim the
        # logical send here, atomically, *before* any Telegram call. The fence
        # is keyed on the deterministic business key, so the legacy direct path
        # and a replay resolve to the same row.
        fence = self._attempt_key(job)
        if fence is not None and self._attempts is not None:
            try:
                outcome, row = await self._attempts.claim_or_check(
                    fence, worker_id, time.time() + max(self._lease_duration * 2, 60.0)
                )
            except Exception as exc:
                # A fence-store hiccup must never silently disable the fence:
                # the send is refused loudly rather than dispatched unprotected.
                logger.error(
                    "[PIPELINE:DELIVERY_FENCE] [%s] idempotency fence unavailable for job %s "
                    "(rule=%s src=%s:%s tgt=%s): %s — refusing to dispatch unprotected",
                    worker_id, job.id, job.rule_id[:8], job.source_chat_id,
                    job.source_message_id, target, exc,
                    extra={"event_code": "DELIVERY_FENCE_UNAVAILABLE"},
                )
                _bump_target("retried")
                await self._defer_job(job, "idempotency fence unavailable", 5.0)
                return None
            if outcome == "already_sent":
                # A previous attempt recorded a confirmed success. Telegram has
                # the copy; re-dispatching would duplicate it. Close the job as
                # SENT with the recorded target id rather than re-sending.
                prior_id = (row or {}).get("target_message_id") or 0
                logger.warning(
                    "[PIPELINE:DELIVERY_FENCE] [%s] job %s already has a confirmed send "
                    "(target_message_id=%s) — marking SENT without re-dispatching",
                    worker_id, job.id, prior_id,
                    extra={"event_code": "DELIVERY_FENCE_ALREADY_SENT"},
                )
                _bump_target("sent")
                await self._note_mapping(job, DeliveryStatus.SENT, prior_id or None)
                await self._queue.mark_sent(job.id, prior_id or None)
                return None
            if outcome in ("in_flight", "stale", "unknown"):
                # Another worker holds a live claim, or an earlier send's
                # outcome was never recorded. Re-dispatching here is exactly
                # the duplicate the fence exists to prevent, so defer the job
                # for the watchdog/reconciliation to resolve.
                logger.warning(
                    "[PIPELINE:DELIVERY_FENCE] [%s] job %s not dispatchable now (outcome=%s, "
                    "rule=%s src=%s:%s tgt=%s) — deferring for reconciliation",
                    worker_id, job.id, outcome, job.rule_id[:8], job.source_chat_id,
                    job.source_message_id, target,
                    extra={"event_code": "DELIVERY_FENCE_BLOCKED"},
                )
                _bump_target("retried")
                await self._defer_job(job, f"delivery fence: {outcome}", 15.0)
                return None

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
            await self._note_mapping(job, DeliveryStatus.SENT, target_msg_id)
            self.stats["sent"] += 1
            # Record the confirmed outcome in the idempotency fence BEFORE the
            # deferred batch commit: if the process dies between the Telegram
            # response and mark_sent_batch, the fence is what stops the
            # watchdog's reclaim from re-dispatching (audit finding #5).
            if fence is not None and self._attempts is not None:
                try:
                    await self._attempts.record_sent(fence, worker_id, int(target_msg_id or 0))
                except Exception as exc:
                    logger.warning(
                        "[PIPELINE:DELIVERY_FENCE] [%s] could not record the confirmed send "
                        "for job %s (a duplicate on recovery is now possible): %s",
                        worker_id, job.id, exc,
                    )
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
                        {
                            "msg_id": target_msg_id,
                            "ms": round(send_duration * 1000),
                            "owner_user_id": int(getattr(rule_obj, "owner_user_id", 0) or 0)
                            or int((job.payload_data or {}).get("owner_user_id") or 0),
                        },
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
            key_, sev, recoverable = tg_error(exc)
            err_msg = tg_detail(exc)
            # Log attribution: the owner this delivery belongs to, resolved
            # from the loaded rule with the enqueue-time snapshot as fallback.
            owner_id = int(getattr(rule_obj, "owner_user_id", 0) or 0) or int(
                (job.payload_data or {}).get("owner_user_id") or 0
            )
            # Failure intelligence: the strategy in force when the call failed,
            # resolved from the snapshotted payload — the diagnostic must name
            # what was actually being attempted, never the live rule.
            send_strategy = strategy_of(
                str((job.payload_data or {}).get("forward_mode", "COPY_MESSAGE")),
                str((job.payload_data or {}).get("content_mode", "AUTO")),
                bool((job.payload_data or {}).get("has_media", False)),
            )

            # The Telegram call failed *before* any ambiguity: no copy is in
            # the target, so the fence is released for a retry. (Timeouts are
            # handled below as UNKNOWN, where the ambiguity is real.)
            if fence is not None and self._attempts is not None and not is_timeout_error(exc):
                try:
                    await self._attempts.record_outcome(fence, worker_id, "FAILED", err_msg or str(exc))
                except Exception:
                    logger.debug("delivery fence failed to record failure for %s", job.id)

            # 0. Everything was tried and *nothing* was sent. Reporting this as
            # SENT (the old behavior: the bypass returned 0) manufactured a
            # success the owner would never see through. The job is FAILED with
            # the full reason, the task stays active, and the message waits in
            # the dead-letter queue for a replay once the cause is fixed.
            if isinstance(exc, DeliveryBlockedError):
                self.stats["failed"] += 1
                _bump_target("failed")
                logger.error(
                    "[PIPELINE:DELIVERY_BLOCKED] [%s] No permitted delivery method succeeded for %s "
                    "(rule=%s, src=%s:%s): %s. Marking job FAILED (task stays active; message is replayable).",
                    worker_id, target, job.rule_id[:8], job.source_chat_id,
                    job.source_message_id, err_msg or exc,
                    extra={"event_code": "DELIVERY_BLOCKED"},
                )
                if self._log_client is not None:
                    try:
                        self._log_client.error(
                            "queue", "forward",
                            f"delivery blocked rule={job.rule_id[:8]} → {target}: {err_msg or exc}",
                            {
                                "rule_id": job.rule_id,
                                "source_chat_id": job.source_chat_id,
                                "source_message_id": job.source_message_id,
                                "target": target,
                                "event_code": "DELIVERY_BLOCKED",
                                "owner_user_id": owner_id,
                            },
                        )
                    except Exception:
                        pass
                await self._queue.mark_failed(job.id, err_msg or str(exc))
                await self._note_mapping(job, DeliveryStatus.FAILED)
                return None

            # 1. Ambiguous timeout / connection drop: evaluated FIRST to prevent blind duplicates
            is_timeout = is_timeout_error(exc)
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
                            {"rule_id": job.rule_id, "target": target, "src": f"{job.source_chat_id}:{job.source_message_id}", "event_code": "DELIVERY_TIMEOUT", "owner_user_id": owner_id},
                        )
                    except Exception:
                        pass
                await self._queue.mark_unknown(job.id, f"Telegram timeout: {err_msg}")
                await self._note_mapping(job, DeliveryStatus.UNKNOWN)
                # The fence mirrors the ambiguity: the send may be sitting in
                # the target right now, so the outcome is UNKNOWN (never FAILED
                # — that would invite a blind re-dispatch) and only an explicit
                # reconciliation may reopen it.
                if fence is not None and self._attempts is not None:
                    try:
                        await self._attempts.record_outcome(fence, worker_id, "UNKNOWN", f"timeout: {err_msg}")
                    except Exception:
                        logger.debug("delivery fence failed to record unknown outcome for %s", job.id)
                # Failure intelligence: an UNKNOWN outcome is the ambiguity the
                # mission says must stay *visibly* unresolved — never converted
                # into a success or a plain failure.
                await self._record_diagnostic(self._diagnostic_for_send_failure(
                    job=job, exc=exc, err_msg=err_msg, owner_id=owner_id, worker_id=worker_id,
                    strategy=send_strategy, category=C_DELIVERY,
                    reason=R_DELIVERY_OUTCOME_UNKNOWN,
                    event_name="delivery.outcome.unknown",
                ))
                return None

            # 2. FloodWait / recoverable retry. Only a *real* rate-limit error
            # suspends the account, for Telegram's own duration; every other
            # recoverable error is an ordinary retry. The old branch suspended
            # the account on any "recoverable" error and used the jittered
            # backoff as the duration, which both invented a limit and made a
            # network blip stop every task on that account.
            limit_seconds = rate_limit_seconds(exc)
            if "FloodWait" in str(type(exc).__name__) or recoverable:
                backoff = wait_seconds(exc, default=5) + random.uniform(0.5, 2.0)
                if limit_seconds:
                    wait_sec = float(limit_seconds) + random.uniform(0.5, 2.0)
                    if self._pool is not None and hasattr(self._pool, "set_time_limit") and session_id:
                        self._pool.set_time_limit(
                            session_id, float(limit_seconds),
                            code=error_code(exc) or "FLOOD_WAIT", reason="telegram_rate_limit",
                        )
                    await self._notify_account_limited(
                        worker_id, job, rule_obj, session_id, limit_seconds, error_code(exc) or "FLOOD_WAIT",
                    )
                else:
                    wait_sec = float(backoff)
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
                            {"rule_id": job.rule_id, "target": target, "wait_seconds": round(wait_sec, 1), "event_code": "RATE_LIMITED", "owner_user_id": owner_id},
                        )
                    except Exception:
                        pass
                await self._queue.mark_retry(job.id, err_msg, backoff_seconds=wait_sec)
                await self._note_mapping(job, DeliveryStatus.RETRY_WAIT)
                # Failure intelligence: a FloodWait is transient and the reason
                # must say so, with Telegram's own wait duration preserved.
                await self._record_diagnostic(self._diagnostic_for_send_failure(
                    job=job, exc=exc, err_msg=err_msg, owner_id=owner_id, worker_id=worker_id,
                    strategy=send_strategy, category=C_TELEGRAM_API,
                    reason=R_TELEGRAM_FLOOD_WAIT, event_name="delivery.flood_wait.retrying",
                ))
                return None

            # 3. Classify: a chat that is permanently gone, an account that
            # cannot send at all, or something worth retrying. One decision
            # (`failure_policy`) reads the normalized error model; the old
            # inline list also treated every CRITICAL error as permanent, which
            # paused tasks for network blips that fixed themselves.
            exc_name = str(type(exc).__name__)
            decision = classify_failure(exc)
            if decision.kind == "permanent":
                self.stats["failed"] += 1
                _bump_target("failed")
                logger.error(
                    "[PIPELINE:DELIVERY_FAIL] [%s] Permanent failure delivering to %s (rule=%s, src=%s:%s, code=%s): %s. Marking job FAILED.",
                    worker_id,
                    target,
                    job.rule_id[:8],
                    job.source_chat_id,
                    job.source_message_id,
                    decision.code or "?",
                    err_msg,
                )
                # Pause only the task that owns this chat: other tasks are
                # unaffected, and the owner gets a reason plus a way back.
                await self._pause_task_for_failure(
                    worker_id, client, job, decision, str(target), err_msg,
                )
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
                                "event_code": "DELIVERY_FAILED",
                                "owner_user_id": owner_id,
                            },
                        )
                    except Exception:
                        pass
                await self._queue.mark_failed(job.id, err_msg)
                await self._note_mapping(job, DeliveryStatus.FAILED)
                # Failure intelligence: a permanent failure with the raw Telegram
                # error preserved and the retryable/fallback flags the policy
                # actually decided — the operator sees the same evidence the
                # engine acted on.
                await self._record_diagnostic(self._diagnostic_for_send_failure(
                    job=job, exc=exc, err_msg=err_msg, owner_id=owner_id, worker_id=worker_id,
                    strategy=send_strategy, category=C_TELEGRAM_API,
                    reason="TELEGRAM_" + (decision.code or "UNKNOWN_RPC_ERROR"),
                    event_name="delivery.telegram_api.failed",
                ))
            elif decision.kind == "account":
                # The session itself cannot send (revoked, unregistered,
                # banned). Nothing about this task is wrong and a re-login
                # fixes it, so keep the message and tell the owner once instead
                # of dead-lettering a queue's worth of work.
                self.stats["retried"] += 1
                _bump_target("retried")
                logger.error(
                    "[PIPELINE:ACCOUNT_BLOCKED] [%s] Session %s cannot send (code=%s): %s. Deferring job %s.",
                    worker_id, str(session_id or "?")[:8], decision.code or "?", err_msg, job.id,
                    extra={"event_code": "ACCOUNT_BLOCKED", "user_id": owner_id},
                )
                await self._notify_owner(
                    worker_id, job,
                    {
                        "kind": "account_blocked",
                        "code": decision.code or "ACCOUNT",
                        "reason": decision.code or "ACCOUNT",
                        "chat_id": "",
                        "scope": "account",
                        "detail": err_msg,
                    },
                )
                await self._defer_job(job, f"account: {decision.code or 'blocked'}", 60.0)
                await self._note_mapping(job, DeliveryStatus.RETRY_WAIT)
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
                                "event_code": "DELIVERY_RETRY",
                                "owner_user_id": owner_id,
                            },
                        )
                    except Exception:
                        pass
                await self._queue.mark_retry(job.id, err_msg, backoff_seconds=backoff)
                await self._note_mapping(job, DeliveryStatus.RETRY_WAIT)
            return None

    async def _reupload_media(
        self,
        client: Any,
        src_chat: Any,
        src_msg_id: int,
        target: Any,
        caption: Optional[str],
        media_type: str = "",
        job: Any = None,
    ) -> int:
        """Rebuild and send one protected message.

        Bypass layers, in order of fidelity (mirrors the live dispatcher):
          1. server-side copy (already failed before reaching here),
          2. media-reference send via the file-id — same quality, no disk I/O,
             and it works even when downloading is blocked,
          3. download + re-upload the original media file,
          4. the text body/caption as the last resort so the words are never
             silently dropped.

        If *nothing* can be delivered there is no text to fall back on and
        ``MEDIA_ONLY`` forbids text anyway — the caller then gets
        :class:`DeliveryBlockedError`, never a fake ``0`` success.
        """
        temp_path = None
        # Correlation for the media events: the job is optional only for older
        # call sites, never for the worker path that always has one.
        d = job.payload_data if job is not None else {}
        ctx = dict(
            task_id=str(job.rule_id if job is not None else ""),
            trace_id=str(d.get("correlation_id") or ""),
            correlation_id=str(d.get("correlation_id") or ""),
            owner_id=int(d.get("owner_user_id") or 0),
            job_id=str(job.id if job is not None else ""),
            source_peer_id=str(src_chat), source_message_id=int(src_msg_id),
            target_peer_id=str(target),
        )
        try:
            src_msg = None
            if hasattr(client, "get_messages"):
                try:
                    src_msg = await client.get_messages(chat_id=src_chat, message_ids=src_msg_id)
                except Exception as e:
                    logger.warning(
                        "[PIPELINE:PROTECTED] get_messages failed while preparing re-upload src=%s msg=%s (%s)",
                        src_chat, src_msg_id, tg_detail(e) or type(e).__name__,
                    )
                    await self._record_diagnostic(build_diagnostic_event(
                        event_name="delivery.media.fetch_failed", stage="MEDIA",
                        category=C_MEDIA, reason="MEDIA_FETCH_FAILED",
                        error_origin=ORIGIN_TELEGRAM, raw_exception=e,
                        explanation=f"get_messages failed for {src_chat}:{src_msg_id}",
                        **ctx,
                    ))

            # Layer 2: media-reference (file-id) send. The worker path lost
            # this layer for months and degraded protected media to a download
            # that could fail; the dispatcher kept it, so the two paths
            # disagreed about what a protected channel could deliver.
            if src_msg is not None:
                ref = extract_media_reference(src_msg)
                if ref is not None:
                    try:
                        sent = await send_media_reference(client, target, ref, caption)
                        if sent is not None:
                            return getattr(sent, "id", 0)
                    except Exception as ref_err:
                        logger.debug("media-reference send failed (%s) — falling back to download", ref_err)

            # Layer 3: download the original file and re-upload it.
            if src_msg is not None and hasattr(client, "download_media"):
                try:
                    temp_path = await client.download_media(src_msg)
                except Exception as dl_err:
                    logger.warning(
                        "[PIPELINE:PROTECTED] download failed src=%s msg=%s (%s) — degrading to text if nothing else delivers",
                        src_chat, src_msg_id, tg_detail(dl_err) or type(dl_err).__name__,
                    )
                    await self._record_diagnostic(build_diagnostic_event(
                        event_name="delivery.media.download.failed", stage="MEDIA",
                        category=C_MEDIA, reason="MEDIA_DOWNLOAD_FAILED",
                        error_origin=ORIGIN_TELEGRAM, raw_exception=dl_err,
                        explanation=f"media download failed for {src_chat}:{src_msg_id}",
                        **ctx,
                    ))

            if temp_path and os.path.exists(temp_path):
                await self._record_diagnostic(build_diagnostic_event(
                    event_name="delivery.media.upload.started", stage="MEDIA",
                    category=C_MEDIA, reason="MEDIA_UPLOAD_STARTED",
                    severity="INFO",
                    explanation=f"re-uploading protected media for {src_chat}:{src_msg_id}",
                    **ctx,
                ))
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

            # Layer 4: send the text body so the words survive even when the
            # media cannot be re-uploaded.
            if caption:
                res = await client.send_message(chat_id=target, text=caption)
                return getattr(res, "id", 0)

            # Nothing was delivered. Returning 0 here is how a protected
            # message used to vanish while the task reported a success.
            raise DeliveryBlockedError(
                f"protected media from {src_chat}:{src_msg_id} could not be copied, "
                "referenced, re-uploaded, or sent as text (no text available)"
            )
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

            # Every item failed to download and there is no text to carry:
            # an honest failure, not a phantom success.
            raise DeliveryBlockedError(
                f"protected album from {src_chat} {album_ids} could not be re-uploaded "
                "and carries no text to fall back on"
            )
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

    async def _bot_route(self, job: DeliveryJob, owner_user_id: Optional[int] = None) -> Optional[Any]:
        """A per-job delivery view onto the bot the task attached.

        Returns ``None`` when the token is missing, inactive, unusable — or
        **not owned by this task's owner**. The caller then retries the job (it
        must not quietly post as the owner, which would leak the sender identity
        the owner deliberately changed).

        ``owner_user_id`` comes from the stored rule, never from the job
        payload: the payload is what we are checking.
        """
        if self._bot_route_cache is None:
            logger.warning("[BOT ROUTE] job %s asks for the bot route but no token repository is wired here", job.id)
            return None

        token_id = str(job.payload_data.get("bot_token_id") or "")
        if not owner_user_id:
            logger.warning(
                "[BOT ROUTE] job %s has no task owner to check the attached token against "
                "(rule %s not found) — refusing the bot route rather than guessing",
                job.id,
                job.rule_id[:8],
            )
            return None
        transport = await self._bot_route_cache.get(token_id, owner_user_id)
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

    def _note_fallback(self, job_id: str, what: str, owner: int = 0) -> None:
        """Count and log a delivery that needed the copy/re-upload bypass."""
        self.stats["fallback_copies"] += 1
        logger.info(
            "[PIPELINE:DELIVERY_MODE] job %s delivered via %s (native forward unavailable)",
            job_id, what,
        )
        if self._log_client is not None:
            try:
                self._log_client.info(
                    "queue", "forward",
                    f"job {job_id} delivered via {what} (native forward unavailable)",
                    {
                        "job_id": job_id,
                        "delivery_method": what,
                        "event_code": "FALLBACK_COPY_USED",
                        "owner_user_id": int(owner or 0),
                    },
                )
            except Exception:
                pass

    async def _on_restricted(self, client: Any, err: BaseException, fallback_enabled: bool, context: str, job: Any = None) -> bool:
        """Decide what a source-restriction error means for this delivery.

        Returns ``True`` when the caller must descend the copy/re-upload
        bypass. Raises otherwise:

        * fallback disabled → the original error. The owner asked for the
          requested mode only; silently copying would betray the setting.
        * bot route (a client that can neither fetch nor download the source
          message) → ``BotTransportUnsupported`` so ``_process_job`` retries
          the job through the account transport, whose bypass chain can
          actually re-upload protected media. The old behavior degraded the
          message to bare text — or returned 0 and reported a success that
          never happened.
        """
        if not fallback_enabled:
            raise err
        if not (hasattr(client, "get_messages") or hasattr(client, "download_media")):
            raise BotTransportUnsupported(
                f"source forbids copies/forwards ({type(err).__name__}) and the bot route "
                "cannot re-upload media — retrying through the account transport"
            )
        logger.warning(
            "[PIPELINE:PROTECTED] %s blocked by source content protection (%s) — descending bypass chain",
            context, tg_detail(err) or type(err).__name__,
        )
        # §6.3: every strategy transition produces an event explaining why the
        # requested strategy was left and what will be attempted instead.
        await self._record_diagnostic(build_diagnostic_event(
            event_name="delivery.fallback.selected", stage="FALLBACK",
            task_id=str(job.rule_id if job is not None else ""),
            trace_id=str((job.payload_data or {}).get("correlation_id", "") or "")
            if job is not None else "",
            correlation_id=str((job.payload_data or {}).get("correlation_id", "") or "")
            if job is not None else "",
            owner_id=int((job.payload_data or {}).get("owner_user_id") or 0)
            if job is not None else 0,
            job_id=str(job.id if job is not None else ""),
            source_peer_id=str(job.source_chat_id if job is not None else ""),
            source_message_id=int(job.source_message_id if job is not None else 0),
            target_peer_id=str(job.target_chat_id if job is not None else ""),
            severity="WARN", category=C_DELIVERY, reason="FORWARD_RESTRICTED",
            error_origin=ORIGIN_TELEGRAM, raw_exception=err,
            explanation=f"{context}: source restricts forwarding, descending bypass chain",
        ))
        return True

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
        # The rule's fallback opt-out, snapshotted at enqueue time (see
        # enqueue_delivery). Missing key → older job → default on.
        fallback_enabled = bool(data.get("fallback_enabled", True))
        # A bot-route adapter has no forward_messages/get_messages/download_media;
        # native forwarding and the media bypass belong to the account transport.
        can_native_forward = hasattr(client, "forward_messages")

        # Phase 2: a keyword-reply job is a pure-text follow-up. Force text-only
        # dispatch regardless of the source message's media so the reply never
        # re-copies or re-forwards the original attachment.
        if bool(data.get("is_reply", False)):
            forward_mode = "COPY_MESSAGE"
            content_mode = "TEXT_ONLY"
            is_album = False
            has_media = False

        # --- Phase 2: advanced task-overview settings execute here -------------
        # The delay is enforced by this job's claim gate (`next_retry_at`, written
        # from `AdvancedSettingsEngine.get_dispatch_delay` at enqueue time): the job
        # was not even claimable until the delay elapsed. Sleeping again here would
        # double the configured wait and burn a worker slot for up to an hour, and
        # the sleep would be lost on restart while the gate is not. Logged only.
        adv_delay = data.get("advanced_delay_seconds")
        delay = float(adv_delay) if adv_delay is not None else float(data.get("delay_seconds", 0.0) or 0.0)
        if delay > 0:
            logger.debug(
                "[DURABLE_QUEUE] dispatch delay %.1fs already elapsed for job %s -> %s",
                delay, job.id, target,
            )

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
            if bool(branding.get("force_forward", True)) and can_native_forward:
                try:
                    res = await client.forward_messages(
                        chat_id=target, from_chat_id=c_chat,
                        message_ids=c_ids, **thread_kwargs,
                    )
                    if isinstance(res, list) and res:
                        return getattr(res[0], "id", 0)
                    return getattr(res, "id", 0)
                except Exception as hop_err:
                    if is_restricted_source_error(hop_err):
                        if await self._on_restricted(client, hop_err, fallback_enabled, "forward from C"):
                            self._note_fallback(job.id, owner=int(data.get("owner_user_id") or 0), what="copy of C")
                    else:
                        raise
            elif bool(branding.get("force_forward", True)):
                # Bot route: no forward_messages to call. A clean copy of C's
                # own post still names C as the author, which is the whole
                # point of the hop — deliver that instead of crashing through
                # an AttributeError retry loop.
                logger.info(
                    "[PIPELINE:DELIVERY_MODE] job %s: bot route has no native forward — "
                    "delivering the hop as a clean copy of C",
                    job.id,
                )
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
                if is_restricted_source_error(copy_err) and await self._on_restricted(
                    client, copy_err, fallback_enabled, "copy from C",
                ):
                    self._note_fallback(job.id, owner=int(data.get("owner_user_id") or 0), what="re-upload of C")
                    return await self._reupload_media(
                        client, c_chat, c_ids[0], target,
                        None if content_mode == "MEDIA_ONLY" else caption,
                        data.get("media_type", ""),
                    )
                raise

        # 1. DIRECT_FORWARD
        if forward_mode == ForwardMode.DIRECT_FORWARD.value:
            if not can_native_forward:
                # The bot route cannot express a native forward at all. Fall
                # through to the copy path rather than raising AttributeError
                # (which the classifier read as an unknown transient error and
                # retried until the job dead-lettered).
                logger.info(
                    "[PIPELINE:DELIVERY_MODE] job %s: bot route has no native forward — delivering as a copy",
                    job.id,
                )
            else:
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
                    if is_restricted_source_error(fwd_err):
                        if await self._on_restricted(client, fwd_err, fallback_enabled, "direct forward"):
                            self._note_fallback(job.id, owner=int(data.get("owner_user_id") or 0), what="copy/re-upload bypass")
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
                    if is_restricted_source_error(one_err) and await self._on_restricted(
                        client, one_err, fallback_enabled, "first-album-item copy",
                    ):
                        self._note_fallback(job.id, owner=int(data.get("owner_user_id") or 0), what="first-item re-upload")
                        return await self._reupload_media(
                            client, src_chat, int(album_ids[0]), target, caption,
                            data.get("media_type", ""),
                        )
                    logger.info("first-album-item copy failed (%s), falling back to the group", one_err)
            elif album_mode == "split":
                # Per-item delivery with resume (audit finding E2E-5): the job
                # payload records which item ids were already confirmed in the
                # target, so a retry re-sends only the missing ones instead of
                # duplicating every item that already landed. Without this, one
                # failed item turned a retry into a wall of duplicates.
                sent_map = dict(data.get("album_sent") or {})
                remaining = [i for i in album_ids if str(i) not in sent_map]
                if remaining and sent_map:
                    logger.warning(
                        "[PIPELINE:ALBUM_SPLIT] job %s resuming split delivery: %d of %d item(s) "
                        "already confirmed, re-sending %d (%s)",
                        job.id, len(album_ids) - len(remaining), len(album_ids),
                        len(remaining), ",".join(str(i) for i in remaining),
                    )
                last_id = 0
                for item_id in remaining:
                    try:
                        res = await client.copy_message(
                            chat_id=target,
                            from_chat_id=src_chat,
                            message_id=int(item_id),
                            caption=caption,
                            **thread_kwargs,
                        )
                    except Exception as item_err:
                        # A protected source refuses every per-item copy; each
                        # item must go through the bypass on its own or the
                        # whole album dies with one refusal (the old behavior
                        # retried the job five times, then dead-lettered it).
                        if is_restricted_source_error(item_err) and await self._on_restricted(
                            client, item_err, fallback_enabled, "split-album copy",
                        ):
                            self._note_fallback(job.id, owner=int(data.get("owner_user_id") or 0), what="split-item re-upload")
                            res = await self._reupload_media(
                                client, src_chat, int(item_id), target, caption,
                                data.get("media_type", ""),
                            )
                        else:
                            # Record what got through before raising, so the
                            # retry resumes rather than duplicates: the items
                            # before this one are physically in the target now.
                            await self._record_album_progress(job.id, sent_map)
                            raise
                    # copy_message returns a Message; the bypass returns the
                    # sent id directly. Both carry the id the mapping needs.
                    sent_id = res if isinstance(res, int) else getattr(res, "id", 0)
                    last_id = sent_id or last_id
                    if sent_id:
                        sent_map[str(item_id)] = int(sent_id)
                # Persist the per-item confirmations for any later retry.
                await self._record_album_progress(job.id, sent_map)
                # The mapping's own target id is the *first* item of the group:
                # edit/delete sync keys on it and the whole group shares it.
                return sent_map.get(str(album_ids[0])) or last_id
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
                if is_restricted_source_error(grp_err) and await self._on_restricted(
                    client, grp_err, fallback_enabled, "album group copy", job=job,
                ):
                    self._note_fallback(job.id, owner=int(data.get("owner_user_id") or 0), what="album re-upload")
                else:
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
                    if is_restricted_source_error(c_err) and await self._on_restricted(
                        client, c_err, fallback_enabled, "MEDIA_ONLY copy", job=job,
                    ):
                        self._note_fallback(job.id, owner=int(data.get("owner_user_id") or 0), what="media re-upload")
                        return await self._reupload_media(client, src_chat, src_msg_id, target, None, data.get("media_type", ""), job=job)
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
                # Failure intelligence: capture the raw Telegram error *here*,
                # at the exact call that raised — before the fallback even
                # decides what to do. This is the only point that sees the
                # strategy actually attempted, so it is the only point that can
                # record "Telegram said X while we were doing Y".
                await self._record_send_diagnostic(
                    job=job, exc=c_err, strategy=S_COPY, context="copy_message",
                )
                if is_restricted_source_error(c_err) and await self._on_restricted(
                    client, c_err, fallback_enabled, "copy_message", job=job,
                ):
                    self._note_fallback(job.id, owner=int(data.get("owner_user_id") or 0), what="media re-upload")
                    return await self._reupload_media(client, src_chat, src_msg_id, target, caption, data.get("media_type", ""), job=job)
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

    async def _holds_singleton(self, name: str) -> bool:
        """True when this replica may run a singleton maintenance action.

        Without a coordinator this is always True, which is the behaviour every
        single-replica deployment already had. With one, "another replica is
        doing it" is a normal answer and the caller simply skips this round.
        """
        if self._lease is None:
            return True
        try:
            return bool(
                await self._lease.try_hold(
                    name, ttl=max(60.0, float(self._lease_duration) * 2)
                )
            )
        except Exception as exc:  # noqa: BLE001 - coordination must never break the loop
            logger.warning("lease %s unavailable (%s): running the maintenance action", name, exc)
            return True

    async def _watchdog_loop(self) -> None:
        """Periodically recovers expired leases and cleans up message maps."""
        cleanup_counter = 0
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(10.0)
                if not await self._holds_singleton("queue-watchdog"):
                    # Another replica is the watchdog this round. The work is
                    # not lost: it is skipped here and picked up by the holder.
                    continue
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
                        # Duplicate Filters keeps its own memory; it expires on
                        # the same 30-day window as the message map, otherwise an
                        # identical post months later would still be dropped as a
                        # "duplicate" by a rule that never repeats anything.
                        purged = await self._map.purge_content_hashes_all(
                            int(time.time()) - 60 * 60 * 24 * 30
                        )
                        if purged:
                            logger.info(
                                "Content-dedup retention purged %d fingerprint(s).", purged
                            )
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

