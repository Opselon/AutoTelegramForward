#!/usr/bin/env python3
"""Reproducible benchmark harness for the **Rust** gRPC logging engine.

This replaces ``scripts/bench/logger_baseline.py`` (which imported the Python
engine's ``logger.main`` and therefore died with it). It measures the real
deployed artifact — the ``autotelegramforward-logger-rust`` container image —
over the real gRPC path, with the real protobuf messages and the real SQLite
archive, and keeps the four states apart:

    OFFERED    events the producers handed to the transport (paced)
    ACCEPTED   ``Log`` answered ``success=true`` (committed, in durable mode)
    PERSISTED  rows actually read back from the database afterwards
    REJECTED   ``Log`` failed or answered ``success=false``

Nothing here trusts the client's own delivery claim: PERSISTED is a row count
read from the on-disk database *after* the run, so ACCEPTED ≠ PERSISTED is
visible rather than hidden.

The harness never touches a production container. It starts its own engine with
``docker run --rm``, its own client with ``docker run --rm``, on its own
throwaway bridge network, with its own scratch bind mount under
``--scratch`` (default ``/root/.hermes/cache/scratch``). It never runs
``docker compose`` and never reuses a production volume.

Two execution modes share one flag set:

  * orchestrator (default) — runs on the host; needs only the ``docker`` CLI and
    Python's stdlib (``sqlite3``). It starts the engine and client containers,
    parses the client's JSON, then reads the row count straight off the bind
    mount.
  * ``--client`` — runs *inside* a container that has grpcio and the generated
    stubs (``autotelegramforward-core``), drives the load, and prints one
    ``BENCH_JSON {...}`` line. The orchestrator mounts this very file into the
    client container and re-invokes it, so there is no second copy to drift.

Examples
--------
    # host-side, drives the already-built image end to end
    python -m scripts.bench.logger_rust_bench \\
        --rates 1000,10000,50000 --seconds 5 --producers 4 \\
        --out /tmp/logger-rust-bench.json

    # a single rate, keeping the scratch dir and its database for inspection
    python -m scripts.bench.logger_rust_bench --rates 20000 --keep

    # the client half, run by hand against any reachable engine
    python -m scripts.bench.logger_rust_bench --client \\
        --target-addr 127.0.0.1:50052 --rate 10000 --seconds 5

The module is import-safe: it pulls in neither ``grpc`` nor any Docker call at
import time, so ``pytest`` can import it to test the pure helpers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

#: The line prefix the in-container client uses to hand its JSON to the host.
SENTINEL = "BENCH_JSON "

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")

DEFAULT_IMAGE = "autotelegramforward-logger-rust:latest"
DEFAULT_CLIENT_IMAGE = "autotelegramforward-core:latest"
DEFAULT_SCRATCH = "/root/.hermes/cache/scratch"

#: The engine's own defaults, made explicit in the command line so a bug in the
#: harness can never silently measure a different configuration than documented.
DEFAULT_ENGINE_ARGS = ("--batch-size", "512", "--batch-delay-ms", "2",
                       "--ack-mode", "durable")


# --------------------------------------------------------------------------- #
# pure helpers (no grpc, no docker — unit-testable)
# --------------------------------------------------------------------------- #

def percentiles(samples: List[float]) -> Dict[str, float]:
    """Nearest-rank percentiles; ``mean`` is the arithmetic mean."""
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


def parse_listening_addr(log_text: str) -> Optional[str]:
    """Last token of the engine's handshake line, ``listening on <addr>``."""
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith("listening on"):
            parts = line.split()
            if len(parts) >= 3:
                return parts[-1]
    return None


def parse_client_json(stdout: str) -> Optional[Dict[str, Any]]:
    """The single ``BENCH_JSON {...}`` line the in-container client prints."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(SENTINEL):
            return json.loads(line[len(SENTINEL):])
    return None


def sqlite_row_count(db_path: str) -> Optional[int]:
    """Rows in the engine's ``logs`` table, read from the archive itself."""
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def build_engine_argv(image: str, name: str, network: str, data_dir: str,
                      port: int, metrics_port: int, ts_unit: str,
                      collect_levels: str,
                      engine_args: List[str],
                      docker: str = "docker") -> List[str]:
    """``docker run`` for the engine under test (foreground, auto-removed)."""
    env_flags = []
    if collect_levels:
        env_flags = ["-e", f"ATF_LOGGER_COLLECT_LEVELS={collect_levels}"]
    return [
        docker, "run", "-d", "--rm",
        "--name", name,
        "--network", network,
        "--network-alias", name,
        *env_flags,
        "-v", f"{data_dir}:/app/data",
        image,
        "--db", "/app/data/bench.db",
        "--listen", f"0.0.0.0:{port}",
        "--metrics-listen", f"0.0.0.0:{metrics_port}",
        "--retention-ts-unit", ts_unit,
        "--print-addr",
        *engine_args,
    ]


def build_client_argv(client_image: str, name: str, network: str,
                      script_path: str, target_addr: str, rate: int,
                      seconds: float, producers: int, profile: str,
                      query_iterations: int, seed: int, max_inflight: int,
                      ts_unit: str, client_processes: int = 1,
                      docker: str = "docker") -> List[str]:
    """``docker run`` for the in-container load generator (mounts this file)."""
    return [
        docker, "run", "--rm",
        "--name", name,
        "--network", network,
        "-v", f"{script_path}:/bench/logger_rust_bench.py:ro",
        client_image,
        "python3", "/bench/logger_rust_bench.py", "--client",
        "--target-addr", target_addr,
        "--rate", str(rate),
        "--seconds", str(seconds),
        "--producers", str(producers),
        "--profile", profile,
        "--query-iterations", str(query_iterations),
        "--seed", str(seed),
        "--max-inflight", str(max_inflight),
        "--ts-unit", ts_unit,
        "--client-processes", str(client_processes),
    ]


# --------------------------------------------------------------------------- #
# load generation (client side — imports grpc lazily)
# --------------------------------------------------------------------------- #

def _load_stubs():
    """Import the generated stubs, container-first then from the repo tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    for candidate in (repo_root, "/app", "/bench"):
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)
    # /app is the container layout; put it ahead of the repo so the image's
    # generated code always wins inside a container.
    if "/app" in sys.path:
        sys.path.insert(0, sys.path.pop(sys.path.index("/app")))
    from core.proto import autoforward_pb2 as pb          # type: ignore
    from core.proto import autoforward_pb2_grpc as pb_grpc  # type: ignore
    return pb, pb_grpc


def make_entry(pb, seq: int, rng: random.Random, profile: str, ts_unit: str):
    """One representative event. Small by default: a log line, not a payload."""
    level = LEVELS[rng.choices([0, 1, 2, 3, 4], weights=[5, 60, 20, 14, 1])[0]]
    if profile == "large":
        detail = json.dumps({"seq": seq, "blob": "x" * 2048, "rule_id": f"r-{seq % 97}"})
    elif profile == "structured":
        detail = json.dumps({
            "seq": seq, "rule_id": f"rule-{seq % 397}", "chat_id": f"-100{seq % 1000}",
            "attempt": seq % 4, "duration_ms": seq % 900,
            "result": "sent" if seq % 7 else "retry",
        })
    else:
        detail = json.dumps({"seq": seq, "rule_id": f"r-{seq % 97}"})
    ts = int(time.time() * 1000) if ts_unit == "ms" else int(time.time())
    return pb.LogEntry(
        ts=ts,
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
                     profile: str, seed: int, ts_unit: str,
                     max_inflight: int = 64) -> Dict[str, Any]:
    """Offer ``rate`` events/second across ``producers`` concurrent clients.

    Offering is paced independently of RPC completion: a worker blocks only when
    it already has ``max_inflight`` requests outstanding. That is the honest
    place to apply backpressure — pausing the loop instead would measure the
    harness's own round-trip latency rather than the server's ceiling. Requests
    still in flight when the clock stops are reported as PENDING, never folded
    into ACCEPTED.
    """
    per_producer = max(1, int(rate / producers))
    interval = 1.0 / per_producer
    stop_at = time.perf_counter() + seconds
    latencies: List[float] = []
    accepted = rejected = offered = 0
    errors: Dict[str, int] = {}
    blocked_ms = 0.0
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
            key = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            errors[key] = errors.get(key, 0) + 1

    async def one(worker: int) -> None:
        nonlocal offered, blocked_ms
        rng = random.Random(seed + worker)
        seq = worker * 1_000_000
        sem = asyncio.Semaphore(max_inflight)
        next_at = time.perf_counter()
        while time.perf_counter() < stop_at:
            seq += 1
            entry = make_entry(pb, seq, rng, profile, ts_unit)
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
                        err = getattr(resp, "error_code", "") or "rejected"
                        raise RuntimeError(f"rejected:{err}")
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
        "latency_ms": percentiles(latencies),
        "duration_s": round(duration, 2),
        # Raw samples ride along so a multi-process client can pool them before
        # computing percentiles. Stripped by strip_samples() before reporting.
        "_latencies": latencies,
    }


def strip_samples(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Drop raw samples (and any merge-only key) from a producer result."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}


async def _query_probe(stub, pb, iterations: int) -> Dict[str, Any]:
    """Read-side latency: severity filter, search, aggregation, page, stats."""
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
            except Exception:  # noqa: BLE001 — a failed probe is not a latency sample
                pass
        out[name] = percentiles(samples)
    return out


async def _client_load(args, rate: int, seed: int) -> Dict[str, Any]:
    """One client process: drive the load, return the raw producer result."""
    import grpc

    pb, pb_grpc = _load_stubs()
    channel = grpc.aio.insecure_channel(
        args.target_addr,
        options=[("grpc.max_receive_message_length", 16 << 20),
                 ("grpc.max_send_message_length", 16 << 20)],
    )
    try:
        stub = pb_grpc.LogControlServiceStub(channel)
        return await _producers(stub, pb, rate, args.seconds, args.producers,
                                args.profile, seed, args.ts_unit,
                                args.max_inflight)
    finally:
        await channel.close()


async def _client_query(args) -> Dict[str, Any]:
    import grpc

    pb, pb_grpc = _load_stubs()
    channel = grpc.aio.insecure_channel(args.target_addr)
    try:
        stub = pb_grpc.LogControlServiceStub(channel)
        return await _query_probe(stub, pb, args.query_iterations)
    finally:
        await channel.close()


def _merge_ingest(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pool N per-process results into one: sum counters, union the samples."""
    samples: List[float] = []
    errors: Dict[str, int] = {}
    offered = accepted = rejected = pending = 0
    blocked = 0.0
    duration = 0.0
    for part in parts:
        samples.extend(part.get("_latencies", []))
        offered += part["offered"]
        accepted += part["accepted"]
        rejected += part["rejected"]
        pending += part["pending_at_stop"]
        blocked += part.get("blocked_ms", 0.0)
        duration = max(duration, part["duration_s"])
        for k, v in part.get("errors", {}).items():
            errors[k] = errors.get(k, 0) + v
    duration = max(duration, 1e-6)
    return {
        "offered": offered,
        "accepted": accepted,
        "rejected": rejected,
        "pending_at_stop": pending,
        "errors": errors,
        "blocked_ms": round(blocked, 1),
        "offered_per_sec": round(offered / duration, 1),
        "accepted_per_sec": round(accepted / duration, 1),
        "latency_ms": percentiles(samples),
        "duration_s": round(duration, 2),
        "client_processes": len(parts),
    }


def run_client(args) -> int:
    """The ``--client`` half: drive one engine address, print one JSON line.

    A single CPython process tops out at roughly ten thousand Log RPCs per
    second (protobuf encode + task churn), which on a small host is reached
    before the engine is. ``--client-processes N`` forks N workers so the
    aggregate offered load can exceed that ceiling; the fork happens *before*
    grpc is imported, so no process inherits a live C-core channel.
    """
    if args.client_processes <= 1:
        ingest = asyncio.run(_client_load(args, args.rate, args.seed))
        query = asyncio.run(_client_query(args))
        payload = {"target_rate": args.rate, "ingest": strip_samples(ingest),
                   "query": query}
        print(SENTINEL + json.dumps(payload), flush=True)
        return 0

    import tempfile

    n = args.client_processes
    per_process = max(1, args.rate // n)
    workdir = tempfile.mkdtemp(prefix="atf-bench-client-")
    paths = [os.path.join(workdir, f"child-{i}.json") for i in range(n)]
    pids: List[int] = []
    for i in range(n):
        pid = os.fork()
        if pid == 0:  # child
            status = 0
            try:
                result = asyncio.run(
                    _client_load(args, per_process, args.seed + i * 7919))
                with open(paths[i], "w", encoding="utf-8") as fh:
                    json.dump(result, fh)
            except BaseException:  # noqa: BLE001 — never leak a traceback to stdout
                status = 1
            finally:
                os._exit(status)
        pids.append(pid)

    failures = 0
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        if status != 0:
            failures += 1
    parts = []
    for path in paths:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                parts.append(json.load(fh))
    if failures or not parts:
        print(f"# {failures}/{n} client processes failed", file=sys.stderr)
    ingest = _merge_ingest(parts)
    query = asyncio.run(_client_query(args))
    payload = {"target_rate": args.rate, "ingest": ingest, "query": query}
    print(SENTINEL + json.dumps(payload), flush=True)
    return 0


# --------------------------------------------------------------------------- #
# orchestration (host side — stdlib + the docker CLI only)
# --------------------------------------------------------------------------- #

def _docker(args: List[str], docker: str = "docker", check: bool = True,
            timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    cmd = [docker] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


def image_id(image: str, docker: str = "docker") -> Optional[str]:
    proc = _docker(["inspect", "--format", "{{index .Id}}", image],
                   docker=docker, check=False)
    return proc.stdout.strip() or None


def wait_for_engine(name: str, metrics_port: int, docker: str = "docker",
                    timeout: float = 30.0) -> str:
    """Wait for the handshake line, then for ``/healthz`` to answer 200."""
    deadline = time.time() + timeout
    addr = None
    while time.time() < deadline:
        logs = _docker(["logs", name], docker=docker, check=False).stdout
        addr = parse_listening_addr(logs)
        if addr:
            break
        time.sleep(0.1)
    if not addr:
        raise RuntimeError(f"engine {name} never printed its listening address")
    while time.time() < deadline:
        probe = _docker(
            ["exec", name, "curl", "-fsS", f"http://127.0.0.1:{metrics_port}/healthz"],
            docker=docker, check=False)
        if probe.returncode == 0:
            return addr
        time.sleep(0.2)
    raise RuntimeError(f"engine {name} never became ready on /healthz")


def scrape_metrics(name: str, metrics_port: int, docker: str = "docker"
                   ) -> Optional[str]:
    """Best-effort scrape of the engine's Prometheus text surface."""
    proc = _docker(
        ["exec", name, "curl", "-fsS", f"http://127.0.0.1:{metrics_port}/metrics"],
        docker=docker, check=False)
    return proc.stdout if proc.returncode == 0 else None


def run_once(args, rate: int, attempt: int, network: str, suffix: str) -> Dict[str, Any]:
    """One engine + one client + one archive, from start to durable row count."""
    run_dir = os.path.join(args.scratch, f"run-{rate}-{attempt}-{suffix}")
    data_dir = os.path.join(run_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    # The engine runs as an unprivileged user inside the image; the bind mount
    # has to be writable by that uid without knowing it.
    os.chmod(run_dir, 0o777)
    os.chmod(data_dir, 0o777)

    engine_name = f"atf-bench-engine-{suffix}"
    client_name = f"atf-bench-client-{suffix}"
    db_path = os.path.join(data_dir, "bench.db")

    engine_argv = build_engine_argv(
        args.image, engine_name, network, data_dir, args.engine_port,
        args.metrics_port, args.ts_unit, args.collect_levels,
        list(args.engine_args), docker=args.docker)
    client_argv = build_client_argv(
        args.client_image, client_name, network, os.path.abspath(__file__),
        f"{engine_name}:{args.engine_port}", rate, args.seconds, args.producers,
        args.profile, args.query_iterations, args.seed, args.max_inflight,
        args.ts_unit, args.client_processes, docker=args.docker)

    result: Dict[str, Any] = {
        "target_rate": rate,
        "attempt": attempt,
        "engine_command": engine_argv,
        "client_command": client_argv,
        "listening_addr": None,
    }

    # Start the engine (build_engine_argv already carries docker + run).
    proc = _docker(engine_argv[1:], docker=args.docker)
    engine_cid = proc.stdout.strip()
    result["engine_container"] = engine_cid
    try:
        result["listening_addr"] = wait_for_engine(
            engine_name, args.metrics_port, docker=args.docker)

        client = _docker(client_argv[1:], docker=args.docker, check=False)
        if client.returncode != 0:
            raise RuntimeError(
                f"client container exited {client.returncode}\n"
                f"stderr: {client.stderr}\nstdout: {client.stdout}")
        payload = parse_client_json(client.stdout)
        if payload is None:
            raise RuntimeError(
                f"client produced no BENCH_JSON line\nstderr: {client.stderr}\n"
                f"stdout: {client.stdout}")

        result["metrics_excerpt"] = _metrics_excerpt(
            scrape_metrics(engine_name, args.metrics_port, docker=args.docker))
    finally:
        # Graceful stop drains the queue; a kill measures the un-drained gap.
        if args.graceful_stop:
            _docker(["stop", "-t", "20", engine_name], docker=args.docker, check=False)
        else:
            _docker(["kill", engine_name], docker=args.docker, check=False)
        result["engine_logs_tail"] = _docker(
            ["logs", engine_name], docker=args.docker, check=False).stdout.splitlines()[-8:]

    # The durability check reads the archive, after the engine has stopped.
    ingest = dict(payload["ingest"])
    persisted = sqlite_row_count(db_path)
    ingest["persisted"] = persisted
    ingest["persisted_per_sec"] = (
        round(persisted / max(ingest["duration_s"], 1e-6), 1) if persisted else None)
    ingest["db_bytes"] = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    result["ingest"] = ingest
    result["query"] = payload["query"]
    if not args.keep:
        shutil.rmtree(run_dir, ignore_errors=True)
    else:
        result["scratch_dir"] = run_dir
    return result


def _metrics_excerpt(text: Optional[str]) -> Optional[List[str]]:
    if not text:
        return None
    keep = ("accepted", "persisted", "rejected", "queue", "dropped", "batch")
    lines = [ln for ln in text.splitlines()
             if ln and not ln.startswith("#") and any(k in ln for k in keep)]
    return lines[:40]


def run(args) -> int:
    suffix = f"{os.getpid()}-{int(time.time())}"
    network = f"atf-bench-net-{suffix}"
    os.makedirs(args.scratch, exist_ok=True)

    engine_digest = image_id(args.image, docker=args.docker)
    client_digest = image_id(args.client_image, docker=args.docker)

    report: Dict[str, Any] = {
        "tool": "scripts/bench/logger_rust_bench.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {
            "sysname": os.uname().sysname,
            "machine": os.uname().machine,
            "cpus": os.cpu_count(),
            "python": sys.version.split()[0],
        },
        "engine_image": {"name": args.image, "id": engine_digest},
        "client_image": {"name": args.client_image, "id": client_digest},
        "config": {
            "seconds": args.seconds,
            "producers": args.producers,
            "profile": args.profile,
            "query_iterations": args.query_iterations,
            "max_inflight": args.max_inflight,
            "client_processes": args.client_processes,
            "ts_unit": args.ts_unit,
            "engine_args": list(args.engine_args),
            "graceful_stop": args.graceful_stop,
            "rates": list(args.rates),
        },
        "runs": [],
    }

    print(f"# Rust logger benchmark — engine={args.image} ({engine_digest})")
    print(f"# client={args.client_image} ({client_digest})")
    print(f"# host={report['host']['machine']} cpus={report['host']['cpus']} "
          f"python={report['host']['python']} mode=container-to-container")
    print(f"# profile={args.profile} producers={args.producers} seconds={args.seconds} "
          f"engine_args={' '.join(args.engine_args)}")

    _docker(["network", "create", network], docker=args.docker)
    try:
        for rate in args.rates:
            for attempt in range(1, args.repeat + 1):
                run_suffix = f"{suffix}-{rate}-{attempt}"
                res = run_once(args, rate, attempt, network, run_suffix)
                report["runs"].append(res)
                ing = res["ingest"]
                print(f"rate={rate:>7}/s attempt={attempt} "
                      f"offered={ing['offered']:>8} accepted={ing['accepted']:>8} "
                      f"persisted={ing['persisted'] if ing['persisted'] is not None else -1:>8} "
                      f"rejected={ing['rejected']:>6} pending={ing['pending_at_stop']:>4} "
                      f"accepted/s={ing['accepted_per_sec']:>10} "
                      f"persisted/s={ing['persisted_per_sec']:>10} "
                      f"p50={ing['latency_ms']['p50']:>8}ms "
                      f"p95={ing['latency_ms']['p95']:>9}ms "
                      f"p99={ing['latency_ms']['p99']:>9}ms "
                      f"db={ing['db_bytes'] / 1e6:.1f}MB")
                if ing["errors"]:
                    print(f"    errors={ing['errors']}")
    finally:
        _docker(["network", "rm", network], docker=args.docker, check=False)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"# wrote {args.out}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Reproducible benchmark for the Rust logger engine.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--image", default=DEFAULT_IMAGE, help="engine image under test")
    ap.add_argument("--client-image", default=DEFAULT_CLIENT_IMAGE,
                    help="image that carries grpcio + the generated stubs")
    ap.add_argument("--rates", default="1000,10000,50000",
                    help="comma-separated offered events/second")
    ap.add_argument("--seconds", type=float, default=5.0, help="load phase length")
    ap.add_argument("--producers", type=int, default=4, help="concurrent client workers")
    ap.add_argument("--profile", choices=["small", "structured", "large"],
                    default="small", help="payload shape")
    ap.add_argument("--query-iterations", type=int, default=20,
                    help="samples per read-side probe")
    ap.add_argument("--max-inflight", type=int, default=64,
                    help="outstanding Log RPCs per producer before it blocks")
    ap.add_argument("--ts-unit", choices=["s", "ms"], default="s",
                    help="unit written into LogEntry.ts (engine stores verbatim)")
    ap.add_argument("--collect-levels", default="all",
                    help="levels the engine collects (ATF_LOGGER_COLLECT_LEVELS), "
                         "e.g. 'all' or 'DEBUG,INFO,WARN,ERROR,CRITICAL'")
    ap.add_argument("--repeat", type=int, default=1, help="attempts per rate")
    ap.add_argument("--seed", type=int, default=20261016, help="producer RNG seed")
    ap.add_argument("--engine-port", type=int, default=50052)
    ap.add_argument("--metrics-port", type=int, default=9109)
    ap.add_argument("--scratch", default=DEFAULT_SCRATCH,
                    help="host directory for per-run bind mounts")
    ap.add_argument("--out", default="", help="write the JSON report here")
    ap.add_argument("--keep", action="store_true",
                    help="keep each run's scratch dir and database")
    ap.add_argument("--no-graceful-stop", dest="graceful_stop",
                    action="store_false", default=True,
                    help="kill the engine instead of draining it (hides the "
                         "queued→persisted gap on purpose)")
    ap.add_argument("--docker", default="docker", help="docker CLI binary")
    ap.add_argument("--client", action="store_true",
                    help="run the in-container load generator against --target-addr")
    ap.add_argument("--target-addr", default="127.0.0.1:50052",
                    help="engine address for --client mode")
    ap.add_argument("--rate", type=int, default=1000,
                    help="offered events/second for --client mode")
    ap.add_argument("--client-processes", type=int, default=1,
                    help="fork this many client processes to lift the harness's "
                         "own ~10k RPC/s per-process ceiling")
    ap.add_argument("--engine-args", nargs=argparse.REMAINDER,
                    default=list(DEFAULT_ENGINE_ARGS),
                    help="extra engine flags, must come last")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(argv)
    if isinstance(args.rates, str):
        args.rates = [int(r.strip()) for r in args.rates.split(",") if r.strip()]
    if args.client:
        return run_client(args)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
