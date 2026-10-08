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


def _log(d, level="INFO", service="core", category="pipeline", message="hi", detail=""):
    import time

    d.insert(ts=int(time.time()), level=level, service=service, category=category,
             message=message, detail=detail)


def test_insert_and_query(db):
    _log(db, message="forwarded ok")
    rows = db.query(limit=10)
    assert len(rows) == 1
    assert rows[0]["service"] == "core"
    assert rows[0]["message"] == "forwarded ok"


def test_query_filters_and_search(db):
    _log(db, level="INFO", category="pipeline", message="forward ok")
    _log(db, level="ERROR", service="bot", category="login", message="login failed")
    _log(db, level="INFO", service="api", category="http", message="GET /stats")
    assert len(db.query(service="core", limit=10)) == 1
    assert len(db.query(level="ERROR", limit=10)) == 1
    assert len(db.query(search="login", limit=10)) == 1
    assert len(db.query(service="api", level="ERROR", limit=10)) == 0


def test_stats(db):
    _log(db, level="INFO", category="pipeline", message="one")
    _log(db, level="DEBUG", category="pipeline", message="two")
    _log(db, level="ERROR", service="api", category="http", message="three")
    total, by_level, by_category = db.stats()
    assert total == 3
    assert by_level == {"INFO": 1, "DEBUG": 1, "ERROR": 1}
    assert by_category == {"pipeline": 2, "http": 1}


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
