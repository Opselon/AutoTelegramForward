"""Transactional audit outbox + durable audit_events store (PostgreSQL).

Design (mission §5):
- `audit_events` is the append-only durable audit record store. Important
  business mutations insert their audit record inside the SAME PostgreSQL
  transaction as the state change (transactional outbox), so a committed
  mutation can never lose its audit evidence and a rolled-back mutation can
  never produce a phantom audit record.
- `audit_outbox` tracks publication to the Central Logger. Rows are marked
  published after the Logger acks; a crash between commit and publish is
  recovered on restart. Publication is at-least-once with event_id
  deduplication downstream.

All writes go through the existing _execute_db adapter so asyncpg $N
placeholders are handled transparently.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional, Tuple

from .audit_events import AuditEvent, SCHEMA_VERSION

logger = logging.getLogger("atf.audit")

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_category TEXT NOT NULL DEFAULT 'feature_execution',
    severity TEXT NOT NULL,
    outcome TEXT NOT NULL,
    ts_utc BIGINT NOT NULL,
    service TEXT NOT NULL,
    service_version TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT '',
    deployment_id TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    span_id TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    operation_id TEXT NOT NULL DEFAULT '',
    parent_operation_id TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    actor_type TEXT NOT NULL DEFAULT 'system',
    actor_id TEXT NOT NULL DEFAULT '',
    feature_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    resource_type TEXT NOT NULL DEFAULT '',
    resource_id TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    queue_job_id TEXT NOT NULL DEFAULT '',
    attempt_number INTEGER NOT NULL DEFAULT 0,
    execution_stage TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL DEFAULT '',
    duration_ms BIGINT NOT NULL DEFAULT 0,
    database_system TEXT NOT NULL DEFAULT '',
    database_operation TEXT NOT NULL DEFAULT '',
    query_fingerprint TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_class TEXT NOT NULL DEFAULT '',
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    retry_after INTEGER NOT NULL DEFAULT 0,
    result_summary TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events (ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_events (severity, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_audit_service ON audit_events (service, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_audit_category ON audit_events (event_category, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_audit_corr ON audit_events (correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_operation ON audit_events (operation_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events (tenant_id, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_audit_feature ON audit_events (feature_id, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_audit_rule ON audit_events (rule_id);
CREATE INDEX IF NOT EXISTS idx_audit_queue_job ON audit_events (queue_job_id);
CREATE INDEX IF NOT EXISTS idx_audit_error_code ON audit_events (error_code);
CREATE INDEX IF NOT EXISTS idx_audit_reason ON audit_events (reason_code);

CREATE TABLE IF NOT EXISTS audit_outbox (
    event_id TEXT PRIMARY KEY,
    published_at BIGINT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON audit_outbox (published_at, created_at);
"""


def _to_row(evt: AuditEvent) -> Tuple[tuple, str]:
    d = evt.to_dict()
    import json
    row = (
        d["event_id"], d["schema_version"], d["event_type"], d["event_category"],
        d["severity"], d["outcome"], d["ts_utc"], d["service"], d["service_version"],
        d["environment"], d["deployment_id"], d["host"], d["correlation_id"],
        d["trace_id"], d["span_id"], d["request_id"], d["operation_id"],
        d["parent_operation_id"], d["tenant_id"], d["actor_type"], d["actor_id"],
        d["feature_id"], d["action"], d["resource_type"], d["resource_id"],
        d["rule_id"], d["task_id"], d["message_id"], d["queue_job_id"],
        d["attempt_number"], d["execution_stage"], d["reason_code"],
        d["duration_ms"], d["database_system"], d["database_operation"],
        d["query_fingerprint"], d["error_code"], d["error_class"],
        d["retryable"], d["retry_after"], d["result_summary"],
        json.dumps(d["metadata"], ensure_ascii=False),
    )
    placeholders = ", ".join("%s" for _ in row)
    query = (
        "INSERT INTO audit_events ("
        "event_id, schema_version, event_type, event_category, severity, outcome, ts_utc,"
        " service, service_version, environment, deployment_id, host, correlation_id,"
        " trace_id, span_id, request_id, operation_id, parent_operation_id, tenant_id,"
        " actor_type, actor_id, feature_id, action, resource_type, resource_id,"
        " rule_id, task_id, message_id, queue_job_id, attempt_number, execution_stage,"
        " reason_code, duration_ms, database_system, database_operation, query_fingerprint,"
        " error_code, error_class, retryable, retry_after, result_summary, metadata"
        f") VALUES ({placeholders}) ON CONFLICT (event_id) DO NOTHING"
    )
    return row, query


class AuditOutboxRepository:
    """Durable audit_event persistence with transactional-outbox publication."""

    def __init__(self, db: Any) -> None:
        self._db = db

    # ---- schema ----------------------------------------------------------- #
    async def initialize(self) -> None:
        """Create the audit tables on the PostgreSQL boundary.

        Uses the shared, comment- and dollar-quote-aware splitter so a future
        statement in AUDIT_DDL that carries a ``;`` inside a comment (the exact
        defect that left ``dead_letter_queue`` and ``web_login_codes`` uncreated
        on PostgreSQL deployments) splits correctly rather than being shipped
        to the server as fragments.
        """
        from .postgres_repositories import _execute_db, _split_sql_script
        for stmt in _split_sql_script(AUDIT_DDL):
            q = stmt.strip()
            if q:
                await _execute_db(self._db, q)

    # ---- transactional write (same tx as the business mutation) ------------ #
    async def record_transactional(self, evt: AuditEvent, conn: Any) -> bool:
        """Insert the audit event inside the caller's open transaction.

        `conn` is the asyncpg connection running the business mutation. If the
        business transaction rolls back, this audit row rolls back with it.
        Returns True when the row was inserted (False on duplicate event_id).
        """
        import json
        row, query = _to_row(evt)
        placeholders = ", ".join(f"${i}" for i in range(1, len(row) + 1))
        q = query.replace(
            "VALUES (" + ", ".join("%s" for _ in row) + ")",
            f"VALUES ({placeholders})",
        )
        res = await conn.execute(q, *row)
        # asyncpg returns e.g. 'INSERT 0 1'
        return str(res).endswith(" 1")

    # ---- standalone durable write ------------------------------------------ #
    async def record(self, evt: AuditEvent) -> bool:
        """Persist one audit event in its own implicit transaction."""
        from .postgres_repositories import _execute_db
        row, query = _to_row(evt)
        await _execute_db(self._db, query, row)
        return True

    # ---- outbox publication lifecycle -------------------------------------- #
    async def enqueue_for_publication(self, evt: AuditEvent) -> None:
        from .postgres_repositories import _execute_db
        await _execute_db(
            self._db,
            "INSERT INTO audit_outbox (event_id, attempts, created_at) VALUES (%s, 0, %s)"
            " ON CONFLICT (event_id) DO NOTHING",
            (evt.event_id, int(time.time())),
        )

    async def pending_publications(self, limit: int = 100) -> List[str]:
        from .postgres_repositories import _fetch_all_db
        rows = await _fetch_all_db(
            self._db,
            "SELECT event_id FROM audit_outbox WHERE published_at IS NULL"
            " ORDER BY created_at ASC LIMIT %s",
            (int(limit),),
        )
        return [r[0] if isinstance(r, (tuple, list)) else r["event_id"] for r in rows]

    async def mark_published(self, event_id: str) -> None:
        from .postgres_repositories import _execute_db
        await _execute_db(
            self._db,
            "UPDATE audit_outbox SET published_at = %s WHERE event_id = %s",
            (int(time.time()), event_id),
        )

    async def mark_publication_failed(self, event_id: str, error: str, attempts: int) -> None:
        from .postgres_repositories import _execute_db
        from .audit_events import redact_value
        await _execute_db(
            self._db,
            "UPDATE audit_outbox SET attempts = %s, last_error = %s WHERE event_id = %s",
            (int(attempts), redact_value(error)[:500], event_id),
        )

    async def outbox_backlog(self) -> int:
        from .postgres_repositories import _fetch_one_db
        row = await _fetch_one_db(
            self._db,
            "SELECT COUNT(*) FROM audit_outbox WHERE published_at IS NULL",
            (),
        )
        val = row[0] if isinstance(row, (tuple, list)) else (list(row.values())[0] if row else 0)
        return int(val or 0)

    # ---- authorized tenant-scoped queries ---------------------------------- #
    async def query(
        self,
        *,
        tenant_id: str = "",
        correlation_id: str = "",
        operation_id: str = "",
        feature_id: str = "",
        service: str = "",
        severity: str = "",
        event_category: str = "",
        outcome: str = "",
        since: int = 0,
        until: int = 0,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """Tenant-scoped, bounded, deterministic audit query.

        A non-empty tenant_id filters to that tenant only — the Go API layer
        resolves the authorized tenant from the authenticated principal and
        passes it here; a normal tenant can never omit or widen the filter.
        """
        from .postgres_repositories import _fetch_all_db
        where, params = [], []
        if tenant_id:
            where.append("tenant_id = %s")
            params.append(str(tenant_id))
        if correlation_id:
            where.append("correlation_id = %s")
            params.append(str(correlation_id))
        if operation_id:
            where.append("operation_id = %s")
            params.append(str(operation_id))
        if feature_id:
            where.append("feature_id = %s")
            params.append(str(feature_id))
        if service:
            where.append("service = %s")
            params.append(str(service))
        if severity:
            where.append("severity = %s")
            params.append(str(severity).upper())
        if event_category:
            where.append("event_category = %s")
            params.append(str(event_category))
        if outcome:
            where.append("outcome = %s")
            params.append(str(outcome).upper())
        if since:
            where.append("ts_utc >= %s")
            params.append(int(since))
        if until:
            where.append("ts_utc <= %s")
            params.append(int(until))
        sql = "SELECT * FROM audit_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts_utc DESC LIMIT %s OFFSET %s"
        params += [min(int(limit), 500), max(int(offset), 0)]
        rows = await _fetch_all_db(self._db, sql, tuple(params))
        out = []
        for r in rows:
            d = dict(r) if hasattr(r, "keys") else dict(zip(
                ["event_id", "schema_version", "event_type", "event_category", "severity",
                 "outcome", "ts_utc", "service"], r[:8]
            )) if isinstance(r, (tuple, list)) else dict(r)
            out.append(d)
        return out

    # ---- retention --------------------------------------------------------- #
    async def prune_older_than(self, days: int) -> int:
        """Delete diagnostic events older than N days. Audit events of category
        'security' and 'audit' are protected from routine pruning."""
        from .postgres_repositories import _execute_db
        cutoff = int(time.time()) - int(days) * 86400
        res = await _execute_db(
            self._db,
            "DELETE FROM audit_events WHERE ts_utc < %s"
            " AND event_category NOT IN ('security', 'audit')",
            (cutoff,),
        )
        # asyncpg returns 'DELETE n'
        try:
            return int(str(res).split()[-1])
        except Exception:
            return 0
