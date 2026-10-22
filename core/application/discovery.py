"""Chat discovery: the catalog of channels, groups, bots and users a task can use.

Discovery has two halves. Scanning a session's dialogs needs a live MTProto
client, so that stays in infrastructure (:mod:`core.infrastructure.telegram.dialog_gateway`).
Everything after the scan is policy — what kind a chat is, how duplicates
collapse, which order the list is shown in, how a page is sliced — and policy
belongs to the application layer so the bot, the dashboard and the CLI all get
the same catalog from the same rules.

The use case returns an immutable :class:`DialogCatalog`; rendering decides what
to do with it. No Telegram type appears in this module.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

#: Chat kinds, most specific first. They double as the `/getchannel`-family
#: filters and as the stable key a UI button carries.
KIND_CHANNEL = "channel"
KIND_GROUP = "group"
KIND_SAVED = "saved"
KIND_BOT = "bot"
KIND_USER = "user"

#: Display order: broadcast channels first, then groups, then the private ends.
PRIORITY: Dict[str, int] = {
    KIND_CHANNEL: 0,
    KIND_GROUP: 1,
    KIND_SAVED: 2,
    KIND_BOT: 3,
    KIND_USER: 4,
}

EMOJI: Dict[str, str] = {
    KIND_CHANNEL: "📢",
    KIND_GROUP: "👥",
    KIND_SAVED: "💾",
    KIND_BOT: "🤖",
    KIND_USER: "👤",
}


def classify(raw_type: Any, is_self: bool = False) -> str:
    """Map a Telegram chat type onto one of the five catalog kinds.

    The type arrives as an enum in pyrogram and as a string in tests/webhooks,
    so it is normalised rather than trusted. Order matters: a supergroup string
    contains "group", and a bot is also a user.
    """
    ctype = str(getattr(raw_type, "value", raw_type) or "").lower()
    if "channel" in ctype:
        return KIND_CHANNEL
    if "supergroup" in ctype or "group" in ctype:
        return KIND_GROUP
    if "bot" in ctype:
        return KIND_BOT
    if is_self:
        return KIND_SAVED
    return KIND_USER


#: A channel's Bot-API id (``-1001234567890``) names the same chat as its raw
#: unique id (``1234567890``): the prefix is Bot-API spelling, not identity.
#: Any id a *user* may paste runs through this normalizer before comparison.
UNIQUE_ID_PATTERN = re.compile(r"^-100(?P<raw>\d{1,20})$")


def unique_chat_id(chat_id: Any) -> str:
    """The ``-100``-stripped form of a numeric chat id; other forms untouched."""
    text = str(chat_id or "").strip()
    match = UNIQUE_ID_PATTERN.match(text)
    return match.group("raw") if match else text


@dataclass(frozen=True)
class ChatEntry:
    """One selectable chat, as every surface must show it: name *and* id."""

    id: str
    title: str
    kind: str
    username: str = ""
    #: The chat is pinned in the source session. Pinned chats sort first
    #: inside their kind: the chats a user deliberately pinned are the ones
    #: most likely to be the task they are building.
    pinned: bool = False

    @property
    def emoji(self) -> str:
        return EMOJI.get(self.kind, EMOJI[KIND_USER])

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else ""

    @property
    def label(self) -> str:
        """`📢 Title (@handle)` — the title half of a two-line row."""
        return f"{self.emoji} {self.title}" + (f" ({self.handle})" if self.handle else "")

    def as_dict(self) -> Dict[str, Any]:
        """The dict shape the bot's FSM buffer and older screens already use."""
        return {
            "id": self.id,
            "title": self.title,
            "emoji": self.emoji,
            "kind": self.kind,
            "username": self.username,
            "pinned": self.pinned,
        }


@dataclass(frozen=True)
class DialogPage:
    """One rendered page plus the arithmetic a nav row needs."""

    entries: Tuple[ChatEntry, ...]
    page: int
    total_pages: int
    total: int
    page_size: int

    @property
    def start(self) -> int:
        return self.page * self.page_size

    @property
    def has_prev(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages - 1

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


class DialogCatalog:
    """An immutable, ordered list of chats with paging and kind filters."""

    def __init__(self, entries: Iterable[ChatEntry] = ()) -> None:
        self._entries: Tuple[ChatEntry, ...] = tuple(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    @property
    def entries(self) -> Tuple[ChatEntry, ...]:
        return self._entries

    def of_kind(self, kind: str) -> "DialogCatalog":
        if not kind or kind == "all":
            return self
        return DialogCatalog(e for e in self._entries if e.kind == kind)

    def kinds(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for entry in self._entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def page(self, page: int = 0, page_size: int = 5) -> DialogPage:
        size = max(1, int(page_size))
        total = len(self._entries)
        total_pages = max(1, (total + size - 1) // size)
        index = max(0, min(int(page), total_pages - 1))
        start = index * size
        return DialogPage(
            entries=self._entries[start:start + size],
            page=index,
            total_pages=total_pages,
            total=total,
            page_size=size,
        )

    def at(self, index: int) -> Optional[ChatEntry]:
        try:
            return self._entries[int(index)]
        except (IndexError, TypeError, ValueError):
            return None

    def find(self, token: str) -> Optional[ChatEntry]:
        """Resolve a typed id, unique id, @username or title — the text fallback.

        A numeric token matches the ``-100…`` Bot-API spelling and the raw
        unique id alike, so an id copied from *any* list (`/getchannel -id`,
        another bot, the dashboard) resolves to the same chat.
        """
        wanted = (token or "").strip().lstrip("@").lower()
        if not wanted:
            return None
        for entry in self._entries:
            if wanted in {entry.id.lower(), unique_chat_id(entry.id), entry.username.lower()}:
                return entry
            if wanted == entry.title.lower():
                return entry
        for entry in self._entries:
            if wanted in entry.title.lower() or (entry.username and wanted in entry.username.lower()):
                return entry
        return None

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [entry.as_dict() for entry in self._entries]

    @classmethod
    def from_dicts(cls, rows: Optional[Sequence[Dict[str, Any]]]) -> "DialogCatalog":
        """Rebuild a catalog from the dict rows a buffer/FSM persisted."""
        entries: List[ChatEntry] = []
        for row in rows or ():
            if not isinstance(row, dict) or not row.get("id"):
                continue
            entries.append(
                ChatEntry(
                    id=str(row.get("id")),
                    title=str(row.get("title") or row.get("id")),
                    kind=str(row.get("kind") or KIND_USER),
                    username=str(row.get("username") or ""),
                    pinned=bool(row.get("pinned")),
                )
            )
        return cls(entries)


class DialogGateway(Protocol):
    """The port infrastructure implements: scan a user's sessions for chats."""

    async def fetch(self, owner_user_id: int) -> List[Dict[str, Any]]:
        """Return raw chat rows: ``id``, ``title``, ``username``, ``type``, ``is_self``."""
        ...


class ListDialogsUseCase:
    """Build a user's catalog from whatever its connected sessions can see."""

    def __init__(self, gateway: DialogGateway) -> None:
        self._gateway = gateway

    @staticmethod
    def build(raw: Sequence[Dict[str, Any]], saved_label: str = "Saved Messages") -> DialogCatalog:
        seen: set = set()
        entries: List[ChatEntry] = []
        for chat in raw or ():
            chat_id = str(chat.get("id", "") or "")
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            kind = str(chat.get("kind") or classify(chat.get("type"), bool(chat.get("is_self"))))
            title = str(chat.get("title") or chat_id)
            if kind == KIND_SAVED:
                title = saved_label or title
            entries.append(
                ChatEntry(
                    id=chat_id,
                    title=title,
                    kind=kind,
                    username=str(chat.get("username") or ""),
                    pinned=bool(chat.get("pinned")),
                )
            )
        # Pinned chats lead within their kind: the kind order stays the
        # broadcast-first contract, but inside a kind a chat the user pinned
        # outranks alphabetical position.
        entries.sort(key=lambda e: (PRIORITY.get(e.kind, 99), not e.pinned, e.title.lower()))
        return DialogCatalog(entries)

    async def execute(
        self,
        owner_user_id: int,
        kind: Optional[str] = None,
        saved_label: str = "Saved Messages",
    ) -> DialogCatalog:
        raw = await self._gateway.fetch(int(owner_user_id or 0))
        catalog = self.build(raw, saved_label=saved_label)
        return catalog.of_kind(kind) if kind else catalog


def with_title(entry: ChatEntry, title: str) -> ChatEntry:
    """A catalog entry renamed for display without changing its identity."""
    return replace(entry, title=title)


# --------------------------------------------------------------------------- #
# Browsing: paging over a scan that is cached, not repeated on every tap
# --------------------------------------------------------------------------- #

#: How many chats one page shows. A page must stay under Telegram's 4096-char
#: message limit even when every title is long, so the number is deliberately
#: small and lives here rather than in each renderer.
DEFAULT_PAGE_SIZE = 10

#: A dialog scan is expensive (a live MTProto call per session). Navigating a
#: list must not rescan; only an explicit refresh or an expired entry does.
DEFAULT_CACHE_TTL_SECONDS = 90.0

#: Bound on remembered catalogs, so a long-running bot cannot grow without
#: limit on users who pressed `/getchannel` once. Eviction is oldest-first.
DEFAULT_CACHE_ENTRIES = 256


class CatalogCache:
    """Per-owner catalog memory with a freshness deadline.

    Why this exists: `/getchannel` scans every dialog of every connected
    session, which is slow and rate-limited. Paging through the result must be
    instant, and a second user must never be served the first user's catalog —
    so entries are keyed by owner and carry their own timestamp.

    Time is injected so freshness is testable without sleeping.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        max_entries: int = DEFAULT_CACHE_ENTRIES,
    ) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._clock = clock or time.monotonic
        self._max = max(1, int(max_entries))
        self._items: Dict[int, Tuple[DialogCatalog, float]] = {}

    def __len__(self) -> int:
        return len(self._items)

    def get(self, owner_user_id: int) -> Optional[DialogCatalog]:
        """A fresh catalog for this owner, or ``None`` when expired/unknown."""
        key = int(owner_user_id or 0)
        found = self._items.get(key)
        if found is None:
            return None
        catalog, stored_at = found
        if self._clock() - stored_at >= self._ttl:
            self._items.pop(key, None)
            return None
        return catalog

    def put(self, owner_user_id: int, catalog: DialogCatalog) -> None:
        key = int(owner_user_id or 0)
        if not key:
            return
        self._items[key] = (catalog, self._clock())
        while len(self._items) > self._max:
            oldest = min(self._items, key=lambda k: self._items[k][1])
            self._items.pop(oldest, None)

    def invalidate(self, owner_user_id: int) -> None:
        self._items.pop(int(owner_user_id or 0), None)

    def clear(self) -> None:
        self._items.clear()


@dataclass(frozen=True)
class CatalogView:
    """One page of one kind, plus what the navigation row needs to be drawn."""

    kind: str
    page: DialogPage
    totals: Dict[str, int]
    cached: bool

    @property
    def total_all(self) -> int:
        return sum(self.totals.values())


class BrowseDialogsUseCase:
    """Show a user's chats page by page without rescanning on every page turn.

    The use case owns two policies that used to be implicit in the bot:

    * a page turn reuses the scan while it is fresh (speed, and no dialogs
      flood on a user holding "next");
    * ``refresh=True`` always rescans, because the user asked for the truth now.

    It returns a :class:`CatalogView`; wording and buttons are presentation.
    """

    def __init__(
        self,
        lister: ListDialogsUseCase,
        cache: Optional[CatalogCache] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._lister = lister
        self._cache = cache or CatalogCache()
        self._page_size = max(1, int(page_size))

    @property
    def cache(self) -> CatalogCache:
        return self._cache

    async def catalog(
        self,
        owner_user_id: int,
        refresh: bool = False,
        saved_label: str = "Saved Messages",
    ) -> DialogCatalog:
        """The owner's whole catalog, scanned at most once per freshness window.

        Kept separate from :meth:`view` because the presentation also needs the
        raw list — to resolve a button's chat id, or to hand the wizard its dict
        rows — and those must see the same snapshot a page was drawn from.
        """
        uid = int(owner_user_id or 0)
        found = None if refresh else self._cache.get(uid)
        if found is not None:
            return found
        catalog = await self._lister.execute(uid, saved_label=saved_label)
        self._cache.put(uid, catalog)
        return catalog

    async def view(
        self,
        owner_user_id: int,
        kind: str = "",
        page: int = 0,
        refresh: bool = False,
        saved_label: str = "Saved Messages",
    ) -> CatalogView:
        uid = int(owner_user_id or 0)
        cached = not refresh and self._cache.get(uid) is not None
        catalog = await self.catalog(uid, refresh=refresh, saved_label=saved_label)
        selected = catalog.of_kind(kind)
        return CatalogView(
            kind=kind,
            page=selected.page(page, self._page_size),
            totals=catalog.kinds(),
            cached=cached,
        )


# --------------------------------------------------------------------------- #
# The `/getchannel`-family argument line: one parser, regex-pinned grammar
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ChatListQuery:
    """A parsed `/getchannel`-family argument line.

    ``with_ids`` asks for the copy-ready one-line list (``-id``); ``all_kinds``
    widens a kind-filtered command to every kind (``-all``); ``page`` is the
    0-based page of the plain list. Parsing is pure: errors carry a ``code``
    the surface localizes, and no Telegram type is involved.
    """

    with_ids: bool = False
    all_kinds: bool = False
    page: int = 0


class ChatQueryError(ValueError):
    """The argument line cannot be read; ``code`` names the reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


#: Flags, regex-pinned: ``-id`` and ``-all``, single or double dash, lowercase
#: only — anything else that starts with ``-`` is a refusal, never a guess.
_ID_FLAG = re.compile(r"^--?id$")
_ALL_FLAG = re.compile(r"^--?all$")

#: The page is a 1-based integer of at most three digits (`/getchannel -id 7`).
_PAGE_TOKEN = re.compile(r"^[1-9]\d{0,2}$")


def parse_chat_list_query(raw: str) -> ChatListQuery:
    """Parse one ``/getchannel``-family argument line.

    Grammar (flags and page in any order, flags not repeated)::

        [(-id | --id)] [(-all | --all)] [PAGE]

    Anything else — a bare word, a second page, an unknown ``-flag`` — is
    refused with :class:`ChatQueryError`, which the surface turns into a
    usage line rather than guessing what was meant.
    """
    with_ids = False
    all_kinds = False
    page = 0
    for token in (str(raw or "").strip()).split():
        if _ID_FLAG.match(token):
            if with_ids:
                raise ChatQueryError("bad_flag", token)
            with_ids = True
            continue
        if _ALL_FLAG.match(token):
            if all_kinds:
                raise ChatQueryError("bad_flag", token)
            all_kinds = True
            continue
        if _PAGE_TOKEN.match(token):
            if page:
                raise ChatQueryError("bad_flag", token)
            page = int(token) - 1
            continue
        raise ChatQueryError("bad_flag", token)
    return ChatListQuery(with_ids=with_ids, all_kinds=all_kinds, page=page)
