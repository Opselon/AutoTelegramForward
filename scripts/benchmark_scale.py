#!/usr/bin/env python3
"""Reproducible multi-tenant scale benchmark.

Readiness for 10,000 tenants cannot be asserted from the architecture — it has to
be measured on a dataset of that size. This builds one, in a throwaway SQLite
database, through the *real* repositories and the *real* pipeline, and reports
what it measures:

  * seeding throughput (rows/second) and the resulting database size;
  * the latency of every read on a message's hot path, as percentiles, and the
    number of SQL statements each one costs — the two things that must stay flat
    as tenants are added;
  * end-to-end ingest throughput through ``DurableMessagePipeline`` with a
    configurable concurrency, and how many delivery jobs that produced;
  * the query plans of the hot statements, so "the index is used" is shown and
    not assumed.

It does not talk to Telegram, the network, or the live database: the pool is a
stub, and every row is synthetic. Nothing here is a substitute for a real load
test against a live deployment — it measures the control plane, which is what
grows with tenant count.

Usage::

    python scripts/benchmark_scale.py --users 10000 --tasks-per-user 2
    python scripts/benchmark_scale.py --users 200 --messages 500 --json out.json

The ``--users 200 --messages 500`` profile is what CI runs: big enough to make
the planner choose indexes and to catch an N+1 that grows with tenant count,
small enough to finish in seconds.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.application.effective_settings import resolve_rule  # noqa: E402
from core.application.pipeline import DurableMessagePipeline  # noqa: E402
from core.application.repositories import BotToken  # noqa: E402
from core.domain.entities import ForwardRule  # noqa: E402
from core.domain.value_objects import ForwardMode, RoutingType  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402
from core.infrastructure.persistence.sqlite_repositories import (  # noqa: E402
    SqliteBotTokenRepository,
    SqliteDeliveryQueueRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
)
from core.infrastructure.security.crypto import CryptoService  # noqa: E402
from core.infrastructure.telegram.durable_queue import DurableQueueManager  # noqa: E402

try:  # present once the forwarder resolves per-bot defaults; measured if so
    from core.application.effective_settings import materialize  # type: ignore
except ImportError:  # pragma: no cover - older revision
    materialize = None

NOW = int(time.time())
#: Settings a tenant is plausible to have touched. The point is that a task
#: carries a real, non-empty settings blob — an empty rule would flatter every
#: number here.
TASK_SETTINGS = {
    "remove_emojis": True,
    "remove_links": False,
    "delay_seconds": 0,
    "process_raw_text": True,
    "max_time_edit": 0,
}
BOT_DEFAULTS = {"remove_emojis": True, "duplicate_window_seconds": 0}
#: How many tenants have a task that watches an extra source chat.
EXTRA_SOURCE_EVERY = 200


# --------------------------------------------------------------------------- #
# Measurement helpers
# --------------------------------------------------------------------------- #
def percentiles(samples: list[float]) -> dict:
    ordered = sorted(samples)

    def at(q: float) -> float:
        if not ordered:
            return 0.0
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return ordered[idx]

    return {
        "count": len(ordered),
        "p50_ms": round(at(0.50), 3),
        "p95_ms": round(at(0.95), 3),
        "p99_ms": round(at(0.99), 3),
        "mean_ms": round(statistics.fmean(ordered), 3) if ordered else 0.0,
        "max_ms": round(ordered[-1], 3) if ordered else 0.0,
    }


def rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def instrument_db(db) -> dict:
    """Count SQL statements issued through the database handle."""
    counts = {"execute": 0, "query_all": 0, "query_one": 0}
    for name in list(counts):
        original = getattr(db, name)

        def wrapper(*args, _original=original, _name=name, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        setattr(db, name, wrapper)
    return counts


def total_sql(counts: dict) -> int:
    return sum(counts[k] for k in ("execute", "query_all", "query_one"))


def db_bytes(path: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            pass
    return total


async def timed(fn, iterations: int) -> tuple[dict, float]:
    """Run ``fn`` ``iterations`` times; return (percentiles, per-call SQL count)."""
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        await fn()
        samples.append((time.perf_counter() - start) * 1000)
    return percentiles(samples), 0.0


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
async def seed(db: SqliteDatabase, users: int, tasks_per_user: int, quiet: bool) -> dict:
    """Create ``users`` independent tenants, each with a bot and N tasks.

    Inserted through the repositories in one transaction per tenant, which is how
    onboarding actually writes: a tenant's rows commit together.
    """
    crypto = CryptoService("benchmark-master-key")
    rules = SqliteForwardRuleRepository(db)
    tokens = SqliteBotTokenRepository(db, crypto)
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES ('bench-session', '+10000000000', '', '', '', '', 1, 1, ?, ?)",
        (NOW, NOW),
    )
    started = time.perf_counter()
    for user in range(users):
        owner = 7000000000 + user
        token_id = f"bt-{user:07d}"
        await tokens.add(BotToken(
            id=token_id, label=f"bot{user}", token=f"{owner}:AA-secret-{user}",
            owner_user_id=owner, created_at=NOW, updated_at=NOW,
            defaults=dict(BOT_DEFAULTS),
        ))
        for task in range(tasks_per_user):
            rule = ForwardRule(
                id=f"rule-{user:07d}-{task}", name=f"task {user}/{task}",
                session_id="bench-session",
                source_chat_id=f"-100{user:07d}{task}", target_chat_id=f"-1009{user:07d}{task}",
                routing_type=RoutingType.CHANNEL_TO_CHANNEL,
                forward_mode=ForwardMode.COPY_MESSAGE, owner_user_id=owner,
            )
            rule.bot_token_id = token_id
            if task == 0 and user % EXTRA_SOURCE_EVERY == 0:
                # One task in EXTRA_SOURCE_EVERY watches a second chat through
                # detection_criteria. This is the branch that used to force a
                # full scan, so the benchmark must contain it.
                rule.detection_criteria = {"source_chat_ids": [f"-10077{user:07d}"]}
            for key, value in TASK_SETTINGS.items():
                setattr(rule, key, value)
            await rules.add(rule)
        if not quiet and users >= 1000 and (user + 1) % max(1, users // 10) == 0:
            elapsed = time.perf_counter() - started
            print(f"  seeded {(user + 1):,}/{users:,} tenants "
                  f"({(user + 1) / elapsed:,.0f} tenants/s)", flush=True)
    elapsed = time.perf_counter() - started
    return {
        "users": users,
        "tasks": users * tasks_per_user,
        "seconds": round(elapsed, 2),
        "rules_per_second": round((users * tasks_per_user) / elapsed, 1),
        "tenants_per_second": round(users / elapsed, 1),
        "rss_mb": rss_mb(),
    }


# --------------------------------------------------------------------------- #
# Hot-path reads
# --------------------------------------------------------------------------- #
async def measure_reads(db, users: int, tasks_per_user: int, iterations: int) -> dict:
    """Every read the worker does per message, plus the API's list path.

    Flat cost as tenants grow is the property under test: ``sql_per_call`` must
    not grow with ``users``, and the percentiles must not either.
    """
    rules = SqliteForwardRuleRepository(db)
    tokens = SqliteBotTokenRepository(db, CryptoService("benchmark-master-key"))
    out: dict = {}

    # The worker's per-message lookup: which tasks watch this source chat?
    target_user = users - 1
    source = f"-100{target_user:07d}0"
    counts = instrument_db(db)
    before = total_sql(counts)
    plan_start = time.perf_counter()
    found = await rules.list_active_by_source(source)
    plan_ms = (time.perf_counter() - plan_start) * 1000
    per_call_sql = total_sql(counts) - before
    out["rules_by_source"] = {
        "rows": len(found), "sql_per_call": per_call_sql, "first_call_ms": round(plan_ms, 3),
    }

    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        await rules.list_active_by_source(source)
        samples.append((time.perf_counter() - start) * 1000)
    out["rules_by_source"]["latency"] = percentiles(samples)

    # A tenant's own task list. This is per-tenant, so it must stay flat in the
    # tenant's *own* task count only — and that is exactly what it does not do:
    # see the report, which prints the row count this reads for a 2-task tenant.
    before = total_sql(counts)
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        rows = await rules.list_by_owner(7000000000 + target_user)
        samples.append((time.perf_counter() - start) * 1000)
    out["rules_by_owner"] = {
        "rows": len(rows),
        "sql_per_call": (total_sql(counts) - before) // max(1, iterations),
        "latency": percentiles(samples),
    }
    # ...and the same read bounded to one page, which is what a screen needs.
    paged = await rules.list_by_owner(7000000000 + target_user, limit=50)
    counted = await rules.count_by_owner(7000000000 + target_user)
    page_samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        await rules.list_by_owner(7000000000 + target_user, limit=50)
        page_samples.append((time.perf_counter() - start) * 1000)
    out["rules_by_owner_page"] = {
        "returned": len(paged), "total": counted, "latency": percentiles(page_samples),
    }

    before = total_sql(counts)
    token = None
    for _ in range(iterations):
        token = await tokens.get_by_id(f"bt-{target_user:07d}")
    out["bot_token_by_id"] = {
        "resolved": token is not None,
        "sql_per_call": (total_sql(counts) - before) // max(1, iterations),
    }

    # The read both the API and the forwarder share: a task's own settings
    # resolved against the bot it has attached.
    before = total_sql(counts)
    rule = (await rules.list_active_by_source(source))[0]
    resolved = None
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        resolved = await resolve_rule(rule, tokens)
        samples.append((time.perf_counter() - start) * 1000)
    out["effective_settings"] = {
        "settings_resolved": len(resolved or {}),
        "sql_per_call": (total_sql(counts) - before) // max(1, iterations),
        "latency": percentiles(samples),
    }
    if materialize is not None:
        # The forwarder's wrapper on top of that read, when this revision has
        # it: it hands the pipeline a copy, so nothing downstream can write an
        # inherited value back as the task's own choice.
        before = total_sql(counts)
        samples = []
        for _ in range(iterations):
            start = time.perf_counter()
            await materialize(rule, tokens)
            samples.append((time.perf_counter() - start) * 1000)
        out["materialized_rule"] = {
            "sql_per_call": (total_sql(counts) - before) // max(1, iterations),
            "latency": percentiles(samples),
        }
    return out


def query_plans(db) -> dict:
    """The planner's own answer for the hot statements — read, not assumed.

    Bound with plausible values: the planner's choice can depend on a value's
    selectivity, and `EXPLAIN QUERY PLAN` will not accept unbound parameters.
    """
    plans: dict = {}
    for name, sql, params in (
        # Verbatim from SqliteForwardRuleRepository.list_active_by_source: the
        # plan that matters is the one the worker actually runs.
        ("rules_by_source",
         "SELECT * FROM forward_rules WHERE is_active=1 AND source_chat_id = ?"
         " UNION ALL"
         " SELECT * FROM forward_rules WHERE is_active=1 AND has_extra_sources = 1"
         "   AND source_chat_id <> ?"
         "   AND json_valid(detection_criteria) AND EXISTS ("
         "        SELECT 1 FROM json_each(detection_criteria, '$.source_chat_ids')"
         "        WHERE value = ?)",
         ("-1000000000", "-1000000000", "-1000000000")),
        ("rules_by_owner",
         "SELECT * FROM forward_rules WHERE owner_user_id = ?", (7000000000,)),
        ("rules_by_owner_page",
         "SELECT * FROM forward_rules WHERE owner_user_id = ? ORDER BY created_at"
         " LIMIT 50 OFFSET 0", (7000000000,)),
        ("rules_by_owner_count",
         "SELECT COUNT(*) AS n FROM forward_rules WHERE owner_user_id = ?", (7000000000,)),
        ("bot_tokens_by_id", "SELECT * FROM bot_tokens WHERE id = ?", ("bt-0000000",)),
        ("jobs_claim",
         "SELECT * FROM delivery_jobs WHERE status = 'PENDING'", ()),
    ):
        rows = db.query_all(f"EXPLAIN QUERY PLAN {sql}", params)
        plans[name] = [
            {"detail": str(r["detail"] if isinstance(r, dict) else r[3])} for r in rows
        ]
    return plans


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
def build_pipeline(db, queue_repo, maps, processed, rules, filters):
    """The real pipeline, wired with whatever this revision accepts.

    ``bot_tokens`` (per-bot defaults) is passed when the constructor takes it, so
    the same harness runs — and is guarded in CI — on a revision with and
    without that work, instead of only on the branch it was written against.
    """
    manager = DurableQueueManager(queue_repo, maps, None, num_workers=1, rule_repo=rules)
    kwargs = {
        "rule_repo": rules, "filter_repo": filters, "queue_manager": manager,
        "msg_map_repo": maps, "processed_repo": processed,
    }
    if "bot_tokens" in inspect.signature(DurableMessagePipeline.__init__).parameters:
        kwargs["bot_tokens"] = None
    return DurableMessagePipeline(**kwargs)


async def measure_ingest(db, users: int, tasks_per_user: int, messages: int,
                         concurrency: int) -> dict:
    """Feed synthetic messages through the real pipeline, with the fake pool.

    Every source chat belongs to exactly one tenant, so the totals also check
    cross-tenant containment: each enqueued job must point at a rule of the
    source's owner.
    """
    rules = SqliteForwardRuleRepository(db)
    filters = SqliteFilterRuleRepository(db)
    queue_repo = SqliteDeliveryQueueRepository(db)
    maps = SqliteMessageMapRepository(db)
    processed = SqliteProcessedMessageRepository(db)
    pipeline = build_pipeline(db, queue_repo, maps, processed, rules, filters)

    sources = [
        (f"-100{u:07d}{t}", 7000000000 + u)
        for u in range(users) for t in range(tasks_per_user)
    ]
    counts = instrument_db(db)
    before = total_sql(counts)
    latencies: list[float] = []
    forwarded = 0
    failures: list[str] = []
    lock = asyncio.Lock()

    async def one(index: int) -> None:
        nonlocal forwarded
        chat_id, owner = sources[index % len(sources)]
        raw = {"chat_id": chat_id, "message_id": 100000 + index,
               "text": f"benchmark message {index} \U0001F600", "owner_user_id": owner}
        start = time.perf_counter()
        ctx = await pipeline.process(raw)
        elapsed = (time.perf_counter() - start) * 1000
        async with lock:
            latencies.append(elapsed)
            if ctx.target_jobs:
                forwarded += 1
                for job in ctx.target_jobs.values():
                    rule = await rules.get_by_id(job.rule_id)
                    if rule is None or rule.owner_user_id != owner:
                        failures.append(
                            f"job {job.rule_id} for owner {owner} resolved to "
                            f"{None if rule is None else rule.owner_user_id}"
                        )

    started = time.perf_counter()
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(index: int) -> None:
        async with semaphore:
            await one(index)

    await asyncio.gather(*(bounded(i) for i in range(messages)))
    elapsed = time.perf_counter() - started
    sql_total = total_sql(counts) - before

    pending = db.query_one(
        "SELECT COUNT(*) AS n FROM delivery_jobs WHERE status = 'PENDING'", ()
    )
    return {
        "messages": messages,
        "concurrency": concurrency,
        "forwarded": forwarded,
        "seconds": round(elapsed, 2),
        "messages_per_second": round(messages / elapsed, 1) if elapsed else 0.0,
        "sql_per_message": round(sql_total / max(1, messages), 2),
        "latency": percentiles(latencies),
        "pending_jobs": int(pending["n"] if isinstance(pending, dict) else pending[0]),
        "cross_tenant_leaks": failures[:5],
        "rss_mb": rss_mb(),
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def report(result: dict) -> None:
    seed = result["seed"]
    print(f"\ndataset: {seed['users']:,} tenants, {seed['tasks']:,} tasks "
          f"in {seed['seconds']}s ({seed['rules_per_second']:,} tasks/s), "
          f"db {result['db_mb']} MB, rss {seed['rss_mb']} MB")
    print("\nhot-path reads (worst tenant's chat, first row of the page):")
    for name, data in result["reads"].items():
        latency = data.get("latency") or data
        print(f"  {name:20s} p50 {latency.get('p50_ms', 0):8.3f} ms  "
              f"p95 {latency.get('p95_ms', 0):8.3f} ms  "
              f"sql/call {data.get('sql_per_call', '-')}")
    ingest = result["ingest"]
    print(f"\ningest: {ingest['messages']:,} messages at concurrency "
          f"{ingest['concurrency']} -> {ingest['messages_per_second']:,} msg/s, "
          f"p95 {ingest['latency']['p95_ms']} ms, "
          f"{ingest['sql_per_message']} sql/message, "
          f"{ingest['forwarded']:,} forwarded, "
          f"{ingest['pending_jobs']:,} jobs queued")
    print("\nquery plans:")
    for name, plan in result["plans"].items():
        print(f"  {name:20s} {' | '.join(p['detail'] for p in plan)}")


def check(result: dict) -> list[str]:
    """The invariants that make the numbers mean something."""
    problems: list[str] = []
    ingest = result["ingest"]
    if ingest["cross_tenant_leaks"]:
        problems.append(f"cross-tenant deliveries: {ingest['cross_tenant_leaks']}")
    if ingest["forwarded"] == 0:
        problems.append("no message was forwarded; the ingest path did nothing")
    page = result["reads"].get("rules_by_owner_page") or {}
    if page.get("total") and page.get("returned") != min(50, page["total"]):
        problems.append(
            f"owner page returned {page['returned']} of {page['total']} rows; paging is broken"
        )
    for name in ("rules_by_source", "rules_by_owner", "bot_tokens_by_id"):
        if name not in result["plans"]:
            continue
        detail = " ".join(p["detail"] for p in result["plans"][name]).upper()
        if "SCAN" in detail and "USING INDEX" not in detail:
            problems.append(f"{name} plan is a full scan: {detail}")
    if result["reads"]["rules_by_source"]["sql_per_call"] > 8:
        problems.append(
            "resolving a source chat costs "
            f"{result['reads']['rules_by_source']['sql_per_call']} statements; "
            "an N+1 is back"
        )
    if ingest["sql_per_message"] > 40:
        problems.append(f"{ingest['sql_per_message']} statements per message is too many")
    return problems


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=10000)
    parser.add_argument("--tasks-per-user", type=int, default=2)
    parser.add_argument("--messages", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--json", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="atf-bench-") as tmp:
        path = os.path.join(tmp, "bench.db")
        db = SqliteDatabase(path)
        print(f"seeding {args.users:,} tenants x {args.tasks_per_user} tasks ...", flush=True)
        seed_stats = await seed(db, args.users, args.tasks_per_user, args.quiet)
        result: dict = {
            "generated_at": NOW,
            "host": {"cpus": os.cpu_count(), "python": sys.version.split()[0]},
            "parameters": vars(args),
            "seed": seed_stats,
        }
        result["reads"] = await measure_reads(
            db, args.users, args.tasks_per_user, args.iterations
        )
        result["plans"] = query_plans(db)
        result["ingest"] = await measure_ingest(
            db, args.users, args.tasks_per_user, args.messages, args.concurrency
        )
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)", ())
        result["db_mb"] = round(db_bytes(path) / 1024 / 1024, 2)
        db.close()

    report(result)
    problems = check(result)
    result["problems"] = problems
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    if problems:
        print("\nFAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nall scale invariants held")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
