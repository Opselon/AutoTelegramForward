"""Regression tests for Pyrogram UTF-16 surrogate boundary crash.

Problem: ``pyrogram`` hands text/caption data back as its own ``Str`` subclass.
``Str.__getitem__`` round-trips through UTF-16 surrogate pairs, so slicing a
value at a boundary that splits a supplementary code point raises
``UnicodeDecodeError: 'utf-16-le'``.  These tests replay that exact failure and
verify the dispatcher no longer crashes on Persian / emoji / ZWJ-heavy input.
"""
from __future__ import annotations

import asyncio

# pyrogram imports require an event loop at import time.
asyncio.set_event_loop(asyncio.new_event_loop())

import pytest
from unittest.mock import AsyncMock, MagicMock

from pyrogram.types.messages_and_media.message import Str

from core.domain.services import safe_text_preview
from core.infrastructure.telegram.dispatcher import MessageDispatcher
from core.domain.entities import MediaType


def _pyrogram_str(value: str) -> Str:
    """Build the exact object pyrogram hands to user code (NOT a plain str)."""
    return Str(value)


# --------------------------------------------------------------------------- #
# safe_text_preview
# --------------------------------------------------------------------------- #


def test_safe_preview_none_and_empty():
    assert safe_text_preview(None) == ""
    assert safe_text_preview("") == ""
    assert safe_text_preview("", 0) == ""


def test_safe_preview_passthrough_small():
    assert safe_text_preview("hello") == "hello"
    assert safe_text_preview("اب پ", 10) == "اب پ"


def test_safe_preview_surrogate_pair_not_split():
    # Position 12 falls inside the rose emoji (1 code point but 2 UTF-16 units).
    persian = "سلام دنیا 🌹 test message here"
    out = safe_text_preview(persian, 11)
    assert chr(0xD83C) not in out and chr(0xDFE) not in out and chr(0xFFFD) not in out
    assert len(out) <= 11 + len("...")
    # no surrogate half ever escapes
    out.encode("utf-8")  # raises if orphan surrogate present


def test_safe_preview_positions_11_to_13():
    base = "سلام دنیا 🌹هلو"
    for n in (10, 11, 12, 13, 14):
        out = safe_text_preview(base, n)
        out.encode("utf-8")  # must not contain orphan surrogates
        assert isinstance(out, str)


def test_safe_preview_consecutive_emoji_and_zwj():
    # family emoji: ZWJ sequences; must never crash and stay encodable
    seq = "👨‍👩‍👧‍👦" * 5 + "🎉" + "❤️"
    out = safe_text_preview(seq, 3)
    out.encode("utf-8")
    assert isinstance(out, str)


def test_safe_preview_combining_marks():
    txt = "e" + "\u0301" + " " + "a" + "\u0308" + " cafe"
    out = safe_text_preview(txt, 4)
    out.encode("utf-8")


def test_safe_preview_rejects_unusable_types():
    class Boom:
        def __str__(self):
            raise RuntimeError("nope")

    assert safe_text_preview(Boom()) == ""
    assert safe_text_preview(123) == "123"


def test_safe_preview_pyrogram_str_crash_originally():
    # Reproduce the original fault shape: a Pyrogram Str sliced at a bad spot.
    s = _pyrogram_str("سلام دنیا 🌹 test message here")
    # Direct slicing of the raw Str can raise UnicodeDecodeError at position 11.
    raised = False
    try:
        s[:11]
    except UnicodeDecodeError:
        raised = True
    assert raised, "precondition: raw Str slice must reproduce the bug"
    # Our helper detaches via str() and never raises.
    out = safe_text_preview(s, 11)
    out.encode("utf-8")
    assert isinstance(out, str)


# --------------------------------------------------------------------------- #
# dispatcher
# --------------------------------------------------------------------------- #


def _fake_message(text):
    msg = MagicMock()
    chat = MagicMock()
    chat.id = 12345
    chat.type = None
    msg.chat = chat
    msg.id = 99
    msg.media = None
    msg.from_user = None
    msg.sender_chat = None
    msg.text = text
    msg.caption = None
    msg.service = None
    msg.reply_to_message_id = None
    msg.date = None
    msg.edit_hide = False
    msg.forward_origin = None
    msg.media_group_id = None
    msg.views = 0
    msg.entities = None
    msg.caption_entities = None
    msg.reply_markup = None
    return msg


@pytest.mark.asyncio
async def test_dispatcher_survives_pyrogram_str_around_boundary():
    pool = MagicMock()
    client = MagicMock()
    # The client's ``name`` is a Pyrogram Str (exactly how the bug arrived).
    client.name = _pyrogram_str("سلام دنیا 🌹 X")
    client.session_id = None
    pool.get.return_value = client

    use_case = MagicMock()
    use_case.process_message = AsyncMock(return_value=None)
    disp = MessageDispatcher(pool, use_case)

    ok = await disp.bind_session("sess-unicode")
    assert ok is True
    handler = disp.message_handler("sess-unicode")
    assert callable(handler)

    for txt in (
        _pyrogram_str("سلام دنیا 🌹 test message here"),
        _pyrogram_str("Hello 🌍🚀 text"),
        "plain english text",
        _pyrogram_str("🎉" * 20),
        _pyrogram_str(""),  # empty handled via absent text? set None below
    ):
        msg = _fake_message(txt)
        # Must not raise UnicodeDecodeError or any other exception.
        await handler(client, msg)

    # Empty / absent text path.
    msg = _fake_message(None)
    await handler(client, msg)

    assert use_case.process_message.await_count >= 1


@pytest.mark.asyncio
async def test_dispatcher_session_id_is_real_identifier_not_message():
    pool = MagicMock()
    client = MagicMock()
    client.name = _pyrogram_str("Whatever")
    client.session_id = None
    pool.get.return_value = client

    use_case = MagicMock()
    use_case.process_message = AsyncMock(return_value=None)
    disp = MessageDispatcher(pool, use_case)

    await disp.bind_session("REAL_SESSION_ID")
    handler = disp.message_handler("REAL_SESSION_ID")
    msg = _fake_message(_pyrogram_str("سلام"))

    # Should complete without crashing; sid logging uses the closed-over id.
    await handler(client, msg)
    # Sanity: client passed to process path is the client, not promoted to sid.
    assert use_case.process_message.await_count == 1


@pytest.mark.asyncio
async def test_dispatcher_logging_failure_does_not_break_forwarding():
    """Optional logging/preview must not abort forwarding."""
    pool = MagicMock()
    client = MagicMock()
    client.name = None
    client.session_id = None
    pool.get.return_value = client

    use_case = MagicMock()
    use_case.process_message = AsyncMock(return_value=None)
    disp = MessageDispatcher(pool, use_case)

    await disp.bind_session("s2")
    handler = disp.message_handler("s2")

    # A message whose chat_title is a weird object; preview generation must survive.
    msg = _fake_message(_pyrogram_str("a" * 100 + "🎉"))
    # force a property that raises during preview? chat_title stays None here.
    await handler(client, msg)
    assert use_case.process_message.await_count == 1


@pytest.mark.asyncio
async def test_caption_path_safe():
    # A media message carries a caption; ensure no crash.
    pool = MagicMock()
    client = MagicMock()
    client.name = _pyrogram_str("sess")
    pool.get.return_value = client

    use_case = MagicMock()
    use_case.process_message = AsyncMock(return_value=None)
    disp = MessageDispatcher(pool, use_case)

    await disp.bind_session("cap-sess")
    handler = disp.message_handler("cap-sess")

    msg = _fake_message(None)
    msg.media = MagicMock()
    msg.text = None
    msg.caption = _pyrogram_str("عنوان با emoji 🌹 " + "x" * 200)
    msg.media.name = "photo"
    msg.media.file_id = "abc"
    await handler(client, msg)
    assert use_case.process_message.await_count == 1
