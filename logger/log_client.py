"""LogClient — async gRPC client the other services use to push debug logs
to the Logger microservice. Non-blocking, fire-and-forget, never raises into
the caller's business logic."""

import asyncio
import json
import logging
import time
from typing import Any, Optional

import grpc

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
    def __init__(self, address: str = "localhost:50052", max_queue_size: int = 4096) -> None:
        self._address = address
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[Any] = None
        self._enabled = True
        # Bounded: an unbounded queue is a memory leak whenever the logger is
        # down or slow (a flood of ERROR/WARN records during an outage could
        # grow it without limit). When full we drop the oldest records — the
        # queue keeps the newest context, which is what an operator needs.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._task: Optional[asyncio.Task] = None
        self._dropped_total = 0
        self._max_queue_size = max_queue_size

    def connect(self) -> None:
        if self._channel is not None:
            return
        self._channel = grpc.aio.insecure_channel(self._address)
        self._stub = _pb_grpc().LogControlServiceStub(self._channel)

    async def start(self) -> None:
        self.connect()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """Drain the queue; reconnect on transient errors.

        Failures are counted and mirrored to the local logger, never swallowed
        silently — a broken pipe is the classic reason a service looks "fine"
        while its logs vanish.
        """
        consecutive_errors = 0
        while True:
            entry = await self._queue.get()
            try:
                await self._stub.Log(_pb().LogRequest(entry=entry))
                if consecutive_errors:
                    logger.info("log client recovered after %d failure(s)", consecutive_errors)
                consecutive_errors = 0
                # v14: drain any records that accumulated while we were
                # blocked, batching them so a burst does not cost one
                # round-trip per entry. Keeps ordering within the burst.
                for _ in range(self._max_queue_size):
                    try:
                        burst_entry = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        await self._stub.Log(_pb().LogRequest(entry=burst_entry))
                    except Exception:
                        # A mid-burst failure: requeue this entry and let the
                        # error path below handle reconnect/backoff.
                        try:
                            self._queue.put_nowait(burst_entry)
                        except Exception:
                            pass
                        raise
            except Exception as exc:
                consecutive_errors += 1
                # Keep the entry; it will be retried on the next iteration
                # after a reconnect.
                if consecutive_errors in (1, 5, 20, 100) or consecutive_errors % 500 == 0:
                    logger.error(
                        "log client could not reach %s (%s): dropped %d entries "
                        "[tail=%s]",
                        self._address, type(exc).__name__, consecutive_errors,
                        (entry.message or "")[:120],
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
                # Avoid a tight loop when the server is down.
                await asyncio.sleep(min(0.25 * consecutive_errors, 2.0))

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
    ) -> None:
        if not self._enabled or self._stub is None:
            return
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

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        if self._channel:
            await self._channel.close()


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
