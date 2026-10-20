#!/usr/bin/env python3
"""Rule resolution / forwarding lookup costs on a real SQLite store.

Builds a throwaway database with the production schema, fills it with a
realistic tenant load (``--rules`` rules, one busy rule with ``--processed``
dedupe rows, ``--map`` delivery-map rows), then times the lookups an inbound
message performs:

  * ``list_active_by_source`` — which rules watch this chat;
  * ``SmartRoutingEngine.matching_rules`` — the in-memory filter over them;
  * ``was_processed`` — the restart-safe dedupe probe;
  * ``has_delivered`` — the message-map probe used before a send;
  * ``list_owned`` — the dashboard's rules page.

It also prints each statement's ``EXPLAIN QUERY PLAN``, so a regression that
turns an index seek into a scan is visible even when the timing on a small
fixture still looks acceptable.

    python scripts/bench/rule_resolution_bench.py
    python scripts/bench/rule_resolution_bench.py --rules 5000 --processed 500000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.domain.entities import MessagePayload  # noqa: E402
from core.domain.services import SmartRoutingEngine  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402
from core.infrastructure.persistence.sqlite_repositories import (  # noqa: E402
    SqliteForwardRuleRepository,
    SqliteProcessedMessageRepository,
)

SOURCE = "-1001234567890"
OTHER_SOURCES = [f"-10020000{i:05d}" for i in range(1000)]


def build_db(path: str, rules: int, processed: int, map_rows: int, all_same_source: bool) -> SqliteDatabase:
    db = SqliteDatabase(path)
    conn = db._conn
    # The fixture writes rules without their session rows (only session_id is
    # referenced; the session itself is irrelevant to a rule lookup).
    conn.execute("PRAGMA foreign_keys=OFF")
    now = int(time.time())
    rows = []
    for i in range(rules):
        source = SOURCE if (all_same_source or i == 0) else OTHER_SOURCES[i % len(OTHER_SOURCES)]
        # Most tasks watch one source. A minority carry extra sources in
        # detection_criteria, which is what has_extra_sources / the partial
        # index exists for — a realistic ratio matters, because a table where
        # *every* row has extras turns the second UNION branch into a scan.
        criteria = "{}"
        if i % 20 == 0 and i:
            criteria = json.dumps({"source_chat_ids": [OTHER_SOURCES[(i * 7) % len(OTHER_SOURCES)]]})
        rows.append(
            (
                f"rule-{i:05d}",
                f"session-{i % 7}",
                source,
                f"target-{i}",
                "COPY_MESSAGE",
                "CHANNEL_TO_CHANNEL",
                1,
                0 if i % 10 else 1,  # is_paused: 1 in 10
                (i % 50),
                i,
                now - i,
                now - i,
                (i % 500) + 1,  # owner_user_id
                criteria,
            )
        )
    conn.executemany(
        "INSERT INTO forward_rules (id, session_id, source_chat_id, target_chat_id,"
        " forward_mode, routing_type, is_active, is_paused, priority, execution_order,"
        " created_at, updated_at, owner_user_id, detection_criteria)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    # One busy task's dedupe history.
    rule_id = "rule-00000"
    conn.executemany(
        "INSERT OR IGNORE INTO processed_messages (rule_id, chat_id, message_id, media_group_id, ts)"
        " VALUES (?,?,?,?,?)",
        ((rule_id, SOURCE, i, "", now - i) for i in range(1, processed + 1)),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO message_map (rule_id, source_chat_id, source_message_id,"
        " target_chat_id, target_message_id, media_group_id, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            (rule_id, SOURCE, i, f"target-{i % 50}", i + 10_000_000, "", now - i, now - i)
            for i in range(1, map_rows + 1)
        ),
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    return db


def plans(db: SqliteDatabase) -> dict:
    statements = {
        "list_active_by_source": (
            "SELECT * FROM forward_rules WHERE is_active=1 AND source_chat_id = ?"
            " UNION ALL"
            " SELECT * FROM forward_rules WHERE is_active=1 AND has_extra_sources = 1"
            "   AND source_chat_id <> ?"
            "   AND json_valid(detection_criteria) AND EXISTS ("
            "        SELECT 1 FROM json_each(detection_criteria, '$.source_chat_ids')"
            "        WHERE value = ?)",
            (SOURCE, SOURCE, SOURCE),
        ),
        "was_processed": (
            "SELECT 1 FROM processed_messages WHERE rule_id=? AND chat_id=? AND message_id=?",
            ("rule-00000", SOURCE, 42),
        ),
        "has_delivered": (
            "SELECT * FROM message_map WHERE rule_id=? AND source_chat_id=? AND source_message_id=?",
            ("rule-00000", SOURCE, 42),
        ),
        "list_owned": (
            "SELECT * FROM forward_rules WHERE owner_user_id=? ORDER BY created_at DESC LIMIT 50",
            (7,),
        ),
    }
    out = {}
    for name, (sql, params) in statements.items():
        rows = db._conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        out[name] = [str(row["detail"]) for row in rows]
    return out


def timed(fn, repeats: int) -> dict:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    return {
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(samples[max(0, int(len(samples) * 0.95) - 1)], 4),
        "max_ms": round(samples[-1], 4),
    }


async def async_timed(coro_fn, repeats: int) -> dict:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        await coro_fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    return {
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(samples[max(0, int(len(samples) * 0.95) - 1)], 4),
        "max_ms": round(samples[-1], 4),
    }


def payload_for_source() -> MessagePayload:
    return MessagePayload(
        chat_id=SOURCE,
        message_id=42,
        text="hello from the source channel",
        date=int(time.time()),
    )


async def run(args) -> dict:
    if args.db:
        path = args.db
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    else:
        path = os.path.join(tempfile.mkdtemp(prefix="atf-rules-"), "rules.db")
    reuse = args.db and os.path.exists(path) and os.path.getsize(path) > 0
    db = SqliteDatabase(path)
    if not reuse:
        db._conn.close()
        os.unlink(path) if os.path.exists(path) else None
        db = build_db(path, args.rules, args.processed, args.map, not args.disjoint)
    rules_repo = SqliteForwardRuleRepository(db)
    processed_repo = SqliteProcessedMessageRepository(db)
    engine = SmartRoutingEngine()

    active = await rules_repo.list_active_by_source(SOURCE)
    payload = payload_for_source()

    results = {
        "fixture": {
            "rules": args.rules,
            "processed_messages": args.processed,
            "message_map": args.map,
            "rules_watching_source": len(active),
            "db_bytes": os.path.getsize(path),
            "db_path": path,
        },
        "list_active_by_source": await async_timed(lambda: rules_repo.list_active_by_source(SOURCE), args.repeats),
        "matching_rules": timed(lambda: engine.matching_rules(payload, active), args.repeats),
        "was_processed": await async_timed(
            lambda: processed_repo.was_processed("rule-00000", SOURCE, 42), args.repeats
        ),
    }
    # Count what one inbound message actually costs end to end.
    async def resolve_once():
        rules = await rules_repo.list_active_by_source(SOURCE)
        engine.matching_rules(payload, rules)
        await processed_repo.was_processed("rule-00000", SOURCE, 42)

    results["resolve_message_total"] = await async_timed(resolve_once, args.repeats)
    results["query_plans"] = plans(db)
    db._conn.close()
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", type=int, default=1000)
    ap.add_argument("--processed", type=int, default=100_000)
    ap.add_argument("--map", type=int, default=50_000)
    ap.add_argument("--repeats", type=int, default=200)
    ap.add_argument("--disjoint", action="store_true",
                    help="spread rules over distinct sources instead of one hot source")
    ap.add_argument("--db", default="", help="reuse/build the fixture at this path")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = asyncio.run(run(args))
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    fixture = results.pop("fixture")
    plans = results.pop("query_plans")
    print("fixture: " + ", ".join(f"{k}={v}" for k, v in fixture.items()))
    print(f"{'lookup':<26} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8}")
    for name, row in results.items():
        print(f"{name:<26} {row['p50_ms']:>8.4f} {row['p95_ms']:>8.4f} {row['max_ms']:>8.4f}")
    print("\nquery plans:")
    for name, detail in plans.items():
        print(f"  {name}: " + " | ".join(detail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
