"""Regressions for the runtime failure modes that filled the log store.

Each test pins one root cause found by deduplicating the ERROR/WARN rows of the
production log database:

* a ``datetime`` message date aborting every auto-reply hand-off,
* a 401 AUTH_KEY_UNREGISTERED session being retried instead of deactivated,
* an invalid channel source being re-polled (and re-logged) every sweep,
* a session with no live client deferring every job every 15s,
* one undecryptable secret failing an entire list endpoint.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from core.domain.entities import DeliveryJob, ForwardRule, MessagePayload
from core.domain.value_objects import MediaType
from core.infrastructure.persistence.sqlite_repositories import _safe_decrypt
from core.infrastructure.security.crypto import CryptoService, DecryptionError
from core.infrastructure.telegram.autoreply_bridge import inbound_from_pyrogram
from core.infrastructure.telegram.channel_polling import ChannelPollingService, PollFailure
from core.infrastructure.telegram.durable_queue import DurableQueueManager


# --------------------------------------------------------------- auto-reply date
class _Chat:
    id = 4242
    type = "private"


class _Sender:
    id = 99
    first_name = "Ada"
    last_name = ""
    username = "ada"
    is_bot = False
    is_self = False


class _Msg:
    id = 7
    chat = _Chat()
    from_user = _Sender()
    text = "hello"
    caption = None
    media = None
    forward_date = None
    forward_origin = None


def test_inbound_accepts_datetime_date_without_raising():
    """pyrogram hands ``Message.date`` over as a datetime, not an epoch int."""
    msg = _Msg()
    msg.date = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
    inbound = inbound_from_pyrogram(msg, session_id="s1", owner_user_id=5)
    assert inbound is not None
    assert inbound.date == 1792584000


def test_inbound_still_accepts_an_epoch_int():
    msg = _Msg()
    msg.date = 1792584000
    assert inbound_from_pyrogram(msg, session_id="s1", owner_user_id=5).date == 1792584000


# ------------------------------------------------------- channel poll backoff
class _NoPeerClient:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_peer(self, chat_id):
        self.calls += 1
        raise ValueError("[400 PEER_ID_INVALID] - The peer id is invalid")


class _Rules:
    def __init__(self, rule) -> None:
        self._rule = rule

    async def list_all(self):
        return [self._rule]


class _Pool:
    def __init__(self, client) -> None:
        self._client = client

    def list_sessions(self):
        return ["sess-1"]

    def get(self, session_id):
        return self._client


def _active_rule():
    return ForwardRule(
        id="rule-1", session_id="sess-1", source_chat_id="-100999",
        source_chat_name="Bad Source", target_chat_id="-100111", is_active=True,
    )


@pytest.mark.asyncio
async def test_invalid_peer_is_logged_once_and_then_backed_off(caplog):
    client = _NoPeerClient()
    service = ChannelPollingService(
        pool=_Pool(client), rules=_Rules(_active_rule()), dispatcher=None,
    )
    caplog.set_level("DEBUG")

    await service.sweep_all()
    await service.sweep_all()  # same channel, still inside the backoff window

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    # Only the first sweep touched Telegram: the second respected the backoff.
    assert client.calls == 2  # both resolve_peer attempts of the first sweep
    key = ("sess-1", "-100999")
    assert service._failures[key] == 1
    assert service._retry_at[key] > 0


@pytest.mark.asyncio
async def test_a_successful_poll_clears_the_backoff():
    service = ChannelPollingService(
        pool=_Pool(_NoPeerClient()), rules=_Rules(_active_rule()), dispatcher=None,
    )
    key = ("sess-1", "-100999")
    service._failures[key] = 3
    service._retry_at[key] = 0

    service._note_success(key)

    assert key not in service._failures
    assert key not in service._retry_at


def test_poll_failure_is_a_named_error():
    assert issubclass(PollFailure, Exception)


# ------------------------------------------------------- queue no-client storm
class _Queue:
    def __init__(self) -> None:
        self.deferred = []

    async def defer(self, job_id, reason, backoff):
        self.deferred.append((job_id, reason, backoff))
        return True

    async def mark_failed(self, job_id, error):
        self.deferred.append((job_id, "failed:" + error, 0))

    async def mark_retry(self, job_id, reason, backoff_seconds=0):
        return True


class _RuleRepo:
    def __init__(self, rule) -> None:
        self._rule = rule

    async def get_by_id(self, rule_id):
        return self._rule


class _EmptyPool:
    def get(self, session_id):
        return None


def _job() -> DeliveryJob:
    return DeliveryJob(
        id="job-1",
        rule_id="rule-1",
        source_chat_id="-100999",
        source_message_id=5,
        target_chat_id="-100111",
        payload_data={"session_id": "sess-dead"},
    )


@pytest.mark.asyncio
async def test_no_client_defers_with_growing_backoff_and_logs_once(caplog):
    queue = _Queue()
    manager = DurableQueueManager(
        queue_repo=queue,
        message_map_repo=object(),
        client_pool=_EmptyPool(),
        rule_repo=_RuleRepo(_active_rule()),
        num_workers=1,
    )
    caplog.set_level("DEBUG")

    await manager._process_job("worker-1", _job())
    await manager._process_job("worker-1", _job())
    await manager._process_job("worker-1", _job())

    assert [d[2] for d in queue.deferred] == [15.0, 30.0, 60.0]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert manager._no_client_backoff["sess-dead"][0] == 3


# ------------------------------------------------------- revoked 401 session
class _Revoked(Exception):
    """Stand-in for pyrogram's AuthKeyUnregistered (not importable on hosts)."""


class _Session:
    id = "sess-dead"
    is_active = True
    is_authorized = True
    session_string_encrypted = "enc"
    api_credential_id = "default"

    def __init__(self) -> None:
        self.deactivated = False

    def deactivate(self) -> None:
        self.deactivated = True


class _Sessions:
    def __init__(self, session) -> None:
        self._session = session
        self._repo = self

    async def get(self, session_id):
        return self._session

    async def list_active(self):
        return [self._session]

    async def update(self, session):
        return session


class _RevokedPool:
    async def start_with_session_string(self, *args, **kwargs):
        raise _Revoked("[401 AUTH_KEY_UNREGISTERED] - The key is not registered")

    def get_state(self, session_id):
        return None

    def all_states(self):
        return {}


@pytest.mark.asyncio
async def test_unregistered_auth_key_deactivates_instead_of_retrying(monkeypatch):
    from core.infrastructure.telegram import session_boot

    monkeypatch.setattr(session_boot, "_fatal_auth_errors", lambda: (_Revoked,))
    session = _Session()
    manager = session_boot.SessionBootManager(
        sessions=_Sessions(session), rules=object(), pool=_RevokedPool(), dispatcher=object(),
    )

    status = await manager.restore_session("sess-dead")

    assert status.startswith("auth_revoked:")
    assert session.deactivated is True
    # reconcile_once skips a session it already saw as revoked, so the boot
    # manager must record that state or it retries the dead key forever.
    await manager.reconcile_once()
    assert manager.statuses["sess-dead"].startswith("auth_revoked:")


def test_decrypt_reports_a_typed_error_for_garbage():
    crypto = CryptoService("master")
    with pytest.raises(DecryptionError):
        crypto.decrypt("not base64 !!!")


def test_decrypt_accepts_missing_base64_padding():
    crypto = CryptoService("master")
    token = crypto.encrypt("payload").rstrip("=")
    assert crypto.decrypt(token) == "payload"


def test_one_bad_secret_does_not_fail_a_list(caplog):
    crypto = CryptoService("master")
    other = CryptoService("a-different-master-key")
    caplog.set_level("WARNING")

    assert _safe_decrypt(crypto, other.encrypt("secret"), kind="bot token", row_id="row-1") == ""
    assert any("cannot be decrypted" in r.getMessage() for r in caplog.records)
    assert _safe_decrypt(crypto, crypto.encrypt("secret"), kind="bot token", row_id="row-2") == "secret"
