"""Live gRPC roundtrip: boot the real Python servicers in-process and query
them with a real gRPC client stub. No Telegram network involved."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import grpc  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from core.config import Config  # noqa: E402
from core.main import build_container, serve_grpc  # noqa: E402
from core.proto import pb, pb_grpc  # noqa: E402


@pytest_asyncio.fixture()
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
        # Unauthenticated / empty owner fails closed
        resp = await stub.ListSessions(pb.ListSessionsRequest())
        assert resp.sessions == []

        # Seed an owned session and query with owner_user_id
        await container["sessions"].create("+989****0000", "enc-session-str", owner_user_id=42)
        resp_empty = await stub.ListSessions(pb.ListSessionsRequest())
        assert resp_empty.sessions == []  # still fails closed without owner

        resp = await stub.ListSessions(pb.ListSessionsRequest(owner_user_id=42))
        assert len(resp.sessions) == 1
        assert resp.sessions[0].phone_number == "+989****0000"


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
    session = await container["sessions"].create("+989****1111", "", owner_user_id=42)
    from core.domain.entities import ForwardRule
    from core.domain.value_objects import ForwardMode, RoutingType

    await container["rules"].create(ForwardRule(
        session_id=session.id,
        source_chat_id="-1001234567890", target_chat_id="-1009876543210",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        owner_user_id=42,
    ))
    async with grpc.aio.insecure_channel(endpoint) as channel:
        stub = pb_grpc.ForwardRuleControlServiceStub(channel)
        # Without owner fails closed
        resp_unauth = await stub.ListRules(pb.ListRulesRequest(session_id=session.id))
        assert resp_unauth.rules == []

        # With owner returns rule
        resp = await stub.ListRules(pb.ListRulesRequest(session_id=session.id, owner_user_id=42))
        assert len(resp.rules) == 1
        assert resp.rules[0].source_chat_id == "-1001234567890"
        assert resp.rules[0].routing_type == pb.RoutingType.CHANNEL_TO_CHANNEL
