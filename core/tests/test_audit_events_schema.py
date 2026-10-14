"""Tests for the centralized audit event schema (mission Phase Three/§3).

Covers: required fields, severity normalization, lifecycle states, event_id
uniqueness, redaction of seeded secrets, bounded metadata, payload limits,
invalid-event rejection, and unknown-field forward compatibility.
"""

import json
import time
import uuid

import pytest

from core.infrastructure.persistence.audit_events import (
    FAILED,
    LIFECYCLE_STATES,
    OUTCOME_UNCONFIRMED,
    REASON_CODES,
    RECEIVED,
    SUCCEEDED,
    AuditEvent,
    SCHEMA_VERSION,
    VALID_SEVERITY,
    MAX_METADATA_BYTES,
    MAX_METADATA_FIELDS,
    redact_value,
)


# --------------------------------------------------------------------------- #
def _evt(**kw):
    kw.setdefault("event_type", "test.event")
    kw.setdefault("service", "core")
    kw.setdefault("outcome", SUCCEEDED)
    return AuditEvent(**kw)


def test_valid_event_has_no_violations():
    e = _evt()
    assert e.validate() == []
    assert e.schema_version == SCHEMA_VERSION
    assert e.event_id  # generated
    assert e.ts_utc > 0


def test_required_fields_missing_rejected():
    e = AuditEvent(event_type="", service="", outcome=SUCCEEDED)
    errs = e.validate()
    assert any("event_type" in x for x in errs)
    assert any("service" in x for x in errs)


def test_invalid_outcome_downgraded_to_unconfirmed():
    e = _evt(outcome="MAGIC_SUCCESS")
    assert e.outcome == OUTCOME_UNCONFIRMED
    # After normalization the event is schema-valid; the original invalid
    # value is deliberately not preserved (fail-safe: unconfirmed ≠ success).


def test_severity_normalization():
    assert _evt(severity="WARN").severity == "WARN"
    assert _evt(severity="warn").severity == "WARN"
    assert _evt(severity="DEBUG").severity == "DEBUG"
    assert _evt(severity="nonsense").severity == "INFO"


def test_event_id_unique():
    ids = {_evt().event_id for _ in range(200)}
    assert len(ids) == 200


def test_lifecycle_states_registry():
    assert "SUCCEEDED" in LIFECYCLE_STATES
    assert "OUTCOME_UNCONFIRMED" in LIFECYCLE_STATES
    assert "FAILED" in LIFECYCLE_STATES


# --------------------------- redaction ------------------------------------- #
def test_seeded_secret_values_redacted_in_metadata():
    e = _evt(metadata={
        "bot_token": "123456:ABCDEF_secret",
        "session_string": "ENCRYPTED_SESSION_BLOB",
        "api_hash": "a" * 32,
        "safe_field": "plain text",
    })
    md = e.metadata
    assert md["bot_token"] == "[REDACTED]"
    assert md["session_string"] == "[REDACTED]"
    assert md["api_hash"] == "[REDACTED]"
    assert md["safe_field"] == "plain text"


def test_secret_hidden_in_json_serialization():
    e = _evt(metadata={"password": "hunter2", "note": "ok"}, result_summary="token=abcdef12345")
    blob = e.to_json()
    assert "hunter2" not in blob
    assert "abcdef12345" not in blob
    assert "ok" in blob


def test_long_hex_and_phone_redacted():
    assert "[REDACTED]" in redact_value("blob " + "a" * 40)
    assert "[PHONE]" in redact_value("call +15551234567 now")


def test_long_hex_redacted_inside_result_summary():
    e = _evt(result_summary="hash " + "b" * 40)
    assert "b" * 40 not in e.result_summary


# --------------------------- bounds --------------------------------------- #
def test_metadata_field_count_bounded():
    big = {f"k{i}": i for i in range(200)}
    e = _evt(metadata=big)
    assert len(e.metadata) <= MAX_METADATA_FIELDS


def test_metadata_payload_size_bounded():
    huge = {"blob": "x" * 100000}
    e = _evt(metadata=huge)
    assert len(json.dumps(e.metadata)) <= MAX_METADATA_BYTES + 64


def test_result_summary_truncated():
    e = _evt(result_summary="y" * 50000)
    assert len(e.result_summary) <= 2000


def test_non_scalar_metadata_summarized_not_stored():
    e = _evt(metadata={"nested": {"secret": "deep"}})
    assert e.metadata["nested"].startswith("<")


# --------------------------- reason codes --------------------------------- #
def test_registered_reason_codes_accepted():
    for code in ("RULE_PAUSED", "FILTER_REJECTED", "QUEUE_ENQUEUE_FAILED"):
        e = _evt(reason_code=code)
        assert e.validate() == []


def test_unknown_reason_code_flagged_not_dropped():
    e = _evt(reason_code="TOTALLY_NEW_CODE")
    assert e.metadata.get("reason_code_unregistered") == "TOTALLY_NEW_CODE"


def test_reason_registry_has_core_codes():
    for code in ("RULE_NOT_MATCHED", "DUPLICATE_DETECTED", "EXTERNAL_RESULT_UNCONFIRMED",
                 "AUTHORIZATION_DENIED", "DATABASE_OPERATION_FAILED"):
        assert code in REASON_CODES


# --------------------------- forward compatibility ------------------------ #
def test_unknown_optional_fields_do_not_break_ingestion_shape():
    """A v1 event with extra future fields must still serialize cleanly."""
    e = _evt()
    d = e.to_dict()
    d["future_field_nobody_knows"] = "x"  # simulates newer producer
    json.dumps(d)  # must not raise
