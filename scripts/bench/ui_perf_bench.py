#!/usr/bin/env python3
"""UI/UX + hot-path performance benchmark for the ATF stack.

Measures the three surfaces the dashboard, the mobile client and the bot all sit
on, so a UI change can be judged by numbers instead of by how it feels:

  http   live API latency per endpoint (p50/p95/p99, requests/s) against the
         running stack, authenticated with the global X-API-Key
  web    production bundle size of web/frontend (raw + gzip, largest chunks)
  bot    catalog + keyboard hot paths (translation lookup, menu assembly)

Usage:
    python3 scripts/bench/ui_perf_bench.py --label baseline
    python3 scripts/bench/ui_perf_bench.py --label after --skip-web-build

Writes docs/benchmarks/ui-perf-<label>.json and prints a comparison-ready
summary. Exits non-zero if an endpoint fails, so CI can gate on it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
OUT_DIR = ROOT / "docs" / "benchmarks"

ENDPOINTS = [
    ("version", "/api/version", 60),
    ("health_services", "/api/v1/health/services", 60),
    ("rules_list", "/api/rules", 40),
    ("tasks_list", "/api/tasks", 40),
    ("logs_tail", "/api/logs?limit=50", 30),
    ("logs_stats", "/api/logs/stats", 30),
    ("settings_schema", "/api/rules/settings/schema", 30),
]

TOKEN_CACHE = Path("/tmp/atf-bench-web-token.json")


def _core_address() -> str:
    """Resolve the core gRPC address: env override, else the container's bridge IP."""
    explicit = os.environ.get("ATF_BENCH_CORE")
    if explicit:
        return explicit
    done = subprocess.run(
        ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", "atf-core"],
        capture_output=True,
        text=True,
    )
    for address in done.stdout.split():
        if address:
            return f"{address}:50051"
    return "127.0.0.1:50051"


def mint_web_token(user_id: int) -> str:
    """Ask the core for a dashboard JWT (the same one the bot hands to a user).

    Dashboard routes validate a Bearer JWT against the core, so an X-API-Key is
    not enough to benchmark them: without this every user-scoped endpoint
    answers 401 in 0.6 ms and the numbers look wonderful and mean nothing.
    The token is cached until shortly before it expires.
    """
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
            if cached.get("user_id") == user_id and cached.get("expires_at", 0) - 300 > time.time():
                return cached["token"]
        except Exception:  # noqa: BLE001 - a broken cache is just a miss
            pass

    sys.path.insert(0, str(ROOT))
    import grpc  # noqa: PLC0415 - optional dependency, only needed for auth

    from core.proto import autoforward_pb2, autoforward_pb2_grpc  # noqa: PLC0415

    token = env_value("ATF_CORE_RPC_TOKEN")
    metadata = [("authorization", f"Bearer {token}")] if token else []
    channel = grpc.insecure_channel(_core_address())
    stub = autoforward_pb2_grpc.AccountControlServiceStub(channel)
    response = stub.IssueWebToken(
        autoforward_pb2.IssueWebTokenRequest(user_id=user_id), timeout=15, metadata=metadata
    )
    channel.close()
    if not response.success or not response.token:
        raise RuntimeError(f"token mint failed: {response.error_code or response.message}")
    TOKEN_CACHE.write_text(
        json.dumps({"user_id": user_id, "token": response.token, "expires_at": response.expires_at or int(time.time()) + 3600})
    )
    return response.token



def env_value(key: str) -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip("'\"")
    return ""


def bench_http(base: str, api_key: str, runs: int | None, token: str | None = None) -> dict:
    results = {}
    for name, path, default_iterations in ENDPOINTS:
        iterations = runs or default_iterations
        url = base.rstrip("/") + path
        timings: list[float] = []
        status = None
        for index in range(iterations + 5):
            headers = {"X-API-Key": api_key, "Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(url, headers=headers)
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    response.read()
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.read()
            except Exception as exc:  # noqa: BLE001 - report, do not abort the sweep
                status = f"error: {exc}"
                break
            elapsed = (time.perf_counter() - started) * 1000
            if index >= 5:  # warmup
                timings.append(elapsed)
        if not timings:
            results[name] = {"path": path, "status": status, "error": "no successful samples"}
            continue
        if status != 200:
            # A 401 is fast and meaningless: never let it read as a good number.
            results[name] = {"path": path, "status": status, "error": f"endpoint did not answer 200 (got {status})"}
            continue
        timings.sort()

        def pct(p: float) -> float:
            return round(timings[min(len(timings) - 1, int(len(timings) * p))], 2)

        results[name] = {
            "path": path,
            "status": status,
            "n": len(timings),
            "mean_ms": round(statistics.fmean(timings), 2),
            "p50_ms": pct(0.50),
            "p95_ms": pct(0.95),
            "p99_ms": pct(0.99),
            "max_ms": round(timings[-1], 2),
            "rps": round(1000 / statistics.fmean(timings), 1) if statistics.fmean(timings) else 0,
        }
    return results


def bench_web(build: bool) -> dict:
    frontend = ROOT / "web" / "frontend"
    started = time.perf_counter()
    if build:
        npm = "npm"
        done = subprocess.run(
            [npm, "run", "build"],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=900,
        )
        build_seconds = round(time.perf_counter() - started, 2)
        if done.returncode != 0:
            return {"error": (done.stderr or done.stdout)[-800:], "build_seconds": build_seconds}
    else:
        build_seconds = None
    assets = sorted((frontend / "dist" / "assets").glob("*.js"), key=lambda p: p.stat().st_size, reverse=True)
    if not assets:
        return {"error": "no dist/assets/*.js — run the build"}
    total_raw = sum(p.stat().st_size for p in assets)
    total_gz = sum(len(gzip.compress(p.read_bytes(), 6)) for p in assets)
    largest = [
        {
            "file": p.name,
            "kb": round(p.stat().st_size / 1024, 1),
            "gzip_kb": round(len(gzip.compress(p.read_bytes(), 6)) / 1024, 1),
        }
        for p in assets[:6]
    ]
    return {
        "chunks": len(assets),
        "total_kb": round(total_raw / 1024, 1),
        "total_gzip_kb": round(total_gz / 1024, 1),
        "largest": largest,
        "build_seconds": build_seconds,
    }


def bench_bot(iterations: int = 20000) -> dict:
    """Micro-benchmark the two bot hot paths: catalog lookup and menu assembly."""
    catalog_path = ROOT / "core" / "infrastructure" / "telegram" / "locales" / "translations.json"
    if not catalog_path.exists():
        return {"error": "catalog missing"}
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    en = catalog["en"]
    keys = list(en.keys())[:2000]

    started = time.perf_counter()
    for index in range(iterations):
        en[keys[index % len(keys)]]
    raw_ns = (time.perf_counter() - started) / iterations * 1e9

    started = time.perf_counter()
    blob = en.get("ui_back", "")
    for _ in range(iterations):
        blob = en.get("ui_back", "")
    get_ns = (time.perf_counter() - started) / iterations * 1e9

    started = time.perf_counter()
    for _ in range(2000):
        row = [(k, en[k]) for k in keys[:40]]
    menu_ms = (time.perf_counter() - started) / 2000 * 1000

    return {
        "catalog_keys": len(en),
        "lookup_ns": round(get_ns, 1),
        "menu_40_buttons_ms": round(menu_ms, 3),
        "_unused": round(raw_ns, 1),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--base", default=os.environ.get("ATF_BENCH_BASE", "http://127.0.0.1:8080"))
    parser.add_argument("--http-runs", type=int, default=None, help="override iterations per endpoint")
    parser.add_argument("--skip-web", action="store_true")
    parser.add_argument("--skip-web-build", action="store_true")
    parser.add_argument("--skip-bot", action="store_true")
    parser.add_argument("--http-only", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.environ.get("ATF_API_KEY") or env_value("ATF_API_KEY")
    token = None
    try:
        user_id = int(os.environ.get("ATF_BENCH_USER") or env_value("ADMIN_USER_ID") or 0)
        if user_id:
            token = mint_web_token(user_id)
            print(f"auth: dashboard JWT for user {user_id}")
    except Exception as exc:  # noqa: BLE001 - fall back to the API key, and say so
        print(f"auth: could not mint a dashboard JWT ({exc}); user-scoped endpoints will 401")
    report: dict = {"label": args.label, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    if api_key:
        report["http"] = bench_http(args.base, api_key, args.http_runs, token)
        report["auth"] = "jwt" if token else "api-key-only"
        failures = {k: v for k, v in report["http"].items() if v.get("error") or (isinstance(v.get("status"), int) and v["status"] >= 500)}
        report["http_failures"] = failures
    else:
        report["http"] = {"error": "ATF_API_KEY not found"}
        report["http_failures"] = report["http"]

    if not args.http_only and not args.skip_bot:
        report["bot"] = bench_bot()
    if not args.http_only and not args.skip_web:
        report["web"] = bench_web(build=not args.skip_web_build)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"ui-perf-{args.label}.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"== ui/ux perf ({args.label}) ==")
    for name, row in (report.get("http") or {}).items():
        if "error" in row:
            print(f"  http {name:16} FAILED {row.get('error')}")
        else:
            print(f"  http {name:16} p50={row['p50_ms']:7.2f}ms p95={row['p95_ms']:7.2f}ms "
                  f"p99={row['p99_ms']:7.2f}ms {row['rps']:7.1f} req/s")
    web = report.get("web") or {}
    if web and "error" in web:
        print(f"  web  FAILED {web['error'][:200]}")
    elif web:
        print(f"  web  bundle      {web['total_kb']:.0f} KB raw / {web['total_gzip_kb']:.0f} KB gzip "
              f"across {web['chunks']} chunk(s), build {web.get('build_seconds')}s")
        for chunk in web["largest"][:3]:
            print(f"       largest     {chunk['file']} {chunk['kb']:.0f} KB / {chunk['gzip_kb']:.0f} KB gzip")
    bot = report.get("bot") or {}
    if bot and "error" not in bot:
        print(f"  bot  catalog     lookup {bot['lookup_ns']:.0f} ns/key, 40-button menu {bot['menu_40_buttons_ms']:.3f} ms")
    print(f"  saved -> {out_path.relative_to(ROOT)}")
    return 1 if report.get("http_failures") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
