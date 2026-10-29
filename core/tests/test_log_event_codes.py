"""Stable event codes on the central log stream.

The operator contract: every failure class the engine emits carries one
deterministic code (``DELIVERY_BLOCKED``, ``FALLBACK_COPY_USED``,
``DELIVERY_TIMEOUT``, …) that survives into the stored entry — in the detail
JSON *and* prefixed on the message — so the Log Center's search answers
"show me every X" without grepping free text.
"""

from __future__ import annotations

import json
import logging

from core.observability.log_client import CentralLogHandler, LogClient


def _client() -> LogClient:
    # No start(): log() only enqueues, so no network is touched.
    return LogClient(address="127.0.0.1:1", max_queue_size=8)


def _queued(client: LogClient):
    return client._queue.get_nowait()


def test_event_code_prefixes_message_and_detail():
    client = _client()
    client.log("ERROR", "queue", "forward", "delivery blocked", {"rule_id": "r-1"},
               event_code="DELIVERY_BLOCKED")
    entry = _queued(client)
    assert entry.message.startswith("[EVT:DELIVERY_BLOCKED] ")
    assert json.loads(entry.detail)["event_code"] == "DELIVERY_BLOCKED"


def test_event_code_promoted_from_detail():
    """Existing call sites that already write event_code into their detail
    blob get the same prefix without a signature change."""
    client = _client()
    client.log("ERROR", "queue", "forward", "boom", {"event_code": "SEND_ERROR"})
    entry = _queued(client)
    assert "[EVT:SEND_ERROR]" in entry.message
    # the detail blob keeps the code too — both storage paths agree
    assert json.loads(entry.detail)["event_code"] == "SEND_ERROR"


def test_event_code_without_detail():
    client = _client()
    client.log("WARN", "queue", "forward", "no detail here", None, event_code="RATE_LIMITED")
    entry = _queued(client)
    assert entry.message.startswith("[EVT:RATE_LIMITED]")
    assert json.loads(entry.detail) == {"event_code": "RATE_LIMITED"}


def test_no_code_no_prefix():
    """Call sites that pass no code keep byte-identical messages."""
    client = _client()
    client.log("INFO", "queue", "forward", "delivered rule=abc 1:2 → 3")
    entry = _queued(client)
    assert entry.message == "delivered rule=abc 1:2 → 3"
    assert entry.detail == ""


def test_message_not_double_prefixed():
    client = _client()
    client.log("ERROR", "queue", "forward", "[EVT:X] already coded", None, event_code="X")
    entry = _queued(client)
    assert entry.message == "[EVT:X] already coded"


def test_central_handler_forwards_extra_event_code():
    """logger.error(..., extra={"event_code": ...}) reaches the entry."""
    client = _client()
    handler = CentralLogHandler(client)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("atf.durable_queue").error(
            "[PIPELINE:ACCOUNT_BLOCKED] session gone",
            extra={"event_code": "ACCOUNT_BLOCKED"},
        )
    finally:
        root.removeHandler(handler)
    entry = _queued(client)
    assert "[EVT:ACCOUNT_BLOCKED]" in entry.message


def test_stdlib_warning_mirrors_without_code():
    """Ordinary warnings keep their shape (no code, no prefix)."""
    client = _client()
    handler = CentralLogHandler(client)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("atf.durable_queue").warning("ordinary warning")
    finally:
        root.removeHandler(handler)
    entry = _queued(client)
    assert entry.message == "ordinary warning"
