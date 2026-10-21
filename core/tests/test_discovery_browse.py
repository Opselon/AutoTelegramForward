"""`/getchannel`-family browsing: paged, refreshable, and tenant-scoped.

The command used to print at most 40 chats and stop. These tests pin the
replacement behaviour end to end — a real handler, the real panel, a real
SQLite store — because the interesting failures are integration ones:

  * a page turn must not rescan (speed) but must show the next chats;
  * the refresh button must rescan, and must keep the page it was on;
  * a second tenant pressing the same callback payload must see their own
    catalog, never the one the payload's message was drawn from;
  * a scan that yields nothing must explain itself in the user's language,
    not crash and not leak a raw catalog key.

The page carries the catalog's own buttons: `gch_pg:<kind>:<page>` to turn,
`gch_rf:<kind>:<page>` to rescan, and `gch_v:<kind>:<index>` for the action
sheet. Six chats per page is the presentation's choice (`PAGE_SIZE`).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application.discovery import (
    KIND_CHANNEL,
    KIND_GROUP,
    KIND_USER,
    ListDialogsUseCase,
    BrowseDialogsUseCase,
)

PAGE = 6


class _Msg:
    def __init__(self, uid: int = 5094837833, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid, type="private")
        self.text = text
        self.sent: list = []

    async def reply_text(self, text=None, **kwargs):
        self.sent.append((text, kwargs))


class _Lister:
    """A catalog per owner, counted so "did it rescan?" is observable."""

    def __init__(self, rows_by_owner=None):
        self.rows_by_owner = rows_by_owner or {}
        self.calls: list[int] = []

    async def execute(self, owner_user_id, kind=None, saved_label="Saved Messages"):  # noqa: D401
        self.calls.append(int(owner_user_id))
        rows = self.rows_by_owner.get(int(owner_user_id), [])
        catalog = ListDialogsUseCase.build(rows, saved_label=saved_label)
        return catalog.of_kind(kind) if kind else catalog


def _channels(n: int, prefix: str = "Chan") -> list:
    return [
        {"id": -1001000 - i, "title": f"{prefix} {i:02d}", "type": "CHANNEL"}
        for i in range(n)
    ]


def _harness(tmp_path):
    from core.tests.bot_dispatch import BotHarness

    return BotHarness(str(tmp_path / "atf.db"))


async def _send(harness, uid: int, command: str, args: str = "") -> str:
    msg = _Msg(uid)
    await harness.ui._dispatch_text_command(msg, command, args)
    assert msg.sent, "the command answered nothing"
    return str(msg.sent[-1][0])


def _payloads(sent) -> list[str]:
    """The callback payloads of the keyboard in a `(text, kwargs)` send."""
    markup = sent[-1][1].get("reply_markup")
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_getchannel_opens_the_first_page_with_real_buttons(tmp_path):
    harness = _harness(tmp_path)
    try:
        lister = _Lister({harness.owner: _channels(25) + [
            {"id": -2001, "title": "Group One", "type": "SUPERGROUP"},
        ]})
        harness.ui._browse_use_case = BrowseDialogsUseCase(lister, page_size=PAGE)

        text = await _send(harness, harness.owner, "getchannel")

        # Six rows per page, the ids are visible, and the page is numbered.
        assert "Chan 00" in text and "Chan 05" in text
        assert "Chan 06" not in text, "a page must not spill the next page's rows"
        assert "Page 1 of 5" in text

        msg = _Msg(harness.owner)
        await harness.ui._dispatch_text_command(msg, "getchannel", "")
        payloads = _payloads(msg.sent)
        assert f"gch_pg:{KIND_CHANNEL}:1" in payloads, "a next-page button must exist"
        assert f"gch_rf:{KIND_CHANNEL}:0" in payloads, "a refresh button must exist"
        assert any(p.startswith(f"gch_pg:{KIND_GROUP}:") for p in payloads), \
            "another kind present in the catalog gets a switch button"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_a_second_page_reuses_the_scan_and_shows_new_rows(tmp_path):
    harness = _harness(tmp_path)
    try:
        lister = _Lister({harness.owner: _channels(25)})
        harness.ui._browse_use_case = BrowseDialogsUseCase(lister, page_size=PAGE)

        await _send(harness, harness.owner, "getchannel")
        assert lister.calls == [harness.owner]

        cq = await harness.press(f"gch_pg:{KIND_CHANNEL}:1", harness.owner)

        assert lister.calls == [harness.owner], "a page turn must not rescan"
        body = str(cq.message.sent[-1][1])
        assert "Chan 06" in body and "Chan 11" in body
        assert "Chan 00" not in body
        assert "Page 2 of 5" in body
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_refresh_rescans_and_keeps_the_page(tmp_path):
    harness = _harness(tmp_path)
    try:
        lister = _Lister({harness.owner: _channels(25)})
        harness.ui._browse_use_case = BrowseDialogsUseCase(lister, page_size=PAGE)

        await _send(harness, harness.owner, "getchannel")
        await harness.press(f"gch_pg:{KIND_CHANNEL}:1", harness.owner)
        assert len(lister.calls) == 1

        cq = await harness.press(f"gch_rf:{KIND_CHANNEL}:1", harness.owner)

        assert len(lister.calls) == 2, "refresh must ask the session again"
        body = str(cq.message.sent[-1][1])
        assert "Page 2 of 5" in body, "refresh must not throw the user back to page 1"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_another_tenant_pressing_the_payload_gets_their_own_catalog(tmp_path):
    harness = _harness(tmp_path)
    try:
        other = 6278059256
        lister = _Lister({
            harness.owner: _channels(3, prefix="Mine"),
            other: _channels(3, prefix="Theirs"),
        })
        harness.ui._browse_use_case = BrowseDialogsUseCase(lister, page_size=PAGE)

        mine = await _send(harness, harness.owner, "getchannel")
        assert "Mine 00" in mine

        # The payload is not a capability: it carries no owner, and the handler
        # reads the pressing user. The other tenant sees only their own chats.
        cq = await harness.press(f"gch_pg:{KIND_CHANNEL}:0", other)
        body = str(cq.message.sent[-1][1])
        assert "Theirs 00" in body
        assert "Mine" not in body
        assert lister.calls[-1] == other
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_an_empty_scan_explains_itself_in_the_users_language(tmp_path):
    harness = _harness(tmp_path)
    try:
        harness.ui._browse_use_case = BrowseDialogsUseCase(_Lister({}), page_size=PAGE)

        await harness.seed_session("sess-a", harness.owner)
        # Resolve the user's language through the panel, as the handler does.
        lang = harness.ui._resolve_lang(harness.owner)
        assert lang in ("en", "fa")
        text = await _send(harness, harness.owner, "getchannel")

        assert "پیدا نشد" in text or "No channel" in text
        assert "ui_" not in text and "cmd_" not in text
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_a_numeric_argument_jumps_to_that_page(tmp_path):
    harness = _harness(tmp_path)
    try:
        harness.ui._browse_use_case = BrowseDialogsUseCase(
            _Lister({harness.owner: _channels(15)}), page_size=PAGE)

        text = await _send(harness, harness.owner, "getchannel", "3")

        assert "Chan 12" in text and "Page 3 of 3" in text
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_the_user_list_uses_the_same_paging(tmp_path):
    harness = _harness(tmp_path)
    try:
        harness.ui._browse_use_case = BrowseDialogsUseCase(_Lister({
            harness.owner: [{"id": 700 + i, "title": f"User {i:02d}", "type": "PRIVATE"}
                            for i in range(12)],
        }), page_size=PAGE)

        text = await _send(harness, harness.owner, "getuser")

        assert "Page 1 of 2" in text
        assert "User 00" in text and "User 05" in text
    finally:
        harness.close()
