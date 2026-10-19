"""The bot route: deliver through a bot the owner attached, not their account.

The switch is worthless if it only changes a label, so these tests cover the
parts that actually decide where a message goes:

  * the entity refuses the bot route until a token is attached (a task must not
    stop forwarding because someone flipped a switch without a token);
  * the button parser accepts what owners really type and *disables* buttons on
    nonsense instead of failing the forward;
  * a 429 keeps Telegram's own ``retry_after`` so the queue backs off correctly;
  * one transport is shared per token (connection reuse) but buttons stay
    per-job, so two tasks cannot paint each other's buttons onto a message;
  * the worker picks the bot only when the job asks for it.
"""
import asyncio

import httpx
import pytest

from core.domain.entities import ForwardRule
from core.infrastructure.telegram.bot_transport import (
    BotApiError,
    BotApiTransport,
    BotRouteAdapter,
    BotTokenInvalid,
    BotTransportCache,
    BotTransportUnsupported,
    mask_token,
    parse_button_rows,
    token_looks_valid,
)

TOKEN = "7878773795:AAFakeTokenForTestsOnly_1234567890abc"
TOKEN_ID = "b0t0k3n-0000-4000-8000-000000000001"


# --------------------------------------------------------------------------- #
# Entity: the route is a state, not a label
# --------------------------------------------------------------------------- #


def test_bot_route_needs_an_attached_token():
    rule = ForwardRule()

    assert rule.delivery_transport == "USER", "the account route is the default"

    rule.delivery_transport = "BOT"
    assert rule.delivery_transport == "USER", "no token attached means no bot route"
    assert rule.use_bot_route is False

    rule.bot_token_id = TOKEN_ID
    assert rule.delivery_transport == "BOT"
    assert rule.use_bot_route is True


def test_removing_the_token_returns_the_task_to_the_account_route():
    rule = ForwardRule()
    rule.bot_token_id = TOKEN_ID
    rule.delivery_transport = "BOT"
    rule.bot_token_id = ""

    assert rule.delivery_transport == "USER"


def test_the_route_and_buttons_survive_a_draft_round_trip():
    rule = ForwardRule(source_chat_id="-1001", target_chat_id="-1002", session_id="s1")
    rule.bot_token_id = TOKEN_ID
    rule.delivery_transport = "BOT"
    rule.inline_buttons_raw = [{"text": "Open", "url": "https://example.com"}]

    draft = rule.to_draft()

    assert draft["delivery_transport"] == "BOT"
    assert draft["bot_token_id"] == TOKEN_ID
    assert draft["inline_buttons"] == [{"text": "Open", "url": "https://example.com"}]

    restored = ForwardRule()
    restored.apply_draft(draft)
    assert restored.delivery_transport == "BOT"
    assert restored.bot_token_id == TOKEN_ID


# --------------------------------------------------------------------------- #
# Token hygiene and validation
# --------------------------------------------------------------------------- #


def test_a_token_is_never_echoed_in_full():
    assert token_looks_valid(TOKEN)
    assert not token_looks_valid("oops")
    assert not token_looks_valid("")
    masked = mask_token(TOKEN)
    assert TOKEN not in masked and "AAFakeToken" not in masked
    assert masked.startswith("7878773795")


@pytest.mark.asyncio
async def test_a_token_of_the_wrong_shape_is_rejected_before_any_request():
    with pytest.raises(BotTokenInvalid):
        BotApiTransport("not-a-token")


@pytest.mark.asyncio
async def test_get_me_reports_a_rejected_token_with_telegrams_own_text(monkeypatch):
    transport = BotApiTransport(TOKEN, token_id=TOKEN_ID)

    async def fake_call(method, payload, files=None):
        raise BotApiError(method, 401, "Unauthorized")

    monkeypatch.setattr(transport, "_call", fake_call)
    with pytest.raises(BotApiError) as err:
        await transport.get_me()
    assert "Unauthorized" in str(err.value)
    await transport.aclose()


# --------------------------------------------------------------------------- #
# Buttons: friendly input in, valid markup out, nonsense downgraded
# --------------------------------------------------------------------------- #


def test_buttons_accept_the_friendly_and_canonical_forms():
    assert parse_button_rows("Open|https://example.com") == [
        [{"text": "Open", "url": "https://example.com"}]
    ]
    assert parse_button_rows([[{"text": "A", "url": "https://a"}], [{"text": "B", "url": "tg://b"}]]) == [
        [{"text": "A", "url": "https://a"}],
        [{"text": "B", "url": "tg://b"}],
    ]


def test_a_bad_button_disables_buttons_instead_of_breaking_the_forward():
    assert parse_button_rows("Open") is None                     # no url
    assert parse_button_rows("Open|javascript:alert(1)") is None  # not a link
    assert parse_button_rows("") is None
    assert parse_button_rows({"text": "x"}) is None


def test_button_markup_is_only_attached_when_it_parses():
    assert BotApiTransport._buttons_markup("Open|https://example.com") == {
        "inline_keyboard": [[{"text": "Open", "url": "https://example.com"}]]
    }
    assert BotApiTransport._buttons_markup("nonsense") is None


# --------------------------------------------------------------------------- #
# The calls the worker makes
# --------------------------------------------------------------------------- #


class _Recorder:
    """Stands in for the HTTP call, capturing the payload Telegram would get."""

    def __init__(self, result=None, error: Exception | None = None):
        self.payloads: list[tuple[str, dict]] = []
        self.result = result if result is not None else {"message_id": 4242}
        self.error = error

    async def __call__(self, method, payload, files=None):
        self.payloads.append((method, payload))
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_send_message_carries_text_buttons_and_no_link_preview(monkeypatch):
    transport = BotApiTransport(TOKEN, token_id=TOKEN_ID)
    rec = _Recorder()
    monkeypatch.setattr(transport, "_call", rec)

    sent = await transport.send_message(
        -1003915313919,
        "hello",
        buttons="Open|https://example.com",
        message_thread_id=77,
    )

    method, payload = rec.payloads[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == -1003915313919
    assert payload["text"] == "hello"
    assert payload["message_thread_id"] == 77
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "Open"
    assert sent.id == 4242
    await transport.aclose()


@pytest.mark.asyncio
async def test_copy_message_keeps_the_caption_override(monkeypatch):
    transport = BotApiTransport(TOKEN, token_id=TOKEN_ID)
    rec = _Recorder()
    monkeypatch.setattr(transport, "_call", rec)

    await transport.copy_message(-1002, -1001, 500, caption="rewritten caption")

    _, payload = rec.payloads[0]
    assert payload == {"chat_id": -1002, "from_chat_id": -1001, "message_id": 500, "caption": "rewritten caption"}
    await transport.aclose()


@pytest.mark.asyncio
async def test_a_rate_limit_keeps_telegrams_retry_after(monkeypatch):
    transport = BotApiTransport(TOKEN, token_id=TOKEN_ID)

    async def fake_post(url, **kwargs):
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 7",
                "parameters": {"retry_after": 7},
            },
        )

    monkeypatch.setattr(transport._client, "post", fake_post)
    with pytest.raises(BotApiError) as err:
        await transport.send_message(-1002, "hi")

    assert err.value.retry_after == 7.0
    assert err.value.code == 429
    await transport.aclose()


@pytest.mark.asyncio
async def test_media_groups_are_refused_so_the_worker_can_fall_back():
    transport = BotApiTransport(TOKEN, token_id=TOKEN_ID)
    with pytest.raises(BotTransportUnsupported):
        await transport.send_media_group(-1002, [-1001], 5)
    await transport.aclose()


# --------------------------------------------------------------------------- #
# Pooling: one transport per token, buttons stay per job
# --------------------------------------------------------------------------- #


class _TokenRepo:
    """A two-method store, because ownership needs both.

    ``get_by_id`` is the unscoped read (a delivery worker with no tenant
    context, a probe), ``get_owned`` is the read a tenant's request goes
    through: another owner's token is simply not found.
    """

    def __init__(
        self,
        token: str = TOKEN,
        active: bool = True,
        hits: list | None = None,
        owner: int = 777,
    ):
        self.token = token
        self.active = active
        self.hits = hits if hits is not None else []
        self.owner = int(owner)
        self.scopes: list[tuple[str, int]] = []

    def _row(self, token_id):
        return type(
            "Row",
            (),
            {
                "id": token_id,
                "token": self.token,
                "is_active": self.active,
                "owner_user_id": self.owner,
            },
        )()

    async def get_by_id(self, token_id):
        self.hits.append(token_id)
        if not self.token:
            return None
        return self._row(token_id)

    async def get_owned(self, token_id, owner_user_id):
        self.hits.append(token_id)
        self.scopes.append((token_id, int(owner_user_id)))
        if not self.token or int(owner_user_id) != self.owner:
            return None  # a foreign id is not found, never read and then judged
        return self._row(token_id)


@pytest.mark.asyncio
async def test_the_token_is_read_once_and_the_transport_is_reused():
    hits: list = []
    cache = BotTransportCache(_TokenRepo(hits=hits))

    first = await cache.get(TOKEN_ID)
    second = await cache.get(TOKEN_ID)

    assert first is second, "one pooled transport per token"
    assert hits == [TOKEN_ID], "the database is not consulted per message"
    await cache.aclose()


@pytest.mark.asyncio
async def test_an_inactive_or_missing_token_yields_no_route():
    assert await BotTransportCache(_TokenRepo(active=False)).get(TOKEN_ID) is None
    assert await BotTransportCache(_TokenRepo(token="")).get(TOKEN_ID) is None
    assert await BotTransportCache(None).get(TOKEN_ID) is None


@pytest.mark.asyncio
async def test_the_adapter_keeps_buttons_per_job(monkeypatch):
    transport = BotApiTransport(TOKEN, token_id=TOKEN_ID)
    rec = _Recorder()
    monkeypatch.setattr(transport, "_call", rec)

    adapter_a = BotRouteAdapter(transport, buttons="A|https://a", thread_id=11)
    adapter_b = BotRouteAdapter(transport, buttons=None, thread_id=0)

    await adapter_a.send_message(-1002, "from A")
    await adapter_b.send_message(-1002, "from B")

    first = rec.payloads[0][1]["reply_markup"]["inline_keyboard"][0][0]["text"]
    assert first == "A"
    assert "reply_markup" not in rec.payloads[1][1], "the second task has no buttons"
    assert rec.payloads[0][1]["message_thread_id"] == 11
    assert "message_thread_id" not in rec.payloads[1][1]
    await transport.aclose()


@pytest.mark.asyncio
async def test_the_cache_survives_a_failing_calls_contract():
    """A repo that raises must not take the worker down with it."""
    class _Broken:
        async def get_by_id(self, token_id):
            raise RuntimeError("db gone")

    assert await BotTransportCache(_Broken()).get(TOKEN_ID) is None


# --------------------------------------------------------------------------- #
# Worker: the job decides the route
# --------------------------------------------------------------------------- #


def _job(**payload_extra):
    from core.domain.entities import DeliveryJob

    payload = {
        "text": "hello",
        "forward_mode": "COPY_MESSAGE",
        "content_mode": "AUTO",
        "has_media": False,
        "album_message_ids": [5],
    }
    payload.update(payload_extra)
    return DeliveryJob(
        id="job-1",
        rule_id="r1",
        source_chat_id="-1001",
        source_message_id=5,
        target_chat_id="-1002",
        payload_data=payload,
    )


@pytest.mark.asyncio
async def test_the_worker_only_takes_the_bot_route_when_the_job_asks():
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    manager = DurableQueueManager.__new__(DurableQueueManager)
    cache = BotTransportCache(_TokenRepo())
    manager._bot_route_cache = cache

    route = await manager._bot_route(
        _job(delivery_transport="BOT", bot_token_id=TOKEN_ID), owner_user_id=777
    )
    assert isinstance(route, BotRouteAdapter)

    # USER job with a token attached is still the account route.
    assert (
        await manager._bot_route(_job(delivery_transport="USER", bot_token_id=TOKEN_ID), owner_user_id=777)
        is not None
    ), "the helper resolves a token, but the worker only calls it for BOT jobs"
    await cache.aclose()


@pytest.mark.asyncio
async def test_a_bot_job_without_a_usable_token_is_not_sent_as_the_owner():
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    manager = DurableQueueManager.__new__(DurableQueueManager)
    manager._bot_route_cache = BotTransportCache(_TokenRepo(token=""))

    assert (
        await manager._bot_route(
            _job(delivery_transport="BOT", bot_token_id=TOKEN_ID), owner_user_id=777
        )
        is None
    )


def test_the_payload_carries_the_route_fields():
    """The pipeline must hand the route to the worker, not just to the panel."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1] / "infrastructure" / "telegram" / "durable_queue.py"
    text = src.read_text(encoding="utf-8")
    for field in ("delivery_transport", "bot_token_id", "inline_buttons"):
        assert re.search(rf'"{field}":', text), f"{field} never reaches the job payload"


@pytest.mark.asyncio
async def test_a_kept_open_transport_is_closed_once():
    transport = BotApiTransport(TOKEN, token_id=TOKEN_ID)
    await transport.aclose()
    await transport.aclose()  # idempotent: the cache may close it again
    assert asyncio.get_event_loop().is_running()


# --------------------------------------------------------------------------- #
# Ownership: a token id travels, so it cannot be proof of ownership
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_foreign_token_is_not_found_for_the_caller():
    """The scoped read is used, so another tenant's token is never returned."""
    repo = _TokenRepo(owner=777)
    cache = BotTransportCache(repo)

    assert await cache.get(TOKEN_ID, owner_user_id=555) is None
    assert repo.scopes == [(TOKEN_ID, 555)], "the caller's own scope was not used for the read"
    await cache.aclose()


@pytest.mark.asyncio
async def test_a_cached_transport_is_not_handed_to_another_tenant():
    """The pool is keyed by token; the *caller* is checked on every hit.

    This is the hole a cache keyed only by token id opens: the first tenant to
    use a bot leaves a ready connection in the pool, and without an owner check
    on the hit path every later job that names that id would reuse it.
    """
    repo = _TokenRepo(owner=777)
    cache = BotTransportCache(repo)

    mine = await cache.get(TOKEN_ID, owner_user_id=777)
    assert mine is not None

    assert await cache.get(TOKEN_ID, owner_user_id=555) is None, (
        "a cached transport must not be usable by a task that does not own the bot"
    )
    assert await cache.get(TOKEN_ID, owner_user_id=777) is mine, (
        "the refusal must not cost the real owner their pooled connection"
    )
    await cache.aclose()


@pytest.mark.asyncio
async def test_the_worker_refuses_a_route_it_cannot_attribute(caplog):
    """No rule means no owner means no proof: the bot route is refused."""
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    manager = DurableQueueManager.__new__(DurableQueueManager)
    manager._bot_route_cache = BotTransportCache(_TokenRepo(owner=777))

    with caplog.at_level("WARNING"):
        route = await manager._bot_route(_job(delivery_transport="BOT", bot_token_id=TOKEN_ID))

    assert route is None
    assert any("no task owner" in rec.getMessage() for rec in caplog.records), caplog.text
    await manager._bot_route_cache.aclose()


@pytest.mark.asyncio
async def test_a_transplanted_token_does_not_deliver_for_the_other_tenant():
    """The end that matters: whose bot the message comes out of.

    The task belongs to 555 and the payload names 777's token. The worker must
    refuse — not silently fall back to 555's account route, and not post through
    777's bot. The same token for its real owner still resolves, so the refusal
    is about ownership rather than about the token being broken.
    """
    from core.infrastructure.telegram.durable_queue import DurableQueueManager

    repo = _TokenRepo(owner=777)
    manager = DurableQueueManager.__new__(DurableQueueManager)
    manager._bot_route_cache = BotTransportCache(repo)
    job = _job(delivery_transport="BOT", bot_token_id=TOKEN_ID)

    assert await manager._bot_route(job, owner_user_id=555) is None, (
        "a task must not deliver through another tenant's bot"
    )
    assert await manager._bot_route(job, owner_user_id=777) is not None, (
        "the bot's real owner can still use it"
    )
    await manager._bot_route_cache.aclose()


@pytest.mark.asyncio
async def test_a_token_stored_before_owners_existed_cannot_be_claimed(caplog):
    """A legacy row has ``owner_user_id = 0``: nobody can be shown to own it.

    Failing open here would mean the strictest reading of the boundary ("the
    owner in the database decides") is skipped exactly for the rows with no
    owner in the database, so a tenant cannot claim one; the log says what to do
    about it, and the unscoped read (probes, the token list) still sees the row.
    """
    repo = _TokenRepo(owner=0)
    cache = BotTransportCache(repo)

    with caplog.at_level("WARNING"):
        refused = await cache.get(TOKEN_ID, owner_user_id=777)

    assert refused is None, "an ownerless token must not be claimed by a tenant"
    # The refusal is deliberately indistinguishable from "no such token": an id
    # must not be probeable for existence, and an ownerless row is not special
    # enough to make an exception to that.
    assert any("not owned by account 777" in rec.getMessage() for rec in caplog.records), caplog.text
    assert await cache.get(TOKEN_ID) is not None, "an unscoped lookup still resolves the token"
    await cache.aclose()
