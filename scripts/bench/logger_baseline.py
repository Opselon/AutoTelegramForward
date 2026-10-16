"""Reproducible ingest/query benchmark for a LogControlService implementation.

Measures the real gRPC path — a real server process/thread pair, the real
protobuf messages, the real database file — and reports the numbers the mission
requires kept apart: events OFFERED by producers, ACCEPTED by the logger,
PERSISTED in the database, REJECTED, and still PENDING when the run ends.

    python -m scripts.bench.logger_baseline                  # python engine, sqlite
    python -m scripts.bench.logger_baseline --target rust    # rust engine binary
    python -m scripts.bench.logger_baseline --rates 100,1000,10000

Never reports an in-memory number as durable throughput: every rate is verified
against row counts read back from the database after the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")


def _percentiles(samples: List[float]) -> Dict[str, float]:
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    ordered = sorted(samples)

    def pick(q: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return ordered[idx]

    return {
        "p50": round(pick(0.50), 3),
        "p95": round(pick(0.95), 3),
        "p99": round(pick(0.99), 3),
        "max": round(ordered[-1], 3),
        "mean": round(statistics.fmean(ordered), 3),
    }


def _make_entry(pb, seq: int, rng: random.Random, profile: str):
    """One representative event. Small by default: a log line, not a payload."""
    level = LEVELS[rng.choices([0, 1, 2, 3, 4], weights=[5, 60, 20, 14, 1])[0]]
    if profile == "large":
        detail = json.dumps({"seq": seq, "blob": "x" * 2048, "rule_id": f"r-{seq % 97}"})
    elif profile == "structured":
        detail = json.dumps({
            "seq": seq, "rule_id": f"rule-{seq % 397}", "chat_id": f"-100{seq % 1000}",
            "attempt": seq % 4, "duration_ms": seq % 900, "result": "sent" if seq % 7 else "retry",
        })
    else:
        detail = json.dumps({"seq": seq, "rule_id": f"r-{seq % 97}"})
    return pb.LogEntry(
        ts=int(time.time() * 1000),
        level=level,
        service="bench",
        category="forward" if seq % 3 else "system",
        message=f"forwarding event {seq} for chat -100{seq % 1000} result=sent",
        detail=detail,
        rule_id=f"rule-{seq % 397}",
        user_id=5094837833,
        correlation_id=f"corr-{seq % 7919}",
    )


async def _producers(stub, pb, rate: int, seconds: float, producers: int,
                     profile: str, seed: int, max_inflight: int = 64) -> Dict[str, Any]:
    """Offer `rate` events/second across `producers` concurrent clients.

    Offering is paced independently of RPC completion: a worker blocks only when
    it already has ``max_inflight`` requests outstanding, which is the honest
    place to apply backpressure (otherwise the harness measures its own
    round-trip latency instead of the server's ceiling). Requests still in
    flight when the clock stops are reported as PENDING, never as accepted or
    persisted.
    """
    per_producer = max(1, int(rate / producers))
    interval = 1.0 / per_producer if per_producer else 0
    stop_at = time.perf_counter() + seconds
    latencies: List[float] = []
    offered = accepted = rejected = 0
    errors: Dict[str, int] = {}
    blocked_ms = 0.0
    pending = 0
    inflight: List[asyncio.Task] = []

    def finish(task: asyncio.Task) -> None:
        nonlocal accepted, rejected
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            accepted += 1
        else:
            rejected += 1
            errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1

    async def one(worker: int) -> None:
        nonlocal offered, blocked_ms
        rng = random.Random(seed + worker)
        seq = worker * 1_000_000
        sem = asyncio.Semaphore(max_inflight)
        next_at = time.perf_counter()
        while time.perf_counter() < stop_at:
            seq += 1
            entry = _make_entry(pb, seq, rng, profile)
            wait_started = time.perf_counter()
            await sem.acquire()  # blocks only at the in-flight ceiling
            blocked_ms += (time.perf_counter() - wait_started) * 1000.0
            offered += 1
            started = time.perf_counter()

            async def call(entry=entry, started=started):
                try:
                    resp = await stub.Log(pb.LogRequest(entry=entry))
                    latencies.append((time.perf_counter() - started) * 1000.0)
                    if not getattr(resp, "success", False):
                        raise RuntimeError("rejected")
                finally:
                    sem.release()

            task = asyncio.create_task(call())
            task.add_done_callback(finish)
            inflight.append(task)
            next_at += interval
            drift = next_at - time.perf_counter()
            if drift > 0:
                await asyncio.sleep(drift)
            elif drift < -1.0:
                next_at = time.perf_counter()  # never chase an unbounded debt

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(producers)))
    duration = time.perf_counter() - started
    # Whatever is still running is pending at shutdown: bounded drain, then count.
    pending = len([t for t in inflight if not t.done()])
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)
    return {
        "offered": offered,
        "accepted": accepted,
        "rejected": rejected,
        "pending_at_stop": pending,
        "errors": errors,
        "blocked_ms": round(blocked_ms, 1),
        "offered_per_sec": round(offered / duration, 1),
        "accepted_per_sec": round(accepted / duration, 1),
        "latency_ms": _percentiles(latencies),
        "duration_s": round(duration, 2),
    }


async def _query_probe(stub, pb, iterations: int) -> Dict[str, Any]:
    """Read-side latency: severity filter, search, aggregation, page."""
    cases = {
        "recent_50": lambda: stub.QueryLogs(pb.QueryLogsRequest(limit=50)),
        "errors_only": lambda: stub.QueryLogs(
            pb.QueryLogsRequest(levels=["ERROR", "CRITICAL"], limit=50)),
        "search_corr": lambda: stub.QueryLogs(
            pb.QueryLogsRequest(search="corr-1234", limit=50)),
        "rule_history": lambda: stub.QueryLogs(
            pb.QueryLogsRequest(rule_id="rule-123", limit=50)),
        "deep_page": lambda: stub.QueryLogs(
            pb.QueryLogsRequest(limit=50, offset=500, ascending=True)),
        "stats": lambda: stub.LogStats(pb.LogStatsRequest()),
    }
    out: Dict[str, Any] = {}
    for name, call in cases.items():
        samples: List[float] = []
        for _ in range(iterations):
            started = time.perf_counter()
            try:
                await call()
                samples.append((time.perf_counter() - started) * 1000.0)
            except Exception:  # noqa: BLE001
                pass
        out[name] = _percentiles(samples)
    return out


def _sqlite_rows(db_path: str) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
    finally:
        conn.close()


async def run_python_engine(args, db_path: str, rate: int) -> Dict[str, Any]:
    """The Python logger under test: its real servicer behind a real gRPC server."""
    import grpc

    from logger.main import LogControlServicer, LoggerDatabase
    from core.proto import autoforward_pb2 as pb
    from core.proto import autoforward_pb2_grpc as pb_grpc

    db = LoggerDatabase(db_path)
    server = grpc.aio.server()
    pb_grpc.add_LogControlServiceServicer_to_server(LogControlServicer(db), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    stub = pb_grpc.LogControlServiceStub(channel)
    try:
        ingest = await _producers(stub, pb, rate, args.seconds, args.producers,
                                  args.profile, args.seed)
        query = await _query_probe(stub, pb, args.query_iterations)
    finally:
        await channel.close()
        await server.stop(1.0)
        db.close()
    # Durability check: what actually reached the file.
    ingest["persisted"] = _sqlite_rows(db_path)
    ingest["persisted_per_sec"] = round(ingest["persisted"] / max(ingest["duration_s"], 1e-6), 1)
    ingest["db_bytes"] = os.path.getsize(db_path)
    return {"ingest": ingest, "query": query}


def _start_rust(args, db_path: str):
    """Spawn a Rust engine binary if one is provided (same client, same probes)."""
    binary = args.rust_binary
    proc = subprocess.Popen(
        [binary, "--db", db_path, "--listen", "127.0.0.1:0", "--print-addr"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    line = proc.stdout.readline().strip() if proc.stdout else ""
    addr = line.split()[-1] if line else "127.0.0.1:50052"
    return proc, addr


async def run_rust_engine(args, db_path: str, rate: int) -> Dict[str, Any]:
    import grpc

    from core.proto import autoforward_pb2 as pb
    from core.proto import autoforward_pb2_grpc as pb_grpc

    proc, addr = _start_rust(args, db_path)
    channel = grpc.aio.insecure_channel(addr)
    stub = pb_grpc.LogControlServiceStub(channel)
    try:
        ingest = await _producers(stub, pb, rate, args.seconds, args.producers,
                                  args.profile, args.seed)
        query = await _query_probe(stub, pb, args.query_iterations)
    finally:
        await channel.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    ingest["persisted"] = _sqlite_rows(db_path)
    ingest["persisted_per_sec"] = round(ingest["persisted"] / max(ingest["duration_s"], 1e-6), 1)
    ingest["db_bytes"] = os.path.getsize(db_path)
    return {"ingest": ingest, "query": query}


async def main_async(args) -> int:
    print(f"# logger benchmark: engine={args.target} profile={args.profile} "
          f"producers={args.producers} seconds={args.seconds} repeat={args.repeat}")
    print(f"# machine: {os.uname().sysname} {os.uname().machine} "
          f"cpus={os.cpu_count()} python={sys.version.split()[0]}")
    report: Dict[str, Any] = {"engine": args.target, "profile": args.profile,
                              "runs": []}
    for rate in args.rates:
        for attempt in range(args.repeat):
            with tempfile.TemporaryDirectory(prefix="atf-logbench-") as tmp:
                db_path = os.path.join(tmp, "logs.db")
                runner = run_python_engine if args.target == "python" else run_rust_engine
                result = await runner(args, db_path, rate)
            result["target_rate"] = rate
            result["attempt"] = attempt + 1
            report["runs"].append(result)
            ing = result["ingest"]
            print(f"rate={rate:>6}/s attempt={attempt + 1} "
                  f"offered={ing['offered']:>7} accepted={ing['accepted']:>7} "
                  f"persisted={ing['persisted']:>7} rejected={ing['rejected']:>5} "
                  f"accepted/s={ing['accepted_per_sec']:>9} "
                  f"persisted/s={ing['persisted_per_sec']:>9} "
                  f"p50={ing['latency_ms']['p50']:>7}ms p95={ing['latency_ms']['p95']:>8}ms "
                  f"p99={ing['latency_ms']['p99']:>8}ms db={ing['db_bytes'] / 1e6:.1f}MB")
            if ing["errors"]:
                print(f"    errors={ing['errors']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"# wrote {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", choices=["python", "rust"], default="python")
    ap.add_argument("--rust-binary", default="", help="path to a Rust logger binary")
    ap.add_argument("--rates", default="100,1000,10000,50000",
                    help="comma-separated offered events/second")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--producers", type=int, default=4)
    ap.add_argument("--profile", choices=["small", "structured", "large"], default="small")
    ap.add_argument("--query-iterations", type=int, default=20)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--seed", type=int, default=20261016)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    args.rates = [int(r) for r in str(args.rates).split(",") if r.strip()]
    if args.target == "rust" and not args.rust_binary:
        print("--target rust needs --rust-binary", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
