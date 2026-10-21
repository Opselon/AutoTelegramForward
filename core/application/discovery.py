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

from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

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


@dataclass(frozen=True)
class ChatEntry:
    """One selectable chat, as every surface must show it: name *and* id."""

    id: str
    title: str
    kind: str
    username: str = ""

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
        """Resolve a typed id, @username or title — the text-input fallback."""
        wanted = (token or "").strip().lstrip("@").lower()
        if not wanted:
            return None
        for entry in self._entries:
            if wanted in {entry.id.lower(), entry.username.lower()}:
                return entry
            if wanted == entry.title.lower():
                return entry
        for entry in self._entries:
            if wanted in entry.title.lower() or (entry.username and wanted in entry.username.lower()):
                return entry
        return None

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [entry.as_dict() for entry in self._entries]


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
                )
            )
        entries.sort(key=lambda e: (PRIORITY.get(e.kind, 99), e.title.lower()))
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
