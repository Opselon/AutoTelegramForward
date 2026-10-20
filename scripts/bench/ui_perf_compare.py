#!/usr/bin/env python3
"""Compare two ui_perf_bench reports and fail on a regression.

The benchmark harness records numbers; this turns them into a gate. Run it after
a UI/performance change to prove the change did not move latency, throughput or
bundle size the wrong way.

    python3 scripts/bench/ui_perf_compare.py \
        docs/benchmarks/ui-perf-baseline.json docs/benchmarks/ui-perf-after.json

Thresholds (per metric, override with --threshold):
  * http p50 / p95 / p99 may not grow more than 15%
  * web initial gzip may not grow more than 5%
  * a missing endpoint or chunk on the "after" side is a failure
Exit code 0 = within budget, 1 = regression or missing data.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare_http(before: dict, after: dict, threshold: float) -> list[str]:
    problems: list[str] = []
    before_http = before.get("http") or {}
    after_http = after.get("http") or {}
    for name, old in before_http.items():
        if "error" in old:
            continue
        new = after_http.get(name)
        if not new or "error" in new:
            problems.append(f"http {name}: missing on the after side ({new})")
            continue
        for metric in ("p50_ms", "p95_ms", "p99_ms"):
            old_value, new_value = old.get(metric), new.get(metric)
            if not old_value or new_value is None:
                continue
            change = (new_value - old_value) / old_value * 100
            flag = "REGRESSION" if change > threshold * 100 else "ok"
            if flag == "REGRESSION":
                problems.append(f"http {name} {metric}: {old_value:.2f} -> {new_value:.2f} ms ({change:+.1f}%)")
            print(f"  http {name:16} {metric:7} {old_value:8.2f} -> {new_value:8.2f} ms  {change:+7.1f}%  {flag}")
    return problems


def compare_web(before: dict, after: dict, threshold: float) -> list[str]:
    old = before.get("web") or {}
    new = after.get("web") or {}
    if not old or not new:
        return []
    problems: list[str] = []
    if "error" in new:
        return [f"web: after build failed: {new['error'][:200]}"]
    for metric, label in (("total_gzip_kb", "total gzip KB"), ("total_kb", "total raw KB")):
        old_value, new_value = old.get(metric), new.get(metric)
        if not old_value or new_value is None:
            continue
        change = (new_value - old_value) / old_value * 100
        flag = "REGRESSION" if change > threshold * 100 else "ok"
        if flag == "REGRESSION":
            problems.append(f"web {metric}: {old_value:.1f} -> {new_value:.1f} ({change:+.1f}%)")
        print(f"  web  {label:16} {old_value:8.1f} -> {new_value:8.1f}      {change:+7.1f}%  {flag}")
    if old.get("chunks") and new.get("chunks") and new["chunks"] < old["chunks"]:
        problems.append(f"web chunks dropped: {old['chunks']} -> {new['chunks']} (code splitting lost?)")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--threshold", type=float, default=0.15, help="allowed growth ratio (0.15 = 15%%)")
    parser.add_argument("--web-threshold", type=float, default=0.05, help="allowed bundle growth ratio")
    args = parser.parse_args(argv)

    before, after = load(args.before), load(args.after)
    print(f"== compare {Path(args.before).name} -> {Path(args.after).name} ==")
    problems = compare_http(before, after, args.threshold)
    problems += compare_web(before, after, args.web_threshold)

    bot_before, bot_after = before.get("bot") or {}, after.get("bot") or {}
    if bot_before and bot_after and "error" not in bot_after:
        for metric, label in (("lookup_ns", "catalog lookup ns"), ("menu_40_buttons_ms", "40-button menu ms")):
            old_value, new_value = bot_before.get(metric), bot_after.get(metric)
            if not old_value or not new_value:
                continue
            change = (new_value - old_value) / old_value * 100
            flag = "REGRESSION" if change > args.threshold * 100 else "ok"
            if flag == "REGRESSION":
                problems.append(f"bot {metric}: {old_value} -> {new_value} ({change:+.1f}%)")
            print(f"  bot  {label:16} {old_value:8.2f} -> {new_value:8.2f}      {change:+7.1f}%  {flag}")

    print()
    if problems:
        print(f"FAIL: {len(problems)} regression/missing item(s)")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("PASS: every measured metric is within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
