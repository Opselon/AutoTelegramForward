"""End-to-end behaviour tests for the protected-content bypass chain.

The scenario the user asked for: a source channel that has restricted
forwarding/saving must still get its messages delivered to the target.
The chain is:  forward_messages -> copy_message -> media-reference send ->
download+reupload -> bare text.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyrogram.errors import ChatForwardsRestricted, ChatSendMediaForbidden

import pytest

from core.domain.entities import ForwardRule, MediaType, MessagePayload
from core.domain.value_objects import ForwardMode
from core.infrastructure.telegram.dispatcher import (
    MessageDispatcher,
    _extract_media_reference,
    _send_media_reference,
)


def _payload(media=True, text="VIP SIGNAL EURUSD", caption=""):
    return MessagePayload(
        message_id=42,
        chat_id="-1001111111111",
        text=text,
        caption=caption,
        media_type=MediaType.PHOTO if media else MediaType.TEXT,
        has_media=media,
    )


def _rule(fallback=True, mode="COPY_MESSAGE"):
    return ForwardRule(
        id="rule-1",
        source_chat_id="-1001111111111",
        target_chat_id="-1002222222222",
        forward_mode=ForwardMode.DIRECT_FORWARD if mode == "DIRECT_FORWARD" else ForwardMode.COPY_MESSAGE,
        fallback_enabled=fallback,
    )


def _media_msg(file_id="AgACAgQAAx"):
    """pyrogram-like message whose photo carries a file-id reference."""
    photo = SimpleNamespace(file_id=file_id, file_name=None)
    return SimpleNamespace(photo=photo, caption=None, media=photo)


def _make_dispatcher(client):
    pool = MagicMock()
    pool.get = MagicMock(return_value=client)
    return MessageDispatcher(pool=pool, use_case=MagicMock())


# ------------------------------------------------------------------ layer 3


def test_extract_media_reference_reads_file_id():
    msg = _media_msg("AgACAgQAAxkBAAEB")
    ref = _extract_media_reference(msg)
    assert ref == {"kind": "photo", "ref": "AgACAgQAAxkBAAEB"}


def test_extract_media_reference_none_for_text_only():
    assert _extract_media_reference(SimpleNamespace(photo=None, caption=None)) is None


@pytest.mark.asyncio
async def test_send_media_reference_sends_photo():
    client = MagicMock()
    client.send_photo = AsyncMock(return_value="sent-photo")
    ref = {"kind": "photo", "ref": "AgACAgQAAx", "caption": "orig"}
    out = await _send_media_reference(client, "-1002222222222", ref, "override caption")
    assert out == "sent-photo"
    client.send_photo.assert_awaited_once()
    _, kw = client.send_photo.call_args
    assert kw["photo"] == "AgACAgQAAx"
    assert kw["caption"] == "override caption"


# ------------------------------------------------- full chain: forward blocked


@pytest.mark.asyncio
async def test_restricted_channel_falls_back_to_media_reference():
    """forward_messages blocked -> copy_message blocked -> file-id reference send."""
    client = MagicMock()
    client.forward_messages = AsyncMock(side_effect=ChatForwardsRestricted())
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    client.get_messages = AsyncMock(return_value=_media_msg("fileid-xyz"))
    client.send_photo = AsyncMock(return_value=SimpleNamespace(id=777))

    dispatcher = _make_dispatcher(client)
    ok = await dispatcher._deliver(
        client, _payload(), _rule(mode="DIRECT_FORWARD"), "VIP SIGNAL", "-1002222222222"
    )
    assert ok is not None
    # server-side attempts both happened, then the reference path delivered it
    assert client.forward_messages.await_count == 1
    assert client.copy_message.await_count == 1
    client.send_photo.assert_awaited_once()
    _, kw = client.send_photo.call_args
    assert kw["photo"] == "fileid-xyz"


@pytest.mark.asyncio
async def test_restricted_channel_falls_back_to_download_reupload():
    """Reference send blocked too -> download the file and re-upload it."""
    tmp = "/tmp/atf-protected-media.bin"

    client = MagicMock()
    client.forward_messages = AsyncMock(side_effect=ChatForwardsRestricted())
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    # media lookup returns a message with no usable file-id, so layer 3 is skipped
    client.get_messages = AsyncMock(return_value=SimpleNamespace(photo=None, caption=None))
    client.download_media = AsyncMock(return_value=tmp)
    client.send_document = AsyncMock(return_value=SimpleNamespace(id=888))

    open(tmp, "wb").write(b"binary")

    try:
        dispatcher = _make_dispatcher(client)
        # has_media False + TEXT payload would take the text path; force the
        # media reupload path directly
        from core.domain.entities import MediaType, MessagePayload

        payload = MessagePayload(
            message_id=42, chat_id="-1001111111111", caption="cap text",
            media_type=MediaType.DOCUMENT, has_media=True,
        )
        res = await dispatcher._reupload_media(client, payload, "-1002222222222", "cap text", "AUTO")
        assert res is not None
        client.send_document.assert_awaited_once()
        assert client.download_media.await_count == 1
    finally:
        import os

        if os.path.exists(tmp):
            os.remove(tmp)


@pytest.mark.asyncio
async def test_restricted_text_message_still_delivered():
    """A protected *text* message (no media) must not be silently dropped."""
    client = MagicMock()
    client.forward_messages = AsyncMock(side_effect=ChatForwardsRestricted())
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    client.get_messages = AsyncMock(return_value=SimpleNamespace(photo=None, text="secret text"))
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=999))

    dispatcher = _make_dispatcher(client)
    res = await dispatcher._deliver(
        client, _payload(media=False, text="secret text"), _rule(mode="DIRECT_FORWARD"),
        "secret text", "-1002222222222",
    )
    assert res is not None
    client.send_message.assert_awaited()
    _, kw = client.send_message.call_args
    assert "secret text" in kw["text"]


@pytest.mark.asyncio
async def test_fallback_disabled_does_not_bypass():
    """When the rule forbids fallback, a restricted channel must raise, not sneak through."""
    client = MagicMock()
    client.forward_messages = AsyncMock(side_effect=ChatForwardsRestricted())
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    client.get_messages = AsyncMock(return_value=_media_msg("nope"))
    client.send_photo = AsyncMock(return_value="nope")
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=1))

    dispatcher = _make_dispatcher(client)
    with pytest.raises(ChatForwardsRestricted):
        await dispatcher._deliver(
            client, _payload(), _rule(fallback=False, mode="DIRECT_FORWARD"),
            "VIP SIGNAL", "-1002222222222",
        )
    client.send_photo.assert_not_awaited()
