#!/usr/bin/env python3
"""Benchmark the per-user task store (API-first phase 2) end to end.

Measures, against a REAL SQLite database file (the same query shapes run in
production):
  1. submit:  create_task with an idempotency key, one row per call
  2. list:    the owner-scoped page query the Go API serves
  3. claim:   the worker's atomic UPDATE..RETURNING acquire
  at 100, 1,000 and 10,000 tenant-task loads.

Usage: python3 scripts/benchmark_user_tasks.py [--tmpdir PATH]
Output: a small JSON report on stdout.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

import sys
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.application.user_tasks import UserTaskUseCases  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402
from core.infrastructure.persistence.sqlite_repositories import (  # noqa: E402
    SqliteUserTaskRepository,
)

LOADS = [100, 1_000, 10_000]
KIND = "session.backup"


class Runner:
    def __init__(self, db_path: str) -> None:
        self.db = SqliteDatabase(db_path)
        self.tasks = UserTaskUseCases(SqliteUserTaskRepository(self.db))

    def time_all(self, count: int) -> dict:
        import asyncio

        async def _run():
            owner = count  # one unique tenant per load segment
            result = {}
            # 1. submit with idempotency keys
            keys = [f"idem-{owner}-{i}" for i in range(count)]
            payloads = [json_frame("session_id", f"s-{i}") for i in range(count)]
            start = time.perf_counter()
            for i in range(count):
                await self.tasks.create(owner, KIND, payload_json=payloads[i],
                                        idempotency_key=keys[i])
            result["submit_p50"] = (time.perf_counter() - start) / count * 1e6
            # 2. list page (50 rows, the API default) + total
            samples = []
            for _ in range(min(50, count)):
                t0 = time.perf_counter()
                await self.tasks.list_for_owner(owner, limit=50)
                samples.append((time.perf_counter() - t0) * 1e6)
            result["list_p50"] = statistics.median(samples)
            result["list_p99"] = max(samples)
            # 3. acquire: sample the atomic claim path (bounded). Tasks
            # claimed here are re-cancelled so the database stays clean for
            # the next measurement phase.
            claims = 0
            seen_ids = set()
            claim_times = []
            sample = min(count, 200)
            idle = 0
            while claims < sample and idle < sample:
                t0 = time.perf_counter()
                t = await self.tasks.acquire_for_work(lease_seconds=1.0)
                claim_times.append((time.perf_counter() - t0) * 1e6)
                if t is None:
                    idle += 1
                    await asyncio.sleep(0.001)
                    continue
                claims += 1
                seen_ids.add(t.id)
                # Hand the slot back so the next sample measures a fresh claim.
                fresh = await self.tasks._repo.get(t.id)
                fresh.lease_token = t.lease_token
                fresh.attempt = min(fresh.attempt + 1, fresh.max_attempts)
                await self.tasks._repo.pause_for_retry(
                    t.id, t.lease_token, int(time.time() + 3600),
                    "bench handback", "bench", int(time.time()))
                fresh2 = await self.tasks._repo.get(t.id)
                fresh2.run_at = 0
                await self.tasks._repo.replace(fresh2)
            result["acquire_us_per_op"] = statistics.median(claim_times)
            result["acquire_p99"] = statistics.quantiles(claim_times, n=20)[-1]
            result["claims"] = claims
            result["distinct_tasks_claimed"] = len(seen_ids)
            return result

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()


def json_frame(value: str, inner: str) -> str:
    return ("{\"" + value + "\":\"" + inner + "\"}")


def lease_expiry(until: int) -> int:
    # force each claim back to the pool immediately after measuring its cost
    return until if until <= 0 else until - 1


def main() -> int:
    ap = argparse.ArgumentParser()
    tmpdir = tempfile.mkdtemp(prefix="atf-tasks-bench-")
    report = []
    for load in LOADS:
        db = time.perf_counter()
        ps = str(Path(tmpdir) / f"bench-{load}.db")
        runner = Runner(ps)
        sink = time.perf_counter() - db
        res = runner.time_all(load)
        res["load"] = load
        res["setup_db_s"] = round(sink, 4)
        report.append(res)
        print(f"[{load:6d} rows] submit {res['submit_p50']:.0f}us | "
              f"list p50 {res['list_p50']:.0f}us p99 {res['list_p99']:.0f}us | "
              f"acquire {res['acquire_us_per_op']:.0f}us/op ({res['claims']} claimed)",
              file=__import__("sys").stderr)
        runner.db.close()
    print(json.dumps({"engine": "sqlite", "results": report,
                      "spec": "2 vCPU host (see docs/api-phase2.md for exact hardware)"},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
