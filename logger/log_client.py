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
    def __init__(self, address: str = "localhost:50052") -> None:
        self._address = address
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[Any] = None
        self._enabled = True
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

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
            except Exception as exc:
                consecutive_errors += 1
                # Keep the entry; the queue is unbounded and the entry will be
                # retried on the next iteration after a reconnect.
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
    ) -> None:
        if not self._enabled or self._stub is None:
            return
        try:
            entry = _pb().LogEntry(
                ts=int(time.time()),
                level=level.upper(),
                service=service,
                category=category,
                message=message,
                detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else "",
            )
            self._queue.put_nowait(entry)
        except Exception:
            pass  # never break the caller

    def debug(self, service: str, category: str, message: str, detail=None) -> None:
        self.log("DEBUG", service, category, message, detail)

    def info(self, service: str, category: str, message: str, detail=None) -> None:
        self.log("INFO", service, category, message, detail)

    def warn(self, service: str, category: str, message: str, detail=None) -> None:
        self.log("WARN", service, category, message, detail)

    def error(self, service: str, category: str, message: str, detail=None) -> None:
        self.log("ERROR", service, category, message, detail)

    async def query(self, level="", service="", category="", search="", limit=100, since=0):
        self.connect()
        pb = _pb()
        req = pb.QueryLogsRequest(
            level=level, service=service, category=category,
            search=search, limit=limit, since=since,
        )
        return await self._stub.QueryLogs(req)

    async def stats(self):
        self.connect()
        pb = _pb()
        return await self._stub.LogStats(pb.LogStatsRequest())

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        if self._channel:
            await self._channel.close()
