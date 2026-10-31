"""Canonical failure taxonomy and diagnostic event contract.

**Ownership.** The *public API contract* is defined in the protobuf (Go-owned).
This module is the Python-side canonical producer: it defines the stage-aware
taxonomy and the structured event that Python emits, and it is the single
place both must agree. Nothing else in Python may invent error codes.

The taxonomy extends the execution-level classification in ``errors.py``
(which answers *"what did Telegram say?"*) with the pipeline dimension
(*"where were we when it happened, and what were we doing?"*). The two are
deliberately separate representations, per the mission's §3.2:

* **raw** — the original Telegram error (``errors.TelegramErrorEvent``);
* **normalized** — a stable ``(category, reason)`` pair plus stage, strategy,
  origin, retryability and fallback eligibility.

Design rules:

* every stage of the pipeline has a category — an event without one is a bug;
* ``UNKNOWN`` is an explicit, first-class value, never a fallback to success;
* never fabricate an RPC code; the raw layer carries what Telegram actually
  returned and ``None`` for what the library does not expose;
* the event never carries message text or secrets, only identifiers and
  lengths, so it is safe to persist and to surface across owners.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Pipeline stages — where in the lifecycle the event was emitted
# --------------------------------------------------------------------------- #
ST_INGEST = "INGEST"                    # update received / poller replay
ST_PARSE = "PARSE"                      # normalization, entities, caption
ST_MEDIA = "MEDIA"                      # download / re-upload
ST_ROUTE = "ROUTE"                      # task matching, config snapshot
ST_ENQUEUE = "ENQUEUE"                  # durable job creation
ST_CLAIM = "CLAIM"                      # worker lease
ST_SEND = "SEND"                        # the Telegram operation itself
ST_FALLBACK = "FALLBACK"                # an alternate strategy was selected
ST_SYNC = "SYNC"                        # edit / delete propagation
ST_RECONCILE = "RECONCILE"              # reconciliation / UNKNOWN resolution

STAGES = (ST_INGEST, ST_PARSE, ST_MEDIA, ST_ROUTE, ST_ENQUEUE, ST_CLAIM,
          ST_SEND, ST_FALLBACK, ST_SYNC, ST_RECONCILE)

# --------------------------------------------------------------------------- #
# Categories — grouped by the subsystem that owns the failure
# --------------------------------------------------------------------------- #
C_TELEGRAM_API = "TELEGRAM_API"         # Telegram returned an error
C_PARSING = "PARSING"                   # our parser/normalizer
C_MEDIA = "MEDIA"                       # download / upload / integrity
C_DELIVERY = "DELIVERY"                 # forward/copy/send execution
C_PIPELINE = "PIPELINE"                 # queue, db, concurrency, dispatch
C_CONFIGURATION = "CONFIGURATION"       # task setup, isolation
C_INFRASTRUCTURE = "INFRASTRUCTURE"     # network, process, transport
C_UNKNOWN = "UNKNOWN_INTERNAL_ERROR"

CATEGORIES = (C_TELEGRAM_API, C_PARSING, C_MEDIA, C_DELIVERY, C_PIPELINE,
              C_CONFIGURATION, C_INFRASTRUCTURE, C_UNKNOWN)

# Where the error originated — the mission's hardest requirement: never
# confuse "Telegram said no" with "we broke".
ORIGIN_TELEGRAM = "TELEGRAM"            # an RPC/transport error from Telegram
ORIGIN_INTERNAL = "INTERNAL"            # our own code raised
ORIGIN_POLICY = "POLICY"                # our rules decided to reject
ORIGIN_UNKNOWN = "UNKNOWN"

# --------------------------------------------------------------------------- #
# Stable reasons — the "why" of a failure, machine-readable
# --------------------------------------------------------------------------- #
R_TELEGRAM_RPC_ERROR = "TELEGRAM_RPC_ERROR"
R_TELEGRAM_PERMISSION_DENIED = "TELEGRAM_PERMISSION_DENIED"
R_TELEGRAM_PEER_INVALID = "TELEGRAM_PEER_INVALID"
R_TELEGRAM_CHANNEL_PRIVATE = "TELEGRAM_CHANNEL_PRIVATE"
R_TELEGRAM_CHANNEL_ACCESS_DENIED = "TELEGRAM_CHANNEL_ACCESS_DENIED"
R_TELEGRAM_MESSAGE_NOT_FOUND = "TELEGRAM_MESSAGE_NOT_FOUND"
R_TELEGRAM_FORWARDS_RESTRICTED = "TELEGRAM_FORWARDS_RESTRICTED"
R_TELEGRAM_MEDIA_RESTRICTED = "TELEGRAM_MEDIA_RESTRICTED"
R_TELEGRAM_FLOOD_WAIT = "TELEGRAM_FLOOD_WAIT"
R_TELEGRAM_RATE_LIMIT = "TELEGRAM_RATE_LIMIT"
R_TELEGRAM_SESSION_INVALID = "TELEGRAM_SESSION_INVALID"
R_TELEGRAM_AUTH_FAILED = "TELEGRAM_AUTH_FAILED"
R_TELEGRAM_FILE_REFERENCE_EXPIRED = "TELEGRAM_FILE_REFERENCE_EXPIRED"
R_TELEGRAM_FILE_PART_MISSING = "TELEGRAM_FILE_PART_MISSING"
R_TELEGRAM_ENTITY_INVALID = "TELEGRAM_ENTITY_INVALID"
R_TELEGRAM_UNKNOWN_RPC_ERROR = "TELEGRAM_UNKNOWN_RPC_ERROR"

R_PARSE_FAILED = "PARSE_FAILED"
R_MESSAGE_NORMALIZATION_FAILED = "MESSAGE_NORMALIZATION_FAILED"
R_ENTITY_EXTRACTION_FAILED = "ENTITY_EXTRACTION_FAILED"
R_CAPTURE_EXTRACTION_FAILED = "CAPTION_EXTRACTION_FAILED"
R_TEXT_ENCODING_ERROR = "TEXT_ENCODING_ERROR"
R_ALBUM_GROUPING_FAILED = "ALBUM_GROUPING_FAILED"
R_MESSAGE_TYPE_UNKNOWN = "MESSAGE_TYPE_UNKNOWN"
R_MEDIA_METADATA_INVALID = "MEDIA_METADATA_INVALID"
R_CONTENT_FIDELITY_MISMATCH = "CONTENT_FIDELITY_MISMATCH"
R_PARSER_VERSION_MISMATCH = "PARSER_VERSION_MISMATCH"

R_MEDIA_CLASSIFICATION_FAILED = "MEDIA_CLASSIFICATION_FAILED"
R_MEDIA_DOWNLOAD_FAILED = "MEDIA_DOWNLOAD_FAILED"
R_MEDIA_DOWNLOAD_TIMEOUT = "MEDIA_DOWNLOAD_TIMEOUT"
R_MEDIA_DOWNLOAD_INCOMPLETE = "MEDIA_DOWNLOAD_INCOMPLETE"
R_MEDIA_FILE_CORRUPTED = "MEDIA_FILE_CORRUPTED"
R_MEDIA_SIZE_LIMIT_EXCEEDED = "MEDIA_SIZE_LIMIT_EXCEEDED"
R_MEDIA_TYPE_UNSUPPORTED = "MEDIA_TYPE_UNSUPPORTED"
R_MEDIA_UPLOAD_FAILED = "MEDIA_UPLOAD_FAILED"
R_MEDIA_UPLOAD_TIMEOUT = "MEDIA_UPLOAD_TIMEOUT"
R_MEDIA_REUPLOAD_FAILED = "MEDIA_REUPLOAD_FAILED"
R_MEDIA_TEMP_STORAGE_FAILED = "MEDIA_TEMP_STORAGE_FAILED"

R_NATIVE_FORWARD_FAILED = "NATIVE_FORWARD_FAILED"
R_COPY_OPERATION_FAILED = "COPY_OPERATION_FAILED"
R_SEND_MESSAGE_FAILED = "SEND_MESSAGE_FAILED"
R_SEND_MEDIA_FAILED = "SEND_MEDIA_FAILED"
R_ALBUM_DELIVERY_FAILED = "ALBUM_DELIVERY_FAILED"
R_PARTIAL_ALBUM_DELIVERY = "PARTIAL_ALBUM_DELIVERY"
R_TARGET_ACCESS_DENIED = "TARGET_ACCESS_DENIED"
R_TARGET_REJECTED_CONTENT = "TARGET_REJECTED_CONTENT"
R_DELIVERY_MAPPING_FAILED = "DELIVERY_MAPPING_FAILED"
R_DELIVERY_DUPLICATE_DETECTED = "DELIVERY_DUPLICATE_DETECTED"
R_DELIVERY_OUTCOME_UNKNOWN = "DELIVERY_OUTCOME_UNKNOWN"

R_INGESTION_FAILED = "INGESTION_FAILED"
R_INGESTION_GAP_DETECTED = "INGESTION_GAP_DETECTED"
R_GAP_PERSISTENCE_FAILED = "GAP_PERSISTENCE_FAILED"
R_CURSOR_ADVANCEMENT_BLOCKED = "CURSOR_ADVANCEMENT_BLOCKED"
R_DISPATCH_FAILED = "DISPATCH_FAILED"
R_QUEUE_ENQUEUE_FAILED = "QUEUE_ENQUEUE_FAILED"
R_JOB_LEASE_EXPIRED = "JOB_LEASE_EXPIRED"
R_JOB_TIMEOUT = "JOB_TIMEOUT"
R_WORKER_CRASHED = "WORKER_CRASHED"
R_DATABASE_READ_FAILED = "DATABASE_READ_FAILED"
R_DATABASE_WRITE_FAILED = "DATABASE_WRITE_FAILED"
R_GRPC_COMMUNICATION_FAILED = "GRPC_COMMUNICATION_FAILED"
R_NETWORK_CONNECTION_FAILED = "NETWORK_CONNECTION_FAILED"
R_CONFIG_INVALID = "CONFIGURATION_INVALID"
R_TASK_ISOLATION_VIOLATION = "TASK_ISOLATION_VIOLATION"

# --------------------------------------------------------------------------- #
# Delivery strategies — for the fallback trail (mission §6)
# --------------------------------------------------------------------------- #
S_NATIVE_FORWARD = "NATIVE_FORWARD"
S_COPY = "COPY_MESSAGE"
S_REUPLOAD = "DOWNLOAD_REUPLOAD"
S_TEXT_RECONSTRUCT = "TEXT_RECONSTRUCT"
S_NONE = "NONE"


# --------------------------------------------------------------------------- #
# The canonical diagnostic event
# --------------------------------------------------------------------------- #
@dataclass
class DiagnosticEvent:
    """One structured outcome, from any stage of the message lifecycle.

    The raw layer (``raw_*`` fields) is Telegram's own answer, carried
    verbatim from ``errors.TelegramErrorEvent``. The normalized layer
    (``category``/``reason``) is our stable, stage-aware classification.
    Both are always present; neither may overwrite the other.

    Identifiers are deliberately *scoped*: no field is ever a bare message id,
    because Telegram ids are only unique within a chat. Every id travels with
    its chat. No text content, caption or session material is ever carried.
    """

    # --- identity ---------------------------------------------------------- #
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = 1
    event_name: str = ""                 # e.g. delivery.telegram_api.failed
    timestamp: float = field(default_factory=lambda: time.time())

    # --- correlation ------------------------------------------------------- #
    trace_id: str = ""
    correlation_id: str = ""
    task_id: str = ""
    owner_id: int = 0
    job_id: str = ""
    delivery_attempt_id: str = ""
    worker_id: str = ""

    # --- source and destination (scoped ids, never bare) ------------------- #
    source_peer_id: str = ""
    source_message_id: int = 0
    target_peer_id: str = ""
    destination_message_id: int = 0
    album_id: str = ""

    # --- pipeline context -------------------------------------------------- #
    stage: str = ""                      # one of STAGES
    strategy: str = S_NONE               # one of S_*
    attempt: int = 0

    # --- the normalized layer ---------------------------------------------- #
    error_origin: str = ORIGIN_UNKNOWN   # TELEGRAM | INTERNAL | POLICY | UNKNOWN
    category: str = C_UNKNOWN
    reason: str = ""                     # one of R_*
    retryable: bool = False
    fallback_eligible: bool = False
    severity: str = "ERROR"              # INFO | WARN | ERROR | CRITICAL

    # --- the raw layer: Telegram's own answer, verbatim -------------------- #
    raw_exception_class: str = ""
    raw_rpc_code: Optional[str] = None   # None = the library exposes none
    raw_rpc_message: str = ""
    raw_rpc_id: str = ""                 # e.g. "FLOOD_WAIT_X" / "CHAT_FORWARDS_RESTRICTED"
    raw_library: str = ""                # detected at runtime
    raw_method: str = ""                 # e.g. "messages.forwardMessages"
    raw_retry_after: Optional[int] = None

    # --- parser context (stage == PARSE only) ------------------------------ #
    parser_name: str = ""
    parser_version: str = ""
    media_type: str = ""
    entity_count: Optional[int] = None
    text_length: Optional[int] = None

    # --- explanation ------------------------------------------------------- #
    explanation: str = ""                # operator-facing, redacted, no secrets

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False,
                          separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------- #
# Builder — the bridge between a raw Telegram error and the canonical event
# --------------------------------------------------------------------------- #
def library_label(client: Any = None) -> str:
    """The Telegram client in use, detected at runtime — never hardcoded."""
    if client is not None:
        name = type(client).__name__
        if name and name != "NoneType":
            return name
    try:
        import pyrogram
        return f"pyrogram/{getattr(pyrogram, '__version__', 'unknown')}"
    except Exception:
        return "pyrogram/unknown"


def strategy_rpc_method(strategy: str) -> str:
    """The Telegram RPC a delivery strategy issues.

    Kept in one place so the raw ``method`` field on every send diagnostic
    names the actual call — an operator reading the log sees
    ``messages.forwardMessages``, not an internal label.
    """
    return {
        S_NATIVE_FORWARD: "messages.forwardMessages",
        S_COPY: "messages.copyMessage",
        S_REUPLOAD: "messages.sendMedia",
        S_TEXT_RECONSTRUCT: "messages.sendMessage",
    }.get(strategy, "messages.sendMessage")


def strategy_of(forward_mode: str, content_mode: str = "", has_media: bool = False) -> str:
    """The delivery strategy a job is executing, for the diagnostic trail.

    Resolved from the job's *snapshotted* payload (never the live rule), so
    the diagnostic names the strategy that was actually in force when the
    Telegram call was made — the same contract `test_job_config_snapshot`
    pins for the rest of the worker.
    """
    mode = str(forward_mode or "").strip().upper()
    content = str(content_mode or "").strip().upper()
    if mode == "DIRECT_FORWARD":
        return S_NATIVE_FORWARD
    if content == "TEXT_ONLY" or not has_media:
        return S_TEXT_RECONSTRUCT if content == "TEXT_ONLY" else S_COPY
    return S_COPY


def reason_for(category: str, tg_event: Any = None, fallback_decision: Any = None) -> str:
    """Derive the stable *reason* from the category and the raw Telegram event.

    Never invents: when the raw event is absent the reason is the category's
    generic form; when it is present the mapping is deterministic, so the same
    Telegram error always yields the same reason.
    """
    if category != C_TELEGRAM_API or tg_event is None:
        return _REASON_BY_CATEGORY.get(category, category)
    code = str(getattr(tg_event, "error_type", "") or "").upper()
    retryable = bool(getattr(tg_event, "retryable", False))
    if "FLOOD_WAIT" in code or "SLOWMODE" in code:
        return R_TELEGRAM_FLOOD_WAIT
    if "FORWARD" in code:
        return R_TELEGRAM_FORWARDS_RESTRICTED
    if "CHANNEL_PRIVATE" in code or "CHANNEL_INVALID" in code:
        return R_TELEGRAM_CHANNEL_PRIVATE
    if "CHAT_ADMIN" in code or "WRITE_FORBIDDEN" in code or "FORBIDDEN" in code:
        return R_TELEGRAM_PERMISSION_DENIED
    if "PEER" in code or "CHAT_ID_INVALID" in code or "USERNAME_INVALID" in code:
        return R_TELEGRAM_PEER_INVALID
    if "FILE_REFERENCE" in code:
        return R_TELEGRAM_FILE_REFERENCE_EXPIRED
    if "FILE_PART" in code:
        return R_TELEGRAM_FILE_PART_MISSING
    if "ENTITY" in code:
        return R_TELEGRAM_ENTITY_INVALID
    if "MEDIA" in code and ("RESTRICT" in code or "FORBIDDEN" in code):
        return R_TELEGRAM_MEDIA_RESTRICTED
    if "AUTH" in code or "SESSION" in code or "UNAUTHORIZED" in code:
        return R_TELEGRAM_SESSION_INVALID
    if "MESSAGE_ID" in code or "MSG_ID" in code:
        return R_TELEGRAM_MESSAGE_NOT_FOUND
    if retryable:
        return R_TELEGRAM_RATE_LIMIT
    return R_TELEGRAM_UNKNOWN_RPC_ERROR


#: The generic reason for a non-Telegram category.
_REASON_BY_CATEGORY = {
    C_PARSING: R_PARSE_FAILED,
    C_MEDIA: R_MEDIA_DOWNLOAD_FAILED,
    C_DELIVERY: R_NATIVE_FORWARD_FAILED,
    C_PIPELINE: R_DISPATCH_FAILED,
    C_CONFIGURATION: R_CONFIG_INVALID,
    C_INFRASTRUCTURE: R_NETWORK_CONNECTION_FAILED,
    C_UNKNOWN: "UNKNOWN_INTERNAL_ERROR",
}


def build_diagnostic_event(
    *,
    # identity / correlation
    event_name: str,
    stage: str,
    task_id: str = "",
    trace_id: str = "",
    correlation_id: str = "",
    owner_id: int = 0,
    job_id: str = "",
    delivery_attempt_id: str = "",
    worker_id: str = "",
    # message
    source_peer_id: str = "",
    source_message_id: int = 0,
    target_peer_id: str = "",
    destination_message_id: int = 0,
    album_id: str = "",
    attempt: int = 0,
    strategy: str = S_NONE,
    # classification
    category: str,
    reason: str = "",
    error_origin: str = ORIGIN_UNKNOWN,
    retryable: bool = False,
    fallback_eligible: bool = False,
    severity: str = "ERROR",
    # the raw Telegram layer
    tg_event: Any = None,
    raw_exception: Optional[BaseException] = None,
    raw_method: str = "",
    # parser context
    parser_name: str = "",
    parser_version: str = "",
    media_type: str = "",
    entity_count: Optional[int] = None,
    text_length: Optional[int] = None,
    # misc
    explanation: str = "",
    client: Any = None,
) -> DiagnosticEvent:
    """Build one canonical event from a raw Telegram error or an internal one.

    Exactly one of ``tg_event`` (an already-extracted
    ``errors.TelegramErrorEvent``) or ``raw_exception`` (a raised exception
    that has not been classified yet) should carry the raw layer. When neither
    is given the event is an internal one — still fully valid, with the origin
    marked ``INTERNAL``, never silently successful.
    """
    raw = tg_event
    if raw is None and raw_exception is not None:
        from .errors import extract as _extract
        try:
            raw = _extract(raw_exception, method=raw_method, chat_id=source_peer_id,
                           rule_id=task_id, attempt=attempt,
                           correlation_id=correlation_id)
        except Exception:  # diagnostics must never fail the operation
            raw = None

    exc_class = ""
    rpc_code: Optional[str] = None
    rpc_message = ""
    rpc_id = ""
    retry_after: Optional[int] = None
    origin = error_origin
    method = raw_method

    if raw is not None:
        exc_class = str(getattr(raw, "error_type", "") or "") or type(raw_exception or "").__name__
        rpc_id = str(getattr(raw, "error_type", "") or "")
        # A Telegram event exists: the error came from Telegram.
        # ``error_code`` on the event is the bucket as a *string*; preserve
        # Telegram's numeric code and stay null when the library gives none.
        code_val = getattr(raw, "error_code", None)
        rpc_code = str(code_val) if str(code_val or "") != "" else None
        from .errors import redact as _redact_msg
        rpc_message = _redact_msg(
            str(getattr(raw, "error_message", "") or ""), keep_phone=True
        )
        wait = getattr(raw, "retry_after", None)
        retry_after = int(wait) if wait else None
        if not method:
            method = str(getattr(raw, "method", "") or "")
        if origin == ORIGIN_UNKNOWN:
            origin = ORIGIN_TELEGRAM
    elif raw_exception is not None:
        exc_class = type(raw_exception).__name__
        from .errors import redact as _redact_raw
        rpc_message = _redact_raw(str(raw_exception), keep_phone=True)
        if origin == ORIGIN_UNKNOWN:
            # A raised exception with no Telegram event behind it is, by
            # construction, our own code failing — never "unknown".
            origin = ORIGIN_INTERNAL
    else:
        # No Telegram answer and no exception: this event describes something
        # *we* decided or did (a policy rejection, a pipeline step), which is
        # an internal origin — not an absence of knowledge.
        if origin == ORIGIN_UNKNOWN:
            origin = ORIGIN_INTERNAL

    final_reason = reason or reason_for(category, raw)
    if not retryable and raw is not None:
        retryable = bool(getattr(raw, "retryable", False))

    # The explanation is free text derived from the raw error string, so it
    # goes through the same redaction gate as the raw message itself — a
    # credential or a private sentence must never ride out on this field.
    from .errors import redact as _redact
    safe_explanation = _redact(str(explanation or ""), keep_phone=True)[:1000]
    # The raw message is the exception's own text: it is preserved verbatim in
    # *shape* (so an operator sees Telegram's answer) but never in a way that
    # can carry a credential or private sentence past this boundary.
    rpc_message = _redact(str(rpc_message or ""), keep_phone=True)[:1000]

    return DiagnosticEvent(
        event_name=event_name, stage=stage, task_id=task_id, trace_id=trace_id,
        correlation_id=correlation_id, owner_id=owner_id, job_id=job_id,
        delivery_attempt_id=delivery_attempt_id, worker_id=worker_id,
        source_peer_id=source_peer_id, source_message_id=source_message_id,
        target_peer_id=target_peer_id, destination_message_id=destination_message_id,
        album_id=album_id, attempt=attempt, strategy=strategy,
        error_origin=origin, category=category, reason=final_reason,
        retryable=retryable, fallback_eligible=fallback_eligible, severity=severity,
        raw_exception_class=exc_class, raw_rpc_code=rpc_code,
        raw_rpc_message=rpc_message[:1000], raw_rpc_id=rpc_id,
        raw_library=library_label(client) if raw is not None else "",
        raw_method=method, raw_retry_after=retry_after,
        parser_name=parser_name, parser_version=parser_version,
        media_type=media_type, entity_count=entity_count, text_length=text_length,
        explanation=safe_explanation,
    )



# --------------------------------------------------------------------------- #
# The Rust-logger bridge.
#
# The mission's §10 contract says the Rust monitoring system must *consume and
# display* the canonical events — not run a second store of them. So this
# projects one DiagnosticEvent onto the existing LogClient record (service,
# category, message, detail, rule_id, user_id, correlation_id, trace_id), which
# is the transport the Rust logger already owns. The authoritative per-attempt
# record stays in diagnostic_events; this is the operational stream the dashboards
# already read, so a failure is visible in both places without duplicating the
# business state.
# --------------------------------------------------------------------------- #


def log_diagnostic_event(log_client: Any, event: "DiagnosticEvent") -> None:
    """Fan one canonical event out to the Rust logger, best effort.

    The logger is bounded and counts what it drops, so an outage degrades to a
    missing stream entry — never to a missed delivery or a lost diagnostic row
    (that row lives in ``diagnostic_events`` independently).
    """
    if log_client is None:
        return
    try:
        level = {
            "INFO": "info", "WARN": "warn", "ERROR": "error",
            "CRITICAL": "error",
        }.get(str(event.severity or "INFO").upper(), "info")
        detail = {
            "event_name": event.event_name,
            "stage": event.stage,
            "strategy": event.strategy,
            "attempt": event.attempt,
            "error_origin": event.error_origin,
            "category": event.category,
            "reason": event.reason,
            "retryable": bool(event.retryable),
            "fallback_eligible": bool(event.fallback_eligible),
            # The raw layer, verbatim: an operator grepping the Rust stream sees
            # Telegram's own answer, not a paraphrase.
            "raw_exception_class": event.raw_exception_class,
            "raw_rpc_code": event.raw_rpc_code,
            "raw_rpc_id": event.raw_rpc_id,
            "raw_retry_after": event.raw_retry_after,
            "raw_method": event.raw_method,
            "source_peer_id": event.source_peer_id,
            "source_message_id": event.source_message_id,
            "target_peer_id": event.target_peer_id,
            "job_id": event.job_id,
            "worker_id": event.worker_id,
            # The v19 structured-context column the Rust logger stores.
            "trace_id": str(event.trace_id or ""),
            "explanation": event.explanation,
        }
        getattr(log_client, level, log_client.info)(
            "diagnostics", str(event.category or "UNKNOWN"),
            str(event.event_name or "diagnostic.event"),
            detail,
            rule_id=str(event.task_id or ""),
            user_id=int(event.owner_id or 0),
            correlation_id=str(event.correlation_id or event.trace_id or ""),
        )
    except Exception:
        # Best effort by construction: the diagnostic row is the source of
        # truth, and a logger hiccup must never break the delivery path.
        pass


__all__ = [
    "DiagnosticEvent", "build_diagnostic_event", "reason_for", "library_label",
    "log_diagnostic_event",
    "STAGES", "CATEGORIES", "ST_INGEST", "ST_PARSE", "ST_MEDIA", "ST_ROUTE",
    "ST_ENQUEUE", "ST_CLAIM", "ST_SEND", "ST_FALLBACK", "ST_SYNC", "ST_RECONCILE",
    "C_TELEGRAM_API", "C_PARSING", "C_MEDIA", "C_DELIVERY", "C_PIPELINE",
    "C_CONFIGURATION", "C_INFRASTRUCTURE", "C_UNKNOWN",
    "ORIGIN_TELEGRAM", "ORIGIN_INTERNAL", "ORIGIN_POLICY", "ORIGIN_UNKNOWN",
    "S_NATIVE_FORWARD", "S_COPY", "S_REUPLOAD", "S_TEXT_RECONSTRUCT", "S_NONE",
]
