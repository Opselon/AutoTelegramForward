"""Changing Core's collection policy must be validated, atomic and durable.

The read path (GetLogPolicy) answered "what is Core collecting?"; this covers the
write path an operator needs next, and the four ways it can lie:

* a badly spelled change being *interpreted* instead of refused (a typo silently
  enabling INFO collection — the exact high-volume path the default exists for),
* a partially applied change (levels accepted, rate rejected), leaving a policy
  nobody asked for,
* a change that is reported as saved but is not, so it silently reverts at the
  next restart,
* a rejected change that still reaches the durable store.

It also pins that the audit record of a change cannot be silenced by the change
itself: the operator who turns collection off must still be visible.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.application.log_policy import (  # noqa: E402
    MAX_DIAGNOSTIC_SECONDS,
    LogPolicy,
    LogPolicyError,
    validate_levels,
)
from core.infrastructure.grpc_server import servicers  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402
from core.infrastructure.persistence.log_policy_store import (  # noqa: E402
    LOG_POLICY_KEY,
    LogPolicyStore,
)
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


def _servicer(policy=None, store=None, log_client=None):
    return servicers.SystemStatusControlServicer(
        None, None, None, "", "test",
        log_policy=policy, log_policy_store=store, log_client=log_client,
    )


class _RecordingLogClient:
    """Stands in for the Logger client so the audit call is observable."""

    def __init__(self):
        self.calls = []

    def log(self, level, service, category, message, detail=None, **kwargs):
        self.calls.append(
            {"level": level, "service": service, "category": category,
             "message": message, "detail": detail or {}, **kwargs}
        )


class _CountingStore:
    """A store whose write can be counted, and made to fail on demand."""

    def __init__(self, fail=False):
        self.saves = 0
        self.fail = fail
        self.saved = None

    def save(self, state):
        self.saves += 1
        if self.fail:
            return False
        self.saved = dict(state)
        return True


# --------------------------------------------------------------------------- #
# validation: refuse what cannot be understood
# --------------------------------------------------------------------------- #
def test_unknown_level_is_refused_instead_of_being_read_as_info():
    # `normalize_level` turns anything unrecognised into INFO, which is right for
    # an event that already exists and wrong for a change request: WARNN would
    # start collecting every successful forward.
    with pytest.raises(LogPolicyError) as err:
        validate_levels(["WARNN"])
    assert "WARNN" in str(err.value)

    with pytest.raises(LogPolicyError):
        validate_levels([])


def test_level_aliases_are_accepted_and_canonicalised():
    # Canonical order (TRACE..CRITICAL), so the reported policy never depends on
    # the order the request happened to list them in.
    assert validate_levels(["warning", "err"]) == ("WARN", "ERROR")
    assert validate_levels(["ERROR", "WARN"]) == validate_levels(["WARN", "ERROR"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"levels": ["NOPE"]},
        {"levels": []},
        {"levels": ["WARN"], "verbose_rate_per_second": -1},
        {"levels": ["WARN"], "verbose_rate_per_second": float("nan")},
        {"levels": ["WARN"], "diagnostic_seconds": 0.5},  # 0 is "unchanged", not a window
        {"levels": ["WARN"], "diagnostic_seconds": MAX_DIAGNOSTIC_SECONDS + 1},
        {"levels": ["WARN"], "diagnostic_seconds": 60, "disable_diagnostics": True},
    ],
)
def test_a_rejected_change_leaves_the_policy_exactly_as_it_was(kwargs):
    policy = LogPolicy(levels=["WARN"], verbose_rate_per_second=5)

    with pytest.raises(LogPolicyError):
        policy.reconfigure(**kwargs)

    snapshot = policy.snapshot()
    assert set(snapshot["configured_levels"]) == {"WARN"}
    assert snapshot["verbose_rate_per_second"] == 5
    assert snapshot["diagnostic_active"] is False


def test_a_partial_change_is_not_applied():
    # Levels are valid, the rate is not: an implementation that assigned as it
    # went would leave a policy the operator never asked for.
    policy = LogPolicy(levels=["WARN"])

    with pytest.raises(LogPolicyError):
        policy.reconfigure(levels=["ERROR", "WARN"], verbose_rate_per_second="lots")

    assert set(policy.snapshot()["configured_levels"]) == {"WARN"}


# --------------------------------------------------------------------------- #
# the change, and what it reports
# --------------------------------------------------------------------------- #
def test_reconfigure_applies_every_field_in_one_step_and_reports_it():
    policy = LogPolicy(levels=["WARN"])

    snapshot = policy.reconfigure(
        levels=["ERROR", "WARN", "INFO"], verbose_rate_per_second=12, diagnostic_seconds=60
    )

    assert set(snapshot["configured_levels"]) == {"WARN", "ERROR", "INFO"}
    assert snapshot["verbose_rate_per_second"] == 12
    assert snapshot["diagnostic_active"] is True
    # The returned state is the enforced one, not the requested one: the effective
    # set widens with the diagnostic window while the configured set does not.
    assert "DEBUG" in set(snapshot["levels"])
    assert set(snapshot["configured_levels"]) == {"WARN", "ERROR", "INFO"}


def test_a_levels_only_change_does_not_cancel_an_open_diagnostic_window():
    policy = LogPolicy(levels=["WARN"])
    policy.enable_diagnostics(300)

    policy.reconfigure(levels=["ERROR"])

    assert policy.diagnostic_active() is True
    assert set(policy.snapshot()["configured_levels"]) == {"ERROR"}


def test_the_window_can_be_closed_without_touching_the_levels():
    policy = LogPolicy(levels=["ERROR"])
    policy.enable_diagnostics(300)

    policy.reconfigure(disable_diagnostics=True)

    assert policy.diagnostic_active() is False
    assert set(policy.snapshot()["configured_levels"]) == {"ERROR"}


def test_a_widened_policy_stops_suppressing_immediately():
    # The point of the mutation is the behaviour change, so assert the behaviour:
    # after the change the previously dropped event is emitted.
    policy = LogPolicy(levels=["WARN", "ERROR"])
    assert policy.allow("INFO", "forward.progress") is False

    policy.reconfigure(levels=["WARN", "ERROR", "INFO"])

    assert policy.allow("INFO", "forward.progress") is True


# --------------------------------------------------------------------------- #
# durability: what a restart must not forget
# --------------------------------------------------------------------------- #
def test_export_state_holds_the_policy_but_never_the_counters():
    policy = LogPolicy(levels=["WARN"])
    policy.allow("INFO")  # moves suppressed_level
    policy.allow("ERROR")

    state = policy.export_state()

    assert set(state["configured_levels"]) == {"WARN"}
    assert "emitted" not in state and "suppressed_level" not in state, (
        "counters describe this process's runtime, not the policy; restoring them"
        " would invent history"
    )


def test_a_restart_restores_the_change_but_not_the_original_window_length():
    policy = LogPolicy(levels=["WARN"], verbose_rate_per_second=3)
    policy.reconfigure(levels=["WARN", "INFO"], verbose_rate_per_second=9, diagnostic_seconds=600)

    restored = LogPolicy(levels=["WARN"])
    assert restored.import_state(policy.export_state()) is True

    snapshot = restored.snapshot()
    assert set(snapshot["configured_levels"]) == {"WARN", "INFO"}
    assert snapshot["verbose_rate_per_second"] == 9
    assert snapshot["diagnostic_active"] is True
    assert snapshot["diagnostic_until"] - int(time.time()) <= 600, (
        "a restart must shorten a bounded window, never lengthen it"
    )


def test_an_expired_window_stays_closed_after_a_restart():
    policy = LogPolicy(levels=["WARN"])

    restored = LogPolicy(levels=["WARN"])
    restored.import_state(
        {"configured_levels": ["ERROR"], "verbose_rate_per_second": 0,
         "diagnostic_until": int(time.time()) - 60}
    )

    assert restored.diagnostic_active() is False
    assert set(restored.snapshot()["configured_levels"]) == {"ERROR"}


@pytest.mark.parametrize(
    "state", [None, {}, "not a mapping", {"configured_levels": ["NOPE"]}, {"configured_levels": []}]
)
def test_an_unusable_saved_state_is_ignored_rather_than_fatal(state):
    # Refusing to start over a settings row is a worse failure than starting with
    # the conservative default.
    assert LogPolicy(levels=["WARN"]).import_state(state) is False


def test_the_saved_policy_is_read_back_by_a_fresh_process(tmp_path):
    db = SqliteDatabase(str(tmp_path / "policy.db"))
    try:
        store = LogPolicyStore(db)
        policy = LogPolicy(levels=["WARN"])
        policy.reconfigure(levels=["ERROR", "WARN", "INFO"], verbose_rate_per_second=7)
        assert store.save(policy.export_state()) is True

        # A new process: environment defaults, then the saved override on top.
        restarted = LogPolicy.from_env({"ATF_LOG_LEVELS": "WARN"})
        assert store.restore_into(restarted) is True
        assert set(restarted.snapshot()["configured_levels"]) == {"WARN", "ERROR", "INFO"}
        assert restarted.snapshot()["verbose_rate_per_second"] == 7
    finally:
        db.close()


def test_a_corrupt_saved_policy_falls_back_to_the_configured_default(tmp_path):
    db = SqliteDatabase(str(tmp_path / "policy.db"))
    try:
        store = LogPolicyStore(db)
        db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)",
            (LOG_POLICY_KEY, "{not json", int(time.time())),
        )

        assert store.load() is None
        policy = LogPolicy(levels=["WARN"])
        assert store.restore_into(policy) is False
        assert set(policy.snapshot()["configured_levels"]) == {"WARN"}, (
            "the environment baseline must survive a corrupt row"
        )
    finally:
        db.close()


def test_a_write_that_cannot_be_persisted_is_reported_not_swallowed():
    assert LogPolicyStore(None).save({"configured_levels": ["WARN"]}) is False


# --------------------------------------------------------------------------- #
# the RPC: what the control plane gets back
# --------------------------------------------------------------------------- #
def test_set_log_policy_applies_the_change_and_answers_with_what_is_enforced():
    policy = LogPolicy(levels=["WARN"])
    store = _CountingStore()

    resp = _run(
        _servicer(policy, store=store).SetLogPolicy(
            pb.SetLogPolicyRequest(levels=["WARN", "INFO"], verbose_rate_per_second=5),
            None,
        )
    )

    assert resp.available is True and resp.applied is True and resp.persisted is True
    assert set(resp.configured_levels) == {"WARN", "INFO"}
    assert resp.verbose_rate_per_second == 5
    assert policy.allow("INFO", "forward.progress") is True, "the process really collects it now"
    assert store.saves == 1


def test_a_change_without_a_store_is_reported_as_volatile():
    # Claiming durability this build does not have is how a change silently
    # reverts at the next restart.
    resp = _run(
        _servicer(LogPolicy(levels=["WARN"])).SetLogPolicy(
            pb.SetLogPolicyRequest(levels=["ERROR"]), None
        )
    )

    assert resp.applied is True
    assert resp.persisted is False


def test_a_change_that_could_not_be_written_says_so():
    store = _CountingStore(fail=True)

    resp = _run(
        _servicer(LogPolicy(levels=["WARN"]), store=store).SetLogPolicy(
            pb.SetLogPolicyRequest(levels=["ERROR"]), None
        )
    )

    assert resp.applied is True, "the process is enforcing it"
    assert resp.persisted is False, "... but it will not survive a restart, and must say so"


def test_a_rejected_change_changes_nothing_and_never_reaches_the_store():
    policy = LogPolicy(levels=["WARN"])
    store = _CountingStore()

    resp = _run(
        _servicer(policy, store=store).SetLogPolicy(
            pb.SetLogPolicyRequest(levels=["WARNN"]), None
        )
    )

    assert resp.available is True
    assert resp.applied is False
    assert "WARNN" in resp.reason
    assert set(policy.snapshot()["configured_levels"]) == {"WARN"}
    assert store.saves == 0, "a rejected change must not be persisted"
    # The answer still describes the policy in force, so the caller can show it.
    assert set(resp.configured_levels) == {"WARN"}


def test_set_log_policy_without_a_policy_is_unavailable_with_a_reason():
    resp = _run(
        _servicer(None).SetLogPolicy(pb.SetLogPolicyRequest(levels=["ERROR"]), None)
    )

    assert resp.available is False
    assert resp.applied is False
    assert "policy" in resp.reason


def test_the_change_is_audited_through_the_policy_it_changed():
    policy = LogPolicy(levels=["WARN", "ERROR"])
    client = _RecordingLogClient()

    _run(
        _servicer(policy, store=_CountingStore(), log_client=client).SetLogPolicy(
            pb.SetLogPolicyRequest(
                levels=["WARN"], actor="42", correlation_id="corr-1"
            ),
            None,
        )
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["event_type"] == "admin.log_policy.changed"
    assert call["correlation_id"] == "corr-1"
    assert call["detail"]["actor"] == "42"
    assert call["detail"]["applied"] is True


def test_the_audit_record_survives_a_policy_that_collects_nothing():
    # The strongest form of "the threshold cannot hide its own change": the event
    # type is what keeps it out of the gate, and a real policy is what proves it.
    policy = LogPolicy(levels=["CRITICAL"])
    sink = _RecordingLogClient()
    gated = _PolicyGatedLogClient(policy, sink)

    # The gate is real: an ordinary event on this policy is dropped here.
    gated.log("INFO", "core", "system", "message forwarded")
    assert gated.suppressed == ["INFO"]

    _run(
        _servicer(policy, store=_CountingStore(), log_client=gated).SetLogPolicy(
            pb.SetLogPolicyRequest(levels=["CRITICAL"], actor="7"), None
        )
    )

    assert len(sink.calls) == 1, "yet the record of the change still got out"
    assert sink.calls[0]["event_type"] == "admin.log_policy.changed"


class _PolicyGatedLogClient:
    """A stand-in for `LogClient` that applies the real policy before recording.

    The point is not to re-test `LogClient` (it has its own tests) but to prove
    the audit call names its event, so a policy that drops everything else still
    lets the record of its own change through.
    """

    def __init__(self, policy, sink):
        self._policy = policy
        self._sink = sink
        self.suppressed = []

    def log(self, level, service, category, message, detail=None, **kwargs):
        if not self._policy.allow(
            level, event_type=kwargs.get("event_type", ""),
            reason_code=kwargs.get("reason_code", ""),
        ):
            self.suppressed.append(level)
            return
        self._sink.log(level, service, category, message, detail, **kwargs)


def test_a_policy_change_opened_from_the_control_plane_expires_on_its_own():
    policy = LogPolicy(levels=["WARN"])

    resp = _run(
        _servicer(policy, store=_CountingStore()).SetLogPolicy(
            pb.SetLogPolicyRequest(levels=["WARN"], diagnostic_seconds=120), None
        )
    )
    assert resp.diagnostic_active is True
    assert resp.diagnostic_until - int(time.time()) <= 120

    closed = _run(
        _servicer(policy, store=_CountingStore()).SetLogPolicy(
            pb.SetLogPolicyRequest(levels=["WARN"], disable_diagnostics=True), None
        )
    )
    assert closed.diagnostic_active is False


def test_the_saved_row_is_the_policy_and_nothing_else(tmp_path):
    # The persisted value is what a restart reads; it must not carry process
    # runtime that would be wrong on the next start.
    db = SqliteDatabase(str(tmp_path / "policy.db"))
    try:
        store = LogPolicyStore(db)
        policy = LogPolicy(levels=["WARN"])
        policy.allow("INFO")
        _run(_servicer(policy, store=store).SetLogPolicy(
            pb.SetLogPolicyRequest(levels=["ERROR", "WARN"]), None
        ))

        row = db.query_one("SELECT value FROM app_settings WHERE key=?", (LOG_POLICY_KEY,))
        state = json.loads(row["value"])
        assert set(state["configured_levels"]) == {"WARN", "ERROR"}
        assert set(state) == {"configured_levels", "verbose_rate_per_second", "diagnostic_until"}
    finally:
        db.close()
