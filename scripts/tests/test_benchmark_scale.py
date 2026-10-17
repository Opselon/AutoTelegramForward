"""The scale benchmark must actually run, and must fail when it should.

A benchmark nobody runs rots silently — it keeps printing numbers while the code
it measures has moved on. This runs the small profile in CI (seconds, hundreds
of tenants: enough for SQLite to choose indexes and for an N+1 to show up) and
checks the JSON it produces, so a broken harness is a red check rather than a
stale paragraph in the documentation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "benchmark_scale.py"


def run_benchmark(tmp_path: Path, *extra: str) -> tuple[subprocess.CompletedProcess, dict]:
    out = tmp_path / "scale.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--users", "150", "--tasks-per-user", "2",
         "--messages", "200", "--concurrency", "8", "--iterations", "5",
         "--quiet", "--json", str(out), *extra],
        cwd=str(REPO), capture_output=True, text=True, timeout=300,
    )
    data = json.loads(out.read_text()) if out.exists() else {}
    return proc, data


def test_the_benchmark_runs_and_reports(tmp_path):
    proc, data = run_benchmark(tmp_path)
    assert proc.returncode == 0, f"benchmark failed:\n{proc.stdout}\n{proc.stderr}"
    assert data["seed"]["users"] == 150
    assert data["seed"]["tasks"] == 300
    assert data["problems"] == []
    assert data["ingest"]["forwarded"] > 0, "nothing was forwarded"
    assert data["db_mb"] > 0


def test_every_hot_read_is_indexed_at_scale(tmp_path):
    """The defect this harness exists to catch: a full scan on the hot path."""
    proc, data = run_benchmark(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for name, plan in data["plans"].items():
        detail = " ".join(step["detail"] for step in plan)
        assert "SCAN forward_rules" not in detail, f"{name} scans the rules table: {detail}"
    assert "idx_rules_source" in json.dumps(data["plans"]["rules_by_source"])
    assert "idx_rules_extra_sources" in json.dumps(data["plans"]["rules_by_source"])
    assert "idx_rules_owner_created" in json.dumps(data["plans"]["rules_by_owner_page"])
    assert "TEMP B-TREE" not in json.dumps(data["plans"]["rules_by_owner_page"])


def test_the_cost_per_call_does_not_grow_with_tenants(tmp_path):
    """Statements per call — the property that makes this scale at all."""
    proc, data = run_benchmark(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    reads = data["reads"]
    assert reads["rules_by_source"]["sql_per_call"] <= 8
    assert reads["rules_by_owner"]["sql_per_call"] <= 8
    assert reads["effective_settings"]["sql_per_call"] <= 8
    assert data["ingest"]["sql_per_message"] <= 40


def test_the_page_is_bounded_by_the_page_not_the_history(tmp_path):
    proc, data = run_benchmark(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    page = data["reads"]["rules_by_owner_page"]
    assert page["total"] == 2, "the fixture tenant should own two tasks"
    assert page["returned"] <= 50


def test_no_forward_crosses_a_tenant_boundary(tmp_path):
    proc, data = run_benchmark(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert data["ingest"]["cross_tenant_leaks"] == []
