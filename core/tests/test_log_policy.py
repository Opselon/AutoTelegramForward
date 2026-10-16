"""Producer-side log policy: the documented defaults must actually hold.

The contract says INFO/SUCCESS are not collected by default while WARN and above
are, that DEBUG/TRACE need explicit enablement, that a diagnostic mode expires on
its own, that verbose output is rate-limited, and that security-relevant events
are never suppressed by a severity threshold. Each of those is a test here,
because "the Logger filters it" is not true of the Python engine.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.application.log_policy import (  # noqa: E402
    LogPolicy,
    is_security_relevant,
    normalize_level,
)


def _run(coro):
    """Run a coroutine without clearing the thread's event loop (asyncio.run
    would, breaking modules that get a loop at import time)."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class FakeClock:
    """A clock we control, so testing an expiry does not mean sleeping."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------- #
# level normalization
# --------------------------------------------------------------------------- #
def test_legacy_warning_maps_to_warn():
    assert normalize_level("warning") == "WARN"
    assert normalize_level("WARNING") == "WARN"
    assert normalize_level("Warn") == "WARN"


def test_unknown_levels_are_treated_as_info_never_upgraded():
    assert normalize_level("banana") == "INFO"
    assert normalize_level("") == "INFO"
    assert normalize_level(None) == "INFO"


# --------------------------------------------------------------------------- #
# the documented defaults
# --------------------------------------------------------------------------- #
def test_warn_error_critical_are_collected_by_default():
    policy = LogPolicy()
    for level in ("WARN", "ERROR", "CRITICAL"):
        assert policy.allow(level), f"{level} must be collected by default"


def test_info_success_debug_trace_are_not_collected_by_default():
    policy = LogPolicy()
    for level in ("INFO", "SUCCESS", "DEBUG", "TRACE"):
        assert not policy.allow(level), f"{level} must be opted into"
    assert policy.snapshot()["suppressed_level"] == 4


def test_suppression_is_counted_not_silent():
    """'Collection is off' must not look like 'nothing happened'."""
    policy = LogPolicy()
    for _ in range(5):
        policy.allow("INFO")
    snapshot = policy.snapshot()
    assert snapshot["suppressed_level"] == 5
    assert snapshot["emitted"] == 0
    assert snapshot["levels"] == ["WARN", "ERROR", "CRITICAL"]


# --------------------------------------------------------------------------- #
# security exemption
# --------------------------------------------------------------------------- #
def test_authorization_failures_survive_a_high_threshold():
    policy = LogPolicy(levels=("CRITICAL",))  # threshold raised to the top
    assert policy.allow("INFO", event_type="auth.failed", reason_code="UNAUTHORIZED")
    assert policy.allow("INFO", event_type="forward.filtered", reason_code="CROSS_TENANT")
    assert not policy.allow("INFO", event_type="forward.enqueued")


def test_security_events_are_exempt_from_the_rate_limit():
    policy = LogPolicy(levels=("INFO",), verbose_rate_per_second=1)
    for _ in range(10):
        assert policy.allow("INFO", event_type="tenant.escape", reason_code="TENANT_MISMATCH")
    assert policy.snapshot()["suppressed_rate"] == 0


def test_security_relevance_is_recognised_by_prefix_and_reason_code():
    assert is_security_relevant("auth.failed")
    assert is_security_relevant("", "PERMISSION_DENIED")
    assert not is_security_relevant("forward.enqueued", "OK")


# --------------------------------------------------------------------------- #
# diagnostic mode
# --------------------------------------------------------------------------- #
def test_diagnostic_mode_enables_verbose_levels_immediately():
    policy = LogPolicy()
    assert not policy.allow("DEBUG")
    policy.enable_diagnostics(60)
    assert policy.allow("DEBUG")
    assert policy.allow("INFO")
    assert policy.snapshot()["diagnostic_active"] is True


def test_diagnostic_mode_expires_on_its_own():
    clock = FakeClock()
    policy = LogPolicy(clock=clock)
    expiry = policy.enable_diagnostics(30)
    assert policy.allow("DEBUG")

    clock.advance(29)
    assert policy.allow("DEBUG")

    clock.advance(2)  # past the window
    assert not policy.allow("DEBUG"), "an expired diagnostic window must close itself"
    assert policy.snapshot()["diagnostic_active"] is False
    assert expiry > 0


def test_diagnostic_mode_can_be_closed_early():
    policy = LogPolicy()
    policy.enable_diagnostics(3600)
    assert policy.diagnostic_active()
    policy.disable_diagnostics()
    assert not policy.diagnostic_active()
    assert not policy.allow("INFO")


# --------------------------------------------------------------------------- #
# rate limiting of verbose events
# --------------------------------------------------------------------------- #
def test_verbose_events_are_rate_limited_per_second():
    clock = FakeClock()
    policy = LogPolicy(levels=("INFO",), verbose_rate_per_second=3, clock=clock)
    allowed = [policy.allow("INFO") for _ in range(6)]
    assert allowed == [True, True, True, False, False, False]
    assert policy.snapshot()["suppressed_rate"] == 3

    clock.advance(1.1)  # a new window opens
    assert policy.allow("INFO")


def test_the_rate_limit_never_touches_warn_or_above():
    """A WARN/ERROR storm is the signal, not the noise."""
    policy = LogPolicy(verbose_rate_per_second=1)
    for _ in range(50):
        assert policy.allow("ERROR")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_env_configures_levels_and_a_bounded_diagnostic_window():
    policy = LogPolicy.from_env({
        "ATF_LOG_LEVELS": "WARN,ERROR,CRITICAL,INFO",
        "ATF_LOG_VERBOSE_RATE": "7",
        "ATF_LOG_DIAGNOSTIC_SECONDS": "120",
    })
    assert policy.allow("INFO")
    snapshot = policy.snapshot()
    assert snapshot["verbose_rate_per_second"] == 7.0
    assert snapshot["diagnostic_active"] is True


def test_env_defaults_are_the_documented_defaults():
    snapshot = LogPolicy.from_env({}).snapshot()
    assert snapshot["configured_levels"] == ["WARN", "ERROR", "CRITICAL"]
    assert snapshot["diagnostic_active"] is False


def test_a_garbage_env_value_does_not_break_logging():
    policy = LogPolicy.from_env({
        "ATF_LOG_VERBOSE_RATE": "fast", "ATF_LOG_DIAGNOSTIC_SECONDS": "soon",
    })
    assert policy.allow("ERROR")
    assert not policy.allow("INFO")


# --------------------------------------------------------------------------- #
# the choke point: _audit must obey the policy, and must keep the audit trail
# --------------------------------------------------------------------------- #
class RecordingLogClient:
    def __init__(self) -> None:
        self.calls: list = []

    def log(self, level, service, category, message, detail=None, **kw) -> None:
        self.calls.append({"level": level, "message": message, "detail": detail, **kw})


class RecordingAuditRepo:
    def __init__(self) -> None:
        self.records: list = []

    async def record(self, event) -> None:
        self.records.append(event)


def _pipeline(log_client, audit_repo, policy):
    from core.application.pipeline import DurableMessagePipeline

    return DurableMessagePipeline(
        rule_repo=None, filter_repo=None,
        log_client=log_client, audit_repo=audit_repo, log_policy=policy,
    )


def test_audit_emits_an_info_event_only_when_the_policy_allows_it():
    async def go():
        client, repo = RecordingLogClient(), RecordingAuditRepo()
        pipeline = _pipeline(client, repo, LogPolicy())
        from core.application.pipeline import PipelineContext

        ctx = PipelineContext()
        await pipeline._audit(ctx, "forward.enqueued", "ACCEPTED")
        assert client.calls == [], "an INFO happy-path event was emitted by default"

        policy = LogPolicy()
        policy.enable_diagnostics(60)
        pipeline = _pipeline(client, repo, policy)
        await pipeline._audit(ctx, "forward.enqueued", "ACCEPTED")
        assert len(client.calls) == 1
        assert client.calls[0]["level"] == "INFO"

    _run(go())


def test_a_warn_event_is_emitted_and_carries_its_correlation_and_latency():
    async def go():
        client, repo = RecordingLogClient(), RecordingAuditRepo()
        pipeline = _pipeline(client, repo, LogPolicy())
        from core.application.pipeline import PipelineContext

        ctx = PipelineContext()
        await pipeline._audit(ctx, "forward.dropped", "FAILED", severity="ERROR",
                              reason_code="SEND_FAILED")
        assert len(client.calls) == 1, client.calls
        call = client.calls[0]
        assert call["level"] == "ERROR"
        assert call["correlation_id"] == ctx.correlation_id
        assert "duration_ms" in (call["detail"] or {}), call["detail"]

    _run(go())


def test_suppressing_log_verbosity_never_erases_the_durable_audit_trail():
    """Log verbosity and audit evidence are separate concerns; the gate is only
    allowed to govern the former."""

    async def go():
        client, repo = RecordingLogClient(), RecordingAuditRepo()
        pipeline = _pipeline(client, repo, LogPolicy())
        from core.application.pipeline import PipelineContext

        await pipeline._audit(PipelineContext(), "forward.enqueued", "ACCEPTED")
        assert client.calls == []
        assert len(repo.records) == 1, "the durable audit record must still be written"

    _run(go())


def test_the_policy_gate_is_recorded_so_operators_can_see_suppression():
    async def go():
        client, repo = RecordingLogClient(), RecordingAuditRepo()
        policy = LogPolicy()
        pipeline = _pipeline(client, repo, policy)
        from core.application.pipeline import PipelineContext

        for _ in range(3):
            await pipeline._audit(PipelineContext(), "forward.enqueued", "ACCEPTED")
        assert policy.snapshot()["suppressed_level"] == 3

    _run(go())
