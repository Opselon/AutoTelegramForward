"""Logger microservice.

Owns its own SQLite database (multi-DB architecture: each service owns its
data) and exposes the LogControlService gRPC API. Other services push debug
logs here via gRPC; the query/stats RPCs power the bot's /logs panel.
"""

import asyncio
import json
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import grpc  # noqa: E402

try:
    from core.proto import pb, pb_grpc
except ImportError:  # pragma: no cover - direct script execution
    from proto import pb, pb_grpc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s logger: %(message)s")
logger = logging.getLogger("atf.logger")


# --------------------------------------------------------------------------- #
class LoggerDatabase:
    """SQLite store for debug logs, owned exclusively by this service."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True) if db_path != ":memory:" else None
        import sqlite3

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    service TEXT NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_service ON logs(service)")
            self._conn.commit()

    def insert(self, ts: int, level: str, service: str, category: str, message: str, detail: str) -> None:
        if not isinstance(message, str):
            message = str(message)
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs (ts, level, service, category, message, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, level, service, category, message, detail),
            )
            self._conn.commit()

    def query(self, level="", service="", category="", search="", limit=100, since=0):
        sql = "SELECT * FROM logs WHERE 1=1"
        params = []
        if level:
            sql += " AND level=?"
            params.append(level)
        if service:
            sql += " AND service=?"
            params.append(service)
        if category:
            sql += " AND category=?"
            params.append(category)
        if search:
            sql += " AND message LIKE ?"
            params.append(f"%{search}%")
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(min(int(limit), 1000))
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def stats(self):
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS c FROM logs").fetchone()["c"]
            by_level = {
                r["level"]: r["c"]
                for r in self._conn.execute(
                    "SELECT level, COUNT(*) AS c FROM logs GROUP BY level"
                ).fetchall()
            }
            by_category = {
                r["category"]: r["c"]
                for r in self._conn.execute(
                    "SELECT category, COUNT(*) AS c FROM logs GROUP BY category"
                ).fetchall()
            }
        return total, by_level, by_category

    def purge(self, level="", service="", category="", older_than_hours=0) -> tuple[int, int]:
        """Delete log rows matching the filters. Returns (purged, remaining).

        older_than_hours > 0 protects the newest window (use it as the default
        safety net so a blanket purge can't wipe the evidence you're debugging).
        """
        sql = "DELETE FROM logs WHERE 1=1"
        params: list = []
        if level:
            sql += " AND level=?"
            params.append(level)
        if service:
            sql += " AND service=?"
            params.append(service)
        if category:
            sql += " AND category=?"
            params.append(category)
        if older_than_hours and older_than_hours > 0:
            cutoff = int(time.time()) - int(older_than_hours) * 3600
            sql += " AND ts < ?"
            params.append(cutoff)
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            purged = cur.rowcount or 0
            # Reclaim disk space right away — a big purge leaves a bloated file
            # and the WAL checkpoint is what actually shrinks it.
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            remaining = self._conn.execute("SELECT COUNT(*) AS c FROM logs").fetchone()["c"]
        return purged, remaining

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- #
class LogControlServicer(pb_grpc.LogControlServiceServicer):
    VALID_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}

    def __init__(self, db: LoggerDatabase) -> None:
        self._db = db

    async def Log(self, request, context):
        entry = request.entry
        level = (entry.level or "INFO").upper()
        if level not in self.VALID_LEVELS:
            level = "INFO"
        try:
            self._db.insert(
                ts=entry.ts or int(time.time()),
                level=level,
                service=entry.service or "core",
                category=entry.category or "system",
                message=entry.message or "",
                detail=entry.detail or "",
            )
        except Exception as exc:
            return pb.StatusResponse(success=False, message=str(exc))
        return pb.StatusResponse(success=True, message="logged")

    async def QueryLogs(self, request, context):
        rows = self._db.query(
            level=request.level, service=request.service,
            category=request.category, search=request.search,
            limit=request.limit or 100, since=request.since,
        )
        logs = [
            pb.LogEntry(
                ts=r["ts"], level=r["level"], service=r["service"],
                category=r["category"], message=r["message"], detail=r["detail"],
            )
            for r in rows
        ]
        return pb.QueryLogsResponse(logs=logs, total=len(logs))

    async def LogStats(self, request, context):
        total, by_level, by_category = self._db.stats()
        return pb.LogStatsResponse(
            total=total,
            by_level=by_level,
            by_category=by_category,
        )

    async def PurgeLogs(self, request, context):
        level = (request.level or "").upper()
        if level and level not in self.VALID_LEVELS:
            return pb.PurgeLogsResponse(
                success=False,
                error=f"invalid level: {level} (expected one of {sorted(self.VALID_LEVELS)})",
            )
        try:
            purged, remaining = self._db.purge(
                level=level,
                service=request.service or "",
                category=request.category or "",
                older_than_hours=request.older_than_hours or 0,
            )
        except Exception as exc:  # noqa: BLE001 — surface any DB failure to the caller
            return pb.PurgeLogsResponse(success=False, error=str(exc))
        return pb.PurgeLogsResponse(success=True, purged=purged, remaining=remaining)


# --------------------------------------------------------------------------- #
async def main() -> None:
    import os

    db_path = os.environ.get("ATF_LOGGER_DB_PATH", os.environ.get("ATF_LOGGER_DB", "data/atf_logs.db"))
    host = os.environ.get("ATF_LOGGER_HOST", "0.0.0.0")
    port = int(os.environ.get("ATF_LOGGER_PORT", "50052"))

    db = LoggerDatabase(db_path)
    server = grpc.aio.server()
    pb_grpc.add_LogControlServiceServicer_to_server(LogControlServicer(db), server)
    addr = f"{host}:{port}"
    server.add_insecure_port(addr)
    await server.start()
    logger.info("Logger microservice listening on %s (db=%s)", addr, db_path)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop(grace=2)
        db.close()
        logger.info("Logger stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
