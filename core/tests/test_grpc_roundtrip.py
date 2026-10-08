"""Live gRPC roundtrip: boot the real Python servicers in-process and query
them with a real gRPC client stub. No Telegram network involved."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import grpc  # noqa: E402
import pytest  # noqa: E402

from core.config import Config  # noqa: E402
from core.main import build_container, serve_grpc  # noqa: E402
from core.proto import pb, pb_grpc  # noqa: E402


@pytest.fixture()
async def grpc_endpoint():
    import socket
    from contextlib import closing

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    cfg = Config.load()
    cfg.db_path = ":memory:"
    cfg.grpc_port = free_port
    cfg.grpc_host = "127.0.0.1"
    container = build_container(cfg)
    server = await serve_grpc(container, cfg)
    yield f"127.0.0.1:{free_port}", container
    await server.stop(grace=1)
    container["db"].close()


@pytest.mark.asyncio
async def test_list_sessions_roundtrip(grpc_endpoint):
    endpoint, container = grpc_endpoint
    async with grpc.aio.insecure_channel(endpoint) as channel:
        stub = pb_grpc.SessionControlServiceStub(channel)
        resp = await stub.ListSessions(pb.ListSessionsRequest())
        assert resp.sessions == []

        # Seed a session through the domain use case and re-query.
        await container["sessions"].create("+989120000000", "enc-session-str")
        resp = await stub.ListSessions(pb.ListSessionsRequest())
        assert len(resp.sessions) == 1
        assert resp.sessions[0].phone_number == "+989120000000"


@pytest.mark.asyncio
async def test_stats_roundtrip(grpc_endpoint):
    endpoint, container = grpc_endpoint
    async with grpc.aio.insecure_channel(endpoint) as channel:
        stub = pb_grpc.SystemStatusControlServiceStub(channel)
        resp = await stub.GetSystemStats(pb.SystemStatsRequest())
        assert resp.core_running is True
        assert resp.version == container["config"].version


@pytest.mark.asyncio
async def test_rules_roundtrip(grpc_endpoint):
    endpoint, container = grpc_endpoint
    session = await container["sessions"].create("+989121111111", "")
    from core.domain.entities import ForwardRule
    from core.domain.value_objects import ForwardMode, RoutingType

    await container["rules"].create(ForwardRule(
        session_id=session.id,
        source_chat_id="-100AAA", target_chat_id="-100BBB",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
    ))
    async with grpc.aio.insecure_channel(endpoint) as channel:
        stub = pb_grpc.ForwardRuleControlServiceStub(channel)
        resp = await stub.ListRules(pb.ListRulesRequest(session_id=session.id))
        assert len(resp.rules) == 1
        assert resp.rules[0].source_chat_id == "-100AAA"
        assert resp.rules[0].routing_type == pb.RoutingType.CHANNEL_TO_CHANNEL
