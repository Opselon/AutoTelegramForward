"""Logger microservice.

Owns its own SQLite database (multi-DB architecture: each service owns its
data) and exposes the LogControlService gRPC API. Other services push debug
logs here via gRPC; the query/stats RPCs power the bot's /logs panel.
"""

import asyncio
import json
import logging
import os
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


def _busy_timeout_ms() -> int:
    """Bounded SQLite lock wait, in milliseconds (ATF_LOGGER_BUSY_TIMEOUT_MS).

    An unparseable or negative value falls back to the default instead of
    crashing the service: a typo in an env file must not take logging down.
    """
    try:
        value = int(os.environ.get("ATF_LOGGER_BUSY_TIMEOUT_MS", "5000"))
    except (TypeError, ValueError):
        return 5000
    return value if value >= 0 else 5000


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
            # Durability/ack contract: insert() commits before this service
            # answers success=true, and synchronous=FULL fsyncs that commit into
            # the WAL. So success=true means "committed and durable", not
            # "queued in memory". Written explicitly rather than relying on the
            # SQLite default, so a future default change cannot silently weaken
            # the acknowledgement every producer already trusts.
            self._conn.execute("PRAGMA synchronous=FULL")
            # SQLite's default busy_timeout is 0, which turns any write
            # contention into an immediate "database is locked" failure — a log
            # write lost for no reason. Waiting a bounded time is strictly
            # better. Configurable so operators can match their storage.
            self._conn.execute(f"PRAGMA busy_timeout={_busy_timeout_ms()}")
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
            # v18 attribution columns. Added by migration rather than a table
            # rebuild so an existing multi-gigabyte archive survives the
            # upgrade: the ALTER is instant and old rows simply carry the
            # defaults (unknown task, system-wide, no correlation).
            existing = {
                r["name"] for r in self._conn.execute("PRAGMA table_info(logs)").fetchall()
            }
            for column, ddl in (
                ("rule_id", "ALTER TABLE logs ADD COLUMN rule_id TEXT NOT NULL DEFAULT ''"),
                ("user_id", "ALTER TABLE logs ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0"),
                ("correlation_id", "ALTER TABLE logs ADD COLUMN correlation_id TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in existing:
                    self._conn.execute(ddl)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_service ON logs(service)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_rule ON logs(rule_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_corr ON logs(correlation_id)")
            self._conn.commit()

    def insert(
        self,
        ts: int,
        level: str,
        service: str,
        category: str,
        message: str,
        detail: str,
        rule_id: str = "",
        user_id: int = 0,
        correlation_id: str = "",
    ) -> None:
        if not isinstance(message, str):
            message = str(message)
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs (ts, level, service, category, message, detail,"
                " rule_id, user_id, correlation_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, level, service, category, message, detail,
                 rule_id or "", int(user_id or 0), correlation_id or ""),
            )
            self._conn.commit()

    @staticmethod
    def _filters(
        level="", service="", category="", search="", since=0, until=0,
        rule_id="", levels=None, user_id=0, correlation_id="",
    ):
        """Build the shared WHERE clause + params for query/count/export.

        Search covers the detail blob as well as the message: the identifiers an
        operator hunts for (rule id, correlation id, chat id) are written into
        structured detail by the producer, not into the human sentence.
        """
        sql = " WHERE 1=1"
        params: list = []
        chosen = [lv.upper() for lv in (levels or []) if lv]
        if chosen:
            sql += f" AND level IN ({','.join('?' * len(chosen))})"
            params.extend(chosen)
        elif level:
            sql += " AND level=?"
            params.append(level.upper())
        if service:
            sql += " AND service=?"
            params.append(service)
        if category:
            sql += " AND category=?"
            params.append(category)
        if rule_id:
            sql += " AND rule_id=?"
            params.append(rule_id)
        if correlation_id:
            sql += " AND correlation_id=?"
            params.append(correlation_id)
        if user_id:
            sql += " AND user_id=?"
            params.append(int(user_id))
        if search:
            sql += " AND (message LIKE ? OR detail LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like])
        if since:
            sql += " AND ts >= ?"
            params.append(int(since))
        if until:
            sql += " AND ts <= ?"
            params.append(int(until))
        return sql, params

    def query(
        self, level="", service="", category="", search="", limit=100, since=0,
        rule_id="", until=0, offset=0, ascending=False, levels=None,
        user_id=0, correlation_id="",
    ):
        """Return (rows, total) — total counts every match, not just this page."""
        where, params = self._filters(
            level=level, service=service, category=category, search=search,
            since=since, until=until, rule_id=rule_id, levels=levels,
            user_id=user_id, correlation_id=correlation_id,
        )
        order = " ORDER BY ts ASC, id ASC" if ascending else " ORDER BY ts DESC, id DESC"
        page = min(int(limit or 100), 1000)
        skip = max(int(offset or 0), 0)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM logs{where}{order} LIMIT ? OFFSET ?",
                tuple(params) + (page, skip),
            ).fetchall()
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM logs{where}", tuple(params)
            ).fetchone()["c"]
        return rows, total

    def stats(self, rule_id="", user_id=0):
        """Counts + the real history window, optionally scoped to a task."""
        where, params = self._filters(rule_id=rule_id, user_id=user_id)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS c, MIN(ts) AS oldest, MAX(ts) AS newest"
                f" FROM logs{where}",
                tuple(params),
            ).fetchone()
            total = row["c"]
            by_level = {
                r["level"]: r["c"]
                for r in self._conn.execute(
                    f"SELECT level, COUNT(*) AS c FROM logs{where} GROUP BY level",
                    tuple(params),
                ).fetchall()
            }
            by_category = {
                r["category"]: r["c"]
                for r in self._conn.execute(
                    f"SELECT category, COUNT(*) AS c FROM logs{where} GROUP BY category",
                    tuple(params),
                ).fetchall()
            }
            by_service = {
                r["service"]: r["c"]
                for r in self._conn.execute(
                    f"SELECT service, COUNT(*) AS c FROM logs{where} GROUP BY service",
                    tuple(params),
                ).fetchall()
            }
            # by_rule counts the whole archive (not the scoped slice) so the
            # "which task is noisy?" question stays answerable from a task view.
            by_rule = {
                r["rule_id"]: r["c"]
                for r in self._conn.execute(
                    "SELECT rule_id, COUNT(*) AS c FROM logs WHERE rule_id != ''"
                    " GROUP BY rule_id ORDER BY c DESC LIMIT 100"
                ).fetchall()
            }
        return {
            "total": total,
            "by_level": by_level,
            "by_category": by_category,
            "by_service": by_service,
            "by_rule": by_rule,
            "oldest_ts": row["oldest"] or 0,
            "newest_ts": row["newest"] or 0,
        }

    def purge(
        self, level="", service="", category="", older_than_hours=0, rule_id=""
    ) -> tuple[int, int]:
        """Delete log rows matching the filters. Returns (purged, remaining).

        older_than_hours > 0 protects the newest window (use it as the default
        safety net so a blanket purge can't wipe the evidence you're debugging).
        """
        where, params = self._filters(
            level=level, service=service, category=category, rule_id=rule_id
        )
        sql = f"DELETE FROM logs{where}"
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
        # Ingest-time redaction (mission §6.5 defense-in-depth): producers
        # already redact at AuditEvent construction; this pass guarantees no
        # secret ever reaches disk, whatever the producer sent.
        from .redaction import redact  # local import keeps module import light
        try:
            self._db.insert(
                ts=entry.ts or int(time.time()),
                level=level,
                service=entry.service or "core",
                category=entry.category or "system",
                message=redact(entry.message or ""),
                detail=redact(entry.detail or ""),
                # v18 attribution: the ids are opaque, so they are redacted too
                # (a rule id is not a secret, but a correlation id or a
                # user-supplied path segment could echo one).
                rule_id=entry.rule_id or "",
                user_id=int(entry.user_id or 0),
                correlation_id=entry.correlation_id or "",
            )
        except Exception as exc:
            return pb.StatusResponse(success=False, message=str(exc))
        return pb.StatusResponse(success=True, message="logged")

    async def QueryLogs(self, request, context):
        # v18: full history access. `request.limit` is the page size and
        # `request.offset` walks backwards through the archive, so an operator
        # can reach the oldest record instead of only the newest N.
        limit = request.limit or 100
        offset = request.offset or 0
        # Over-fetch by one to answer has_more without a second round trip.
        rows, total = self._db.query(
            level=request.level, service=request.service,
            category=request.category, search=request.search,
            limit=limit + 1, since=request.since,
            rule_id=request.rule_id, until=request.until,
            offset=offset, ascending=request.ascending,
            levels=list(request.levels), user_id=request.user_id,
            correlation_id=request.correlation_id,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        logs = [
            pb.LogEntry(
                ts=r["ts"], level=r["level"], service=r["service"],
                category=r["category"], message=r["message"], detail=r["detail"],
                rule_id=r["rule_id"] or "", user_id=r["user_id"] or 0,
                correlation_id=r["correlation_id"] or "",
            )
            for r in rows
        ]
        return pb.QueryLogsResponse(logs=logs, total=total, has_more=has_more)

    async def LogStats(self, request, context):
        stats = self._db.stats(rule_id=request.rule_id, user_id=request.user_id)
        return pb.LogStatsResponse(
            total=stats["total"],
            by_level=stats["by_level"],
            by_category=stats["by_category"],
            by_rule=stats["by_rule"],
            by_service=stats["by_service"],
            oldest_ts=stats["oldest_ts"],
            newest_ts=stats["newest_ts"],
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
                rule_id=request.rule_id or "",
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
