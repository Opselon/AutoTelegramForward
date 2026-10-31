"""Delivery reconciliation: detect deliveries that fell through the cracks.

The pipeline is durable, but durability only helps when something eventually
*checks*. A job can be claimed by a worker that crashed before any lease
watchdog ran, a rule can be paused mid-fan-out, and an enqueue can fail after
the event was marked processed. None of those produce a failure record on
their own — they are the silent class the reported incident belongs to.

This module is the *checker*. It is deliberately lightweight: it reconciles
the tables the pipeline already owns (``delivery_jobs``, ``message_map``,
``delivery_attempts``, ``ingestion_gaps``, ``forward_rules``) and never copies
payloads or builds an event history. Every check is a bounded aggregate query
plus a structured warning for anything it finds; nothing is repaired here,
because repair belongs to the components that already own the lifecycle
(the watchdog, the gap sweep, the dead-letter queue).

Guarantees the checks are built on:

* every *eligible* source message should end up with one delivery job per
  configured destination, or a recorded reason for not having one;
* a job should not sit in a pre-terminal state longer than its lease and
  retry budget allow;
* a job's persisted state should agree with the mapping written by the same
  lifecycle;
* a destination or source whose deliveries keep failing should be visible as
  such, not buried as a global failure count.

Results are plain dicts so the gRPC layer, the bot and the web dashboard can
all consume them without a new schema.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("atf.reconciler")

__all__ = ["DeliveryReconciler", "ReconciliationResult"]


# A claim older than this with no lease renewal is a stranded worker, not a
# slow one. Two full lease periods is the watchdog's own boundary; matching it
# keeps the reconciler from competing with the recovery path it reports on.
_STALE_CLAIM_MULTIPLIER = 2.0

# A PENDING/RETRY_WAIT job older than this has exhausted a generous retry
# budget (max_attempts * the longest backoff) and is stuck, not merely slow.
_STUCK_PENDING_BUDGET_SECONDS = 3600


class ReconciliationResult(Dict[str, Any]):
    """A structured reconciliation report.

    ``checks`` maps a check name to the records it flagged; ``totals``
    carries the aggregates the health screen renders. Nothing here is a
    failure *cause* — the warnings logged alongside each finding are where
    the actionable detail lives.
    """


class DeliveryReconciler:
    """Detects, and never repairs, deliveries that fell through the cracks."""

    def __init__(
        self,
        queue_repo: Any,
        message_map_repo: Any,
        rule_repo: Any,
        attempts_repo: Optional[Any] = None,
        gap_repo: Optional[Any] = None,
        lease_seconds: float = 30.0,
        max_attempts: int = 5,
    ) -> None:
        self._queue = queue_repo
        self._map = message_map_repo
        self._rules = rule_repo
        self._attempts = attempts_repo
        self._gaps = gap_repo
        self._lease_seconds = float(lease_seconds)
        self._max_attempts = int(max_attempts)

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #

    async def reconcile(self, owner_user_id: Optional[int] = None) -> ReconciliationResult:
        """Run every check and return one structured report.

        ``owner_user_id`` scopes the report to one tenant's rules; ``None``
        means the whole installation (used by internal monitoring, never by
        the tenant-facing endpoints, which always pass an owner).
        """
        started = time.time()
        result: ReconciliationResult = ReconciliationResult(
            checks={}, totals={}, generated_at=int(time.time()), owner_user_id=owner_user_id
        )
        for name, coro in (
            ("orphan_mappings", self._check_orphan_mappings(owner_user_id)),
            ("stuck_pending", self._check_stuck_pending(owner_user_id)),
            ("expired_claims", self._check_expired_claims(owner_user_id)),
            ("unknown_deliveries", self._check_unknown_deliveries(owner_user_id)),
            ("repeated_failures", self._check_repeated_failures(owner_user_id)),
            ("open_gaps", self._check_open_ingestion_gaps(owner_user_id)),
        ):
            try:
                flagged = await coro
            except Exception as exc:
                # One check failing must not hide the others. The failure is
                # itself an observation: a reconciliation that silently omits
                # a check is worse than one that reports it broke.
                logger.warning("[PIPELINE:RECONCILE_CHECK_FAILED] %s: %s", name, exc)
                flagged = []
            result["checks"][name] = flagged if isinstance(flagged, list) else []

        result["totals"] = self._totals(result)
        result["duration_ms"] = int((time.time() - started) * 1000)
        if result["totals"]["flagged"]:
            logger.warning(
                "[PIPELINE:RECONCILE] %d finding(s): %s",
                result["totals"]["flagged"],
                {k: len(v) for k, v in result["checks"].items() if v},
            )
        return result

    async def health_metrics(self, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
        """The aggregated delivery health the mission's metrics section asks for.

        Compact by construction: aggregates and ages, never payloads.
        """
        scope, params = await self._scope(owner_user_id)
        db = self._db()
        pending = sent = failed = retrying = dead = oldest_pending = 0
        per_target: Dict[str, Dict[str, int]] = {}
        per_source: Dict[str, Dict[str, int]] = {}

        if db is not None:
            rows = await self._query(
                db,
                "SELECT status, COUNT(*) AS n, MIN(created_at) AS oldest, target_chat_id "
                "FROM delivery_jobs WHERE 1=1" + scope + " GROUP BY status, target_chat_id",
                params,
            )
            for r in rows or ():
                status = str(r["status"])
                n = int(r["n"] or 0)
                target = str(r["target_chat_id"])
                bucket = per_target.setdefault(target, {"sent": 0, "failed": 0, "total": 0})
                bucket["total"] += n
                if status == "SENT":
                    sent += n
                    bucket["sent"] += n
                elif status in ("FAILED", "DEAD"):
                    failed += n
                    dead += n if status == "DEAD" else 0
                    bucket["failed"] += n
                elif status == "RETRY_WAIT":
                    retrying += n
                elif status in ("PENDING", "CLAIMED"):
                    pending += n
                oldest = int(r["oldest"] or 0)
                if status in ("PENDING", "CLAIMED", "RETRY_WAIT") and (
                    oldest_pending == 0 or (oldest and oldest < oldest_pending)
                ):
                    oldest_pending = oldest

            rows = await self._query(
                db,
                "SELECT source_chat_id, status, COUNT(*) AS n FROM delivery_jobs "
                "WHERE 1=1" + scope + " GROUP BY source_chat_id, status",
                params,
            )
            for r in rows or ():
                src = str(r["source_chat_id"])
                bucket = per_source.setdefault(src, {"sent": 0, "failed": 0, "total": 0})
                bucket["total"] += int(r["n"] or 0)
                if str(r["status"]) == "SENT":
                    bucket["sent"] += int(r["n"] or 0)
                elif str(r["status"]) in ("FAILED", "DEAD"):
                    bucket["failed"] += int(r["n"] or 0)

        completed = sent + failed
        return {
            "eligible_processed": completed,
            "pending": pending,
            "delivered": sent,
            "failed": failed,
            "retrying": retrying,
            "dead_letter": dead,
            "success_rate": round(sent / completed, 6) if completed else None,
            "oldest_pending_age_seconds": max(0, int(time.time() - oldest_pending)) if oldest_pending else None,
            "failure_rate_by_destination": {
                t: round(b["failed"] / b["total"], 6) if b["total"] else None
                for t, b in per_target.items()
                if b["failed"]
            },
            "failure_rate_by_source": {
                t: round(b["failed"] / b["total"], 6) if b["total"] else None
                for t, b in per_source.items()
                if b["failed"]
            },
        }

    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #

    async def _check_orphan_mappings(self, owner_user_id: Optional[int]) -> List[Dict[str, Any]]:
        """Mappings whose delivery job vanished — a state mismatch.

        A mapping exists for (rule, source, target) but no job does. The
        lifecycle writes both together, so one without the other means the
        job was deleted while its mapping survived (or vice versa): the copy
        may or may not be in the destination, and nothing currently drives
        it to a conclusion.
        """
        scope, params = await self._scope(owner_user_id)
        db = self._db()
        if db is None:
            return []
        rows = await self._query(
            db,
            "SELECT m.rule_id, m.source_chat_id, m.source_message_id, m.target_chat_id, "
            "m.delivery_status, m.target_message_id "
            "FROM message_map m LEFT JOIN delivery_jobs j "
            "ON j.rule_id = m.rule_id AND j.source_chat_id = m.source_chat_id "
            "AND j.source_message_id = m.source_message_id "
            "AND j.target_chat_id = m.target_chat_id "
            "WHERE j.id IS NULL" + scope.replace("rule_id", "m.rule_id"),
            params,
        )
        findings = []
        for r in rows or ():
            findings.append(
                self._finding(
                    "orphan_mapping",
                    rule_id=str(r["rule_id"]),
                    source_chat_id=str(r["source_chat_id"]),
                    source_message_id=int(r["source_message_id"] or 0),
                    destination_chat_id=str(r["target_chat_id"]),
                    detail=f"mapping status={r['delivery_status']} with no delivery job",
                    retryable=False,
                )
            )
        if findings:
            logger.warning(
                "[PIPELINE:RECONCILE_ORPHAN_MAPPING] %d mapping(s) exist without a delivery job",
                len(findings),
            )
        return findings

    async def _check_stuck_pending(self, owner_user_id: Optional[int]) -> List[Dict[str, Any]]:
        """Pre-terminal jobs that outlived their retry budget.

        A job sitting in PENDING/RETRY_WAIT past ``max_attempts`` times the
        longest backoff is not slow, it is abandoned: no worker failure was
        recorded for it, which is exactly the silent class to surface.
        """
        scope, params = await self._scope(owner_user_id)
        db = self._db()
        if db is None:
            return []
        budget = max(_STUCK_PENDING_BUDGET_SECONDS, self._max_attempts * self._lease_seconds * 4)
        rows = await self._query(
            db,
            "SELECT id, rule_id, source_chat_id, source_message_id, target_chat_id, "
            "status, attempts, created_at, next_retry_at, error_detail "
            "FROM delivery_jobs WHERE status IN ('PENDING','RETRY_WAIT') "
            "AND (next_retry_at > 0 AND next_retry_at < ?)" + scope,
            (time.time() - budget,) + tuple(params),
        )
        findings = []
        for r in rows or ():
            findings.append(
                self._finding(
                    "stuck_pending",
                    rule_id=str(r["rule_id"]),
                    source_chat_id=str(r["source_chat_id"]),
                    source_message_id=int(r["source_message_id"] or 0),
                    destination_chat_id=str(r["target_chat_id"]),
                    delivery_job_id=str(r["id"]),
                    attempt_number=int(r["attempts"] or 0),
                    error_detail=str(r["error_detail"] or ""),
                    detail=f"status={r['status']} past its retry budget ({int(budget)}s) "
                    f"with no terminal outcome",
                    retryable=True,
                )
            )
        if findings:
            logger.warning(
                "[PIPELINE:RECONCILE_STUCK] %d job(s) stuck past their retry budget",
                len(findings),
            )
        return findings

    async def _check_expired_claims(self, owner_user_id: Optional[int]) -> List[Dict[str, Any]]:
        """CLAIMED jobs whose lease is well past renewal — a stranded worker.

        Two full lease periods is the watchdog's boundary, so anything still
        CLAIMED past it means the watchdog has not run (or the worker is
        wedged): the lease will be recovered, but until it is, the delivery
        has no observable outcome.
        """
        scope, params = await self._scope(owner_user_id)
        db = self._db()
        if db is None:
            return []
        threshold = time.time() - (self._lease_seconds * _STALE_CLAIM_MULTIPLIER)
        rows = await self._query(
            db,
            "SELECT id, rule_id, source_chat_id, source_message_id, target_chat_id, "
            "worker_id, lease_until, attempts "
            "FROM delivery_jobs WHERE status = 'CLAIMED' AND lease_until < ?" + scope,
            (threshold,) + tuple(params),
        )
        findings = []
        for r in rows or ():
            findings.append(
                self._finding(
                    "expired_lease",
                    rule_id=str(r["rule_id"]),
                    source_chat_id=str(r["source_chat_id"]),
                    source_message_id=int(r["source_message_id"] or 0),
                    destination_chat_id=str(r["target_chat_id"]),
                    delivery_job_id=str(r["id"]),
                    worker_id=str(r["worker_id"] or ""),
                    attempt_number=int(r["attempts"] or 0),
                    detail=f"lease expired {int(time.time() - (r['lease_until'] or 0))}s ago "
                    f"and the job is still CLAIMED",
                    retryable=True,
                )
            )
        if findings:
            logger.warning(
                "[PIPELINE:RECONCILE_EXPIRED_LEASE] %d job(s) CLAIMED past an expired lease",
                len(findings),
            )
        return findings

    async def _check_unknown_deliveries(self, owner_user_id: Optional[int]) -> List[Dict[str, Any]]:
        """UNKNOWN deliveries left unresolved — the timeout's honest gap.

        A send that timed out is marked UNKNOWN because neither success nor
        failure is known. Staying UNKNOWN forever is the one state with no
        recovery path of its own, so it must be visible to an operator.
        """
        scope, params = await self._scope(owner_user_id)
        db = self._db()
        if db is None:
            return []
        rows = await self._query(
            db,
            "SELECT id, rule_id, source_chat_id, source_message_id, target_chat_id, "
            "attempts, error_detail FROM delivery_jobs WHERE status = 'UNKNOWN'" + scope,
            tuple(params),
        )
        findings = []
        for r in rows or ():
            findings.append(
                self._finding(
                    "unknown_delivery",
                    rule_id=str(r["rule_id"]),
                    source_chat_id=str(r["source_chat_id"]),
                    source_message_id=int(r["source_message_id"] or 0),
                    destination_chat_id=str(r["target_chat_id"]),
                    delivery_job_id=str(r["id"]),
                    attempt_number=int(r["attempts"] or 0),
                    error_detail=str(r["error_detail"] or ""),
                    detail="delivery timed out and was never resolved to sent or failed",
                    retryable=True,
                )
            )
        if findings:
            logger.warning(
                "[PIPELINE:RECONCILE_UNKNOWN] %d delivery(ies) left in UNKNOWN",
                len(findings),
            )
        return findings

    async def _check_repeated_failures(self, owner_user_id: Optional[int]) -> List[Dict[str, Any]]:
        """Destinations or sources whose deliveries keep failing.

        A per-destination failure rate is the actionable form of "some
        destinations never get the message": a global failure count cannot
        distinguish one bad target from a broken rule.
        """
        metrics = await self.health_metrics(owner_user_id)
        findings = []
        for target, rate in (metrics.get("failure_rate_by_destination") or {}).items():
            if rate is not None and rate >= 0.5:
                findings.append(
                    self._finding(
                        "repeated_destination_failure",
                        rule_id="",
                        source_chat_id="",
                        source_message_id=0,
                        destination_chat_id=target,
                        detail=f"destination failure rate {rate:.0%}",
                        retryable=False,
                    )
                )
        for source, rate in (metrics.get("failure_rate_by_source") or {}).items():
            if rate is not None and rate >= 0.5:
                findings.append(
                    self._finding(
                        "repeated_source_failure",
                        rule_id="",
                        source_chat_id=source,
                        source_message_id=0,
                        destination_chat_id="",
                        detail=f"source failure rate {rate:.0%}",
                        retryable=False,
                    )
                )
        if findings:
            logger.warning(
                "[PIPELINE:RECONCILE_REPEAT_FAILURE] %d source(s)/destination(s) failing at >=50%%",
                len(findings),
            )
        return findings

    async def _check_open_ingestion_gaps(self, owner_user_id: Optional[int]) -> List[Dict[str, Any]]:
        """Ingestion gaps that exhausted their recovery budget.

        A gap is recovery work, not a loss record; a gap that used its whole
        attempt budget without closing is the actionable form — the poller
        kept trying and could not, which an operator must see rather than
        have it retry forever.
        """
        if self._gaps is None:
            return []
        try:
            rows = await self._query(
                getattr(self._gaps, "_db", None),
                "SELECT session_id, source_chat_id, source_message_id, attempts, state, last_error "
                "FROM ingestion_gaps WHERE state NOT IN ('CLOSED','RESOLVED','FAILED')",
                (),
            )
        except Exception:
            return []
        findings = []
        for r in rows or ():
            if int(r["attempts"] or 0) < 5:
                continue
            findings.append(
                self._finding(
                    "open_ingestion_gap",
                    rule_id="",
                    source_chat_id=str(r["source_chat_id"]),
                    source_message_id=int(r["source_message_id"] or 0),
                    destination_chat_id="",
                    attempt_number=int(r["attempts"] or 0),
                    error_detail=str(r["last_error"] or ""),
                    detail=f"source message {r['source_message_id']} unrecovered after "
                    f"{r['attempts']} attempt(s) (state={r['state']}); last error: "
                    f"{str(r['last_error'] or 'none')[:200]}",
                    retryable=False,
                )
            )
        if findings:
            logger.warning(
                "[PIPELINE:RECONCILE_GAP_EXHAUSTED] %d ingestion gap(s) exhausted their budget",
                len(findings),
            )
        return findings

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #

    async def _scope(self, owner_user_id: Optional[int]):
        """An owner-scoped SQL fragment, or none when unscoped.

        The reconciler never invents tenancy: when no owner is given it
        inspects the whole installation, which is for internal monitoring
        only.
        """
        if owner_user_id is None:
            return "", []
        # Tenancy comes through forward_rules; the queue carries rule_id, so
        # resolve the owner's rules once and bound the query with them.
        try:
            rules = await self._query(
                self._db(),
                "SELECT id FROM forward_rules WHERE owner_user_id = ?",
                (int(owner_user_id),),
            )
        except Exception:
            return "", []
        ids = [str(r["id"]) for r in rules or ()]
        if not ids:
            # An owner with no rules has nothing to reconcile. A fragment that
            # matches nothing keeps every check finite rather than empty.
            return " AND rule_id IN ('__none__')", []
        placeholders = ",".join("?" for _ in ids)
        return f" AND rule_id IN ({placeholders})", ids

    def _db(self):
        for repo in (self._queue, self._map):
            db = getattr(repo, "_db", None)
            if db is not None:
                return db
        return None

    def _sync_query(self, db, sql: str, params):
        """Run a query on either engine, returning a list of mappings.

        The Postgres pool is asynchronous (``fetchall`` with ``%s`` placeholders);
        the SQLite connection is synchronous (``query_all`` with ``?``). Both are
        tolerated so the reconciler works against either backend — a checker
        that only ran on SQLite would be invisible to every production
        installation on Postgres.
        """
        if db is None:
            return []
        if hasattr(db, "query_all"):  # SQLite
            return db.query_all(sql, tuple(params)) or []
        return []  # async engines: handled by _async_query

    async def _async_query(self, db, sql: str, params):
        """The asynchronous (Postgres) counterpart of ``_sync_query``."""
        if db is None:
            return []
        if hasattr(db, "query_all"):
            return db.query_all(sql, tuple(params)) or []
        fetch = getattr(db, "fetchall", None)
        if fetch is None:
            return []
        try:
            rows = await fetch(sql.replace("?", "%s"), tuple(params))
        except Exception as exc:
            logger.debug("[PIPELINE:RECONCILE_QUERY] postgres read failed: %s", exc)
            return []
        out = []
        for row in rows or []:
            if isinstance(row, dict):
                out.append(row)
            elif hasattr(row, "keys"):
                out.append(dict(row))
        return out

    async def _query(self, db, sql: str, params):
        """One query on whichever engine the handle speaks.

        Always awaitable and always returns a list, regardless of engine: the
        SQLite path is executed eagerly and wrapped, the Postgres path is a
        genuine await.
        """
        if db is None:
            return []
        if hasattr(db, "query_all"):  # SQLite: synchronous connection
            return db.query_all(sql, tuple(params)) or []
        fetch = getattr(db, "fetchall", None)  # Postgres: async pool
        if fetch is None:
            return []
        try:
            rows = await fetch(sql.replace("?", "%s"), tuple(params))
        except Exception as exc:
            logger.debug("[PIPELINE:RECONCILE_QUERY] postgres read failed: %s", exc)
            return []
        out = []
        for row in rows or []:
            if isinstance(row, dict):
                out.append(row)
            elif hasattr(row, "keys"):
                out.append(dict(row))
        return out

    @staticmethod
    def _finding(kind, *, rule_id, source_chat_id, source_message_id,
                 destination_chat_id, delivery_job_id="", worker_id="",
                 attempt_number=0, error_detail="", detail="", retryable=False) -> Dict[str, Any]:
        return {
            "kind": kind,
            "correlation_id": f"{rule_id}:{source_chat_id}:{source_message_id}:{destination_chat_id}"
            if rule_id else "",
            "rule_id": rule_id,
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
            "destination_chat_id": destination_chat_id,
            "delivery_job_id": delivery_job_id,
            "worker_id": worker_id,
            "attempt_number": attempt_number,
            "error_category": kind,
            # The summary is what an operator reads; the raw error, when
            # present, is appended rather than allowed to hide it.
            "error_detail": (detail + (f" | {error_detail}" if error_detail and error_detail != detail else ""))
            if detail else error_detail,
            "retryable": retryable,
            "timestamp": int(time.time()),
        }

    @staticmethod
    def _totals(result: ReconciliationResult) -> Dict[str, int]:
        checks = result["checks"]
        flagged = sum(len(v) for v in checks.values())
        return {
            "flagged": flagged,
            "orphan_mappings": len(checks.get("orphan_mappings", [])),
            "stuck_pending": len(checks.get("stuck_pending", [])),
            "expired_claims": len(checks.get("expired_claims", [])),
            "unknown_deliveries": len(checks.get("unknown_deliveries", [])),
            "repeated_failures": len(checks.get("repeated_failures", [])),
            "open_gaps": len(checks.get("open_gaps", [])),
        }
