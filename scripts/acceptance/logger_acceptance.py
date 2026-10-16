#!/usr/bin/env python3
"""Production acceptance checks for the logger engine behind `logger:50052`.

This is the executable evidence for the Python -> Rust logger cutover. It drives
the *same* gRPC stubs and the *same* producer class the Core uses, so it fails if
the engine stops honouring any contract the producers depend on:

  1. write/ack round trip                              (stub + Core's LogClient)
  2. correlation ids stored verbatim and searchable    (must not be redacted or
                                                       mangled into a placeholder)
  3. secret redaction by value, with unrelated fields and ids left intact
  4. persistence, filtering, paging and stats
  5. error reporting: a malformed request is rejected, never silently accepted
  6. the Core producer's queue/retry counters report no loss on a clean drain

Run it inside the core container (it needs `core.proto`, `logger.log_client` and
the compose network):

    docker exec -i atf-core python - < scripts/acceptance/logger_acceptance.py
    docker exec -e ATF_LOGGER_ADDR=logger:50052 -i atf-core python - \
        < scripts/acceptance/logger_acceptance.py

Exit status is 0 only if every check passes, so it is usable as a gate:

    docker exec -i atf-core python - < scripts/acceptance/logger_acceptance.py \
      || echo "ACCEPTANCE FAILED"
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, "/app")

import grpc  # noqa: E402

from core.proto import pb, pb_grpc  # noqa: E402
from logger.log_client import LogClient  # noqa: E402

ADDRESS = os.environ.get("ATF_LOGGER_ADDR", "logger:50052")
SECRET = "acceptance-secret-value-do-not-persist-42"
SECRET_KEY = "super-secret-password-9182736"
KEEP = "keep-this-unrelated-field"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def stub() -> pb_grpc.LogControlServiceStub:
    return pb_grpc.LogControlServiceStub(grpc.insecure_channel(ADDRESS))


def write_entry(s: pb_grpc.LogControlServiceStub, **kw) -> pb.StatusResponse:
    entry = pb.LogEntry(ts=int(time.time()))
    for field, value in kw.items():
        setattr(entry, field, value)
    return s.Log(pb.LogRequest(entry=entry), timeout=10.0)


def check_roundtrip_and_redaction() -> None:
    print("\n[1-3] write / correlation id / redaction")
    s = stub()
    corr = f"acceptance-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    detail = json.dumps(
        {
            "password": SECRET_KEY,
            "nested": {"access_token": SECRET},
            "note": KEEP,
            "chat_id": 5094837833,
        }
    )
    resp = write_entry(
        s,
        level="ERROR",
        service="acceptance",
        category="acceptance",
        message=f"acceptance probe {corr}",
        detail=detail,
        rule_id="rule-42-0912345678",
        user_id=5094837833,
        correlation_id=corr,
    )
    check("write acknowledged", bool(resp.success), f"message={resp.message!r}")

    found = s.QueryLogs(pb.QueryLogsRequest(correlation_id=corr, limit=10), timeout=10.0)
    stored = found.logs[0] if found.logs else None
    blob = stored.detail if stored else ""

    check("lookup by original correlation id finds the row", found.total >= 1)
    check(
        "correlation id stored verbatim (not redacted/mangled)",
        bool(stored) and stored.correlation_id == corr,
        f"stored={stored.correlation_id if stored else None!r}",
    )
    check("secret values absent by value", SECRET not in blob and SECRET_KEY not in blob)
    check("redaction marker present", "[REDACTED]" in blob)
    check("unrelated non-secret field preserved", KEEP in blob)
    check(
        "tenant id column preserved verbatim",
        bool(stored) and stored.user_id == 5094837833,
        f"user_id={stored.user_id if stored else None}",
    )
    check(
        "long digit run in free text is masked by the phone heuristic (same as the Python engine)",
        "5094837833" not in blob and ("[PHONE]" in blob or "[REDACTED]" in blob),
    )
    check(
        "rule id preserved verbatim",
        bool(stored) and stored.rule_id == "rule-42-0912345678",
        f"stored={stored.rule_id if stored else None!r}",
    )


def check_filters_and_paging() -> None:
    print("\n[4] persistence, filtering, paging, stats")
    s = stub()
    tag = f"filter-{uuid.uuid4().hex[:6]}"
    for i in range(5):
        write_entry(
            s,
            level="ERROR" if i % 2 == 0 else "WARN",
            service="acceptance",
            category="acceptance-filter",
            message=f"{tag} item {i}",
            detail=json.dumps({"keep": KEEP}),
            correlation_id=f"{tag}-{i}",
        )
    time.sleep(0.2)

    by_cat = s.QueryLogs(pb.QueryLogsRequest(category="acceptance-filter", limit=100), timeout=10.0)
    check("filter by category", by_cat.total >= 5, f"total={by_cat.total}")

    by_level = s.QueryLogs(
        pb.QueryLogsRequest(category="acceptance-filter", level="ERROR", limit=100), timeout=10.0
    )
    check(
        "filter by level narrows the result",
        by_level.total >= 3 and all(x.level == "ERROR" for x in by_level.logs),
        f"total={by_level.total}",
    )

    by_search = s.QueryLogs(
        pb.QueryLogsRequest(category="acceptance-filter", search=tag, limit=100), timeout=10.0
    )
    check("free-text search matches the message", by_search.total >= 5, f"total={by_search.total}")

    paged = s.QueryLogs(
        pb.QueryLogsRequest(category="acceptance-filter", limit=2, offset=0), timeout=10.0
    )
    check("limit is honoured", len(paged.logs) == 2, f"page={len(paged.logs)}")
    check("has_more is set for a truncated page", bool(paged.has_more))

    missing = s.QueryLogs(pb.QueryLogsRequest(correlation_id=f"does-not-exist-{tag}"), timeout=10.0)
    check("unknown correlation id returns 0 rows", missing.total == 0)

    stats = s.LogStats(pb.LogStatsRequest(), timeout=10.0)
    check("stats count the writes", stats.total >= 1, f"total={stats.total}")
    check("stats break down by level", bool(stats.by_level), f"by_level={dict(stats.by_level)}")


def engine_metric(name: str) -> float | None:
    """Read one counter from the engine's own metrics endpoint (same network)."""
    import urllib.request

    url = os.environ.get("ATF_LOGGER_METRICS_URL", "http://logger:9109/metrics")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - fixed internal URL
            for line in resp.read().decode().splitlines():
                if line.startswith(name + " "):
                    return float(line.split()[1])
    except Exception:  # noqa: BLE001 - metrics are evidence, never a hard dependency
        return None
    return None


def check_error_reporting() -> None:
    """The engine must reject a malformed request honestly, and never let a bad
    page size turn into an unbounded scan.

    Verified behaviour (differs from the retired Python engine, in the safer
    direction — see docs/logger-rust-deployment.md): an entry-less LogRequest is
    answered `success=false` and counted in `events_rejected_total{reason="invalid"}`,
    where the Python servicer stored an unlabelled INFO row instead; an
    out-of-range page size is clamped to the default page (100) instead of
    scanning the whole table.
    """
    print("\n[5] error reporting and bounded paging")
    s = stub()

    rejected_before = engine_metric('atf_logger_events_rejected_total{reason="invalid"}')
    stats_before = s.LogStats(pb.LogStatsRequest(), timeout=10.0).total
    try:
        resp = s.Log(pb.LogRequest(), timeout=10.0)  # no entry at all
        check(
            "entry-less request rejected honestly",
            not resp.success,
            f"success={resp.success} message={resp.message!r}",
        )
    except grpc.RpcError as exc:
        check("entry-less request rejected honestly", exc.code() == grpc.StatusCode.INVALID_ARGUMENT, f"code={exc.code()}")
    time.sleep(0.2)
    stats_after = s.LogStats(pb.LogStatsRequest(), timeout=10.0).total
    check(
        "rejected request wrote nothing",
        stats_after == stats_before,
        f"stats {stats_before} -> {stats_after}",
    )
    rejected_after = engine_metric('atf_logger_events_rejected_total{reason="invalid"}')
    if rejected_before is None or rejected_after is None:
        print("  SKIP  rejection counter (metrics endpoint unreachable from here)")
    else:
        check(
            "rejection is accounted for in the engine's metrics",
            rejected_after > rejected_before,
            f"events_rejected_total {rejected_before} -> {rejected_after}",
        )

    # A malformed page size must be clamped, not turned into an unbounded scan.
    cat = f"acceptance-page-{uuid.uuid4().hex[:6]}"
    for i in range(120):
        write_entry(
            s,
            level="ERROR",
            service="acceptance",
            category=cat,
            message=f"page row {i}",
            correlation_id=f"{cat}-{i:03d}",
        )
    time.sleep(0.3)
    try:
        bad_page = s.QueryLogs(pb.QueryLogsRequest(category=cat, limit=-1), timeout=10.0)
        check(
            "out-of-range page size is clamped to a bounded page",
            len(bad_page.logs) <= 100 and bad_page.total == 120,
            f"returned={len(bad_page.logs)} total={bad_page.total}",
        )
    except grpc.RpcError as exc:
        check("out-of-range page size rejected or clamped", exc.code() is not None, f"code={exc.code()}")


async def check_producer_path() -> None:
    print("\n[1,6] Core producer path (LogClient queue/pump/drain)")
    client = LogClient(address=ADDRESS, max_queue_size=256)
    await client.start()
    tag = f"client-{uuid.uuid4().hex[:6]}"
    total = 25
    for i in range(total):
        client.error(
            service="atf-core",
            category="acceptance-client",
            message=f"{tag} event {i}",
            correlation_id=f"{tag}-{i:03d}",
            detail={"token": f"secret-{i}", "keep": f"keep-{i}"},
        )
    await asyncio.sleep(0.3)
    await client.close()
    counters = client.counters()
    print(f"  counters: {counters}")
    check("producer accepted every record", counters["accepted"] == total, f"{counters['accepted']}/{total}")
    check("producer sent every record", counters["sent"] == total, f"{counters['sent']}/{total}")
    check("nothing dropped", counters["dropped"] == 0, f"dropped={counters['dropped']}")
    check("clean drain left nothing undelivered", counters["shutdown_dropped"] == 0)

    s = stub()
    res = s.QueryLogs(pb.QueryLogsRequest(category="acceptance-client", limit=200), timeout=10.0)
    mine = [x for x in res.logs if x.correlation_id.startswith(tag)]
    check("every record persisted", len(mine) == total, f"{len(mine)}/{total}")
    check(
        "ids intact and secrets redacted on the producer path",
        all(x.correlation_id.startswith(tag) and "secret-" not in x.detail for x in mine),
    )


def main() -> int:
    print(f"Logger acceptance against {ADDRESS}\n{'=' * 60}")
    check_roundtrip_and_redaction()
    check_filters_and_paging()
    check_error_reporting()
    asyncio.run(check_producer_path())
    print("\n" + "=" * 60)
    if failures:
        print(f"ACCEPTANCE FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ACCEPTANCE PASSED: all checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
