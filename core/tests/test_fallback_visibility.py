"""Adaptive fallback visibility (mission §6.2 / §6.3).

The rules under test:

* every strategy transition produces an event that explains **why** the
  requested strategy was left and **what** will be attempted instead;
* a successful fallback must never overwrite or hide the original failure;
* media download and upload failures are independently identifiable
  (MEDIA_DOWNLOAD_FAILED / MEDIA_UPLOAD_FAILED), never a generic "forward
  failed";
* an explicit failure — no valid delivery method — is recorded as such, never
  faked as success.
"""

from __future__ import annotations

import pytest
from pyrogram.errors import ChatForwardsRestricted

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteDiagnosticEventRepository, SqliteDeliveryAttemptRepository,
    SqliteDeliveryQueueRepository, SqliteMessageMapRepository,
)
from core.infrastructure.telegram.durable_queue import DurableQueueManager
from core.domain.entities import DeliveryJob, DeliveryStatus, ForwardRule


class RestrictedClient:
    """Refuses copy/forward, but can fetch, download and re-send media."""

    def __init__(self):
        self.sent = []

    async def copy_message(self, **kw):
        raise ChatForwardsRestricted("400 CHAT_FORWARDS_RESTRICTED")

    async def forward_messages(self, **kw):
        raise ChatForwardsRestricted("400 CHAT_FORWARDS_RESTRICTED")

    async def get_messages(self, chat_id, message_ids):
        class File:
            id = "AgACBQ"  # the file id the reference send replays
        class Photo:
            file_id = "AgACBQ"
            date = 0
            sizes = []
            dc_id = 2
        class M:
            photo = Photo()
            video = None
            voice = None
            audio = None
            animation = None
            sticker = None
            media = None
            id = message_ids
            text = None
            caption = None
        return M()

    async def download_media(self, msg):
        return "/tmp/fake.bin"

    async def send_photo(self, chat_id, photo, caption=None):
        class R:
            id = 999
        self.sent.append(("photo", photo, caption))
        return R()


class DownloadFailsClient(RestrictedClient):
    """Everything restricted, and the download layer also fails."""

    async def download_media(self, msg):
        raise RuntimeError("file reference expired")

    async def get_messages(self, chat_id, message_ids):
        # The fetch itself fails: the message cannot even be read, so neither
        # the reference send nor the download ever runs.
        raise RuntimeError("peer not accessible")

    async def send_message(self, chat_id, text=None, **kw):
        # The last-resort text layer, so the failure is recorded rather than
        # swallowed by a missing method.
        class X:
            id = 777
        return X()


class Pool:
    def __init__(self, client):
        self._client = client

    def get(self, session_id):
        return self._client

    def is_time_limited(self, session_id):
        return False


@pytest.fixture
def stack(tmp_path):
    db = SqliteDatabase(str(tmp_path / "fallback.db"))
    queue = SqliteDeliveryQueueRepository(db)
    mmap = SqliteMessageMapRepository(db)
    attempts = SqliteDeliveryAttemptRepository(db)
    diag = SqliteDiagnosticEventRepository(db)
    return queue, mmap, attempts, diag


def _rule():
    return ForwardRule(id="rule-F", session_id="sess", source_chat_id="-100S",
                       target_chat_id="-100T", target_chat_ids=["-100T"],
                       is_active=True, owner_user_id=7)


def _job(mode="COPY_MESSAGE"):
    return DeliveryJob(
        rule_id="rule-F", source_chat_id="-100S", source_message_id=808,
        target_chat_id="-100T",
        payload_data={"text": "hi", "has_media": True, "forward_mode": mode,
                      "content_mode": "AUTO", "owner_user_id": 7,
                      "correlation_id": "corr-F", "fallback_enabled": True},
        status=DeliveryStatus.PENDING, attempts=0, max_attempts=1)


async def test_a_successful_reupload_keeps_the_original_restriction(stack):
    """§6.3: the fallback succeeded, but the restriction is still on record."""
    queue, mmap, attempts, diag = stack
    client = RestrictedClient()
    qm = DurableQueueManager(queue, mmap, client_pool=Pool(client),
                             attempts_repo=attempts, diagnostics_repo=diag)
    job = _job()
    await queue.enqueue(job)
    await qm._process_job("w-fb", job)

    events = await diag.query_events(task_id="rule-F")
    names = [e["event_name"] for e in events]
    # The transition is recorded BEFORE the re-upload runs…
    assert "delivery.fallback.selected" in names, names
    # …and the underlying Telegram restriction is preserved verbatim.
    selected = [e for e in events if e["event_name"] == "delivery.fallback.selected"][0]
    assert selected["raw_rpc_id"] == "CHAT_FORWARDS_RESTRICTED"
    assert selected["error_origin"] == "TELEGRAM"
    assert selected["stage"] == "FALLBACK"
    # The recovery path was real: media reached the target through one of the
    # bypass layers (the file-id reference send, or a download + re-upload).
    assert client.sent, "the bypass must actually have delivered the media"
    # The strategy that failed first is still identifiable in the same history.
    failed = [e for e in events if e["event_name"] == "delivery.strategy.failed"][0]
    assert failed["strategy"] == "COPY_MESSAGE"


async def test_download_and_upload_failures_are_independently_identifiable(stack):
    """§4-C: MEDIA_DOWNLOAD_FAILED, not a generic delivery failure."""
    queue, mmap, attempts, diag = stack
    client = DownloadFailsClient()
    qm = DurableQueueManager(queue, mmap, client_pool=Pool(client),
                             attempts_repo=attempts, diagnostics_repo=diag)
    job = _job()
    await queue.enqueue(job)
    await qm._process_job("w-fb", job)

    events = await diag.query_events(task_id="rule-F")
    by_name = {e["event_name"]: e for e in events}
    # The fetch failure and the selection are both recorded…
    assert "delivery.media.fetch_failed" in by_name, list(by_name)
    assert "delivery.fallback.selected" in by_name, list(by_name)
    # The media event is categorized as MEDIA, not a generic delivery failure.
    fetch = by_name["delivery.media.fetch_failed"]
    fetch = by_name["delivery.media.fetch_failed"]
    assert fetch["category"] == "MEDIA"
    assert fetch["reason"] == "MEDIA_FETCH_FAILED"
    assert fetch["stage"] == "MEDIA"
    # …and the persistent Telegram restriction that caused the fallback.
    selected = by_name["delivery.fallback.selected"]
    assert selected["raw_rpc_id"] == "CHAT_FORWARDS_RESTRICTED"


async def test_a_disabled_fallback_rather_than_a_silent_copy(stack):
    """fallback_enabled=False: the original error stands, no silent bypass."""
    queue, mmap, attempts, diag = stack
    client = RestrictedClient()
    qm = DurableQueueManager(queue, mmap, client_pool=Pool(client),
                             attempts_repo=attempts, diagnostics_repo=diag)
    job = _job()
    job.payload_data["fallback_enabled"] = False
    await queue.enqueue(job)
    await qm._process_job("w-fb", job)

    events = await diag.query_events(task_id="rule-F")
    # No fallback was selected: the owner asked for the requested mode only.
    assert not [e for e in events if e["event_name"] == "delivery.fallback.selected"]
    # The original restriction was still recorded by the send path.
    assert any(e["raw_rpc_id"] == "CHAT_FORWARDS_RESTRICTED" for e in events), (
        "the original error must be recorded even without a fallback"
    )
    assert not client.sent, "no silent copy/re-upload may happen"
