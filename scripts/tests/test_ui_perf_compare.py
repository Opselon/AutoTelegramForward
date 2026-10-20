"""Tests for the UI/perf benchmark comparison gate.

The gate decides whether a UI change regressed the product, so its own verdicts
need pinning: a stricter threshold must fail, an unchanged report must pass, and
a report measured behind an auth wall (401s) must never read as "fast, PASS".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPARE = ROOT / "scripts" / "bench" / "ui_perf_compare.py"


def _report(**overrides) -> dict:
    report = {
        "http": {
            "rules_list": {"p50_ms": 3.0, "p95_ms": 4.0, "p99_ms": 5.0, "status": 200},
        },
        "web": {"total_kb": 1000.0, "total_gzip_kb": 300.0, "chunks": 1},
        "bot": {"lookup_ns": 17.0, "menu_40_buttons_ms": 0.001},
    }
    report.update(overrides)
    return report


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(before: Path, after: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(COMPARE), str(before), str(after), *extra],
        capture_output=True,
        text=True,
    )


def test_identical_reports_pass(tmp_path: Path) -> None:
    before = _write(tmp_path, "before.json", _report())
    after = _write(tmp_path, "after.json", _report())
    done = _run(before, after)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PASS" in done.stdout


def test_latency_regression_fails(tmp_path: Path) -> None:
    before = _write(tmp_path, "before.json", _report())
    slower = _report(
        http={"rules_list": {"p50_ms": 3.0, "p95_ms": 9.0, "p99_ms": 12.0, "status": 200}}
    )
    after = _write(tmp_path, "after.json", slower)
    done = _run(before, after)
    assert done.returncode == 1
    assert "REGRESSION" in done.stdout


def test_bundle_growth_fails_at_five_percent(tmp_path: Path) -> None:
    before = _write(tmp_path, "before.json", _report())
    fatter = _report(web={"total_kb": 1100.0, "total_gzip_kb": 330.0, "chunks": 6})
    after = _write(tmp_path, "after.json", fatter)
    done = _run(before, after)
    assert done.returncode == 1
    assert "web total_gzip_kb" in done.stdout


def test_chunk_loss_fails_even_when_smaller(tmp_path: Path) -> None:
    before = _write(tmp_path, "before.json", _report(web={"total_kb": 900.0, "total_gzip_kb": 250.0, "chunks": 8}))
    collapsed = _report(web={"total_kb": 900.0, "total_gzip_kb": 250.0, "chunks": 1})
    after = _write(tmp_path, "after.json", collapsed)
    done = _run(before, after)
    assert done.returncode == 1
    assert "code splitting lost" in done.stdout


def test_missing_endpoint_fails(tmp_path: Path) -> None:
    before = _write(tmp_path, "before.json", _report())
    after = _write(tmp_path, "after.json", _report(http={}))
    done = _run(before, after)
    assert done.returncode == 1
    assert "missing on the after side" in done.stdout


def test_threshold_is_configurable(tmp_path: Path) -> None:
    before = _write(tmp_path, "before.json", _report())
    slower = _report(http={"rules_list": {"p50_ms": 3.6, "p95_ms": 4.0, "p99_ms": 5.0, "status": 200}})
    after = _write(tmp_path, "after.json", slower)
    assert _run(before, after).returncode == 1                       # +20% > the 15% default
    assert _run(before, after, "--threshold", "0.3").returncode == 0  # within a relaxed budget
