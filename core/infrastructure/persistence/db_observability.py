"""Database query observability — error capture, latency, and slow-query events.

Wraps the existing _execute_db/_fetch_*_db adapters so every real PostgreSQL
operation (including call sites that previously bypassed the wrapper) emits a
structured query event on failure or when latency exceeds the configured
slow-query threshold. Successes emit only a cheap in-memory metric bump;
failures and slow queries are the events worth persisting (§6.4).

Secret safety: only query fingerprints (normalized template ids) and driver
error codes are recorded — never raw SQL text, never parameter values.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from typing import Any, Optional

logger = logging.getLogger("atf.dbobs")

# Bounded metric labels: fingerprints are normalized template ids, never raw
# values (mission §11.1 — no unbounded label cardinality).
_MAX_LABEL_LEN = 64
_SLOW_QUERY_THRESHOLD_MS = 500.0

# Normalized fingerprint: strip literals and placeholder noise from SQL.
_SQL_LITERALS = re.compile(r"'[^']*'")
_SQL_NUMBERS = re.compile(r"\b\d+\b")
_SPACES = re.compile(r"\s+")


def query_fingerprint(sql: str) -> str:
    """Stable, bounded fingerprint for a query template. Never contains values."""
    try:
        s = _SQL_LITERALS.sub("'?'", sql)
        s = _SQL_NUMBERS.sub("N", s)
        s = _SPACES.sub(" ", s).strip()
        # Keep the operation verb + first table/reference for a short label.
        parts = s.split()
        if not parts:
            return "empty"
        verb = parts[0].upper()
        ref = ""
        for kw in ("FROM", "INTO", "UPDATE", "TABLE"):
            if kw in parts:
                i = parts.index(kw)
                if i + 1 < len(parts):
                    ref = parts[i + 1][:48]
                break
        fp = f"{verb} {ref}".strip()
        return fp[:_MAX_LABEL_LEN] or "unknown"
    except Exception:
        return "unknown"


class _QueryMetrics:
    """Bounded in-memory counters. Labels are fingerprints/severities only."""

    def __init__(self) -> None:
        self.total = 0
        self.failed = 0
        self.slow = 0
        self.by_error_class: dict = defaultdict(int)
        self.by_fingerprint: dict = defaultdict(int)

    def observe(self, fp: str, duration_ms: float, error_class: str = "") -> None:
        self.total += 1
        if error_class:
            self.failed += 1
            self.by_error_class[error_class[:_MAX_LABEL_LEN]] += 1
        if duration_ms >= _SLOW_QUERY_THRESHOLD_MS:
            self.slow += 1
            self.by_fingerprint[fp[:_MAX_LABEL_LEN]] += 1

    def snapshot(self) -> dict:
        return {
            "total": self.total,
            "failed": self.failed,
            "slow": self.slow,
            "slow_by_fingerprint": dict(list(self.by_fingerprint.items())[:20]),
            "errors_by_class": dict(list(self.by_error_class.items())[:20]),
        }


metrics = _QueryMetrics()


def classify_driver_error(exc: BaseException) -> str:
    """Stable error class from driver exception type / SQLSTATE. Never raw text."""
    name = type(exc).__name__
    sqlstate = getattr(exc, "sqlstate", "") or getattr(exc, "pgcode", "") or ""
    if sqlstate:
        return f"{name}:{sqlstate}"
    return name[:_MAX_LABEL_LEN]


async def instrumented_execute(
    db: Any,
    query: str,
    params: tuple,
    executor,
    *,
    correlation_id: str = "",
    tenant_id: str = "",
    emit_audit=None,
) -> Any:
    """Run one DB operation with error capture and slow-query observation.

    `executor(db, formatted_query, params)` performs the real driver call.
    On failure the structured event includes the driver error class, the
    query fingerprint, and timing — but never parameters or raw SQL.
    """
    fp = query_fingerprint(query)
    t0 = time.monotonic()
    try:
        result = await executor(db, query, params)
        duration_ms = (time.monotonic() - t0) * 1000
        metrics.observe(fp, duration_ms)
        if duration_ms >= _SLOW_QUERY_THRESHOLD_MS and emit_audit is not None:
            from ..persistence.audit_events import AuditEvent
            await emit_audit(AuditEvent(
                event_type="db.slow_query",
                service="core",
                outcome="SUCCEEDED",
                severity="WARN",
                event_category="database",
                database_system="postgresql",
                database_operation=fp.split(" ", 1)[0].lower(),
                query_fingerprint=fp,
                duration_ms=int(duration_ms),
                correlation_id=correlation_id,
                tenant_id=tenant_id,
                result_summary=f"slow query {fp} took {int(duration_ms)}ms",
            ))
        return result
    except Exception as exc:
        duration_ms = (time.monotonic() - t0) * 1000
        error_class = classify_driver_error(exc)
        metrics.observe(fp, duration_ms, error_class=error_class)
        logger.error(
            "[DB:ERROR] fingerprint=%s class=%s duration=%.1fms corr=%s",
            fp, error_class, duration_ms, correlation_id[:12],
        )
        if emit_audit is not None:
            try:
                from ..persistence.audit_events import AuditEvent
                await emit_audit(AuditEvent(
                    event_type="db.query_failed",
                    service="core",
                    outcome="FAILED",
                    severity="ERROR",
                    event_category="database",
                    database_system="postgresql",
                    query_fingerprint=fp,
                    duration_ms=int(duration_ms),
                    error_code=error_class,
                    error_class=error_class,
                    retryable=error_class.split(":")[-1] in ("40001", "40P01", "53300", "08006"),
                    correlation_id=correlation_id,
                    tenant_id=tenant_id,
                    result_summary=f"query {fp} failed after {int(duration_ms)}ms",
                ))
            except Exception:
                pass  # never mask the original DB error
        raise


def snapshot_metrics() -> dict:
    return metrics.snapshot()
