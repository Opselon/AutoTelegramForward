"""Tests for the Logger microservice store + live gRPC roundtrip."""

import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from logger.main import LoggerDatabase, LogControlServicer


@pytest.fixture()
def db(tmp_path):
    d = LoggerDatabase(str(tmp_path / "logger_test.db"))
    yield d
    d.close()


def _log(d, level="INFO", service="core", category="pipeline", message="hi", detail="",
        ts=None, rule_id="", user_id=0, correlation_id=""):
    import time

    d.insert(ts=ts if ts is not None else int(time.time()), level=level, service=service,
             category=category, message=message, detail=detail, rule_id=rule_id,
             user_id=user_id, correlation_id=correlation_id)


def _q(d, **kw):
    """Query returning just the page (the store returns (rows, total))."""
    rows, _total = d.query(**kw)
    return rows


def test_insert_and_query(db):
    _log(db, message="forwarded ok")
    rows, total = db.query(limit=10)
    assert total == 1
    assert len(rows) == 1
    assert rows[0]["service"] == "core"
    assert rows[0]["message"] == "forwarded ok"


def test_query_filters_and_search(db):
    _log(db, level="INFO", category="pipeline", message="forward ok")
    _log(db, level="ERROR", service="bot", category="login", message="login failed")
    _log(db, level="INFO", service="api", category="http", message="GET /stats")
    assert len(_q(db, service="core", limit=10)) == 1
    assert len(_q(db, level="ERROR", limit=10)) == 1
    assert len(_q(db, search="login", limit=10)) == 1
    assert len(_q(db, service="api", level="ERROR", limit=10)) == 0


def test_stats(db):
    _log(db, level="INFO", category="pipeline", message="one")
    _log(db, level="DEBUG", category="pipeline", message="two")
    _log(db, level="ERROR", service="api", category="http", message="three")
    stats = db.stats()
    assert stats["total"] == 3
    assert stats["by_level"] == {"INFO": 1, "DEBUG": 1, "ERROR": 1}
    assert stats["by_category"] == {"pipeline": 2, "http": 1}
    assert stats["by_service"] == {"core": 2, "api": 1}
    assert stats["oldest_ts"] > 0 and stats["newest_ts"] >= stats["oldest_ts"]


def test_grpc_roundtrip(tmp_path):
    """Live Logger servicer over real async gRPC with a temp DB."""
    import asyncio

    import grpc.aio

    from core.proto import pb, pb_grpc

    async def scenario() -> None:
        store = LoggerDatabase(str(tmp_path / "rt.db"))
        server = grpc.aio.server()
        pb_grpc.add_LogControlServiceServicer_to_server(LogControlServicer(store), server)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server.add_insecure_port(f"127.0.0.1:{port}")
        await server.start()
        try:
            ch = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            await ch.channel_ready()
            stub = pb_grpc.LogControlServiceStub(ch)

            r = await stub.Log(pb.LogRequest(entry=pb.LogEntry(
                service="core", level="DEBUG", category="t", message="hello")))
            assert r.success is True

            q = await stub.QueryLogs(pb.QueryLogsRequest(limit=10))
            assert q.total == 1
            assert q.logs[0].message == "hello"
            assert q.logs[0].level == "DEBUG"

            s = await stub.LogStats(pb.LogStatsRequest())
            assert s.total == 1
            assert s.by_level["DEBUG"] == 1
            await ch.close()
        finally:
            await server.stop(grace=1)
            store.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Purge (API-first log retention)
# --------------------------------------------------------------------------- #
def test_purge_all(db):
    _log(db, level="INFO", message="a")
    _log(db, level="ERROR", message="b")
    purged, remaining = db.purge(older_than_hours=0)
    assert purged == 2
    assert remaining == 0


def test_purge_by_level_keeps_others(db):
    _log(db, level="INFO", message="keep me")
    _log(db, level="DEBUG", message="keep me too")
    _log(db, level="ERROR", message="drop me")
    purged, remaining = db.purge(level="ERROR", older_than_hours=0)
    assert purged == 1
    assert remaining == 2
    assert [r["level"] for r in _q(db, limit=10)] == ["DEBUG", "INFO"]


def test_purge_respects_time_window(db):
    import time

    now = int(time.time())
    db.insert(ts=now, level="INFO", service="core", category="pipeline",
              message="fresh", detail="")
    db.insert(ts=now - 48 * 3600, level="INFO", service="core", category="pipeline",
              message="old", detail="")
    purged, remaining = db.purge(older_than_hours=24)
    assert purged == 1
    assert remaining == 1
    assert _q(db, limit=10)[0]["message"] == "fresh"


def test_purge_scoped_by_service_and_category(db):
    _log(db, level="WARN", service="api", category="http", message="api warn")
    _log(db, level="WARN", service="core", category="http", message="core warn")
    purged, remaining = db.purge(service="api", older_than_hours=0)
    assert purged == 1
    assert remaining == 1
    assert _q(db, limit=10)[0]["service"] == "core"


# --------------------------------------------------------------------------- #
# v18 attribution, deep paging and history access
# --------------------------------------------------------------------------- #
def test_v18_attribution_columns_round_trip(db):
    _log(db, message="with owner", rule_id="rule-abc", user_id=5094837833,
         correlation_id="corr-1")
    rows = _q(db, limit=10)
    assert rows[0]["rule_id"] == "rule-abc"
    assert rows[0]["user_id"] == 5094837833
    assert rows[0]["correlation_id"] == "corr-1"


def test_v18_migration_adds_columns_to_legacy_table(tmp_path):
    """An archive created before v18 must gain the columns, not fail to open."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,"
        " level TEXT NOT NULL, service TEXT NOT NULL, category TEXT NOT NULL,"
        " message TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')"
    )
    legacy.execute(
        "INSERT INTO logs (ts, level, service, category, message, detail)"
        " VALUES (1, 'INFO', 'core', 'sys', 'pre-v18 row', '')"
    )
    legacy.commit()
    legacy.close()

    store = LoggerDatabase(path)
    try:
        rows, total = store.query(limit=10)
        assert total == 1
        # The historical row survives and reads back as unattributed.
        assert rows[0]["message"] == "pre-v18 row"
        assert rows[0]["rule_id"] == ""
        # And the new columns are writable on the migrated table.
        store.insert(ts=2, level="INFO", service="core", category="sys",
                     message="post-v18 row", detail="", rule_id="r9")
        assert store.query(rule_id="r9", limit=10)[1] == 1
    finally:
        store.close()


def test_v18_search_matches_detail_not_only_message(db):
    _log(db, message="opaque sentence", detail='{"rule_id": "deep-needle-42"}')
    assert db.query(search="deep-needle-42", limit=10)[1] == 1
    assert db.query(search="nothing-here", limit=10)[1] == 0


def test_v18_offset_pages_through_full_history(db):
    """Total must count every match, and offset must reach the oldest rows."""
    import time

    now = int(time.time())
    for i in range(25):
        _log(db, message=f"row-{i:02d}", ts=now - (25 - i))
    first, total = db.query(limit=10, offset=0)
    assert total == 25
    assert len(first) == 10
    assert first[0]["message"] == "row-24"  # newest first
    second, _ = db.query(limit=10, offset=10)
    assert second[0]["message"] == "row-14"
    last, _ = db.query(limit=10, offset=20)
    assert len(last) == 5
    assert last[-1]["message"] == "row-00"  # the oldest record is reachable


def test_v18_ascending_order_for_timeline_views(db):
    import time

    now = int(time.time())
    _log(db, message="oldest", ts=now - 100)
    _log(db, message="newest", ts=now)
    rows = _q(db, limit=10, ascending=True)
    assert [r["message"] for r in rows] == ["oldest", "newest"]


def test_v18_until_and_levels_filters(db):
    import time

    now = int(time.time())
    _log(db, level="ERROR", message="old error", ts=now - 500)
    _log(db, level="ERROR", message="new error", ts=now)
    _log(db, level="INFO", message="new info", ts=now)
    assert db.query(until=now - 100, limit=10)[1] == 1
    assert db.query(levels=["ERROR", "WARN"], limit=10)[1] == 2
    assert db.query(levels=["INFO"], limit=10)[1] == 1


def test_v18_stats_scoped_and_by_rule(db):
    _log(db, message="a", rule_id="rule-1", user_id=11)
    _log(db, message="b", rule_id="rule-1", user_id=11)
    _log(db, message="c", rule_id="rule-2", user_id=22)
    scoped = db.stats(rule_id="rule-1")
    assert scoped["total"] == 2
    # by_rule always describes the whole archive, so a task view can still
    # answer "which task is the noisy one?".
    assert scoped["by_rule"]["rule-1"] == 2 and scoped["by_rule"]["rule-2"] == 1
    assert db.stats(user_id=22)["total"] == 1


def test_v18_purge_scoped_to_one_rule(db):
    _log(db, message="keep", rule_id="rule-keep")
    _log(db, message="drop", rule_id="rule-drop")
    purged, remaining = db.purge(rule_id="rule-drop", older_than_hours=0)
    assert purged == 1
    assert remaining == 1
    assert _q(db, limit=10)[0]["rule_id"] == "rule-keep"


def test_v18_grpc_paging_and_attribution(tmp_path):
    """End-to-end: attribution in, real totals + has_more + rule_id out."""
    import asyncio
    import time

    import grpc.aio

    from core.proto import pb, pb_grpc

    async def scenario() -> None:
        store = LoggerDatabase(str(tmp_path / "v18.db"))
        server = grpc.aio.server()
        pb_grpc.add_LogControlServiceServicer_to_server(LogControlServicer(store), server)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server.add_insecure_port(f"127.0.0.1:{port}")
        await server.start()
        try:
            ch = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            await ch.channel_ready()
            stub = pb_grpc.LogControlServiceStub(ch)
            now = int(time.time())
            for i in range(6):
                await stub.Log(pb.LogRequest(entry=pb.LogEntry(
                    ts=now + i, level="INFO", service="core", category="forward",
                    message=f"msg-{i}", rule_id="rule-x", user_id=5094837833,
                    correlation_id=f"corr-{i}")))

            page = await stub.QueryLogs(pb.QueryLogsRequest(rule_id="rule-x", limit=4))
            assert page.total == 6, page.total
            assert page.has_more is True
            assert len(page.logs) == 4
            assert page.logs[0].rule_id == "rule-x"
            assert page.logs[0].user_id == 5094837833

            tail = await stub.QueryLogs(
                pb.QueryLogsRequest(rule_id="rule-x", limit=4, offset=4, ascending=True)
            )
            assert tail.has_more is False
            assert len(tail.logs) == 2
            assert tail.logs[0].message == "msg-4"

            by_corr = await stub.QueryLogs(pb.QueryLogsRequest(correlation_id="corr-3"))
            assert by_corr.total == 1 and by_corr.logs[0].message == "msg-3"

            stats = await stub.LogStats(pb.LogStatsRequest(rule_id="rule-x"))
            assert stats.total == 6
            assert stats.by_rule["rule-x"] == 6
            assert stats.by_service["core"] == 6

            other = await stub.QueryLogs(pb.QueryLogsRequest(rule_id="nope"))
            assert other.total == 0 and other.has_more is False
            await ch.close()
        finally:
            await server.stop(grace=1)
            store.close()

    asyncio.run(scenario())


@pytest.mark.asyncio()
async def test_purge_rpc_roundtrip(tmp_path):
    """The PurgeLogs RPC must validate the level and return honest counts.

    Spins up its own logger server on an ephemeral port so it works in CI
    (where nothing is listening on 6002).
    """
    import socket
    import time

    import grpc.aio

    from core.proto import pb, pb_grpc
    from logger.main import LogControlServicer, LoggerDatabase

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    store = LoggerDatabase(str(tmp_path / "purge_rpc.db"))
    servicer = LogControlServicer(store)
    server = grpc.aio.server()
    pb_grpc.add_LogControlServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
            stub = pb_grpc.LogControlServiceStub(ch)
            marker = "purge-rpc-probe-7741"
            now = int(time.time())
            for lvl in ("INFO", "WARN", "ERROR"):
                await stub.Log(pb.LogRequest(entry=pb.LogEntry(
                    ts=now - 72 * 3600, level=lvl, service="core", category="test",
                    message=f"{marker}-old-{lvl}")))
                await stub.Log(pb.LogRequest(entry=pb.LogEntry(
                    ts=now, level=lvl, service="core", category="test",
                    message=f"{marker}-fresh-{lvl}")))

            bad = await stub.PurgeLogs(pb.PurgeLogsRequest(level="NOPE"))
            assert not bad.success
            assert "invalid level" in bad.error

            r = await stub.PurgeLogs(pb.PurgeLogsRequest(level="WARN", older_than_hours=24))
            assert r.success

            fresh = await stub.QueryLogs(pb.QueryLogsRequest(level="WARN", search=marker, limit=50))
            got = [l.message for l in fresh.logs]
            assert f"{marker}-fresh-WARN" in got, got
            assert f"{marker}-old-WARN" not in got, got
    finally:
        await server.stop(grace=1)
        store.close()


# --------------------------------------------------------------------------- #
# Durability / acknowledgement contract. Producers treat `success=true` as
# "stored", so the pragmas these guarantees rest on are asserted rather than
# assumed — a default changing underneath us must fail a test, not lose logs.
# --------------------------------------------------------------------------- #
def test_durability_pragmas_are_explicit(db):
    conn = db._conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2, "FULL expected"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_busy_timeout_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("ATF_LOGGER_BUSY_TIMEOUT_MS", "1234")
    store = LoggerDatabase(str(tmp_path / "busy.db"))
    try:
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    finally:
        store.close()


def test_a_bad_busy_timeout_value_falls_back_instead_of_breaking_logging(tmp_path, monkeypatch):
    monkeypatch.setenv("ATF_LOGGER_BUSY_TIMEOUT_MS", "not-a-number")
    store = LoggerDatabase(str(tmp_path / "busy2.db"))
    try:
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        store.close()


def test_an_insert_is_committed_before_it_returns(db):
    """The producer's contract: once insert() returns, another connection sees it."""
    import sqlite3

    db.insert(ts=1234, level="ERROR", service="core", category="probe",
              message="committed?", detail="")
    other = sqlite3.connect(db._conn.execute("PRAGMA database_list").fetchone()[2])
    try:
        rows = other.execute("SELECT COUNT(*) FROM logs WHERE message = 'committed?'").fetchone()[0]
    finally:
        other.close()
    assert rows == 1, "insert() returned but the row was not visible to a second connection"
