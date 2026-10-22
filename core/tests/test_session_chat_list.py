"""Defect-pinning tests for the session-scoped chat list (ListChats).

The chat list is a tenant-scoped, ownership-checked read, and the mobile
source/target pickers depend on every one of these guarantees:

* a missing owner is refused (fail closed) — never an unscoped listing;
* a session the caller does not own answers exactly like one that does not
  exist, so the reply cannot be used to probe for foreign session ids;
* a session with no live client is an error, not an empty list: an empty page
  is indistinguishable from "this account has no chats" and would silently
  break the picker;
* one malformed dialog is skipped and logged, the rest of the page survives;
* query/limit/offset are applied server-side over the fetched snapshot, and an
  offset past the end is an empty page rather than an error.

Removing any one of the guards below must fail one of these tests.
"""

import asyncio

import grpc
import pytest

from core.infrastructure.grpc_server import servicers as S
from core.proto import pb


class _Aborted(Exception):
    """Raised by _RecordingContext.abort to mirror grpc's abort semantics."""


class _RecordingContext:
    """Minimal grpc.ServicerContext stand-in that records the status set."""

    def __init__(self):
        self.code = None
        self.details = None

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details

    async def abort(self, code, details):
        self.code = code
        self.details = details
        raise _Aborted(f"{code} {details}")


class _Session:
    def __init__(self, session_id, owner_user_id):
        self.id = session_id
        self.owner_user_id = owner_user_id


class _Sessions:
    """Owner-scoped lookup mirroring SessionUseCases.get_owned exactly."""

    def __init__(self, sessions):
        self._by_id = {s.id: s for s in sessions}

    async def get_owned(self, session_id, owner_user_id):
        s = self._by_id.get(session_id)
        if s is None or not owner_user_id:
            return None
        if int(getattr(s, "owner_user_id", 0) or 0) != int(owner_user_id):
            return None
        return s


class _Chat:
    def __init__(self, chat_id, chat_type, title=None, username=None,
                 members=None, is_forum=False, first_name=None):
        self.id = chat_id
        self.type = chat_type
        self.title = title
        self.username = username
        self.members_count = members
        self.is_forum = is_forum
        self.first_name = first_name


class _Dialog:
    def __init__(self, chat, pinned=False):
        self.chat = chat
        # pyrogram carries the user's own ordering on the dialog, not the chat.
        self.pinned = pinned


class _MalformedDialog:
    """A dialog whose chat cannot be read — the mapper must skip it."""


class _Client:
    def __init__(self, dialogs=None, raises=None):
        self._dialogs = dialogs or []
        self._raises = raises
        self.limits = []

    def get_dialogs(self, limit=100):
        self.limits.append(limit)

        async def _gen():
            if self._raises is not None:
                raise self._raises
            for dialog in self._dialogs:
                yield dialog

        return _gen()


class _Pool:
    def __init__(self, client):
        self._client = client
        self.requested = []

    def get(self, session_id):
        self.requested.append(session_id)
        return self._client


def _servicer(sessions, client):
    return S.SessionControlServicer(_Sessions(sessions), _Pool(client))


def _call(servicer, **request_kwargs):
    ctx = _RecordingContext()
    resp = asyncio.run(servicer.ListChats(pb.ListChatsRequest(**request_kwargs), ctx))
    return resp, ctx


# ------------------------------------------------------------- fail-closed

def test_list_chats_refuses_a_missing_owner():
    """owner_user_id == 0 must never produce an unscoped listing."""
    svc = _servicer([_Session("s1", 42)], _Client([_Dialog(_Chat(-1, "channel"))]))
    ctx = _RecordingContext()
    with pytest.raises(_Aborted):
        asyncio.run(svc.ListChats(
            pb.ListChatsRequest(session_id="s1", owner_user_id=0), ctx))
    assert ctx.code == grpc.StatusCode.UNAUTHENTICATED
    assert svc._pool.requested == []      # never even picked a client


def test_list_chats_refuses_a_missing_session_id():
    svc = _servicer([_Session("s1", 42)], _Client([]))
    ctx = _RecordingContext()
    with pytest.raises(_Aborted):
        asyncio.run(svc.ListChats(
            pb.ListChatsRequest(session_id="", owner_user_id=42), ctx))
    assert ctx.code == grpc.StatusCode.INVALID_ARGUMENT


def test_list_chats_foreign_session_is_not_found():
    """B asking for A's session gets the same answer as a non-existent one."""
    svc = _servicer([_Session("s1", 42)], _Client([_Dialog(_Chat(-1, "channel"))]))
    resp, ctx = _call(svc, session_id="s1", owner_user_id=99)
    assert resp.chats == [] and resp.count == 0
    assert ctx.code == grpc.StatusCode.NOT_FOUND
    assert ctx.details == "session_not_found"


def test_list_chats_unknown_session_is_not_found():
    svc = _servicer([_Session("s1", 42)], _Client([_Dialog(_Chat(-1, "channel"))]))
    resp, ctx = _call(svc, session_id="does-not-exist", owner_user_id=42)
    assert resp.chats == []
    assert ctx.code == grpc.StatusCode.NOT_FOUND
    assert ctx.details == "session_not_found"


def test_list_chats_no_live_client_is_an_error_not_an_empty_list():
    """A disconnected session must not be reported as 'you have no chats'."""
    svc = _servicer([_Session("s1", 42)], None)   # pool.get -> None
    resp, ctx = _call(svc, session_id="s1", owner_user_id=42)
    assert resp.chats == []
    assert ctx.code == grpc.StatusCode.FAILED_PRECONDITION
    assert ctx.details == "session_not_connected"
    # A read path must not spawn a client; the pool is only ever asked for one.
    assert svc._pool.requested == ["s1"]


def test_list_chats_owner_scoping_is_enforced_per_session():
    """Only the owning tenant can list a session's chats."""
    svc = _servicer([_Session("s1", 42)], _Client([_Dialog(_Chat(-1, "channel", title="Mine"))]))
    own, own_ctx = _call(svc, session_id="s1", owner_user_id=42)
    assert own_ctx.code is None and [c.chat_id for c in own.chats] == ["-1"]
    foreign, foreign_ctx = _call(svc, session_id="s1", owner_user_id=7)
    assert foreign.chats == [] and foreign_ctx.code == grpc.StatusCode.NOT_FOUND


# ------------------------------------------------------------ mapping

def test_list_chats_maps_and_normalises_every_field():
    dialogs = [
        _Dialog(_Chat(-100111, "channel", title="News", username="news", members=1234)),
        _Dialog(_Chat(-100222, "SUPERGROUP", title="Forum", members=None, is_forum=True)),
    ]
    svc = _servicer([_Session("s1", 42)], _Client(dialogs))
    resp, ctx = _call(svc, session_id="s1", owner_user_id=42, limit=100)

    assert ctx.code is None
    assert resp.session_id == "s1"
    assert [c.chat_id for c in resp.chats] == ["-100111", "-100222"]

    first, second = resp.chats
    assert (first.title, first.username, first.chat_type, first.members, first.is_forum) == (
        "News", "news", "channel", 1234, False,
    )
    # chat_type is lowercased regardless of the source casing
    assert second.chat_type == "supergroup"
    # an absent participant count is unknown (0), never a failed row
    assert second.members == 0
    assert second.is_forum is True
    assert resp.count == 2 and resp.total == 2 and resp.truncated is False


def test_list_chats_reports_the_users_pin_state_per_row(monkeypatch):
    """`pinned` is per-chat and must survive the mapping, both ways.

    The pin set is the raw pin lookup discovery already uses. It deliberately
    is *not* read off the dialog: pyrogram's high-level ``Dialog`` never
    carries the raw pin flag, so a `getattr(dialog, "pinned", False)` mapping
    would report every row unpinned and quietly undo the ordering on all three
    surfaces.
    """
    async def _pins(client):
        return {"-100111"}

    monkeypatch.setattr(S, "pinned_chat_ids", _pins)
    dialogs = [
        _Dialog(_Chat(-100111, "channel", title="Pinned")),
        _Dialog(_Chat(-100222, "channel", title="Loose")),
    ]
    svc = _servicer([_Session("s1", 42)], _Client(dialogs))
    resp, _ = _call(svc, session_id="s1", owner_user_id=42)

    assert [c.pinned for c in resp.chats] == [True, False]
    assert [c.title for c in resp.chats] == ["Pinned", "Loose"]


def test_list_chats_leads_with_pinned_rows_before_paging(monkeypatch):
    """The order must be applied to the snapshot, not to one page of it.

    A picker asks for one page at a time; sorting after the slice would leave
    a pinned chat on the third page and defeat the ordering entirely.
    """
    async def _pins(client):
        return {"-100005"}

    monkeypatch.setattr(S, "pinned_chat_ids", _pins)
    dialogs = [_Dialog(_Chat(-100000 - i, "channel", title=f"C{i}")) for i in range(1, 6)]
    svc = _servicer([_Session("s1", 42)], _Client(dialogs))

    first_page, _ = _call(svc, session_id="s1", owner_user_id=42, limit=2, offset=0)
    assert [c.chat_id for c in first_page.chats] == ["-100005", "-100001"]
    assert first_page.total == 5

    # The pinned row is not duplicated into later pages.
    second_page, _ = _call(svc, session_id="s1", owner_user_id=42, limit=2, offset=2)
    assert [c.chat_id for c in second_page.chats] == ["-100002", "-100003"]


def test_list_chats_survives_a_failed_pin_lookup(monkeypatch):
    """A pin lookup that blows up must not cost the user their chat list."""
    async def _boom(client):
        raise RuntimeError("pins unavailable")

    monkeypatch.setattr(S, "pinned_chat_ids", _boom)
    svc = _servicer([_Session("s1", 42)], _Client([_Dialog(_Chat(-100333, "channel", title="Only"))]))
    resp, ctx = _call(svc, session_id="s1", owner_user_id=42)

    assert ctx.code is None
    assert [c.chat_id for c in resp.chats] == ["-100333"]
    assert resp.chats[0].pinned is False


def test_list_chats_uses_first_name_for_a_private_chat_title():
    svc = _servicer([_Session("s1", 42)],
                    _Client([_Dialog(_Chat(555, "private", first_name="Ali", username=None))]))
    resp, _ = _call(svc, session_id="s1", owner_user_id=42)
    assert resp.chats[0].title == "Ali"
    assert resp.chats[0].username == ""


def test_list_chats_skips_one_bad_dialog_without_dropping_the_rest():
    dialogs = [
        _Dialog(_Chat(-1, "channel", title="Good A")),
        _MalformedDialog(),
        _Dialog(_Chat(-2, "channel", title="Good B")),
    ]
    svc = _servicer([_Session("s1", 42)], _Client(dialogs))
    resp, ctx = _call(svc, session_id="s1", owner_user_id=42)
    assert ctx.code is None
    assert [c.chat_id for c in resp.chats] == ["-1", "-2"]
    assert resp.count == 2 and resp.total == 2


def test_list_chats_dialog_fetch_failure_is_unavailable():
    svc = _servicer([_Session("s1", 42)], _Client(raises=RuntimeError("boom")))
    resp, ctx = _call(svc, session_id="s1", owner_user_id=42)
    assert resp.chats == []
    assert ctx.code == grpc.StatusCode.UNAVAILABLE
    assert "dialog_fetch_failed" in (ctx.details or "")


# ------------------------------------------------------ query / pagination

def test_list_chats_query_matches_title_or_username_case_insensitively():
    dialogs = [
        _Dialog(_Chat(-1, "channel", title="Tech News", username="technews")),
        _Dialog(_Chat(-2, "group", title="Cooking", username="chef")),
    ]
    svc = _servicer([_Session("s1", 42)], _Client(dialogs))

    by_title, _ = _call(svc, session_id="s1", owner_user_id=42, query="NEWS")
    assert [c.chat_id for c in by_title.chats] == ["-1"]

    by_username, _ = _call(svc, session_id="s1", owner_user_id=42, query="chef")
    assert [c.chat_id for c in by_username.chats] == ["-2"]

    everything, _ = _call(svc, session_id="s1", owner_user_id=42, query="")
    assert len(everything.chats) == 2
    assert everything.total == 2


def test_list_chats_paginates_over_the_snapshot():
    dialogs = [_Dialog(_Chat(-i, "channel", title=f"C{i}")) for i in range(1, 6)]
    svc = _servicer([_Session("s1", 42)], _Client(dialogs))

    page, _ = _call(svc, session_id="s1", owner_user_id=42, limit=2, offset=1)
    assert [c.chat_id for c in page.chats] == ["-2", "-3"]
    assert page.count == 2 and page.total == 5

    # offset beyond the end is an empty page, not an error
    past, past_ctx = _call(svc, session_id="s1", owner_user_id=42, limit=2, offset=50)
    assert past.chats == [] and past.count == 0 and past.total == 5
    assert past_ctx.code is None


def test_list_chats_resolves_and_caps_the_page_size():
    dialogs = [_Dialog(_Chat(-i, "channel", title=f"C{i}")) for i in range(1, 251)]
    svc = _servicer([_Session("s1", 42)], _Client(dialogs))

    # An omitted (proto3-zero) limit takes the default page, not a single row:
    # faulting to one would look like an account that owns exactly one chat.
    defaulted, _ = _call(svc, session_id="s1", owner_user_id=42)
    assert defaulted.count == S._DEFAULT_CHAT_LIMIT and defaulted.total == 250

    # A zero or negative limit is the same "unset" case.
    zero, _ = _call(svc, session_id="s1", owner_user_id=42, limit=0)
    assert zero.count == S._DEFAULT_CHAT_LIMIT
    negative, _ = _call(svc, session_id="s1", owner_user_id=42, limit=-3)
    assert negative.count == S._DEFAULT_CHAT_LIMIT

    # A positive out-of-range limit is capped at the documented maximum.
    capped, _ = _call(svc, session_id="s1", owner_user_id=42, limit=999)
    assert capped.count == 200 and capped.total == 250


def test_list_chats_fetches_a_bounded_snapshot_and_flags_truncation():
    client = _Client([_Dialog(_Chat(-i, "channel", title=f"C{i}"))
                      for i in range(S._CHAT_LIST_MAX_DIALOGS)])
    svc = _servicer([_Session("s1", 42)], client)
    resp, _ = _call(svc, session_id="s1", owner_user_id=42, limit=5)

    # the Telegram fetch is capped so one call cannot allocate an unbounded list
    assert client.limits == [S._CHAT_LIST_MAX_DIALOGS]
    assert resp.truncated is True
    assert resp.total == S._CHAT_LIST_MAX_DIALOGS
    assert resp.count == 5
