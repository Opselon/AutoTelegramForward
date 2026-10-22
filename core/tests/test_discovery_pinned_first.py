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


def test_dialog_gateway_row_carries_pinned():
    from core.infrastructure.telegram.dialog_gateway import SessionDialogGateway

    def _attr(obj, name):
        return None

    chat = SimpleNamespace(id=-1001, title="C", username="", type=SimpleNamespace(value="CHANNEL"))
    dialog = SimpleNamespace(chat=chat, pinned=True)
    row = SessionDialogGateway._row(object(), chat, dialog)
    assert row["pinned"] is True
    row = SessionDialogGateway._row(object(), chat, None)
    assert row["pinned"] is False
