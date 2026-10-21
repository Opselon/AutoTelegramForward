"""Discovery policy: classification, ordering and paging of a chat catalog.

These rules are what the bot, the dashboard and any future client share, so they
are tested without a Telegram client, a session pool or a database.
"""
from __future__ import annotations

import pytest

from core.application.discovery import (
    KIND_BOT,
    KIND_CHANNEL,
    KIND_GROUP,
    KIND_SAVED,
    KIND_USER,
    ChatEntry,
    DialogCatalog,
    ListDialogsUseCase,
    classify,
)


@pytest.mark.parametrize(
    "raw,is_self,expected",
    [
        ("CHANNEL", False, KIND_CHANNEL),
        ("channel", False, KIND_CHANNEL),
        ("SUPERGROUP", False, KIND_GROUP),
        ("GROUP", False, KIND_GROUP),
        ("BOT", False, KIND_BOT),
        ("PRIVATE", True, KIND_SAVED),
        ("PRIVATE", False, KIND_USER),
    ],
)
def test_classify_maps_telegram_types(raw, is_self, expected):
    assert classify(raw, is_self=is_self) == expected


def _entry(chat_id: str, title: str, kind: str = KIND_CHANNEL, username: str = "") -> ChatEntry:
    return ChatEntry(id=chat_id, title=title, kind=kind, username=username)


def test_catalog_dedups_sorts_and_localizes_saved_messages():
    catalog = ListDialogsUseCase.build(
        [
            {"id": -1001, "title": "Zeta", "type": "CHANNEL"},
            {"id": -1001, "title": "Zeta", "type": "CHANNEL"},
            {"id": -2002, "title": "Group B", "type": "SUPERGROUP"},
            {"id": 42, "title": "", "type": "PRIVATE", "is_self": True},
        ],
        saved_label="Saved Messages",
    )
    assert len(catalog) == 3
    assert [e.id for e in catalog] == ["-1001", "-2002", "42"]
    assert catalog.at(2).title == "Saved Messages"
    assert catalog.at(2).kind == KIND_SAVED


def test_catalog_pages_and_clamps_an_out_of_range_page():
    catalog = DialogCatalog(_entry(str(i), f"Chat {i:02d}") for i in range(12))
    first = catalog.page(0, page_size=5)
    assert first.total_pages == 3
    assert first.start == 0 and len(first) == 5
    assert first.has_next and not first.has_prev

    last = catalog.page(99, page_size=5)
    assert last.page == 2 and len(last) == 2
    assert last.has_prev and not last.has_next


def test_catalog_filters_by_kind_without_mutating_the_source():
    catalog = ListDialogsUseCase.build(
        [
            {"id": -1, "title": "Chan", "type": "CHANNEL"},
            {"id": -2, "title": "Grp", "type": "SUPERGROUP"},
            {"id": -3, "title": "Bot", "type": "BOT"},
        ]
    )
    assert len(catalog) == 3
    assert [e.id for e in catalog.of_kind(KIND_GROUP)] == ["-2"]
    assert len(catalog) == 3


def test_catalog_find_accepts_id_username_and_title():
    catalog = DialogCatalog([
        _entry("-1001", "News", username="news_chan"),
        _entry("-1002", "Other"),
    ])
    assert catalog.find("-1001").title == "News"
    assert catalog.find("@news_chan").id == "-1001"
    assert catalog.find("other").id == "-1002"
    assert catalog.find("") is None
    assert catalog.find("nope") is None


def test_entries_expose_name_and_id_together():
    entry = _entry("-1001", "News", username="news_chan")
    assert entry.as_dict() == {
        "id": "-1001",
        "title": "News",
        "emoji": "📢",
        "kind": KIND_CHANNEL,
        "username": "news_chan",
    }
    assert entry.label == "📢 News (@news_chan)"


class _Gateway:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[int] = []

    async def fetch(self, owner_user_id):
        self.calls.append(owner_user_id)
        return self.rows


@pytest.mark.asyncio
async def test_use_case_reads_through_the_gateway_and_applies_the_kind():
    gateway = _Gateway([
        {"id": -1, "title": "Chan", "type": "CHANNEL"},
        {"id": -2, "title": "Grp", "type": "SUPERGROUP"},
    ])
    catalog = await ListDialogsUseCase(gateway).execute(5094837833, kind=KIND_CHANNEL)
    assert gateway.calls == [5094837833]
    assert [e.title for e in catalog] == ["Chan"]


@pytest.mark.asyncio
async def test_use_case_tolerates_an_empty_scan():
    catalog = await ListDialogsUseCase(_Gateway([])).execute(1)
    assert len(catalog) == 0 and not catalog
    assert catalog.page(0).total_pages == 1
