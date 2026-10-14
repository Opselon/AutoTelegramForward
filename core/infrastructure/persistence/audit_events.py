"""Centralized audit event schema — stable, versioned event envelope (v1).

Every service that needs durable, queryable audit evidence builds an
AuditEvent through this module. The schema is intentionally flat, typed, and
forward-compatible: unknown optional fields are ignored by the Logger, and a
new optional field never breaks older producers.

Design rules (mission §3):
- Required fields are minimal; everything diagnostic is optional.
- No unbounded payloads: metadata is a bounded flat dict of scalars.
- Secrets are redacted at construction time (defense in depth: the Logger
  redacts again on ingest).
- Outcome semantics follow the operation lifecycle, not the transport:
  OUTCOME_UNCONFIRMED exists so a lost Telegram response is never converted
  into success or failure.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = "1.0.0"

# Stable operation lifecycle states (mission §2.2 / §7.1).
RECEIVED = "RECEIVED"
VALIDATING = "VALIDATING"
AUTHORIZED = "AUTHORIZED"
ACCEPTED = "ACCEPTED"
IN_PROGRESS = "IN_PROGRESS"
RETRY_WAIT = "RETRY_WAIT"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
OUTCOME_UNCONFIRMED = "OUTCOME_UNCONFIRMED"

LIFECYCLE_STATES = frozenset({
    RECEIVED, VALIDATING, AUTHORIZED, ACCEPTED, IN_PROGRESS,
    RETRY_WAIT, SUCCEEDED, FAILED, CANCELLED, OUTCOME_UNCONFIRMED,
})

# Severity normalization (mission §3.2). WARN (not WARNING) is canonical.
VALID_SEVERITY = frozenset({"DEBUG", "INFO", "WARN", "ERROR", "FATAL"})

# Stable reason-code registry (mission §7.2). Additions are forward-compatible.
REASON_CODES = frozenset({
    "RULE_NOT_MATCHED", "RULE_PAUSED", "RULE_DISABLED", "FILTER_REJECTED",
    "DUPLICATE_DETECTED", "REPLY_TARGET_UNAVAILABLE", "PROTECTED_CONTENT",
    "RATE_LIMITED", "DESTINATION_UNAVAILABLE", "AUTHORIZATION_DENIED",
    "DATABASE_OPERATION_FAILED", "QUEUE_ENQUEUE_FAILED",
    "EXTERNAL_RESULT_UNCONFIRMED", "VALIDATION_FAILED", "TIMEOUT",
    "MAX_ATTEMPTS_EXCEEDED", "POISON_EVENT",
})

# Field-aware redaction — same vocabulary as core/infrastructure/telegram/errors.py
_SECRET_KEY = re.compile(
    r"(?i)(api[_-]?hash|api[_-]?id|bot[_-]?token|master[_-]?key|"
    r"session[_-]?(string)?|auth[_-]?key|password|phone[_-]?code|2fa)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?hash|api[_-]?id|bot[_-]?token|master[_-]?key|"
    r"session[_-]?string|password|phone[_-]?code|token|secret|2fa)"
    r"(\s*[:=]\s*)(\S+)"
)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_PHONE = re.compile(r"\+\d{7,16}\b")
_REDACTED = "[REDACTED]"

# Bounded field limits (mission §9.4).
MAX_MESSAGE_LEN = 2000
MAX_METADATA_BYTES = 4096
MAX_METADATA_FIELDS = 32


def redact_value(text: Any) -> str:
    """Redact a single string value. Total: never raises."""
    try:
        s = text if isinstance(text, str) else str(text)
        s = _SECRET_VALUE.sub(r"\1\2" + _REDACTED, s)
        s = _SECRET_KEY.sub(_REDACTED, s)
        s = _LONG_HEX.sub(_REDACTED, s)
        return _PHONE.sub("[PHONE]", s)
    except Exception:  # pragma: no cover — safety net
        return _REDACTED


def _safe_metadata(metadata: Optional[dict]) -> dict:
    """Bounded, redacted, JSON-serializable metadata. Total: never raises."""
    if not metadata:
        return {}
    out: dict = {}
    for i, (k, v) in enumerate(metadata.items()):
        if i >= MAX_METADATA_FIELDS:
            break
        key = str(k)[:64]
        if _SECRET_KEY.search(key):
            out[key] = _REDACTED
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            s = v if isinstance(v, str) else ("" if v is None else str(v))
            out[key] = redact_value(s)[:512]
        else:
            # Non-scalars are summarized, never stored raw (no unbounded nesting).
            out[key] = f"<{type(v).__name__}>"
    try:
        blob = json.dumps(out, ensure_ascii=False)
        if len(blob) > MAX_METADATA_BYTES:
            return {"_truncated": True}
    except Exception:
        return {}
    return out


@dataclass
class AuditEvent:
    """One audit/observability event following the central envelope (§3.1)."""

    # ---- required identity -------------------------------------------------
    event_type: str                  # e.g. "rule.pause", "delivery.completed"
    service: str                     # core | api | web | logger | bot
    outcome: str                     # one of LIFECYCLE_STATES

    # ---- optional identity/context -----------------------------------------
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_category: str = "feature_execution"
    severity: str = "INFO"
    ts_utc: int = field(default_factory=lambda: int(time.time()))
    schema_version: str = SCHEMA_VERSION
    service_version: str = ""
    environment: str = ""
    deployment_id: str = ""
    host: str = ""
    correlation_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    request_id: str = ""
    operation_id: str = ""
    parent_operation_id: str = ""
    tenant_id: str = ""
    actor_type: str = "system"       # user | system | admin | scheduler
    actor_id: str = ""
    feature_id: str = ""
    action: str = ""
    resource_type: str = ""
    resource_id: str = ""
    rule_id: str = ""
    task_id: str = ""
    message_id: str = ""
    queue_job_id: str = ""
    attempt_number: int = 0
    execution_stage: str = ""
    reason_code: str = ""            # one of REASON_CODES when applicable
    duration_ms: int = 0
    database_system: str = ""        # postgresql | sqlite
    database_operation: str = ""     # select | insert | update | delete | migrate
    query_fingerprint: str = ""      # normalized template id, never raw SQL
    error_code: str = ""
    error_class: str = ""
    retryable: bool = False
    retry_after: int = 0
    result_summary: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.severity = self.severity.upper()
        if self.severity not in VALID_SEVERITY:
            self.severity = "INFO"
        self.outcome = self.outcome.upper()
        if self.outcome not in LIFECYCLE_STATES:
            self.outcome = OUTCOME_UNCONFIRMED
        self.event_category = self.event_category or "feature_execution"
        self.message = ""
        self.metadata = _safe_metadata(self.metadata)
        self.result_summary = redact_value(self.result_summary)[:MAX_MESSAGE_LEN]
        if self.reason_code and self.reason_code not in REASON_CODES:
            # Unknown codes are preserved (forward compatibility) but flagged.
            self.metadata["reason_code_unregistered"] = self.reason_code

    # ------------------------------------------------------------------ #
    def validate(self) -> list:
        """Return a list of schema violations (empty list = valid)."""
        errors = []
        if not self.event_type:
            errors.append("event_type is required")
        if not self.service:
            errors.append("service is required")
        if self.severity not in VALID_SEVERITY:
            errors.append(f"invalid severity: {self.severity}")
        if self.outcome not in LIFECYCLE_STATES:
            errors.append(f"invalid outcome: {self.outcome}")
        if not self.event_id:
            errors.append("event_id is required")
        if self.ts_utc <= 0:
            errors.append("ts_utc must be a positive epoch")
        if self.reason_code and self.reason_code not in REASON_CODES:
            errors.append(f"unregistered reason_code: {self.reason_code}")
        if len(self.result_summary) > MAX_MESSAGE_LEN:
            errors.append("result_summary exceeds limit")
        return errors

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_category": self.event_category,
            "severity": self.severity,
            "ts_utc": self.ts_utc,
            "service": self.service,
            "service_version": self.service_version,
            "environment": self.environment,
            "deployment_id": self.deployment_id,
            "host": self.host,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "parent_operation_id": self.parent_operation_id,
            "tenant_id": self.tenant_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "feature_id": self.feature_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "rule_id": self.rule_id,
            "task_id": self.task_id,
            "message_id": self.message_id,
            "queue_job_id": self.queue_job_id,
            "attempt_number": self.attempt_number,
            "execution_stage": self.execution_stage,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "duration_ms": self.duration_ms,
            "database_system": self.database_system,
            "database_operation": self.database_operation,
            "query_fingerprint": self.query_fingerprint,
            "error_code": self.error_code,
            "error_class": self.error_class,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "result_summary": self.result_summary,
            "metadata": self.metadata,
        }
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
