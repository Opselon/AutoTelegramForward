"""Raw MTProto tracer: off by default, bounded and credential-free when on.

Field report: the core container log had grown to 434k lines / 46 MB, almost all
of it pretty-printed payload dumps like::

    [RAW MTPROTO << 1d12518c] GetDialogs OK (498.0ms): {
        "_": "types.messages.DialogsSlice",
        ...
        "access_hash": -6872547666990262989,

Two defects: the tracer ran unconditionally in production, and
`_summarize_raw_object` reached its `str(obj)` fallback for pyrogram `TLObject`
instances (they use `__slots__`, so they have no `__dict__`), and `TLObject`'s
`__repr__` pretty-prints — so one RPC wrote hundreds of log lines, including
peer credentials.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.infrastructure.telegram import raw_tracer as rt


class FakeTLObject:
    """Stands in for a pyrogram raw object: slots, no ``__dict__``, chatty repr."""

    __slots__ = ("channel_id", "access_hash", "file_reference", "pts", "users")

    def __init__(self) -> None:
        self.channel_id = -1001234567890
        self.access_hash = 8130875772
        self.file_reference = b"\x00\xffpeer"
        self.pts = 36947
        self.users = []

    def __repr__(self) -> str:  # pragma: no cover - the point is that it is never used
        return (
            '{\n    "_": "types.Dialog",\n'
            '    "channel_id": -1001234567890,\n'
            '    "access_hash": 8130875772,\n'
            '    "file_reference": "\'\\x00\\xffpeer\'"\n}'
        )


def _client() -> MagicMock:
    client = MagicMock()
    client.invoke = AsyncMock(return_value={"pts": 1})
    client.handle_updates = AsyncMock()
    return client


# --------------------------------------------------------------------------- #
# Off by default
# --------------------------------------------------------------------------- #


def test_tracer_does_not_attach_without_the_flag(monkeypatch) -> None:
    monkeypatch.delenv(rt.RAW_TRACE_ENV, raising=False)
    client = _client()
    before_invoke, before_updates = client.invoke, client.handle_updates

    rt.RawTelegramApiTracer(client, session_id="sess-1").attach()

    assert client.invoke is before_invoke, "invoke must not be wrapped by default"
    assert client.handle_updates is before_updates, "updates must not be wrapped"


@pytest.mark.parametrize("value", ["", "  ", "0", "false", "False", "no", "off", "random"])
def test_falsey_flag_values_keep_it_off(monkeypatch, value: str) -> None:
    monkeypatch.setenv(rt.RAW_TRACE_ENV, value)
    assert rt.tracing_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_truthy_flag_values_enable_it(monkeypatch, value: str) -> None:
    monkeypatch.setenv(rt.RAW_TRACE_ENV, value)
    assert rt.tracing_enabled() is True


@pytest.mark.asyncio
async def test_no_raw_logs_are_emitted_when_disabled(monkeypatch, caplog) -> None:
    monkeypatch.delenv(rt.RAW_TRACE_ENV, raising=False)
    client = _client()
    rt.RawTelegramApiTracer(client, session_id="sess-2").attach()

    with caplog.at_level(logging.DEBUG, logger="atf.raw_api"):
        await client.invoke(FakeTLObject())
        await client.handle_updates(MagicMock(updates=[]))

    assert not [r for r in caplog.records if "[RAW MTPROTO" in r.message]
    assert not [r for r in caplog.records if "[RAW UPDATE" in r.message]


@pytest.mark.asyncio
async def test_attaches_and_traces_when_enabled(monkeypatch, caplog) -> None:
    monkeypatch.setenv(rt.RAW_TRACE_ENV, "1")
    client = _client()
    rt.RawTelegramApiTracer(client, session_id="sess-3").attach()

    with caplog.at_level(logging.INFO, logger="atf.raw_api"):
        await client.invoke(FakeTLObject())

    traced = [r.message for r in caplog.records if "[RAW MTPROTO >>" in r.message]
    assert traced, "enabling the flag must attach the tracer"
    assert all("\n" not in r.message for r in caplog.records), "logs must stay one line"
    assert all(len(r.message) < 400 for r in caplog.records), "logs must stay bounded"


# --------------------------------------------------------------------------- #
# Bounded, single-line, credential-free summaries
# --------------------------------------------------------------------------- #


def test_tl_object_summary_is_one_bounded_line() -> None:
    out = rt._summarize_raw_object(FakeTLObject())

    assert "\n" not in out, f"summary must be a single line: {out!r}"
    assert len(out) <= rt.MAX_SUMMARY_CHARS, f"summary not bounded: {len(out)}"


def test_tl_object_summary_masks_peer_credentials() -> None:
    out = rt._summarize_raw_object(FakeTLObject())

    assert "8130875772" not in out, "access_hash leaked"
    assert "access_hash" not in out, out
    assert "file_reference" not in out, out
    # The harmless fields are still reported, so the trace stays useful.
    assert "channel_id=-1001234567890" in out
    assert out.startswith("FakeTLObject(")


def test_summary_fallback_collapses_a_chatty_repr() -> None:
    class Chatty:
        __slots__ = ()

        def __repr__(self) -> str:
            return '{\n    "a": 1,\n    "b": "two"\n}'

    out = rt._summarize_raw_object(Chatty())

    assert "\n" not in out
    assert len(out) <= rt.MAX_SUMMARY_CHARS
    assert out.startswith("{ "), out


def test_nested_credentials_are_masked_in_dicts_and_lists() -> None:
    out = rt._summarize_raw_object(
        {
            "peer": {"access_hash": 42, "channel_id": -100},
            "photo": {"file_reference": b"ref", "id": 7},
            "list": ["token=abc", 5],
        }
    )

    assert "\n" not in out
    assert "access_hash=[REDACTED]" in out, out
    assert "file_reference=[REDACTED]" in out, out
    assert "channel_id=-100" in out, out
    assert "token=[REDACTED]" in out, out


@pytest.mark.parametrize(
    "field",
    ["access_hash", "file_reference", "auth_key_id", "api_hash", "session", "token"],
)
def test_sensitive_field_names_are_detected(field: str) -> None:
    assert rt._is_sensitive_attr(field) is True


@pytest.mark.parametrize("field", ["channel_id", "pts", "user_id", "msg_id", "date"])
def test_ordinary_field_names_are_not_masked(field: str) -> None:
    assert rt._is_sensitive_attr(field) is False
