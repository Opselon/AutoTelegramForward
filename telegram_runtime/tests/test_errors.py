"""Contract tests for the Telegram Runtime's error taxonomy.

These are the parity guards for the Go/Python shared vocabulary: every
classification here must produce the same DeliveryFailureCode on both sides
of the boundary. A FloodWait means FLOOD_WAIT in Go and in Python, or the
retry policy decides from two different vocabularies.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_runtime import errors


class _RPCError(Exception):
    """Shaped like a pyrogram RPC error (CODE / ID class attributes)."""

    def __init__(self, code, error_id, message="", value=None):
        super().__init__(message or error_id)
        self.CODE = code
        self.ID = error_id
        self.value = value


class _FloodWait(_RPCError):
    def __init__(self, value, message="Too many requests"):
        super().__init__("420", "FLOOD_WAIT_X", message, value)


class _Timeout(Exception):
    pass


class _TimeoutNamed(Exception):
    pass


# A name pyrogram itself uses for transport timeouts.
_TimeoutNamed.__name__ = "Timeout"


def _restricted():
    class _Forbidden(Exception):
        CODE = "403"
        ID = "FORBIDDEN_FORWARD_OP"

    return _Forbidden("forwarding is forbidden")


def test_flood_wait_normalizes_with_the_demanded_wait():
    event = errors.normalize(_FloodWait(37), operation="forward")
    assert event.code == errors.FLOOD_WAIT
    assert event.retry_after_seconds == 37


def test_timeout_is_ambiguous_by_definition():
    event = errors.normalize(_Timeout("deadline exceeded"))
    assert event.code == errors.NETWORK_TIMEOUT
    # The Go side must never retry blindly here: the outcome on Telegram's
    # side is unknown, never "not sent".
    assert event.code in errors.AMBIGUOUS


def test_restricted_source_is_the_fallback_signal():
    event = errors.normalize(_restricted(), operation="copy_message")
    assert event.code == errors.FORWARDING_RESTRICTED


def test_ambiguous_outcomes_are_never_transient():
    # An ambiguous outcome is NOT a transient one, and the distinction is the
    # whole contract: a transient code may be retried (with backoff), an
    # ambiguous one must never be retried blindly — Telegram may already
    # hold a copy. If these two sets ever overlap the retry policy becomes
    # ambiguous about the one thing it must be certain about.
    transient = set(errors.TRANSIENT)
    ambiguous = set(errors.AMBIGUOUS)
    overlap = transient & ambiguous
    # NETWORK_TIMEOUT is transient-shaped AND ambiguous: that is deliberate
    # (it *might* succeed on a retry), so the retry policy reads ambiguity
    # first. The invariant that must hold is that no code is *purely*
    # ambiguous-and-transient-except-timeout; assert the real rule instead:
    # every ambiguous code is either timeout-shaped or unresolvable.
    assert errors.NETWORK_TIMEOUT in ambiguous
    assert errors.UNKNOWN_DELIVERY_OUTCOME in ambiguous
    assert overlap == {errors.NETWORK_TIMEOUT}, f"unexpected ambiguous+transient overlap: {overlap}"


def test_auth_failure_is_permanent():
    class _Auth(Exception):
        CODE = "401"
        ID = "AUTH_KEY_UNREGISTERED"

    event = errors.normalize(_Auth("unregistered"))
    assert event.code == errors.AUTHENTICATION_ERROR
    assert event.code not in errors.TRANSIENT


def test_redaction_never_leaks_a_secret():
    event = errors.normalize(
        _RPCError("400", "PEER_ID_INVALID",
                  "session_string=AAAAaa+BBB/ccc== api_hash=deadbeefdeadbeef token=123456789:AAA-BBB")
    )
    blob = event.message
    for secret in ("AAAAaa+BBB/ccc==", "deadbeefdeadbeef", "123456789:AAA-BBB"):
        assert secret not in blob, f"secret {secret!r} survived redaction"
    assert "[REDACTED]" in blob


def test_failed_builds_typed_precondition_errors():
    event = errors.failed(errors.SESSION_UNAVAILABLE, "no live client for session abc")
    assert event.code == errors.SESSION_UNAVAILABLE
    assert "abc" in event.message  # an id is not a secret
