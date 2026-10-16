"""Tests for the Rust-logger benchmark harness's pure helpers.

These tests never start a container and never import grpc: they pin the parts of
``scripts/bench/logger_rust_bench.py`` that decide what gets measured (the
command lines, the handshake parsing, the report merge) plus the arithmetic the
numbers rest on. The end-to-end run needs Docker and the engine image, so it
stays a manual step documented in ``docs/logger-benchmarks.md``.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "bench" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = _load("logger_rust_bench")


def test_percentiles_use_nearest_rank_and_never_extrapolate():
    assert bench.percentiles([]) == {"p50": 0.0, "p95": 0.0, "p99": 0.0,
                                     "max": 0.0, "mean": 0.0}
    # 1..100 -> nearest rank picks ordered[round(q*(n-1))].
    # round(0.5*99)=50 -> ordered[50]=51.0; round(0.95*99)=94 -> 95.0;
    # round(0.99*99)=98 -> 99.0.
    samples = [float(i) for i in range(1, 101)]
    out = bench.percentiles(samples)
    assert out["p50"] == 51.0
    assert out["p95"] == 95.0
    assert out["p99"] == 99.0
    assert out["max"] == 100.0
    assert out["mean"] == 50.5


def test_percentiles_are_stable_for_a_single_sample():
    assert bench.percentiles([7.5]) == {"p50": 7.5, "p95": 7.5, "p99": 7.5,
                                        "max": 7.5, "mean": 7.5}


def test_parse_listening_addr_reads_the_handshake_line():
    log = "some noise\nlistening on 0.0.0.0:50052\nversion=1"
    assert bench.parse_listening_addr(log) == "0.0.0.0:50052"
    assert bench.parse_listening_addr("no handshake here") is None


def test_parse_client_json_takes_the_last_sentinel_line():
    payload = {"target_rate": 1000, "ingest": {"offered": 3}, "query": {}}
    out = "chatter\n" + bench.SENTINEL + json.dumps(payload) + "\n"
    assert bench.parse_client_json(out) == payload
    assert bench.parse_client_json("no sentinel") is None


def test_engine_argv_only_sets_the_collection_policy_when_asked(tmp_path):
    argv = bench.build_engine_argv(
        "img", "engine", "net", str(tmp_path), 50052, 9109, "s", "all",
        ["--ack-mode", "durable"])
    assert argv[:3] == ["docker", "run", "-d"]
    assert "--rm" in argv and "--name" in argv and "--network" in argv
    assert "-e" in argv and "ATF_LOGGER_COLLECT_LEVELS=all" in argv
    assert "--print-addr" in argv
    assert argv[argv.index("--db") + 1] == "/app/data/bench.db"
    assert argv[argv.index("--listen") + 1] == "0.0.0.0:50052"
    assert argv[argv.index("--metrics-listen") + 1] == "0.0.0.0:9109"
    assert argv[-2:] == ["--ack-mode", "durable"]

    # Empty collect_levels must leave the engine on its own contract default.
    default = bench.build_engine_argv(
        "img", "engine", "net", str(tmp_path), 50052, 9109, "s", "", [])
    assert "-e" not in default


def test_client_argv_mounts_this_file_and_carries_the_process_count(tmp_path):
    script = str(tmp_path / "logger_rust_bench.py")
    argv = bench.build_client_argv(
        "client-img", "client", "net", script, "engine:50052", 50000, 5.0,
        4, "small", 20, 7, 64, "s", client_processes=4)
    assert f"{script}:/bench/logger_rust_bench.py:ro" in argv
    assert "--client" in argv
    assert argv[argv.index("--target-addr") + 1] == "engine:50052"
    assert argv[argv.index("--client-processes") + 1] == "4"
    assert argv[argv.index("--rate") + 1] == "50000"


def test_strip_samples_drops_merge_only_keys():
    raw = {"offered": 1, "_latencies": [1.0, 2.0], "_internal": True}
    assert bench.strip_samples(raw) == {"offered": 1}


def test_merge_ingest_sums_counters_and_pools_samples():
    parts = [
        {"offered": 10, "accepted": 9, "rejected": 1, "pending_at_stop": 2,
         "blocked_ms": 1.5, "errors": {"RuntimeError": 1}, "duration_s": 4.0,
         "_latencies": [1.0, 3.0]},
        {"offered": 5, "accepted": 5, "rejected": 0, "pending_at_stop": 0,
         "blocked_ms": 0.5, "errors": {}, "duration_s": 5.0,
         "_latencies": [2.0]},
    ]
    merged = bench._merge_ingest(parts)
    assert merged["offered"] == 15
    assert merged["accepted"] == 14
    assert merged["rejected"] == 1
    assert merged["pending_at_stop"] == 2
    assert merged["errors"] == {"RuntimeError": 1}
    assert merged["duration_s"] == 5.0
    assert merged["client_processes"] == 2
    assert merged["latency_ms"]["max"] == 3.0
    assert merged["accepted_per_sec"] == round(14 / 5.0, 1)


def test_sqlite_row_count_reads_the_logs_table(tmp_path):
    db = tmp_path / "bench.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, message TEXT)")
    conn.executemany("INSERT INTO logs (message) VALUES (?)",
                     [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()
    assert bench.sqlite_row_count(str(db)) == 3
    assert bench.sqlite_row_count(str(tmp_path / "missing.db")) is None


def test_module_does_not_import_grpc_or_docker_at_import_time():
    """Import safety: CI imports this module without the engine or Docker."""
    assert not hasattr(bench, "grpc")
    assert not hasattr(bench, "docker")
    assert "grpc" not in bench.__dict__
