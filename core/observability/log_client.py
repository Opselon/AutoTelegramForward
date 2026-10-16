"""LogClient — async gRPC client the other services use to push logs to the
Logger microservice. Non-blocking, fire-and-forget, never raises into the
caller's business logic.

Delivery contract (what this client's numbers mean)
--------------------------------------------------
* **accepted** — admitted to this process's bounded in-memory queue. NOT
  durable yet; `accepted - sent` is what a crash would lose.
* **sent**     — the Logger answered `success=true`. Whether that means
  "queued in the Logger's memory" or "committed to disk" is the Logger's
  contract, not this client's (see docs/logging-contract.md).
* **failed**   — an RPC attempt errored. The record is KEPT and retried, so a
  failure is never itself a loss.
* **dropped**  — the queue was full and the oldest queued record was evicted to
  make room (bounded-memory policy: keep the newest context).
* **pending**  — still queued or in flight when the process stopped.

At-least-once, never at-most-once: a timeout can duplicate a record the Logger
already stored. Suppressing duplicates needs a stable event id plus a unique
index in the store — a schema/contract change, deliberately not faked here.

The defect this file used to have: `_pump` removed a record from the queue and
dropped it on the floor whenever the RPC raised, so a Logger outage ate exactly
the records that explained the outage.
"""

import asyncio
import json
import logging
import random
import time
from typing import Any, Dict, Optional

import grpc

# Bounded retry backoff: exponential with jitter, capped so a long outage does
# not turn into a hammer-storm when the Logger returns.
_BACKOFF_BASE = 0.25
_BACKOFF_CAP = 5.0
# How many queued records one successful round trip may drain before yielding.
_MAX_BURST = 512

logger = logging.getLogger("atf.logclient")


def _pb():
    try:
        from core.proto import pb as _pb_mod
        return _pb_mod
    except ImportError:
        return __import__("proto", fromlist=["pb"]).pb


def _pb_grpc():
    try:
        from core.proto import pb_grpc as _stub_mod
        return _stub_mod
    except ImportError:
        return __import__("proto", fromlist=["pb_grpc"]).pb_grpc


class LogClient:
    def __init__(
        self,
        address: str = "localhost:50052",
        max_queue_size: int = 4096,
        policy: Optional[Any] = None,
    ) -> None:
        self._address = address
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[Any] = None
        self._enabled = True
        # Optional collection policy, duck-typed: anything exposing
        # `allow(level, event_type=..., reason_code=...)`. This client is the door
        # every event in this process leaves through — the audit choke point, the
        # info/warn/error helpers and the stdlib logging bridge all funnel here —
        # so it is the only place where "is this collected?" can be answered once
        # for all of them. None keeps the pre-policy behaviour (no filtering).
        self._policy = policy
        # Bounded: an unbounded queue is a memory leak whenever the logger is
        # down or slow (a flood of ERROR/WARN records during an outage could
        # grow it without limit). When full we drop the oldest records — the
        # queue keeps the newest context, which is what an operator needs.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._task: Optional[asyncio.Task] = None
        self._max_queue_size = max_queue_size
        self._closing = False
        # A record that left the queue but has not been delivered yet. It is the
        # whole reason this file exists: it is retried until the Logger takes it.
        self._pending_entry: Optional[Any] = None
        # Records that have left the queue and are not acknowledged yet: being
        # sent right now, or held for retry after a failure. Without this
        # counter a drain cannot see mid-burst work and cancels the pump while a
        # record is in the air — which is how a "clean" shutdown lost one.
        self._in_flight = 0
        self._burst_limit = min(_MAX_BURST, max_queue_size)
        # Counters — the honest accounting an operator needs during an outage.
        self._accepted_total = 0
        self._sent_total = 0
        self._failed_total = 0
        self._dropped_total = 0
        self._disabled_total = 0
        self._shutdown_dropped_total = 0

    def connect(self) -> None:
        if self._channel is not None:
            return
        self._channel = grpc.aio.insecure_channel(self._address)
        self._stub = _pb_grpc().LogControlServiceStub(self._channel)

    def _backoff(self, consecutive_errors: int) -> float:
        """Bounded exponential backoff with jitter (see the constants above)."""
        window = min(_BACKOFF_BASE * (2 ** min(consecutive_errors - 1, 5)), _BACKOFF_CAP)
        return window * (0.5 + random.random())

    async def start(self) -> None:
        """Begin delivery. Records logged before this call are not lost: the
        queue is the buffer, and the pump drains whatever is already in it."""
        self.connect()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """Deliver records in order; never drop one because an RPC failed.

        A failure keeps the record in `_pending_entry` and backs off; the queue
        behind it is untouched, so nothing is reordered to the tail and nothing
        is lost. Delivery is at-least-once (see the module docstring).
        """
        consecutive_errors = 0
        while True:
            if self._closing and self._queue.empty() and self._pending_entry is None:
                return
            if self._pending_entry is None:
                self._pending_entry = await self._queue.get()
                self._in_flight += 1
            try:
                await self._stub.Log(_pb().LogRequest(entry=self._pending_entry))
                self._sent_total += 1
                self._pending_entry = None
                self._in_flight -= 1
                if consecutive_errors:
                    logger.info(
                        "log client recovered after %d failure(s); sent=%d failed=%d "
                        "dropped=%d pending=%d",
                        consecutive_errors, self._sent_total, self._failed_total,
                        self._dropped_total, self.pending,
                    )
                consecutive_errors = 0
                # v14: drain any records that accumulated while we were blocked,
                # batching them so a burst does not cost one round-trip per
                # entry. Ordering within the burst is preserved.
                for _ in range(self._burst_limit):
                    try:
                        burst_entry = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self._in_flight += 1
                    try:
                        await self._stub.Log(_pb().LogRequest(entry=burst_entry))
                        self._sent_total += 1
                        self._in_flight -= 1
                    except Exception:
                        # Keep the record that failed (and everything queued
                        # behind it): it becomes the pending head, so order is
                        # preserved and the error path below applies backoff.
                        self._pending_entry = burst_entry
                        raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                self._failed_total += 1
                # The record STAYS pending — this is the fix. It is not dropped,
                # and this counter is a failure count, not a loss count.
                if consecutive_errors in (1, 5, 20, 100) or consecutive_errors % 500 == 0:
                    logger.error(
                        "log client cannot reach %s (%s): %d consecutive failure(s), "
                        "retrying the same record; sent=%d dropped=%d pending=%d "
                        "[tail=%s]",
                        self._address, type(exc).__name__, consecutive_errors,
                        self._sent_total, self._dropped_total, self.pending,
                        (getattr(self._pending_entry, "message", "") or "")[:120],
                    )
                # Recreate the channel only occasionally: recreating on every
                # failure turns a short outage into a connection storm.
                if consecutive_errors in (5, 50):
                    try:
                        if self._channel is not None:
                            await self._channel.close()
                    except Exception:
                        pass
                    self._channel = None
                    self._stub = None
                    self.connect()
                # Avoid a tight loop when the server is down: exponential with
                # jitter so N services that lost the logger together do not
                # re-synchronize into a connection storm.
                await asyncio.sleep(self._backoff(consecutive_errors))

    def log(
        self,
        level: str,
        service: str,
        category: str,
        message: str,
        detail: Optional[dict] = None,
        rule_id: str = "",
        user_id: int = 0,
        correlation_id: str = "",
        event_type: str = "",
        reason_code: str = "",
    ) -> None:
        if not self._enabled or self._closing:
            self._disabled_total += 1
            return
        # Severity policy, applied at the door so every emission path obeys it.
        # A caller that can name its event lets a security-relevant event through
        # even when its level sits below the configured threshold.
        if self._policy is not None:
            try:
                if not self._policy.allow(
                    level, event_type=event_type, reason_code=reason_code
                ):
                    return
            except Exception:
                pass  # a policy bug must never silence logging entirely
        try:
            # Promote ids found in the structured detail onto the record's own
            # columns. Every existing call site writes rule_id/correlation_id
            # into its detail blob already, so this single rule makes the whole
            # codebase attributable (per-task history is then an indexed lookup)
            # without touching hundreds of call sites.
            if isinstance(detail, dict):
                if not rule_id:
                    rule_id = str(detail.get("rule_id") or detail.get("rule") or "")
                if not correlation_id:
                    correlation_id = str(detail.get("correlation_id") or "")
                if not user_id:
                    user_id = int(
                        detail.get("user_id") or detail.get("owner_user_id") or 0
                    )
            # v18 attribution travels with the record, so the log explorer can
            # answer "show me this task's history" and "show me everything this
            # user did" without grepping free text.
            entry = _pb().LogEntry(
                ts=int(time.time()),
                level=level.upper(),
                service=service,
                category=category,
                message=message,
                detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else "",
                rule_id=rule_id or "",
                user_id=int(user_id or 0),
                correlation_id=correlation_id or "",
            )
            self._accepted_total += 1
            try:
                self._queue.put_nowait(entry)
            except asyncio.QueueFull:
                # Bounded-queue backpressure (v14): the logger is behind.
                # Drop the OLDEST record to make room and keep the newest
                # context. Losing the oldest entry is the right trade: an
                # unreachable logger used to grow this queue without bound.
                try:
                    self._queue.get_nowait()
                except Exception:
                    pass
                self._dropped_total += 1
                if self._dropped_total in (1, 10, 100) or self._dropped_total % 1000 == 0:
                    logger.warning(
                        "log queue full (%d); dropped %d oldest record(s) total",
                        self._max_queue_size, self._dropped_total,
                    )
                try:
                    self._queue.put_nowait(entry)
                except Exception:
                    pass
        except Exception:
            pass  # never break the caller

    def debug(self, service: str, category: str, message: str, detail=None, **kw) -> None:
        self.log("DEBUG", service, category, message, detail, **kw)

    def info(self, service: str, category: str, message: str, detail=None, **kw) -> None:
        self.log("INFO", service, category, message, detail, **kw)

    def warn(self, service: str, category: str, message: str, detail=None, **kw) -> None:
        self.log("WARN", service, category, message, detail, **kw)

    def error(self, service: str, category: str, message: str, detail=None, **kw) -> None:
        self.log("ERROR", service, category, message, detail, **kw)

    async def query(
        self, level="", service="", category="", search="", limit=100, since=0,
        rule_id="", until=0, offset=0, ascending=False, levels=None,
        user_id=0, correlation_id="",
    ):
        self.connect()
        pb = _pb()
        req = pb.QueryLogsRequest(
            level=level, service=service, category=category,
            search=search, limit=limit, since=since,
            rule_id=rule_id, until=until, offset=offset,
            ascending=ascending, levels=list(levels or []),
            user_id=int(user_id or 0), correlation_id=correlation_id,
        )
        return await self._stub.QueryLogs(req)

    async def stats(self, rule_id="", user_id=0):
        self.connect()
        pb = _pb()
        return await self._stub.LogStats(
            pb.LogStatsRequest(rule_id=rule_id, user_id=int(user_id or 0))
        )

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #
    @property
    def pending(self) -> int:
        """Records admitted but not yet acknowledged: queued + in flight."""
        return self._queue.qsize() + self._in_flight

    def counters(self) -> Dict[str, int]:
        """Delivery accounting (see the module docstring for each name)."""
        return {
            "accepted": self._accepted_total,
            "sent": self._sent_total,
            "failed": self._failed_total,
            "dropped": self._dropped_total,
            "disabled": self._disabled_total,
            "shutdown_dropped": self._shutdown_dropped_total,
            "pending": self.pending,
            "queue_capacity": self._max_queue_size,
        }

    # ------------------------------------------------------------------ #
    # shutdown
    # ------------------------------------------------------------------ #
    async def flush(self, timeout: float = 5.0) -> int:
        """Try to deliver everything already queued, bounded by `timeout`.

        Returns the number of records still undelivered — 0 means every record
        reached the Logger. Never discards silently: whatever is left is
        reported and counted as `shutdown_dropped`.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while self.pending and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return self.pending

    async def close(self, drain_timeout: float = 3.0) -> None:
        """Stop accepting, drain what we can, then shut down — reporting loss.

        A bounded drain is the honest choice: a Logger that is down must not
        hold the process open forever, but records that did not make it are
        counted and logged loudly rather than vanishing.
        """
        self._closing = True
        leftover = await self.flush(drain_timeout)
        if leftover:
            self._shutdown_dropped_total += leftover
            logger.error(
                "log client shutting down with %d undelivered record(s) after a "
                "%.1fs drain (sent=%d failed=%d dropped=%d) — these are LOST",
                leftover, drain_timeout, self._sent_total,
                self._failed_total, self._dropped_total,
            )
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
        if self._channel:
            await self._channel.close()
            self._channel = None
            self._stub = None


# Noise filter — pyrogram's TCP layer spams "Send exception: ConnectionResetError"
# and asyncio emits one "socket.send() raised exception." per dropped packet
# during any network blip (641 warnings in 6h on a healthy box). These carry no
# actionable signal (the client watchdog reconnects) so they never reach the
# central logger; local stdout keeps them for deep debugging.
_NOISY_MESSAGES = (
    "Send exception: ConnectionResetError",
    "socket.send() raised exception",
)


class CentralLogHandler(logging.Handler):
    """Mirror every ERROR/WARN log record into the central logger service.

    Installed once on the root logger, so every module's logger.warning/error
    (pv_responder, dispatcher, pipeline, …) lands in the Web-UI Log Explorer
    without each call-site needing to know about the LogClient. INFO stays
    opt-in per call-site to keep the central store high-signal.
    """

    def __init__(self, client: "LogClient") -> None:
        super().__init__(level=logging.WARNING)
        self._client = client

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if any(noise in msg for noise in _NOISY_MESSAGES):
                return
            detail = None
            if record.exc_info:
                import traceback as _tb
                detail = {"traceback": "".join(_tb.format_exception(*record.exc_info))[-1500:]}
            # Call sites attach context with logger.warning(..., extra={...});
            # picking it up here means a module does not have to thread a
            # rule_id through every function just to be attributable.
            self._client.log(
                "WARN" if record.levelname == "WARNING" else record.levelname,
                "core",
                record.name.replace("atf.", "") or "core",
                msg,
                detail,
                rule_id=str(getattr(record, "rule_id", "") or ""),
                user_id=int(getattr(record, "user_id", 0) or 0),
                correlation_id=str(getattr(record, "correlation_id", "") or ""),
            )
        except Exception:
            self.handleError(record)
