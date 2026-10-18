"""The core's gRPC surface must not answer a caller that cannot prove it is the API.

Every servicer in `core.infrastructure.grpc_server` takes the acting tenant from
`owner_user_id` in the request. The Go API fills that field from the identity it
verified, so the ownership *contract* is sound — but the transport had no
credential at all, which meant any process able to open a socket to core:50051
could act as any tenant by writing a different number into that field.

These tests drive a real `grpc.aio` server over loopback with the real
interceptor and a handler that counts its invocations, so "refused" means the
servicer did not run — not that an exception was mapped to a status somewhere.

The tests also pin the *absence* of the interceptor when nothing is configured:
that is the compatibility guarantee every existing deployment depends on.
"""

from __future__ import annotations

import asyncio
import logging

import grpc
import pytest

from core.infrastructure.grpc_server.internal_auth import (
    AUTH_METADATA_KEY,
    InternalAuthInterceptor,
    _credential_matches,
    _unauthenticated,
    build_interceptors,
    configured_token,
)

TOKEN = "s3cret-internal-token"

#: Incremented by the echo handler below. If a refused call ever reaches it, the
#: test fails on the counter rather than on a status code — the point is that no
#: servicer code runs.
SERVICER_CALLS = 0


async def _echo(request, context):
    global SERVICER_CALLS
    SERVICER_CALLS += 1
    return b"pong"


async def _echo_stream(request, context):
    global SERVICER_CALLS
    SERVICER_CALLS += 1
    yield b"pong"


class _RunningServer:
    """A real aio server holding one unary and one server-streaming method."""

    def __init__(self, port: int, server: grpc.aio.Server):
        self.port = port
        self.server = server

    async def stop(self):
        await self.server.stop(None)


async def _start(token: str | None) -> _RunningServer:
    server = grpc.aio.server(interceptors=build_interceptors(token))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "test.Echo",
                {
                    "Echo": grpc.unary_unary_rpc_method_handler(_echo),
                    "Stream": grpc.unary_stream_rpc_method_handler(_echo_stream),
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return _RunningServer(port, server)


async def _call(port: int, metadata=(), method="/test.Echo/Echo") -> bytes:
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        if method.endswith("Stream"):
            stream = channel.unary_stream(method)
            out = b""
            async for chunk in stream(b"hi", metadata=tuple(metadata)):
                out += chunk
            return out
        return await channel.unary_unary(method)(b"hi", metadata=tuple(metadata))
    finally:
        await channel.close()


def _run(coro):
    return asyncio.run(coro)


# --- the refusal is real: the servicer must not run --------------------------


def test_call_without_the_credential_never_reaches_the_servicer():
    global SERVICER_CALLS
    SERVICER_CALLS = 0

    async def scenario():
        running = await _start(TOKEN)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await _call(running.port)
            return err.value.code()
        finally:
            await running.stop()

    code = _run(scenario())
    assert code == grpc.StatusCode.UNAUTHENTICATED
    assert SERVICER_CALLS == 0, "the handler ran for an unauthenticated caller"


def test_the_credential_lets_the_call_through():
    global SERVICER_CALLS
    SERVICER_CALLS = 0

    async def scenario():
        running = await _start(TOKEN)
        try:
            return await _call(running.port, [(AUTH_METADATA_KEY, f"Bearer {TOKEN}")])
        finally:
            await running.stop()

    assert _run(scenario()) == b"pong"
    assert SERVICER_CALLS == 1


def test_a_wrong_credential_is_refused_like_a_missing_one():
    async def scenario(metadata):
        running = await _start(TOKEN)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await _call(running.port, metadata)
            return err.value.code()
        finally:
            await running.stop()

    for metadata in (
        [(AUTH_METADATA_KEY, "Bearer wrong")],
        [(AUTH_METADATA_KEY, TOKEN)],  # bare token, no scheme
        [(AUTH_METADATA_KEY, f"Basic {TOKEN}")],  # right secret, wrong scheme
        [(AUTH_METADATA_KEY, "Bearer")],  # scheme with no value
        [("x-core-rpc-token", TOKEN)],  # a plausible-looking other header
    ):
        assert _run(scenario(metadata)) == grpc.StatusCode.UNAUTHENTICATED, metadata


def test_the_scheme_is_case_insensitive_but_the_secret_is_not():
    async def scenario(metadata):
        running = await _start(TOKEN)
        try:
            return await _call(running.port, metadata)
        finally:
            await running.stop()

    assert _run(scenario([(AUTH_METADATA_KEY, f"bearer {TOKEN}")])) == b"pong"
    # Leading/trailing whitespace in a header value is insignificant (HTTP says
    # so), so a credential that differs only in padding is the same credential.
    assert _run(scenario([(AUTH_METADATA_KEY, f"Bearer  {TOKEN} ")])) == b"pong"
    with pytest.raises(grpc.aio.AioRpcError):
        _run(scenario([(AUTH_METADATA_KEY, f"Bearer {TOKEN.upper()}")]))


def test_a_refusal_keeps_the_method_arity():
    """A stream method must be refused as a stream, not as an internal error.

    gRPC dispatches on the published handler's cardinality: publishing a
    unary-unary refusal in place of a server-streaming method turns "you are not
    authenticated" into an INTERNAL error that reads like a servicer bug.
    """

    async def scenario():
        running = await _start(TOKEN)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await _call(running.port, method="/test.Echo/Stream")
            return err.value.code()
        finally:
            await running.stop()

    assert _run(scenario()) == grpc.StatusCode.UNAUTHENTICATED


# --- configuration -----------------------------------------------------------


def test_unset_credential_installs_no_interceptor():
    for value in (None, "", "   ", "\t\n"):
        assert build_interceptors(value) == []


def test_a_whitespace_credential_is_treated_as_unset_not_as_a_public_secret():
    # " " would otherwise install a boundary whose secret is one space.
    assert configured_token("  ") == ""
    assert build_interceptors(" token ") and len(build_interceptors(" token ")) == 1
    assert configured_token(" token ") == "token"


def test_config_reads_the_token_from_the_environment(monkeypatch):
    from core.config import Config

    monkeypatch.setenv("ATF_CORE_RPC_TOKEN", "from-env")
    cfg = Config.load(config_path="/nonexistent/config.yaml")
    assert cfg.core_rpc_token == "from-env"


def test_config_defaults_to_no_credential(monkeypatch):
    from core.config import Config

    monkeypatch.delenv("ATF_CORE_RPC_TOKEN", raising=False)
    cfg = Config.load(config_path="/nonexistent/config.yaml")
    assert cfg.core_rpc_token == ""


# --- the credential must not end up in the logs ------------------------------


def test_the_credential_is_never_logged(caplog):
    async def scenario():
        running = await _start(TOKEN)
        try:
            with pytest.raises(grpc.aio.AioRpcError):
                await _call(running.port, [(AUTH_METADATA_KEY, "Bearer wrong-secret")])
        finally:
            await running.stop()

    with caplog.at_level(logging.DEBUG):
        _run(scenario())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "wrong-secret" not in joined
    assert TOKEN not in joined
    # ...and the operator still learns a call was refused.
    assert any("unauthenticated" in record.getMessage() for record in caplog.records), (
        joined
    )


# --- unit-level: the matcher itself -----------------------------------------


def test_matcher_reads_every_credential_header_and_ignores_others():
    assert _credential_matches(TOKEN, [(AUTH_METADATA_KEY, f"Bearer {TOKEN}")])
    assert _credential_matches(
        TOKEN,
        [
            ("x-trace", "abc"),
            (AUTH_METADATA_KEY, "Bearer no"),
            (AUTH_METADATA_KEY, f"Bearer {TOKEN}"),
        ],
    )
    assert not _credential_matches(TOKEN, [("x-trace", "abc")])
    assert not _credential_matches(TOKEN, None)
    # A header whose name merely contains "authorization" is not the header.
    assert not _credential_matches(TOKEN, [("x-authorization", f"Bearer {TOKEN}")])


def test_build_interceptors_returns_the_real_interceptor():
    built = build_interceptors(TOKEN)
    assert len(built) == 1
    assert isinstance(built[0], InternalAuthInterceptor)


@pytest.mark.parametrize("method", ["/test.Echo/Echo", "/test.Echo/Stream"])
def test_unauthenticated_wrapper_answers_with_unauthenticated(method):
    """Direct check on the wrapper's status, independent of the interceptor."""

    class _Context:
        def __init__(self):
            self.code = None

        async def abort(self, code, details):
            self.code = code
            raise RuntimeError(details)

    async def scenario():
        handler = _unauthenticated(
            grpc.unary_unary_rpc_method_handler(_echo)
            if method.endswith("Echo")
            else grpc.unary_stream_rpc_method_handler(_echo_stream)
        )
        context = _Context()
        invoke = handler.unary_unary or handler.unary_stream
        with pytest.raises(RuntimeError):
            await invoke(b"", context)
        return context.code

    assert _run(scenario()) == grpc.StatusCode.UNAUTHENTICATED
