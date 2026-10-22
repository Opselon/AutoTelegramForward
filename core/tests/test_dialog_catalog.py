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
    BrowseDialogsUseCase,
    CatalogCache,
    CatalogView,
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


# --------------------------------------------------------------------------- #
# Browsing: page turns reuse the scan; only refresh rescans
# --------------------------------------------------------------------------- #


class _CountingGateway:
    """A gateway that counts scans, so a cache decision is observable."""

    def __init__(self, rows):
        self.rows = rows
        self.scans = 0

    async def fetch(self, owner_user_id):
        self.scans += 1
        return self.rows


def _browse_rows():
    return [
        {"id": -1001, "title": "Alpha", "type": "CHANNEL"},
        {"id": -1002, "title": "Beta", "type": "CHANNEL"},
        {"id": -1003, "title": "Gamma", "type": "CHANNEL"},
        {"id": -1004, "title": "Delta", "type": "CHANNEL"},
        {"id": -1005, "title": "Epsilon", "type": "CHANNEL"},
        {"id": -2001, "title": "Group One", "type": "SUPERGROUP"},
        {"id": -2002, "title": "Group Two", "type": "SUPERGROUP"},
        {"id": 900, "title": "Botto", "type": "BOT"},
        {"id": 700, "title": "Alice", "type": "PRIVATE"},
    ]


async def test_a_page_turn_does_not_rescan_but_refresh_does():
    """Two consecutive views share one scan; `refresh=True` scans again."""
    gateway = _CountingGateway(_browse_rows())
    browse = BrowseDialogsUseCase(ListDialogsUseCase(gateway), page_size=4)

    first = await browse.view(5094837833, KIND_CHANNEL, page=0)
    second = await browse.view(5094837833, KIND_CHANNEL, page=1)

    assert gateway.scans == 1, "a page turn must reuse the cached scan"
    assert first.page.total_pages == 2 and second.page.page == 1
    assert [e.title for e in second.page] == ["Gamma"]

    await browse.view(5094837833, KIND_CHANNEL, page=0, refresh=True)
    assert gateway.scans == 2, "an explicit refresh must rescan"


def test_the_cache_expires_and_serves_only_its_owner():
    """A stale entry is dropped, and one tenant never sees another's catalog."""
    clock = {"now": 1000.0}
    cache = CatalogCache(ttl_seconds=30.0, clock=lambda: clock["now"])

    cache.put(5094837833, DialogCatalog([_entry("-1", "Mine")]))
    clock["now"] += 20.0
    cache.put(6278059256, DialogCatalog([_entry("-2", "Theirs")]))

    assert cache.get(5094837833) is not None
    assert cache.get(6278059256).at(0).title == "Theirs"

    clock["now"] += 25.0
    assert cache.get(5094837833) is None, "an expired entry must not be served"

    # The other tenant's entry is unaffected by this user's expiry.
    assert cache.get(6278059256) is not None
    assert len(cache) == 1


def test_the_cache_evicts_oldest_first_and_refuses_the_zero_owner():
    """A bounded cache cannot grow without limit; uid 0 is never a cache key."""
    clock = {"now": 0.0}
    cache = CatalogCache(ttl_seconds=1000.0, clock=lambda: clock["now"], max_entries=2)

    cache.put(1, DialogCatalog([_entry("-1", "One")]))
    cache.put(2, DialogCatalog([_entry("-2", "Two")]))
    cache.put(3, DialogCatalog([_entry("-3", "Three")]))

    assert cache.get(1) is None, "the oldest entry is evicted when the bound is hit"
    assert cache.get(3) is not None
    assert len(cache) == 2

    cache.put(0, DialogCatalog([_entry("-4", "Anonymous")]))
    assert cache.get(0) is None, "uid 0 (no authenticated owner) is never cached"


async def test_a_view_reports_kind_totals_and_stays_read_only():
    """The kind switcher needs every kind's count; the catalog itself is not
    mutated by browsing, so the same view can be taken twice."""
    gateway = _CountingGateway(_browse_rows())
    browse = BrowseDialogsUseCase(ListDialogsUseCase(gateway), page_size=10)

    view = await browse.view(5094837833)

    assert view.totals == {
        KIND_CHANNEL: 5, KIND_GROUP: 2, KIND_BOT: 1, KIND_USER: 1,
    }
    assert view.total_all == 9
    again = await browse.view(5094837833)
    assert again.totals == view.totals and gateway.scans == 1
