"""Pinned chats come first in every catalog the bot renders.

A user's pinned chats are the ones they touch every day, so the chat picker
and the `/getchannel`-family lists must lead with them inside their kind —
without ever breaking the broadcast-first kind ordering that both surfaces
already promise.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.application.discovery import (
    KIND_CHANNEL,
    ListDialogsUseCase,
)


def _build(raw):
    return ListDialogsUseCase.build(raw)


def test_pinned_chat_leads_its_kind():
    catalog = _build([
        {"id": -101, "title": "Alpha", "type": "CHANNEL"},
        {"id": -102, "title": "Beta", "type": "CHANNEL", "pinned": True},
        {"id": -103, "title": "Charlie", "type": "CHANNEL"},
    ])
    ids = [e.id for e in catalog.of_kind(KIND_CHANNEL).entries]
    assert ids == ["-102", "-101", "-103"]


def test_kind_order_still_beats_pinned_elsewhere():
    catalog = _build([
        {"id": -201, "title": "Zed Group", "type": "GROUP", "pinned": True},
        {"id": -202, "title": "Alpha Channel", "type": "CHANNEL"},
    ])
    assert [(e.kind, e.id) for e in catalog.entries] == [
        ("channel", "-202"),
        ("group", "-201"),
    ]


def test_pinned_flag_survives_as_dict_round_trip():
    catalog = _build([
        {"id": -301, "title": "Pinned", "type": "CHANNEL", "pinned": True},
        {"id": -302, "title": "Plain", "type": "CHANNEL"},
    ])
    rows = catalog.as_dicts()
    assert rows[0]["pinned"] is True
    assert rows[1]["pinned"] is False
    rebuilt = type(catalog).from_dicts(rows)
    assert [e.pinned for e in rebuilt.entries] == [True, False]


def test_find_accepts_both_spellings_of_one_id():
    catalog = _build([
        {"id": -1001206461239, "title": "News", "type": "CHANNEL", "username": "News"},
        {"id": "-1009990000001", "title": "Talk", "type": "GROUP"},
    ])
    raw_spelling = catalog.find("1206461239")
    bot_spelling = catalog.find("-1001206461239")
    assert raw_spelling is not None and bot_spelling is not None
    assert raw_spelling.id == bot_spelling.id == "-1001206461239"


def test_dialog_gateway_row_is_unpinned_by_default():
    """The row carries no pin claim of its own — only the raw lookup decides."""
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    chat = SimpleNamespace(id=-1001, title="C", username="", type=SimpleNamespace(value="CHANNEL"))
    row = SessionDialogGateway._row(object(), chat)
    assert row["pinned"] is False


class _PinnedClient:
    """A client whose high-level dialogs know nothing about pins.

    That is exactly what pyrogram 2.0.106 gives us: ``Dialog`` has no
    ``pinned`` attribute, so the pin list must come from the raw
    ``messages.GetPinnedDialogs`` call. A regression to ``dialog.pinned``
    would leave every row unpinned here.
    """

    def __init__(self, chats, pinned_peers=(), fail_pins=False, folder_pins=None, folders=()):
        self.me = SimpleNamespace(id=999)
        self._chats = list(chats)
        self._pinned_peers = list(pinned_peers)
        self._fail_pins = fail_pins
        self._folder_pins = dict(folder_pins or {})
        self._folders = list(folders)
        self.invoked = []

    async def invoke(self, query):
        name = type(query).__name__
        self.invoked.append(name)
        if name == "GetDialogFilters":
            return SimpleNamespace(filters=[SimpleNamespace(id=f) for f in self._folders])
        if self._fail_pins:
            raise RuntimeError("pins unavailable")
        folder_id = getattr(query, "folder_id", 0)
        peers = self._folder_pins.get(folder_id, self._pinned_peers)
        return SimpleNamespace(dialogs=[SimpleNamespace(peer=p) for p in peers])

    def get_dialogs(self, limit=100):
        async def _gen():
            for chat in self._chats:
                yield SimpleNamespace(chat=chat, pinned=True)  # attr must be ignored

        return _gen()


async def test_scan_marks_pins_from_the_raw_pin_lookup():
    from pyrogram.raw.types import PeerChannel, PeerUser

    from core.application.discovery import ListDialogsUseCase
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    chats = [
        SimpleNamespace(id=-1001000000001, title="Alpha", username="", type="channel"),
        SimpleNamespace(id=-1001000000002, title="Beta", username="", type="channel"),
        SimpleNamespace(id=555000111, title="Someone", username="", type="private"),
    ]
    client = _PinnedClient(chats, pinned_peers=[PeerChannel(channel_id=1000000002), PeerUser(user_id=555000111)])
    gateway = SessionDialogGateway(sessions=None, pool=None)

    rows = await gateway.scan(client, set())

    assert "GetPinnedDialogs" in client.invoked
    by_id = {row["id"]: row["pinned"] for row in rows}
    assert by_id == {"-1001000000001": False, "-1001000000002": True, "555000111": True}

    catalog = ListDialogsUseCase.build(rows)
    assert [e.title for e in catalog.of_kind("channel").entries] == ["Beta", "Alpha"]


async def test_scan_reads_pins_from_custom_folders_too():
    """A chat pinned only inside a custom folder is still pinned for the user."""
    from pyrogram.raw.types import PeerChannel

    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    chats = [SimpleNamespace(id=-1001000000009, title="Forex", username="", type="channel")]
    client = _PinnedClient(
        chats,
        folder_pins={0: [], 7: [PeerChannel(channel_id=1000000009)]},
        folders=[7],
    )

    rows = await SessionDialogGateway(sessions=None, pool=None).scan(client, set())

    assert client.invoked.count("GetPinnedDialogs") == 2  # main folder + folder 7
    assert rows[0]["pinned"] is True


async def test_scan_still_returns_rows_when_the_pin_lookup_fails():
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    chats = [SimpleNamespace(id=-1001000000001, title="Alpha", username="", type="channel")]
    client = _PinnedClient(chats, fail_pins=True)

    rows = await SessionDialogGateway(sessions=None, pool=None).scan(client, set())

    assert [row["id"] for row in rows] == ["-1001000000001"]
    assert rows[0]["pinned"] is False

