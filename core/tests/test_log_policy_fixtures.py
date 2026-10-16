"""Parity tests: the Python producer policy must satisfy the shared contract.

`contracts/log_policy_decisions.json` is evaluated by BOTH producers — here and
in `api/internal/logpolicy/policy_test.go`. That is deliberate: the Go API and
the Python Core apply the same collection policy in two different languages, and
two hand-maintained implementations of one rule drift apart exactly when nobody
is looking. Neither side owns the expectations; the fixture does.

If a case here fails, the fix is a real behavioural change in one implementation,
never an edit that makes the fixture agree with the code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.log_policy import LogPolicy, _parse_levels, normalize_level

_FIXTURE = Path(__file__).resolve().parents[2] / "contracts" / "log_policy_decisions.json"


def _fixture() -> dict:
    assert _FIXTURE.exists(), f"the shared policy fixture must exist ({_FIXTURE})"
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


class FrozenClock:
    """Injectable monotonic-style clock: expires windows without waiting."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_fixture_is_not_vacuous() -> None:
    data = _fixture()
    assert data.get("scenarios"), "a fixture with no scenarios is a vacuous pass"
    assert data.get("level_normalization"), "normalization cases are part of the contract"


@pytest.mark.parametrize("case", _fixture()["level_normalization"], ids=lambda c: repr(c["input"]))
def test_level_normalization_matches_the_shared_fixture(case: dict) -> None:
    assert normalize_level(case["input"]) == case["expected"], case.get("why", "")


@pytest.mark.parametrize("case", _fixture()["level_list_parsing"], ids=lambda c: repr(c["input"]))
def test_level_list_parsing_matches_the_shared_fixture(case: dict) -> None:
    """A level *list* has one rule the single-level normalisation does not.

    An unrecognised name is dropped rather than becoming INFO, so a typo in
    ATF_LOG_LEVELS cannot silently re-enable a verbose level.
    """
    assert list(_parse_levels(case["input"])) == case["expected"], case.get("why", "")


@pytest.mark.parametrize("scenario", _fixture()["scenarios"], ids=lambda s: s["name"])
def test_collection_decisions_match_the_shared_fixture(scenario: dict) -> None:
    clock = FrozenClock()
    policy = LogPolicy(
        levels=scenario["levels"],
        verbose_rate_per_second=scenario["verbose_rate_per_second"],
        clock=clock,
    )

    if scenario.get("diagnostic_seconds"):
        policy.enable_diagnostics(scenario["diagnostic_seconds"])
    if scenario.get("expire_diagnostic"):
        # Walk past the window so the expiry path itself is exercised.
        clock.advance(scenario["diagnostic_seconds"] + 1)
        assert not policy.diagnostic_active(), "an expired diagnostic window must close itself"
    if scenario.get("advance_seconds_before"):
        clock.advance(scenario["advance_seconds_before"])

    for index, event in enumerate(scenario["events"]):
        got = policy.allow(event["level"], event["event_type"], event["reason_code"])
        assert got is event["expected"], (
            f"event {index} ({event['level']} {event['event_type']}/{event['reason_code']}): "
            f"allow = {got}, fixture requires {event['expected']} ({event.get('why', '')})"
        )


def test_defaults_are_the_documented_defaults() -> None:
    snapshot = LogPolicy().snapshot()
    assert snapshot["configured_levels"] == ["WARN", "ERROR", "CRITICAL"]
    assert snapshot["diagnostic_active"] is False


def test_a_refusal_is_counted_as_suppression_not_as_loss() -> None:
    policy = LogPolicy()
    for _ in range(3):
        assert policy.allow("INFO", "http.request", "") is False
    snapshot = policy.snapshot()
    assert snapshot["suppressed_level"] == 3
    assert snapshot["emitted"] == 0
    # Suppression is a policy decision, reported by the policy. It is not, and
    # must never be conflated with, a transport or queue-loss counter.
    assert snapshot["emitted_security"] == 0
    assert "dropped" not in snapshot


def test_config_from_env_fails_safe_to_the_documented_defaults(monkeypatch) -> None:
    monkeypatch.setenv("ATF_LOG_LEVELS", "warn, ERROR ,bogus;CRITICAL")
    monkeypatch.setenv("ATF_LOG_VERBOSE_RATE", "")
    monkeypatch.setenv("ATF_LOG_DIAGNOSTIC_SECONDS", "not-a-number")
    policy = LogPolicy.from_env()
    snapshot = policy.snapshot()
    assert snapshot["configured_levels"] == ["WARN", "ERROR", "CRITICAL"]
    assert snapshot["verbose_rate_per_second"] == 0.0
    assert snapshot["diagnostic_active"] is False

    monkeypatch.setenv("ATF_LOG_LEVELS", "!!!")
    assert LogPolicy.from_env().snapshot()["configured_levels"] == ["WARN", "ERROR", "CRITICAL"]
