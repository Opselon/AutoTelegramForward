"""The Rust-logger fan-out: one canonical event, two surfaces.

Mission §10 requires the Rust monitoring system to *consume and display* the
canonical events — not to run a second store of them. `log_diagnostic_event`
projects a `DiagnosticEvent` onto the existing `LogClient` record, which is
the transport the Rust logger already owns. These tests pin that projection:

* the raw Telegram layer arrives in ``detail`` verbatim, so an operator
  grepping the Rust stream sees Telegram's own answer, not a paraphrase;
* the level mapping is honest — CRITICAL never gets quietly demoted to INFO;
* the attribution columns (rule_id, user_id, correlation_id) survive, so the
  log explorer can still answer "show me this task's history";
* a dead logger is survivable: the call is best effort and never raises into
  the delivery path.
"""

from __future__ import annotations

import pytest
from pyrogram.errors import ChatForwardsRestricted, FloodWait

from core.infrastructure.telegram.diagnostics_contract import (
    C_TELEGRAM_API, S_NATIVE_FORWARD, build_diagnostic_event,
    log_diagnostic_event,
)


class RecordingLogClient:
    """Captures what the Rust-logger transport would receive."""

    def __init__(self):
        self.calls = []

    def info(self, service, category, message, detail=None, **kw):
        self.calls.append(("INFO", service, category, message, detail, kw))

    def warn(self, service, category, message, detail=None, **kw):
        self.calls.append(("WARN", service, category, message, detail, kw))

    def error(self, service, category, message, detail=None, **kw):
        self.calls.append(("ERROR", service, category, message, detail, kw))


def _event(raw_exception=None, severity="WARN", **kw):
    params = dict(
        event_name="delivery.strategy.failed", stage="SEND",
        category=C_TELEGRAM_API, task_id="rule-1", owner_id=7,
        source_peer_id="-100SRC", source_message_id=900,
        target_peer_id="-100DST", attempt=2, strategy=S_NATIVE_FORWARD,
        severity=severity, raw_exception=raw_exception,
    )
    params.update(kw)
    return build_diagnostic_event(**params)


def test_the_raw_telegram_layer_arrives_verbatim():
    """The Rust stream sees Telegram's own answer, not a paraphrase."""
    sink = RecordingLogClient()
    log_diagnostic_event(sink, _event(
        raw_exception=ChatForwardsRestricted("400 CHAT_FORWARDS_RESTRICTED")))

    assert len(sink.calls) == 1
    level, service, category, message, detail, kw = sink.calls[0]
    assert detail["raw_rpc_id"] == "CHAT_FORWARDS_RESTRICTED"
    assert detail["raw_rpc_code"] == "400"
    assert detail["error_origin"] == "TELEGRAM"
    assert detail["category"] == C_TELEGRAM_API
    # The event name is the message — greppable, not a free-text sentence.
    assert message == "delivery.strategy.failed"


def test_a_floodwait_keeps_telegrams_wait_in_the_stream():
    """Retry-after survives the projection so monitoring can see it."""
    sink = RecordingLogClient()
    log_diagnostic_event(sink, _event(
        event_name="delivery.flood_wait.retrying", severity="WARN",
        raw_exception=FloodWait(45), retryable=True))

    _, _, _, _, detail, _ = sink.calls[0]
    assert detail["raw_retry_after"] == 45
    assert detail["retryable"] is True


def test_the_attribution_columns_survive():
    """The log explorer can still group by task, owner and correlation."""
    sink = RecordingLogClient()
    log_diagnostic_event(sink, _event(
        correlation_id="corr-42", trace_id="trace-7"))

    _, _, _, _, _, kw = sink.calls[0]
    assert kw["rule_id"] == "rule-1"
    assert kw["user_id"] == 7
    assert kw["correlation_id"] == "corr-42"
    # trace_id rides in detail (the v19 structured-context column).
    assert sink.calls[0][4]["trace_id"] == "trace-7"


def test_critical_is_not_demoted():
    """CRITICAL maps to the error channel, never to INFO."""
    sink = RecordingLogClient()
    log_diagnostic_event(sink, _event(severity="CRITICAL"))
    assert sink.calls[0][0] == "ERROR"


def test_a_dead_logger_is_survivable():
    """A raising logger never escapes into the delivery path."""
    class Exploding:
        def error(self, *a, **kw):
            raise RuntimeError("logger down")

    # Must not raise.
    log_diagnostic_event(Exploding(), _event(severity="ERROR"))
    # And None is accepted (the store-only wiring).
    log_diagnostic_event(None, _event())


def test_the_service_and_category_are_stable():
    """The Rust side groups by service/category; both must be stable."""
    sink = RecordingLogClient()
    log_diagnostic_event(sink, _event())
    _, service, category, _, _, _ = sink.calls[0]
    assert service == "diagnostics"
    assert category == C_TELEGRAM_API
