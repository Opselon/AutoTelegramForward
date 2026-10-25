"""Regressions for the two recurring production warnings of 2026-10-23..25.

Pattern 1 (219 rows in the logger store, one raw_api WARN per failing RPC):
the channel poller called ``channels.GetFullChannel`` on a *basic-group* /
*user* peer (positive chat id), Telegram answered ``400 CHANNEL_INVALID`` on
every sweep, and the raw MTProto tracer logged each refusal as a WARNING.
A non-channel source never becomes a channel, so the retry is permanently
useless: the poller must branch on peer type *before* issuing a channel-only
MTProto RPC and leave the backoff to the ``PollFailure`` path.

Pattern 2: the raw tracer is an *opt-in* diagnostic
(``ATF_RAW_API_TRACE=1``). Its FAILED line was emitted at WARNING, which made
an operator who switched the tracer on for a diagnosis land every failed RPC
in the structured logger store as a WARN — the diagnostic tool itself became
the log storm it was meant to debug. A tracer failure line must stay at the
tracer's own INFO level (it is a trace, not a health signal); the owning call
path is the one that decides severity.
"""

import asyncio
import logging
import os

import pytest

from core.infrastructure.telegram.channel_polling import (
    ChannelPollingService,
    PollFailure,
)
from core.infrastructure.telegram.raw_tracer import (
    RAW_TRACE_ENV,
    RawTelegramApiTracer,
    redact,
)


# --------------------------------------------------------------------------- #
# Peer-type branch: channel-only RPCs must not be issued for non-channel peers
# --------------------------------------------------------------------------- #

class _StubRules:
    def __init__(self, rules) -> None:
        self._rules = rules

    async def list_all(self):
        return list(self._rules)


class _Rule:
    def __init__(self, src: str, session: str = "s1", active: bool = True) -> None:
        self.id = "rule-" + src
        self.session_id = session
        self.source_chat_id = src
        self.source_chat_name = src
        self.is_active = active
        self.is_paused = False


class _RecordingClient:
    """A client that records every invoke() and answers per-RPC behaviour.

    ``behaviour`` maps a query class name to either a value to return or an
    exception instance to raise. Anything else resolves successfully.
    """

    def __init__(self, behaviour=None) -> None:
        self.behaviour = behaviour or {}
        self.invoked = []
        self.resolved = []
        self.history_calls = 0

    async def resolve_peer(self, peer):
        self.resolved.append(peer)
        return peer  # the poller inspects the resolved peer's own type

    async def invoke(self, query):
        name = type(query).__name__
        self.invoked.append(name)
        action = self.behaviour.get(name)
        if isinstance(action, Exception):
            raise action
        return action if action is not None else object()

    async def get_chat_history(self, chat_id, limit=1):
        self.history_calls += 1

        class _M:
            id = 5

        yield _M()


def _make_service(client, rules):
    pool = type("Pool", (), {})()
    pool.list_sessions = lambda: ["s1"]
    pool.get = lambda sid: client
    return ChannelPollingService(
        pool=pool,
        rules=_StubRules(rules),
        dispatcher=type("D", (), {"on_replay": lambda self, *a, **k: True})(),
    )


def _channel_invalid():
    """Telegram's 400 CHANNEL_INVALID, or a stand-in when pyrogram is absent."""
    try:
        from pyrogram.errors import ChannelInvalid

        return ChannelInvalid("The channel parameter is invalid")
    except Exception:  # pragma: no cover - slim test environments
        return ValueError("CHANNEL_INVALID")


@pytest.mark.asyncio
async def test_poller_does_not_invoke_getfullchannel_for_user_peer(caplog):
    """A positive (user/bot/basic-group) source must never reach GetFullChannel.

    Before the fix the poller resolved the peer (successfully) and immediately
    issued GetFullChannel, which answers CHANNEL_INVALID for every non-channel
    peer — a permanent condition retried once per sweep, forever.
    """
    client = _RecordingClient({"GetFullChannel": _channel_invalid()})
    service = _make_service(client, [_Rule("8397346615", session="s1")])
    caplog.set_level(logging.WARNING, logger="atf.boot")

    await service.sweep_all()

    assert "GetFullChannel" not in client.invoked, (
        "GetFullChannel was issued for a non-channel source (user/basic group): "
        + str(client.invoked)
    )


@pytest.mark.asyncio
async def test_poller_does_not_invoke_getfullchannel_for_basic_group(caplog):
    """A negative id without the -100 mark is a basic group, not a channel."""
    client = _RecordingClient({"GetFullChannel": _channel_invalid()})
    service = _make_service(client, [_Rule("-5191245716", session="s1")])
    caplog.set_level(logging.WARNING, logger="atf.boot")

    await service.sweep_all()

    assert "GetFullChannel" not in client.invoked, (
        "GetFullChannel was issued for a basic-group source: "
        + str(client.invoked)
    )


@pytest.mark.asyncio
async def test_poller_records_one_warning_for_a_permanently_bad_source(caplog):
    """A permanently failing source logs the refusal once, not once per sweep."""
    client = _RecordingClient({"GetFullChannel": _channel_invalid()})
    service = _make_service(client, [_Rule("8397346615", session="s1")])
    caplog.set_level(logging.WARNING, logger="atf.boot")

    await service.sweep_all()
    await service.sweep_all()
    await service.sweep_all()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) <= 1, (
        "a permanently invalid source must produce one warning, not one per "
        "sweep: " + str([r.message for r in warnings])
    )


@pytest.mark.asyncio
async def test_poller_still_polls_real_channels_after_the_branch(caplog):
    """The peer-type branch must not stop polling genuine channels."""
    client = _RecordingClient()
    service = _make_service(client, [_Rule("-1001957768371", session="s1")])
    caplog.set_level(logging.WARNING, logger="atf.boot")

    await service.sweep_all()

    assert "GetFullChannel" in client.invoked, (
        "a real channel source must still be polled: " + str(client.invoked)
    )


@pytest.mark.asyncio
async def test_poller_reports_unresolvable_peer_as_pollfailure(caplog):
    """A peer that cannot be resolved is a PollFailure, which backoffs handle."""
    client = _RecordingClient({"GetFullChannel": _channel_invalid()})

    async def no_peer(peer):
        raise LookupError("PEER_ID_INVALID")

    client.resolve_peer = no_peer
    service = _make_service(client, [_Rule("8397346615", session="s1")])
    caplog.set_level(logging.WARNING, logger="atf.boot")

    replayed, checked = await service.sweep_all()

    assert checked == 0 or replayed == 0  # nothing was replayed
    assert "GetFullChannel" not in client.invoked


# --------------------------------------------------------------------------- #
# Raw tracer: a FAILED trace line is a trace, not a log storm
# --------------------------------------------------------------------------- #

class _FailingInvokeClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def invoke(self, query, *a, **kw):
        raise self._exc


def test_raw_tracer_failed_line_stays_info_not_warning(monkeypatch, caplog):
    """The tracer is opt-in diagnostics; its FAILED line must not be a WARN.

    With ATF_RAW_API_TRACE enabled, a failing RPC produced one WARNING per
    call and each was mirrored into the structured logger store, so enabling
    the tracer to diagnose one problem created a second one. The trace line
    stays INFO — the *caller* decides how severe the failure is.
    """
    monkeypatch.setenv(RAW_TRACE_ENV, "1")
    tracer = RawTelegramApiTracer(
        _FailingInvokeClient(_channel_invalid()), session_id="t", log_client=None
    )
    caplog.set_level(logging.INFO, logger="atf.raw_api")

    tracer.attach()

    with pytest.raises(Exception):
        asyncio.run(tracer.client.invoke(_Query()))

    failed = [r for r in caplog.records if "FAILED" in r.message]
    assert failed, "the tracer must still record the failure"
    assert all(r.levelno == logging.INFO for r in failed), (
        "tracer FAILED lines must be INFO, not WARNING: "
        + str([(r.levelname, r.message) for r in failed])
    )


def test_raw_tracer_does_not_mirror_failures_to_the_log_store(
    monkeypatch, caplog
):
    """The tracer must not push its FAILED lines into the structured store."""
    monkeypatch.setenv(RAW_TRACE_ENV, "1")

    class _RecordingLogClient:
        def __init__(self) -> None:
            self.warns = []

        def warn(self, *a, **kw):
            self.warns.append((a, kw))

    log_client = _RecordingLogClient()
    tracer = RawTelegramApiTracer(
        _FailingInvokeClient(_channel_invalid()), session_id="t", log_client=log_client
    )
    caplog.set_level(logging.INFO, logger="atf.raw_api")

    tracer.attach()

    with pytest.raises(Exception):
        asyncio.run(tracer.client.invoke(_Query()))

    assert not log_client.warns, (
        "the tracer mirrored a FAILED line into the structured logger store: "
        + str(log_client.warns)
    )


class _Query:
    """Any MTProto query shape — the tracer only reads its type name."""

    pass


def test_redact_still_masks_secrets():
    """The tracer's redaction keeps masking the shapes it always masked.

    `redact` targets credential *shapes* (bot tokens, bearer values, long hex
    hashes, phone numbers), not arbitrary digits, so the assertion pins those.
    """
    assert "[REDACTED]" in redact("token=supersecretvalue123 token")
    assert "[PHONE]" in redact("phone +989123456789 called")
    # A bot token in the Bot API URL shape is always masked (the payload after
    # the colon must be a real 35-char token; the regex requires >=25 chars).
    assert "AAHvx" not in redact(
        "/bot123456789:AAHvxKq3pQ7mYvS4n6fBzLt2e2gRt6p1uX/sendMessage"
    )


# --------------------------------------------------------------------------- #
# The peer-type classifier itself
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "chat_id, expected",
    [
        # Channels / supergroups: marked with -100, the only peers
        # channels.GetFullChannel accepts.
        ("-1004400615270", True),
        ("-1001957768371", True),
        ("-1001695513745", True),
        ("-1001234567890", True),
        # The synthetic short id the existing polling fixtures use: marked, so
        # a channel peer (the mark is the discriminator, not magnitude).
        ("-1001", True),
        ("-100123456", True),
        # Users / bots: positive ids, never channel peers.
        ("8397346615", False),
        ("5094837833", False),
        # Basic groups: negative without the -100 mark.
        ("-5191245716", False),
        ("-1234567890", False),
        # Not an id at all.
        ("", False),
        ("@username", False),
        ("not-a-number", False),
        # Shorter than the mark: no channel id behind it.
        ("-100", False),
        ("-13", False),
    ],
)
def test_is_channel_peer_id(chat_id, expected):
    from core.infrastructure.telegram.channel_polling import _is_channel_peer_id

    assert _is_channel_peer_id(chat_id) is expected
