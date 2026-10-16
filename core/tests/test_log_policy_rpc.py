"""The core process must be able to answer for its own collection policy.

The control plane used to report atf-core's scope as unavailable because nothing
in the core exposed it, so an operator could only be told "configure it and
restart". These tests pin the RPC that closes that gap, and — more importantly —
that it reports what the process is *enforcing*: a policy that actually
suppresses an event must show the suppression, and a core with no policy must say
so rather than answering with zeros, which would read as "nothing suppressed".
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.application.log_policy import LogPolicy  # noqa: E402
from core.infrastructure.grpc_server import servicers  # noqa: E402
from core.proto import autoforward_pb2 as pb  # noqa: E402


def _run(coro):
    """Run a coroutine without clearing the thread's event loop."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _servicer(log_policy=None):
    # GetLogPolicy touches none of the other collaborators: it answers from the
    # policy object alone, which is exactly why it can be tested in isolation.
    return servicers.SystemStatusControlServicer(
        None, None, None, "", "test", log_policy=log_policy,
    )


def test_get_log_policy_answers_with_the_attached_policy():
    policy = LogPolicy.from_env({"ATF_LOG_LEVELS": "ERROR,WARN"})
    resp = _run(_servicer(policy).GetLogPolicy(pb.LogPolicyRequest(), None))

    assert resp.available is True
    assert resp.reason == ""
    # Core reports the levels it is really enforcing, and the configured set it
    # was given: the two are distinct so a diagnostic window can widen one
    # without rewriting what the operator configured.
    assert set(resp.effective_levels) == {"WARN", "ERROR"}
    assert set(resp.configured_levels) == {"WARN", "ERROR"}
    assert resp.diagnostic_active is False
    assert resp.emitted == 0


def test_get_log_policy_reports_the_suppression_it_enforced():
    # A policy that refuses INFO/SUCCESS by threshold. The counters must move, or
    # the control plane would show a quiet system while events are being dropped.
    policy = LogPolicy.from_env({"ATF_LOG_LEVELS": "WARN,ERROR"})
    assert policy.allow("INFO", "forward.progress") is False
    assert policy.allow("ERROR", "forward.failed") is True

    resp = _run(_servicer(policy).GetLogPolicy(pb.LogPolicyRequest(), None))

    assert resp.available is True
    assert resp.suppressed_level == 1, "a threshold suppression must be visible to the API"
    assert resp.emitted == 1


def test_get_log_policy_reports_an_open_diagnostic_window():
    policy = LogPolicy(levels=["WARN"], verbose_rate_per_second=25)
    until = policy.enable_diagnostics(120)

    resp = _run(_servicer(policy).GetLogPolicy(pb.LogPolicyRequest(), None))

    assert resp.available is True
    assert resp.diagnostic_active is True
    assert resp.diagnostic_until == until
    assert resp.verbose_rate_per_second == 25
    assert resp.diagnostic_activations == 1
    # The window widens what is collected without pretending the operator's own
    # configuration changed.
    assert "DEBUG" in set(resp.effective_levels)
    assert set(resp.configured_levels) == {"WARN"}


def test_get_log_policy_without_a_policy_is_unavailable_with_a_reason():
    # Fail-closed and honest: zeros here would read as "nothing suppressed".
    resp = _run(_servicer(None).GetLogPolicy(pb.LogPolicyRequest(), None))

    assert resp.available is False
    assert "policy" in resp.reason
    assert resp.effective_levels == []
    assert resp.configured_levels == []
    assert resp.emitted == 0
