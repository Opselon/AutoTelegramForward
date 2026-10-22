"""The multi-chat selection: one value object for both roles of a route.

Every screen of the source/target picker — the list, the search, the direct-id
entry, the confirmation card — reads and writes this one object. That is what
makes "selections survive paging, search and switching roles" a property of the
data instead of a promise each renderer has to keep.

Rules encoded here, not in the buttons:

* the two roles are independent: toggling a target never touches a source;
* a chat is identified by its canonical peer key, so `-100…` and its raw id
  are one entry, and a duplicate can never be added twice;
* toggling a picked chat removes it (the picker's second click);
* every role is bounded, so a crafted state cannot grow without limit;
* the object round-trips through the UI-state buffer as plain JSON and is
  pruned against what Telegram still offers before every render.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.domain.peer_ids import looks_like_chat_id, unique_peer_key

ROLE_SOURCE = "s"
ROLE_TARGET = "t"
ROLES: Tuple[str, str] = (ROLE_SOURCE, ROLE_TARGET)

#: A route with more ends than this is a mistake, not a plan; the cap keeps
#: the confirmation card readable and the creation loop bounded.
MAX_SELECTION = 50
#: Ceiling for one pasted line of ids.
MAX_BATCH = 50

#: Resolution states, in the order the UI shows them.
STATE_OK = "ok"
STATE_READ_ONLY = "read_only"
STATE_UNKNOWN = "unknown"
STATE_UNCHECKED = "unchecked"


def role_key(role: str) -> str:
    """Normalize a role token; anything unknown is the source role."""
    return ROLE_TARGET if str(role or "").strip().lower() in ("t", "target", "targets") else ROLE_SOURCE


@dataclass(frozen=True)
class PickedChat:
    """One chat in a selection: its id, a label, and what we know about it."""

    chat_id: str
    title: str = ""
    state: str = STATE_UNCHECKED

    @property
    def label(self) -> str:
        return self.title or self.chat_id

    @property
    def key(self) -> str:
        return unique_peer_key(self.chat_id)

    def to_state(self) -> Dict[str, str]:
        return {"id": self.chat_id, "title": self.title, "state": self.state}

    @classmethod
    def from_state(cls, raw: Mapping[str, Any]) -> "PickedChat":
        return cls(
            chat_id=str(raw.get("id") or "").strip(),
            title=str(raw.get("title") or ""),
            state=str(raw.get("state") or STATE_UNCHECKED),
        )


@dataclass(frozen=True)
class BatchParse:
    """The result of one pasted line of ids."""

    ids: Tuple[str, ...] = ()
    rejected: Tuple[str, ...] = ()
    duplicates: Tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.ids


def parse_id_batch(raw: Any, limit: int = MAX_BATCH) -> BatchParse:
    """Parse ids separated by commas, spaces or newlines.

    A negative sign belongs to the id: `-1001234567890` is one id, never a
    separator and never two tokens. Splitting therefore happens on separators
    only (ASCII comma, Persian comma, whitespace) and a token keeps its sign.
    Entries that cannot name a chat (`@name`, `t.me/…`, words) are reported
    as rejected instead of being silently dropped.
    """
    text = "" if raw is None else str(raw)
    for separator in ("\u060c",):
        text = text.replace(separator, " ")
    tokens: List[str] = []
    for chunk in text.replace(",", " ").split():
        token = chunk.strip()
        if token:
            tokens.append(token)

    ids: List[str] = []
    rejected: List[str] = []
    duplicates: List[str] = []
    seen: set = set()
    for token in tokens:
        # A lone dash (or several) is punctuation, not a chat.
        cleaned = token.strip().strip("\"'`")
        if cleaned in ("", "-", "--") or not looks_like_chat_id(cleaned):
            rejected.append(token)
            continue
        key = unique_peer_key(cleaned)
        if key in seen:
            duplicates.append(cleaned)
            continue
        seen.add(key)
        if len(ids) >= max(1, int(limit)):
            rejected.append(cleaned)
            continue
        ids.append(cleaned)
    return BatchParse(ids=tuple(ids), rejected=tuple(rejected), duplicates=tuple(duplicates))


@dataclass(frozen=True)
class MergeResult:
    """What :meth:`Selection.merge` changed."""

    added: Tuple[str, ...] = ()
    duplicates: Tuple[str, ...] = ()
    overflow: Tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added)


class Selection:
    """Both roles of one route, as a serializable value."""

    def __init__(self, picks: Optional[Mapping[str, Iterable[PickedChat]]] = None) -> None:
        self._picks: Dict[str, List[PickedChat]] = {role: [] for role in ROLES}
        for role, items in (picks or {}).items():
            for chat in items:
                self.add(role_key(role), chat)

    # -- reading ---------------------------------------------------------
    def __len__(self) -> int:
        return sum(len(items) for items in self._picks.values())

    def of(self, role: str) -> Tuple[PickedChat, ...]:
        return tuple(self._picks[role_key(role)])

    def ids(self, role: str) -> Tuple[str, ...]:
        return tuple(chat.chat_id for chat in self.of(role))

    @property
    def source_ids(self) -> Tuple[str, ...]:
        return self.ids(ROLE_SOURCE)

    @property
    def target_ids(self) -> Tuple[str, ...]:
        return self.ids(ROLE_TARGET)

    def counts(self) -> Dict[str, int]:
        return {role: len(items) for role, items in self._picks.items()}

    def count(self, role: str) -> int:
        return len(self._picks[role_key(role)])

    @property
    def pairs(self) -> int:
        """How many source→target links the route will carry."""
        return self.count(ROLE_SOURCE) * self.count(ROLE_TARGET)

    def get(self, role: str, chat_id: str) -> Optional[PickedChat]:
        key = unique_peer_key(chat_id)
        for chat in self._picks[role_key(role)]:
            if chat.key == key:
                return chat
        return None

    def has(self, role: str, chat_id: str) -> bool:
        return self.get(role, chat_id) is not None

    def by_state(self, role: str, *states: str) -> Tuple[PickedChat, ...]:
        wanted = set(states)
        return tuple(chat for chat in self.of(role) if chat.state in wanted)

    def source_keys(self) -> set:
        return {chat.key for chat in self.of(ROLE_SOURCE)}

    def overlap(self) -> Tuple[str, ...]:
        """Ids picked as both source and target (a self-loop)."""
        shared = self.source_keys() & {chat.key for chat in self.of(ROLE_TARGET)}
        if not shared:
            return ()
        return tuple(chat.chat_id for chat in self.of(ROLE_SOURCE) if chat.key in shared)

    # -- writing ---------------------------------------------------------
    def add(self, role: str, chat: PickedChat) -> bool:
        bucket = self._picks[role_key(role)]
        if len(bucket) >= MAX_SELECTION or any(item.key == chat.key for item in bucket):
            return False
        bucket.append(chat)
        return True

    def remove(self, role: str, chat_id: str) -> bool:
        bucket = self._picks[role_key(role)]
        key = unique_peer_key(chat_id)
        before = len(bucket)
        bucket[:] = [chat for chat in bucket if chat.key != key]
        return len(bucket) != before

    def toggle(self, role: str, chat: PickedChat) -> bool:
        """Pick or unpick one chat; returns whether it is picked afterwards."""
        if self.remove(role, chat.chat_id):
            return False
        return self.add(role, chat)

    def select_all(self, role: str, chats: Sequence[PickedChat]) -> int:
        return sum(1 for chat in chats if self.add(role, chat))

    def clear(self, role: str) -> int:
        removed = len(self._picks[role_key(role)])
        self._picks[role_key(role)] = []
        return removed

    def merge(self, role: str, chats: Iterable[PickedChat]) -> MergeResult:
        """Add many chats at once, reporting duplicates and overflow."""
        added: List[str] = []
        duplicates: List[str] = []
        overflow: List[str] = []
        for chat in chats:
            if self.has(role, chat.chat_id):
                duplicates.append(chat.chat_id)
            elif self.add(role, chat):
                added.append(chat.chat_id)
            else:
                overflow.append(chat.chat_id)
        return MergeResult(tuple(added), tuple(duplicates), tuple(overflow))

    def set_state(self, role: str, chat_id: str, state: str,
                  title: Optional[str] = None) -> bool:
        bucket = self._picks[role_key(role)]
        key = unique_peer_key(chat_id)
        for index, chat in enumerate(bucket):
            if chat.key != key:
                continue
            bucket[index] = replace(chat, state=state, title=title or chat.title)
            return True
        return False

    def prune(self, role: str, available: Optional[Iterable[str]]) -> int:
        """Drop picks the current catalog no longer offers.

        ``available=None`` means "unknown" (the catalog could not be fetched),
        so nothing is dropped: losing a user's picks because of a network blip
        would be worse than showing one stale row.
        """
        if available is None:
            return 0
        keep = {unique_peer_key(item) for item in available}
        bucket = self._picks[role_key(role)]
        before = len(bucket)
        bucket[:] = [chat for chat in bucket if chat.key in keep]
        return before - len(bucket)

    # -- persistence -----------------------------------------------------
    def to_state(self) -> Dict[str, List[Dict[str, str]]]:
        return {role: [chat.to_state() for chat in items] for role, items in self._picks.items()}

    @classmethod
    def from_state(cls, raw: Any) -> "Selection":
        """Rebuild from the UI buffer; malformed entries are ignored.

        The buffer is JSON in SQLite, so it may be stale, hand-edited or from
        an older release. A bad entry must cost that entry, never the screen.
        """
        if not isinstance(raw, Mapping):
            return cls()
        picks: Dict[str, List[PickedChat]] = {role: [] for role in ROLES}
        for role in ROLES:
            items = raw.get(role)
            if not isinstance(items, (list, tuple)):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                chat = PickedChat.from_state(item)
                if chat.chat_id:
                    picks[role].append(chat)
        return cls(picks)
