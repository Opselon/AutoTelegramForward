"""Tests for the API contract audit (scripts/audit_api_contract.py).

The audit is the gate that keeps the Go producer and the Dart/TypeScript
consumers speaking about the same JSON fields. An audit that cannot fail is
worse than none, so these tests drive it with deliberately drifted inputs and
assert it reports them - then assert the real repository is in sync.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "audit_api_contract.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_api_contract", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


audit = _load()


def test_repository_contract_is_in_sync():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "contract in sync" in result.stdout


def test_json_mode_emits_exactly_one_object():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["errors"] == []
    assert "system_setup" in payload["fixtures"]


def test_a_deleted_consumer_field_is_an_error():
    report = audit.Report()
    audit.compare(
        report,
        "services[]",
        {"name", "online", "detail", "latency_ms"},
        {"ts:ServiceHealth": {"name", "online", "detail"}},
    )
    assert any("does not read field" in e and "latency_ms" in e for e in report.errors)


def test_a_phantom_consumer_field_is_a_warning():
    report = audit.Report()
    audit.compare(
        report,
        "services[]",
        {"name", "online", "detail", "latency_ms"},
        {"dart:ServiceHealth": {"name", "online", "detail", "latency_ms", "latencyMs"}},
    )
    assert report.errors == []
    assert any("absent from the capture" in w for w in report.warnings)


def test_a_value_bearing_field_is_refused():
    """A contract payload is booleans-only: reading `value` would leak a secret."""
    report = audit.Report()
    audit.compare(
        report,
        "checks[]",
        {"key", "level", "set", "reason"},
        {"x:EnvCheck": {"key", "level", "set", "reason", "value"}},
    )
    assert any("booleans only" in e for e in report.errors)


def test_credential_shaped_fixture_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "FIXTURES", tmp_path)
    (tmp_path / "system_setup.json").write_text(
        json.dumps({"token": "123456789:" + "A" * 38}), encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        audit.load_fixture("system_setup")


def test_the_parsers_find_the_real_sources():
    """The three extractors must keep resolving after a refactor."""
    go = audit.go_struct_tags(audit.read(audit.GO_HANDLER))
    assert go["EnvCheck"] == {"key", "level", "set", "reason", "hint"}
    assert go["ServiceProbe"] == {"name", "online", "detail", "latency_ms"}
    assert audit.go_response_keys(audit.read(audit.GO_HANDLER), "SetupReport") == {
        "ready",
        "time",
        "deployment",
        "product_version",
        "api_version",
        "commit_sha",
        "features",
        "checks",
        "services",
        "missing_required",
        "missing_recommended",
        "next_steps",
        # The setup report summarises the optional rate limiter's wiring and
        # outage posture since the Redis integration (#158).
        "ratelimit",
    }
    dart = audit.dart_class_keys(audit.read(audit.DART_MODELS), "EnvCheck")
    assert {"key", "level", "set", "reason", "hint"} <= dart
    ts = audit.ts_interface_fields(audit.read(audit.TS_TYPES), "EnvCheck")
    assert {"key", "level", "set", "reason", "hint"} <= ts
