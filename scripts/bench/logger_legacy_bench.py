#!/usr/bin/env python3
"""Side-by-side half of the logging benchmark: the *retired* Python engine.

``logger_rust_bench.py`` measures the deployed Rust engine. This script drives
the same client half — same stubs, same messages, same rates, same host, same
run length — against the **retired Python engine image** (the rollback tag kept
after the cutover), so the migration has a measured before/after instead of a
remembered one.

It exists to make the comparison reproducible, not to keep the Python engine
alive: the engine source is deleted from the tree, and this script only ever
starts the archived image, on its own throwaway bridge network, with its own
scratch bind mount. It never starts the production stack, never reuses a
production volume, and never opens the retained historical archive.

ISOLATION GUARANTEES (identical in spirit to the Rust harness)
    * ``docker run --rm`` for the engine, own network, own data dir per rate.
    * the engine's ``ATF_LOGGER_DB_PATH`` points inside the scratch mount, so
      the retired ``atf-data/logger.db`` archive is never opened.
    * nothing here imports the deleted ``logger.main`` module; only the image.

USAGE
    python3 scripts/bench/logger_legacy_bench.py \\
        --rates 1000,10000 --seconds 5 --producers 4 \\
        --out docs/benchmarks/logger-python-bench-scenario-a.json

Pair it with the Rust run of the same shape::

    python3 -m scripts.bench.logger_rust_bench --rates 1000,10000 --seconds 5 \\
        --producers 4 --profile small --query-iterations 20 \\
        --engine-args --batch-size 512 --batch-delay-ms 2 --ack-mode durable \\
        --out docs/benchmarks/logger-rust-bench-scenario-a.json

Read the numbers as "what the host + this client can push through engine X",
not as a saturation point: a single CPython client saturates before either
engine does, and the Python engine exposes no batching/ack-mode knobs at all,
so it runs with its own defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logger_rust_bench import (  # noqa: E402  — shared client half, single copy
    DEFAULT_CLIENT_IMAGE,
    DEFAULT_SCRATCH,
    build_client_argv,
    image_id,
    parse_client_json,
    sqlite_row_count,
)

DEFAULT_ENGINE_IMAGE = "autotelegramforward-logger-python:legacy-rollback"
ENGINE_PORT = 50052


def docker(args: List[str], bin_: str = "docker", check: bool = True,
           timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run([bin_] + args, capture_output=True, text=True,
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {bin_} {' '.join(args)}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return proc


def wait_for_tcp(name: str, port: int, bin_: str = "docker",
                 timeout: float = 60.0) -> None:
    """The Python engine has no HTTP surface; wait for its gRPC port instead.

    Probed from inside the engine container, which is where its interpreter is.
    """
    code = (
        "import socket,sys,time\n"
        f"dl=time.time()+{timeout}\n"
        "while time.time()<dl:\n"
        "    try:\n"
        f"        socket.create_connection(('127.0.0.1',{port}),2).close()\n"
        "        sys.exit(0)\n"
        "    except OSError:\n"
        "        time.sleep(0.2)\n"
        "sys.exit(1)\n"
    )
    proc = docker(["exec", name, "python", "-c", code], bin_=bin_, check=False,
                  timeout=timeout + 30)
    if proc.returncode != 0:
        logs = docker(["logs", name], bin_=bin_, check=False).stdout
        raise RuntimeError(f"engine {name} never accepted TCP on {port}\n{logs}")


def run_rate(args, rate: int, attempt: int, network: str, suffix: str) -> Dict[str, Any]:
    run_dir = os.path.join(args.scratch, f"legacy-run-{rate}-{attempt}-{suffix}")
    data_dir = os.path.join(run_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    os.chmod(run_dir, 0o777)
    os.chmod(data_dir, 0o777)
    db_path = os.path.join(data_dir, "bench.db")

    engine_name = f"atf-legacy-engine-{suffix}"
    client_name = f"atf-legacy-client-{suffix}"
    engine_env = {
        "ATF_LOGGER_DB_PATH": "/app/data/bench.db",
        "ATF_LOGGER_HOST": "0.0.0.0",
        "ATF_LOGGER_PORT": str(ENGINE_PORT),
        "ATF_LOGGER_BUSY_TIMEOUT_MS": "5000",
    }
    engine_argv = [args.docker, "run", "-d", "--rm", "--name", engine_name,
                   "--network", network, "--network-alias", engine_name]
    for key, value in engine_env.items():
        engine_argv += ["-e", f"{key}={value}"]
    engine_argv += ["-v", f"{data_dir}:/app/data", args.engine_image]

    print(f"# {rate}/s: starting legacy engine {engine_name}", file=sys.stderr)
    docker(["rm", "-f", engine_name], bin_=args.docker, check=False)
    try:
        started = docker(engine_argv[1:], bin_=args.docker).stdout.strip()
        wait_for_tcp(engine_name, ENGINE_PORT, bin_=args.docker)

        client_argv = build_client_argv(
            args.client_image, client_name, network,
            os.path.abspath(Path(__file__).resolve().parent / "logger_rust_bench.py"),
            f"{engine_name}:{ENGINE_PORT}", rate, args.seconds, args.producers,
            args.profile, args.query_iterations, args.seed, args.max_inflight,
            args.ts_unit, args.client_processes, docker=args.docker)
        client = subprocess.run(client_argv, capture_output=True, text=True,
                                timeout=max(120.0, args.seconds * 12))
        payload = parse_client_json(client.stdout) or {}
        entry: Dict[str, Any] = {
            "target_rate": rate,
            "attempt": attempt,
            "engine_command": engine_argv,
            "client_command": client_argv,
            "engine_container": started,
            "engine_env": engine_env,
            "engine_logs_tail": docker(["logs", "--tail", "20", engine_name],
                                       bin_=args.docker, check=False).stdout,
            "ingest": payload.get("ingest"),
            "query": payload.get("query"),
            "client_exit_code": client.returncode,
        }
        # The row count is read off the archive, never taken from the client.
        entry["persisted_rows"] = sqlite_row_count(db_path)
        return entry
    finally:
        if not args.keep:
            docker(["rm", "-f", engine_name], bin_=args.docker, check=False)
        else:
            print(f"# kept {engine_name} (--keep)", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Drive the retired Python engine with the shared benchmark client.")
    ap.add_argument("--engine-image", default=DEFAULT_ENGINE_IMAGE,
                    help="archived Python engine image under test")
    ap.add_argument("--client-image", default=DEFAULT_CLIENT_IMAGE,
                    help="image providing grpcio + the generated stubs")
    ap.add_argument("--rates", default="1000,10000")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--producers", type=int, default=4)
    ap.add_argument("--client-processes", type=int, default=1)
    ap.add_argument("--profile", choices=["small", "structured", "large"],
                    default="small")
    ap.add_argument("--query-iterations", type=int, default=20)
    ap.add_argument("--max-inflight", type=int, default=64)
    ap.add_argument("--ts-unit", choices=["s", "ms"], default="s")
    ap.add_argument("--seed", type=int, default=20261016)
    ap.add_argument("--scratch", default=DEFAULT_SCRATCH)
    ap.add_argument("--out", default="")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--docker", default="docker")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    os.makedirs(args.scratch, exist_ok=True)
    rates = [int(r) for r in args.rates.split(",") if r.strip()]
    suffix = f"{os.getpid()}-{int(time.time())}"
    network = f"atf-legacy-bench-{suffix}"

    docker(["network", "create", network], bin_=args.docker)
    report: Dict[str, Any] = {
        "tool": "scripts/bench/logger_legacy_bench.py",
        "engine_kind": "python (retired; rollback image)",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": {"sysname": platform.system(), "machine": platform.machine(),
                 "cpus": os.cpu_count(), "python": platform.python_version()},
        "engine_image": {"name": args.engine_image,
                         "id": image_id(args.engine_image, docker=args.docker)},
        "client_image": {"name": args.client_image,
                         "id": image_id(args.client_image, docker=args.docker)},
        "config": {"seconds": args.seconds, "producers": args.producers,
                   "profile": args.profile,
                   "query_iterations": args.query_iterations,
                   "max_inflight": args.max_inflight,
                   "client_processes": args.client_processes,
                   "ts_unit": args.ts_unit, "rates": rates,
                   "engine_env": "defaults (no batching/ack-mode knobs exist)"},
        "runs": [],
    }
    try:
        for rate in rates:
            report["runs"].append(run_rate(args, rate, 1, network, suffix))
    finally:
        docker(["network", "rm", network], bin_=args.docker, check=False)

    text = json.dumps(report, indent=1, sort_keys=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"# wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    for run in report["runs"]:
        ingest = run.get("ingest") or {}
        print(f"# {run['target_rate']}/s offered={ingest.get('offered')} "
              f"accepted={ingest.get('accepted')} rejected={ingest.get('rejected')} "
              f"persisted_rows={run.get('persisted_rows')} "
              f"p50={((ingest.get('latency_ms') or {}).get('p50'))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
