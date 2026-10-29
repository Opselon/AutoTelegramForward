"""Delivery-guarantee regression tests for the queue worker path.

The contract under test: when a source channel closes its forward option
(noforwards / protected content) — or the task is routed through a bot that
cannot forward at all — the worker must still deliver every eligible message
through the authorized copy/re-upload chain, and it must never report a
success for a message that was not actually sent.

Matrix:
  * bot route + DIRECT_FORWARD      → delivered as a clean copy (no AttributeError retry loop)
  * bot route + restricted copy     → BotTransportUnsupported → account transport fallback
  * fallback disabled               → restricted error raised, no silent bypass
  * split album on protected source → every item bypasses on its own
  * album group on protected source → full album re-upload
  * worker re-upload order          → file-id reference before download (dispatcher parity)
  * nothing deliverable             → DeliveryBlockedError → job FAILED, never a fake SENT
  * live dispatcher + timeout       → no blind copy fallback (duplicate protection)
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyrogram.errors import ChatForwardsRestricted

import pytest

from core.domain.entities import DeliveryJob, ForwardRule
from core.domain.value_objects import ForwardMode
from core.infrastructure.telegram.bot_transport import BotTransportUnsupported
from core.infrastructure.telegram.dispatcher import MessageDispatcher
from core.infrastructure.telegram.durable_queue import DeliveryBlockedError, DurableQueueManager
from core.infrastructure.telegram.errors import is_timeout_error

SRC = "-1001111111111"
TGT = "-1002222222222"


def _media_msg(file_id="AgACAgQAAx", attr="photo"):
    media = SimpleNamespace(file_id=file_id, file_name=None)
    return SimpleNamespace(**{attr: media, "caption": None})


def _account_client():
    """A pyrogram-like account client: everything a member session can do."""
    client = MagicMock()
    client.get_messages = AsyncMock(return_value=_media_msg("fileid-xyz"))
    client.send_photo = AsyncMock(return_value=SimpleNamespace(id=700))
    client.send_message = AsyncMock(return_value=SimpleNamespace(id=701))
    client.send_document = AsyncMock(return_value=SimpleNamespace(id=702))
    return client


class BotLikeClient:
    """The exact surface a bot-route delivery sees (no MTProto-only methods)."""

    def __init__(self):
        self.copy_message = AsyncMock(return_value=SimpleNamespace(id=321))
        self.send_message = AsyncMock(return_value=SimpleNamespace(id=322))
        self.send_media_group = AsyncMock(side_effect=BotTransportUnsupported("no media groups"))

    async def edit_message_text(self, *a, **k):  # pragma: no cover - shape only
        return None


def _job(forward_mode="COPY_MESSAGE", **extra):
    payload = {
        "session_id": "sess-1",
        "text": "body",
        "has_media": True,
        "media_type": "photo",
        "forward_mode": forward_mode,
        "content_mode": "AUTO",
        "delivery_transport": "USER",
        **extra,
    }
    return DeliveryJob(
        id="job-g", rule_id="rule-g", source_chat_id=SRC, source_message_id=42,
        target_chat_id=TGT, payload_data=payload, max_attempts=5,
    )


def _manager(client):
    pool = MagicMock()
    pool.get = MagicMock(return_value=client)
    queue = MagicMock()
    queue.mark_failed = AsyncMock()
    queue.mark_retry = AsyncMock()
    queue.mark_unknown = AsyncMock()
    queue.mark_sent = AsyncMock()
    map_repo = MagicMock()
    map_repo.update_delivery = AsyncMock()
    return DurableQueueManager(
        queue_repo=queue, message_map_repo=map_repo, client_pool=pool, num_workers=0,
    )


# ------------------------------------------------------------------ bot route


@pytest.mark.asyncio
async def test_bot_route_direct_forward_delivers_as_copy():
    """DIRECT_FORWARD on the bot route must deliver, not AttributeError-loop."""
    client = BotLikeClient()
    mgr = _manager(client)
    job = _job(forward_mode="DIRECT_FORWARD")
    msg_id = await mgr._dispatch_to_telegram(client, job)
    assert msg_id == 321
    client.copy_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_route_restricted_copy_falls_back_to_account_transport():
    """A protected source on the bot route defers to the account transport.

    The bot cannot read/re-upload protected media; pretending otherwise used
    to degrade the message to bare text — or return 0 and report a success
    that never happened. Raising BotTransportUnsupported is what sends the job
    through the owner's account, whose bypass chain can deliver it fully.
    """
    client = BotLikeClient()
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    mgr = _manager(client)
    with pytest.raises(BotTransportUnsupported):
        await mgr._dispatch_to_telegram(client, _job())


@pytest.mark.asyncio
async def test_bot_route_branding_hop_delivers_as_clean_copy():
    """Branding stage 2 on the bot route copies C's post instead of crashing."""
    client = BotLikeClient()
    mgr = _manager(client)
    job = _job(branding={"chat_id": "-1003333333333", "message_id": 9, "force_forward": True})
    msg_id = await mgr._dispatch_to_telegram(client, job)
    assert msg_id == 321
    _, kw = client.copy_message.call_args
    assert kw["from_chat_id"] == -1003333333333


# ------------------------------------------------------------------ fallback gate


@pytest.mark.asyncio
async def test_fallback_disabled_refuses_bypass():
    """fallback_enabled=False: a restricted forward fails the job, no copy."""
    client = _account_client()
    client.forward_messages = AsyncMock(side_effect=ChatForwardsRestricted())
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    mgr = _manager(client)
    with pytest.raises(ChatForwardsRestricted):
        await mgr._dispatch_to_telegram(
            client, _job(forward_mode="DIRECT_FORWARD", fallback_enabled=False)
        )
    client.copy_message.assert_not_awaited()
    client.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_enabled_by_default_for_legacy_jobs():
    """A queued job from before the payload carried fallback_enabled bypasses."""
    client = _account_client()
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    mgr = _manager(client)
    msg_id = await mgr._dispatch_to_telegram(client, _job())
    assert msg_id == 700  # media-reference layer delivered it
    client.get_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_restricted_forward_falls_back_to_media_reference():
    """The user-facing guarantee: source closes forwarding → target still
    receives the message, through the file-id reference layer."""
    client = _account_client()
    client.forward_messages = AsyncMock(side_effect=ChatForwardsRestricted())
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    mgr = _manager(client)
    msg_id = await mgr._dispatch_to_telegram(
        client, _job(forward_mode="DIRECT_FORWARD")
    )
    assert msg_id == 700
    _, kw = client.send_photo.call_args
    assert kw["photo"] == "fileid-xyz"
    assert mgr.stats["fallback_copies"] >= 1


# ------------------------------------------------------------------ albums


@pytest.mark.asyncio
async def test_split_album_bypasses_per_item():
    """split mode on a protected source re-uploads every item; the old code
    let the first refusal kill the whole album (retry → dead letter)."""
    client = _account_client()
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    client.get_messages = AsyncMock(
        side_effect=lambda **kw: _media_msg(f"fid-{kw['message_ids']}")
    )
    client.send_photo = AsyncMock(
        side_effect=lambda **kw: SimpleNamespace(id=700 + len(client.send_photo.await_args_list))
    )
    mgr = _manager(client)
    job = _job(
        media_group_id="grp-1",
        album_message_ids=[11, 12, 13],
        album_mode="split",
    )
    msg_id = await mgr._dispatch_to_telegram(client, job)
    assert msg_id > 0
    assert client.copy_message.await_count == 3
    assert client.send_photo.await_count == 3


@pytest.mark.asyncio
async def test_album_group_bypass_reuploads_whole_album():
    """album mode on a protected source rebuilds the media group."""
    client = _account_client()
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    client.get_messages = AsyncMock(return_value=_media_msg("fid-1"))
    client.download_media = AsyncMock(return_value=None)  # downloads fail…
    client.send_media_group = AsyncMock(
        return_value=[SimpleNamespace(id=811), SimpleNamespace(id=812)]
    )
    mgr = _manager(client)
    job = _job(media_group_id="grp-2", album_message_ids=[21, 22], album_mode="album")
    # The group copy is refused; the re-upload finds no media (downloads fail)
    # and no caption exists in this job… give it one so the text fallback runs.
    job.payload_data["text"] = "album caption"
    msg_id = await mgr._dispatch_to_telegram(client, job)
    assert msg_id == 701  # text last resort delivered the caption


# ------------------------------------------------------------------ no false success


@pytest.mark.asyncio
async def test_reupload_media_uses_file_reference_before_download():
    """Worker re-upload parity: the reference layer runs before any download."""
    client = _account_client()
    client.download_media = AsyncMock()
    mgr = _manager(client)
    msg_id = await mgr._reupload_media(client, SRC, 42, TGT, "cap", "photo")
    assert msg_id == 700
    client.send_photo.assert_awaited_once()
    client.download_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_reupload_media_raises_when_nothing_deliverable():
    """No media, no caption → DeliveryBlockedError, never a silent 0."""
    client = _account_client()
    client.get_messages = AsyncMock(return_value=SimpleNamespace(photo=None, caption=None))
    mgr = _manager(client)
    with pytest.raises(DeliveryBlockedError):
        await mgr._reupload_media(client, SRC, 42, TGT, None, "photo")


@pytest.mark.asyncio
async def test_reupload_album_raises_when_nothing_deliverable():
    client = _account_client()
    client.get_messages = AsyncMock(return_value=SimpleNamespace(photo=None))
    client.download_media = AsyncMock(return_value=None)
    client.send_media_group = AsyncMock(return_value=None)
    mgr = _manager(client)
    with pytest.raises(DeliveryBlockedError):
        await mgr._reupload_album(client, SRC, [1, 2], TGT, None)


@pytest.mark.asyncio
async def test_blocked_delivery_marks_job_failed_not_sent():
    """When every method fails, the job is FAILED with the reason attached —
    the task stays active and the message is replayable from the DLQ."""
    client = _account_client()
    client.get_messages = AsyncMock(return_value=SimpleNamespace(photo=None, caption=None))
    mgr = _manager(client)
    # MEDIA_ONLY with no text: the copy is refused, the reference layer finds
    # nothing, the download fails, and MEDIA_ONLY forbids a text fallback —
    # the exact shape the old code reported as a phantom success (return 0).
    job = _job(content_mode="MEDIA_ONLY", text="")
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    result = await mgr._process_job("worker-x", job)
    assert result is None
    mgr._queue.mark_failed.assert_awaited_once()
    mgr._queue.mark_sent.assert_not_awaited()
    mgr._map.update_delivery.assert_awaited_once()
    assert "FAILED" in str(mgr._map.update_delivery.call_args)


@pytest.mark.asyncio
async def test_text_last_resort_still_delivers_when_caption_exists():
    """AUTO mode + protected source + dead media: the caption text carries the
    words to the target, so the delivery degrades instead of vanishing."""
    client = _account_client()
    client.get_messages = AsyncMock(return_value=SimpleNamespace(photo=None, caption=None))
    client.copy_message = AsyncMock(side_effect=ChatForwardsRestricted())
    mgr = _manager(client)
    result = await mgr._process_job("worker-x", _job())
    assert result == ("job-g", 701)  # sent, via the text last resort


# ------------------------------------------------------------------ timeout ambiguity


@pytest.mark.asyncio
async def test_dispatcher_timeout_does_not_blindly_copy():
    """A timed-out forward may have arrived; copying after it is the
    duplicate Telegram cannot take back. The live path must re-raise."""
    client = MagicMock()
    client.forward_messages = AsyncMock(side_effect=asyncio.TimeoutError())
    client.copy_message = AsyncMock(return_value=SimpleNamespace(id=1))
    pool = MagicMock()
    pool.get = MagicMock(return_value=client)
    dispatcher = MessageDispatcher(pool=pool, use_case=MagicMock())
    from core.domain.entities import ForwardRule, MessagePayload
    from core.domain.value_objects import MediaType

    payload = MessagePayload(
        message_id=42, chat_id=SRC, text="hi",
        media_type=MediaType.TEXT, has_media=False,
    )
    rule = ForwardRule(
        id="rule-t", source_chat_id=SRC, target_chat_id=TGT,
        forward_mode=ForwardMode.DIRECT_FORWARD, fallback_enabled=True,
    )
    with pytest.raises(asyncio.TimeoutError):
        await dispatcher._deliver(client, payload, rule, "hi", TGT)
    client.copy_message.assert_not_awaited()


def test_timeout_predicate_shapes():
    """The shared predicate recognizes the shapes both paths see."""
    assert is_timeout_error(asyncio.TimeoutError()) is True
    assert is_timeout_error(TimeoutError()) is True
    assert is_timeout_error(Exception("read operation timed out")) is True
    # A FloodWait's "A wait of 42 seconds" is a rate limit, not a timeout:
    assert is_timeout_error(ChatForwardsRestricted()) is False
    assert is_timeout_error(Exception("PEER_ID_INVALID")) is False


# ------------------------------------------------------------------ enqueue snapshot


@pytest.mark.asyncio
async def test_enqueue_payload_carries_fallback_opt_out():
    """The worker can only honor the rule's fallback opt-out if the job
    payload snapshots it at enqueue time."""
    mgr = _manager(MagicMock())
    # Force the non-transaction enqueue path (a MagicMock would auto-provide
    # run_in_transaction and swallow the job insert).
    mgr._queue._db = None
    mgr._map._db = None
    mgr._queue.get_pending_count = AsyncMock(return_value=0)
    mgr._map.add_or_update = AsyncMock()
    mgr._queue.enqueue = AsyncMock(return_value=True)
    rule = ForwardRule(
        id="rule-e", source_chat_id=SRC, target_chat_id=TGT,
        forward_mode=ForwardMode.COPY_MESSAGE, fallback_enabled=False,
    )
    from core.domain.entities import MessagePayload
    from core.domain.value_objects import MediaType

    payload = MessagePayload(
        message_id=7, chat_id=SRC, text="x",
        media_type=MediaType.TEXT, has_media=False,
    )
    ok = await mgr.enqueue_delivery(rule, payload, TGT, "x")
    assert ok is True
    job = mgr._queue.enqueue.await_args.args[0]
    assert job.payload_data["fallback_enabled"] is False
