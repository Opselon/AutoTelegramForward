#!/usr/bin/env python3
"""Bounded 10,000-tenant load test against PostgreSQL.

SQLite is this deployment's engine, so the control plane's own numbers come from
`scripts/benchmark_scale.py`. This script answers the other question — *would the
schema and the query shapes hold up on PostgreSQL at 10k tenants* — because
inferring PostgreSQL behaviour from SQLite results is exactly the mistake the
mission forbids. It therefore speaks to PostgreSQL directly, with the same
owner-scoped query shapes the repositories use, and reports what it measures
rather than what it hopes.

What it does
------------
1. Creates a representative control-plane schema (tenants, sessions, bots,
   rules) with the indexes the owner-scoped reads depend on. Idempotent.
2. Seeds a realistic distribution: most tenants have one or two rules, some have
   twenty, one bot each, a mix of active/paused, JSONB metadata on every rule —
   the shape of a real install rather than one uniform row copied N times.
3. Runs a mixed read/write workload at a fixed concurrency: owner-scoped list,
   by-id get, bot-defaults read, and rule writes (create + settings update).
4. Measures p50/p95/p99 latency per operation class, throughput, error rate,
   connection-pool saturation, database growth and process memory.
5. Compares the same workload on a small dataset and on the full one, so
   "latency does not grow with tenant count" is shown, not assumed.

Acceptance thresholds are constants below, agreed before the run; the exit code
is non-zero if any of them fails. Run it with:

    python scripts/benchmark_postgres_scale.py --dsn postgresql://... \
        --tenants 10000 --concurrency 40 --duration 30 --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# Acceptance thresholds, fixed before the run. Failure here is a finding, not a
# tuning knob: raise them only with a measurement that justifies it in the repo.
# --------------------------------------------------------------------------- #
THRESHOLDS = {
    "error_rate_max": 0.001,          # <= 0.1 % of operations fail
    "p95_ms_max": 250.0,              # owner-scoped reads stay interactive
    "p99_ms_max": 600.0,              # tail stays bounded under concurrency
    "throughput_min_ops_s": 300.0,    # mixed workload, --concurrency 40
    "pool_timeouts_max": 0,           # a saturated pool is a hard failure
    "pool_acquire_p95_ms_max": 50.0,  # waiting for a connection must stay short
    "latency_growth_max": 1.5,        # 10k tenants vs 100 tenants, p95 ratio
}

#: The real schema keeps ownership in a plain column (SQLite's forward_rules has
#: exactly one foreign key, on session_id). The harness mirrors that: adding
#: constraints production does not have would make the measurement describe a
#: schema nobody runs.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    id            BIGINT PRIMARY KEY,
    plan          TEXT NOT NULL DEFAULT 'free',
    language      TEXT NOT NULL DEFAULT 'en',
    created_at    BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id        TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bot_tokens (
    id            TEXT PRIMARY KEY,
    label         TEXT NOT NULL DEFAULT '',
    token_enc     TEXT NOT NULL DEFAULT '',
    owner_user_id BIGINT NOT NULL,
    defaults      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    BIGINT NOT NULL DEFAULT 0,
    updated_at    BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS forward_rules (
    id            TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    session_id    TEXT NOT NULL,
    bot_token_id  TEXT,
    source_chat_id TEXT NOT NULL DEFAULT '',
    target_chat_id TEXT NOT NULL DEFAULT '',
    routing_type  TEXT NOT NULL DEFAULT 'CHANNEL_TO_CHANNEL',
    forward_mode  TEXT NOT NULL DEFAULT 'COPY',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    is_paused     BOOLEAN NOT NULL DEFAULT FALSE,
    delay_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    remove_links  BOOLEAN NOT NULL DEFAULT FALSE,
    sync_replies  BOOLEAN NOT NULL DEFAULT FALSE,
    version       INTEGER NOT NULL DEFAULT 1,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    BIGINT NOT NULL DEFAULT 0,
    updated_at    BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_rules_owner        ON forward_rules (owner_user_id);
CREATE INDEX IF NOT EXISTS ix_rules_owner_active ON forward_rules (owner_user_id, is_active);
CREATE INDEX IF NOT EXISTS ix_rules_owner_session ON forward_rules (owner_user_id, session_id);
CREATE INDEX IF NOT EXISTS ix_rules_bot          ON forward_rules (bot_token_id);
CREATE INDEX IF NOT EXISTS ix_bots_owner         ON bot_tokens (owner_user_id);
"""

LIST_RULES_SQL = (
    "SELECT id, metadata->>'name' AS name, is_active FROM forward_rules"
    " WHERE owner_user_id = $1 ORDER BY created_at DESC LIMIT 50"
)
GET_RULE_SQL = (
    "SELECT id, session_id, bot_token_id, metadata FROM forward_rules"
    " WHERE id = $1 AND owner_user_id = $2"
)
BOT_DEFAULTS_SQL = (
    "SELECT id, defaults FROM bot_tokens WHERE id = $1 AND owner_user_id = $2"
)
INSERT_RULE_SQL = (
    "INSERT INTO forward_rules (id, owner_user_id, session_id, bot_token_id,"
    " source_chat_id, target_chat_id, metadata, created_at, updated_at)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $8)"
)
UPDATE_RULE_SQL = (
    "UPDATE forward_rules SET delay_seconds = $1, metadata = $2::jsonb, updated_at = $3,"
    " version = version + 1 WHERE id = $4 AND owner_user_id = $5"
)


@dataclass
class Samples:
    """Latencies per operation class, in milliseconds."""

    by_op: Dict[str, List[float]] = field(default_factory=dict)
    errors: int = 0
    total: int = 0
    pool_timeouts: int = 0

    def record(self, op: str, ms: float) -> None:
        self.by_op.setdefault(op, []).append(ms)
        self.total += 1

    def error(self) -> None:
        self.errors += 1
        self.total += 1

    @staticmethod
    def _pct(values: List[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * len(ordered) + 0.5)) - 1))
        return ordered[idx]

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"errors": self.errors, "total": self.total,
                               "pool_timeouts": self.pool_timeouts,
                               "error_rate": (self.errors / self.total) if self.total else 0.0}
        for op, values in sorted(self.by_op.items()):
            out[op] = {
                "count": len(values),
                "p50_ms": round(self._pct(values, 50), 3),
                "p95_ms": round(self._pct(values, 95), 3),
                "p99_ms": round(self._pct(values, 99), 3),
                "max_ms": round(max(values), 3),
                "mean_ms": round(statistics.fmean(values), 3),
            }
        return out


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


async def create_schema(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def seed(pool, tenants: int, seed_value: int) -> Dict[str, int]:
    """Seed a realistic distribution. Returns row counts."""
    rng = _rng(seed_value)
    now = int(time.time())
    counts = {"tenants": 0, "sessions": 0, "bots": 0, "rules": 0}
    batch_tenants: List[tuple] = []
    batch_sessions: List[tuple] = []
    batch_bots: List[tuple] = []
    batch_rules: List[tuple] = []

    async def flush(conn) -> None:
        nonlocal batch_tenants, batch_sessions, batch_bots, batch_rules
        if batch_tenants:
            await conn.executemany("INSERT INTO tenants (id, plan, language, created_at)"
                                   " VALUES ($1,$2,$3,$4) ON CONFLICT (id) DO NOTHING", batch_tenants)
            counts["tenants"] += len(batch_tenants)
        if batch_sessions:
            await conn.executemany("INSERT INTO sessions (id, owner_user_id, is_active, created_at)"
                                   " VALUES ($1,$2,$3,$4) ON CONFLICT (id) DO NOTHING", batch_sessions)
            counts["sessions"] += len(batch_sessions)
        if batch_bots:
            await conn.executemany("INSERT INTO bot_tokens (id, label, token_enc, owner_user_id,"
                                   " defaults, created_at, updated_at)"
                                   " VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7) ON CONFLICT (id) DO NOTHING",
                                   batch_bots)
            counts["bots"] += len(batch_bots)
        if batch_rules:
            await conn.executemany(
                "INSERT INTO forward_rules (id, owner_user_id, session_id, bot_token_id,"
                " source_chat_id, target_chat_id, routing_type, forward_mode, is_active,"
                " is_paused, delay_seconds, remove_links, sync_replies, metadata,"
                " created_at, updated_at)"
                " VALUES ($1,$2,$3,$4,$5,$6,'CHANNEL_TO_CHANNEL','COPY',$7,$8,$9,$10,$11,"
                " $12::jsonb,$13,$13) ON CONFLICT (id) DO NOTHING", batch_rules)
            counts["rules"] += len(batch_rules)
        batch_tenants, batch_sessions, batch_bots, batch_rules = [], [], [], []

    # 2,000 tenants per flush keeps each round trip useful without holding the
    # whole dataset in memory.
    async with pool.acquire() as conn:
        for index in range(tenants):
            uid = 9_000_000_000 + index
            plan = "pro" if index % 50 == 0 else ("plus" if index % 7 == 0 else "free")
            batch_tenants.append((uid, plan, rng.choice(["en", "fa", "ru", "pt"]), now))
            session_id = f"s-{uid}"
            batch_sessions.append((session_id, uid, True, now))

            # one bot per tenant, with a couple of behavioural defaults
            bot_id = f"bot-{uid}"
            defaults = json.dumps({
                "forward_delay_seconds": rng.choice([0, 5, 30, 120]),
                "remove_links": rng.random() < 0.4,
                "sync_replies": rng.random() < 0.3,
            })
            batch_bots.append((bot_id, f"@bot_{uid}", "", uid, defaults, now, now))

            # rules per tenant: most have 1-2, a tail has 20
            draw = rng.random()
            rule_count = 1 if draw < 0.60 else (2 if draw < 0.85 else (5 if draw < 0.97 else 20))
            for n in range(rule_count):
                metadata = {
                    "bot_token_id": bot_id,
                    "delivery_transport": "BOT" if n % 3 == 0 else "USER",
                    "name": f"rule {n}",
                    "paid_addons": [] if n % 2 else ["ai_rewrite"],
                }
                batch_rules.append((
                    str(uuid.uuid4()), uid, session_id, bot_id,
                    f"-100{rng.randint(10**9, 10**10)}", f"-100{rng.randint(10**9, 10**10)}",
                    rng.random() < 0.9, rng.random() < 0.1,
                    float(rng.choice([0, 5, 30, 120])), rng.random() < 0.4,
                    rng.random() < 0.3, json.dumps(metadata), now,
                ))
            if len(batch_tenants) >= 1000:
                await flush(conn)
        await flush(conn)
    return counts


async def workload(pool, tenant_ids: List[int], duration: float, concurrency: int,
                   mix: Dict[str, float], samples: Samples, deadline_holder: Dict[str, Any]) -> None:
    """Mixed workload: mostly owner-scoped reads, some writes, until `duration`."""
    rng = _rng(os.getpid())
    op_names = list(mix)
    weights = [mix[name] for name in op_names]

    async def one_worker() -> None:
        while time.monotonic() < deadline_holder["deadline"]:
            op = rng.choices(op_names, weights=weights, k=1)[0]
            uid = rng.choice(tenant_ids)
            try:
                # Waiting for a connection is its own measurement: a slow query
                # and a saturated pool look identical in end-to-end latency.
                waiting = time.perf_counter()
                async with pool.acquire() as conn:
                    samples.record("acquire", (time.perf_counter() - waiting) * 1000.0)
                    if op == "list_rules":
                        await conn.fetch(LIST_RULES_SQL, uid)
                    elif op == "get_rule":
                        row = await conn.fetchrow(
                            "SELECT id FROM forward_rules WHERE owner_user_id=$1 LIMIT 1", uid)
                        if row:
                            await conn.fetchrow(GET_RULE_SQL, row["id"], uid)
                    elif op == "bot_defaults":
                        await conn.fetchrow(BOT_DEFAULTS_SQL, f"bot-{uid}", uid)
                    elif op == "create_rule":
                        session_id = f"s-{uid}"
                        await conn.execute(
                            INSERT_RULE_SQL, str(uuid.uuid4()), uid, session_id, f"bot-{uid}",
                            f"-100{rng.randint(10**9, 10**10)}", f"-100{rng.randint(10**9, 10**10)}",
                            json.dumps({"name": "load", "bot_token_id": f"bot-{uid}"}),
                            int(time.time()))
                    elif op == "update_rule":
                        row = await conn.fetchrow(
                            "SELECT id FROM forward_rules WHERE owner_user_id=$1 LIMIT 1", uid)
                        if row:
                            await conn.execute(
                                UPDATE_RULE_SQL, 42.0,
                                json.dumps({"name": "load", "bot_token_id": f"bot-{uid}"}),
                                int(time.time()), row["id"], uid)
                samples.record(op, (time.perf_counter() - waiting) * 1000.0)
            except Exception as exc:
                if "pool" in type(exc).__name__.lower() or "pool" in str(exc).lower():
                    samples.pool_timeouts += 1
                samples.error()

    await asyncio.gather(*[one_worker() for _ in range(concurrency)])


async def _pool_stats(pool) -> Dict[str, Any]:
    """Connections in use, idle and — the number that matters — waiting.

    A waiter is an operation that could not get a connection. Any non-zero value
    at the end of a run means the pool, not the database, was the bottleneck.
    """
    return {
        "max_size": pool.get_max_size(),
        "current_size": pool.get_size(),
        "idle": pool.get_idle_size(),
    }


async def run_dataset(dsn: str, tenants: int, duration: float, concurrency: int,
                      seed_value: int, mix: Dict[str, float]) -> Dict[str, Any]:
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=max(4, concurrency), timeout=10)
    try:
        await create_schema(pool)
        async with pool.acquire() as conn:
            before_bytes = await conn.fetchval(
                "SELECT pg_database_size(current_database())")
            existing = await conn.fetchval("SELECT COUNT(*) FROM forward_rules")
        seeded = {}
        if tenants:
            seeded = await seed(pool, tenants, seed_value)
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT owner_user_id FROM forward_rules"
                                    " ORDER BY owner_user_id LIMIT 20000")
            tenant_ids = [r["owner_user_id"] for r in rows]
            rules_total = await conn.fetchval("SELECT COUNT(*) FROM forward_rules")
            after_bytes = await conn.fetchval("SELECT pg_database_size(current_database())")

        samples = Samples()
        holder = {"deadline": time.monotonic() + duration}
        started = time.perf_counter()
        await workload(pool, tenant_ids, duration, concurrency, mix, samples, holder)
        elapsed = time.perf_counter() - started
        stats = await _pool_stats(pool)

        async with pool.acquire() as conn:
            final_bytes = await conn.fetchval("SELECT pg_database_size(current_database())")

        summary = samples.summary()
        summary.update({
            "seeded": seeded,
            "rules_in_table": rules_total,
            "rules_before": existing,
            "db_size_mb_before": round(before_bytes / 1048576, 2),
            "db_size_mb_after_seed": round(after_bytes / 1048576, 2),
            "db_size_mb_final": round(final_bytes / 1048576, 2),
            "db_growth_mb": round((final_bytes - after_bytes) / 1048576, 2),
            "throughput_ops_s": round(samples.total / elapsed, 1) if elapsed else 0.0,
            "elapsed_s": round(elapsed, 2),
            "concurrency": concurrency,
            "pool": stats,
        })
        return summary
    finally:
        await pool.close()


def evaluate(full: Dict[str, Any], small: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the thresholds fixed at the top of this file."""
    checks: Dict[str, Any] = {}
    checks["error_rate"] = {
        "value": full["error_rate"], "threshold": THRESHOLDS["error_rate_max"],
        "ok": full["error_rate"] <= THRESHOLDS["error_rate_max"],
    }
    for op in ("list_rules", "get_rule", "bot_defaults", "create_rule", "update_rule"):
        measured = full.get(op)
        if not measured:
            continue
        checks[f"{op}_p95"] = {
            "value": measured["p95_ms"], "threshold": THRESHOLDS["p95_ms_max"],
            "ok": measured["p95_ms"] <= THRESHOLDS["p95_ms_max"],
        }
        checks[f"{op}_p99"] = {
            "value": measured["p99_ms"], "threshold": THRESHOLDS["p99_ms_max"],
            "ok": measured["p99_ms"] <= THRESHOLDS["p99_ms_max"],
        }
    checks["throughput"] = {
        "value": full["throughput_ops_s"], "threshold": THRESHOLDS["throughput_min_ops_s"],
        "ok": full["throughput_ops_s"] >= THRESHOLDS["throughput_min_ops_s"],
    }
    checks["pool_timeouts"] = {
        "value": full.get("pool_timeouts", 0), "threshold": THRESHOLDS["pool_timeouts_max"],
        "ok": full.get("pool_timeouts", 0) <= THRESHOLDS["pool_timeouts_max"],
    }
    acquire = full.get("acquire") or {}
    if acquire:
        checks["pool_acquire_p95"] = {
            "value": acquire.get("p95_ms", 0.0),
            "threshold": THRESHOLDS["pool_acquire_p95_ms_max"],
            "ok": acquire.get("p95_ms", 0.0) <= THRESHOLDS["pool_acquire_p95_ms_max"],
        }
    # The same workload on a small dataset must not be much faster: if it is, the
    # cost grows with tenant count and 10k is not the same system.
    if small.get("list_rules") and full.get("list_rules"):
        ratio = full["list_rules"]["p95_ms"] / max(small["list_rules"]["p95_ms"], 0.001)
        checks["list_rules_latency_growth"] = {
            "value": round(ratio, 3), "threshold": THRESHOLDS["latency_growth_max"],
            "ok": ratio <= THRESHOLDS["latency_growth_max"],
        }
    checks["all_ok"] = all(entry["ok"] for entry in checks.values() if isinstance(entry, dict))
    return checks


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded 10,000-tenant load test against PostgreSQL.")
    parser.add_argument("--dsn", default=os.environ.get("ATF_TEST_PG_URL", ""),
                        help="postgresql:// DSN (defaults to $ATF_TEST_PG_URL)")
    parser.add_argument("--tenants", type=int, default=10000)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--json", dest="json_path", default="")
    parser.add_argument("--skip-small", action="store_true",
                        help="skip the small-dataset comparison run")
    args = parser.parse_args(argv)

    if not args.dsn:
        print("error: no --dsn and no $ATF_TEST_PG_URL", file=sys.stderr)
        return 2

    mix = {"list_rules": 0.55, "get_rule": 0.15, "bot_defaults": 0.10,
           "create_rule": 0.10, "update_rule": 0.10}

    small: Dict[str, Any] = {}
    if not args.skip_small:
        print("== small dataset (100 tenants), same workload and concurrency")
        small = asyncio.run(run_dataset(args.dsn, 100, max(5.0, args.duration / 3),
                                        args.concurrency, args.seed + 1, mix))
        print(json.dumps(small, indent=2, sort_keys=True))

    print(f"== full dataset ({args.tenants} tenants), {args.duration}s at "
          f"concurrency {args.concurrency}")
    full = asyncio.run(run_dataset(args.dsn, args.tenants, args.duration,
                                   args.concurrency, args.seed, mix))
    print(json.dumps(full, indent=2, sort_keys=True))

    checks = evaluate(full, small)
    report = {"thresholds": THRESHOLDS, "mix": mix, "small": small, "full": full,
              "checks": checks}
    print("== thresholds")
    print(json.dumps(checks, indent=2, sort_keys=True))

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        print(f"wrote {args.json_path}")

    return 0 if checks["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
