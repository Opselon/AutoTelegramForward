"""Multi-source / multi-target route list parsing for the Telegram surfaces.

One module implements the list contract so the dashboard steps and the
`/route` command can never disagree about what a list means:

  * split on the ASCII comma and on the Persian comma (U+060C);
  * trim whitespace around every entry and drop empty entries;
  * dedupe preserving first occurrence, and *report* what was dropped so the
    caller can refuse the input and name the offending id;
  * accept numeric ids (`-1001234567890`), `@usernames` and `t.me/<name>`
    links (the server resolves them);
  * refuse a duplicate inside a list and any id present in both lists
    (a source that is also a target is a self-loop).

Callback payloads are built here too, so the 64-byte Telegram limit is
enforced in one place instead of being re-derived at every call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# ASCII comma plus the Persian/Arabic comma. Both are spelled as escapes so
# this file carries no literal non-ASCII punctuation.
LIST_SEPARATORS: Tuple[str, ...] = (",", "\u060c")
ASCII_ARROW = "->"
UNICODE_ARROWS: Tuple[str, ...] = ("\u2192", "\u27f6")  # → and ⟶
LINK_PREFIXES: Tuple[str, ...] = (
    "https://t.me/",
    "http://t.me/",
    "https://telegram.me/",
    "http://telegram.me/",
    "t.me/",
    "telegram.me/",
)

MAX_CALLBACK_BYTES = 64
SHORT_ID_LENGTH = 8

# Validation codes, mapped to catalog keys by the surfaces.
ERR_DUPLICATE_SOURCE = "dup_source"
ERR_DUPLICATE_TARGET = "dup_target"
ERR_OVERLAP = "overlap"
ERR_EMPTY_SOURCES = "empty_sources"
ERR_EMPTY_TARGETS = "empty_targets"

# /route parse codes.
ROUTE_MISSING_ARROW = "missing_arrow"
ROUTE_EMPTY_SOURCES = "empty_sources"
ROUTE_EMPTY_TARGETS = "empty_targets"


class RouteParseError(ValueError):
    """`/route` input that cannot be turned into a source/target split."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ParsedList:
    """A parsed list: the canonical entries plus the duplicates dropped."""

    items: List[str] = field(default_factory=list)
    duplicates: List[str] = field(default_factory=list)

    @property
    def has_duplicates(self) -> bool:
        return bool(self.duplicates)


def strip_link_prefix(token: str) -> str:
    """Drop a t.me/telegram.me prefix from one entry (keeps `@name`)."""
    text = str(token or "").strip()
    low = text.lower()
    for prefix in LINK_PREFIXES:
        if low.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def peer_key(token: str) -> str:
    """Normalized comparison key for one entry.

    Two spellings of the same chat (`@News`, `news`, `https://t.me/news`)
    must collide, so duplicate and self-loop detection is spelled-insensitively
    identical to how Telegram resolves the entry.
    """
    text = strip_link_prefix(token)
    return text.lstrip("@").strip().lower()


def looks_like_peer(token: str) -> bool:
    """True when an entry plausibly names a chat rather than a task name."""
    text = str(token or "").strip()
    if not text:
        return False
    if text.startswith("@") or text.startswith("-") or text.startswith("+"):
        return True
    low = text.lower()
    if any(low.startswith(prefix) for prefix in LINK_PREFIXES):
        return True
    return text.isdigit()


def parse_route_list(raw) -> ParsedList:
    """Parse one comma-separated list (ASCII ',' and Persian '\u060c')."""
    text = "" if raw is None else str(raw)
    for separator in LIST_SEPARATORS[1:]:
        text = text.replace(separator, LIST_SEPARATORS[0])
    seen: set = set()
    items: List[str] = []
    duplicates: List[str] = []
    for chunk in text.split(LIST_SEPARATORS[0]):
        token = chunk.strip()
        if not token:
            continue
        key = peer_key(token)
        if key in seen:
            if token not in duplicates:
                duplicates.append(token)
            continue
        seen.add(key)
        items.append(token)
    return ParsedList(items=items, duplicates=duplicates)


def split_route_list(raw) -> List[str]:
    """Canonical order-preserving list (deduped, empties dropped)."""
    return parse_route_list(raw).items


def find_overlap(sources: Sequence[str], targets: Sequence[str]) -> List[str]:
    """Entries present in both lists, reported from the sources side."""
    source_keys: set = set()
    for token in sources:
        source_keys.add(peer_key(token))
    out: List[str] = []
    for token in targets:
        if peer_key(token) in source_keys and token not in out:
            out.append(token)
    return out


def validate_route_lists(
    sources,
    targets,
    require_sources: bool = True,
    require_targets: bool = True,
) -> List[Tuple[str, str]]:
    """Return (code, offending entry) for every reason the route is invalid.

    An empty list is only an error when the caller asks for that list; the
    dashboard asks for sources alone while the target list is still unknown.
    """
    problems: List[Tuple[str, str]] = []
    parsed_sources = parse_route_list(sources)
    parsed_targets = parse_route_list(targets)
    for token in parsed_sources.duplicates:
        problems.append((ERR_DUPLICATE_SOURCE, token))
    for token in parsed_targets.duplicates:
        problems.append((ERR_DUPLICATE_TARGET, token))
    for token in find_overlap(parsed_sources.items, parsed_targets.items):
        problems.append((ERR_OVERLAP, token))
    if require_sources and not parsed_sources.items:
        problems.append((ERR_EMPTY_SOURCES, ""))
    if require_targets and not parsed_targets.items:
        problems.append((ERR_EMPTY_TARGETS, ""))
    return problems


def validate_resolved_uniqueness(
    sources: Sequence[str], targets: Sequence[str]
) -> List[Tuple[str, str]]:
    """Same duplicate/self-loop rules applied to server-resolved ids.

    `@news` and `-100123` can name one chat; after resolution that is a real
    duplicate, so the check runs again on the resolved ids before saving.
    """
    problems: List[Tuple[str, str]] = []
    seen: set = set()
    for token in sources:
        key = peer_key(token)
        if key in seen:
            problems.append((ERR_DUPLICATE_SOURCE, str(token)))
            continue
        seen.add(key)
    seen = set()
    for token in targets:
        key = peer_key(token)
        if key in seen:
            problems.append((ERR_DUPLICATE_TARGET, str(token)))
            continue
        seen.add(key)
    source_keys = {peer_key(token) for token in sources}
    for token in targets:
        if peer_key(token) in source_keys:
            problems.append((ERR_OVERLAP, str(token)))
    return problems


@dataclass(frozen=True)
class RouteCommand:
    """Parsed `/route <task name or id> <sources> -> <targets>`."""

    name: str = ""
    sources: List[str] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)
    source_text: str = ""
    target_text: str = ""


def _split_arrow(text: str) -> Optional[Tuple[str, str]]:
    for arrow in (ASCII_ARROW, *UNICODE_ARROWS):
        index = text.find(arrow)
        if index >= 0:
            return text[:index], text[index + len(arrow):]
    return None


def _take_name(head: str) -> Tuple[str, str]:
    """Split the part before the arrow into (task name, source list text).

    The name is the leading run of tokens that do not look like a chat
    reference, so `Crypto Alerts -1001, @src -> -2001` names the task
    "Crypto Alerts". A quoted name is taken verbatim and wins over that rule.
    """
    text = head.strip()
    if not text:
        return "", ""
    if text[0] in ('"', "'", "`"):
        quote = text[0]
        end = text.find(quote, 1)
        if end == -1:
            return text[1:].strip(), ""
        return text[1:end].strip(), text[end + 1:].strip()
    tokens = text.split()
    index = 0
    while index < len(tokens) and not looks_like_peer(tokens[index]):
        index += 1
    name = " ".join(tokens[:index]).strip()
    source_text = " ".join(tokens[index:]).strip()
    return name, source_text


def parse_route_command(text: str) -> RouteCommand:
    """Parse a `/route` body (the leading `/route` is optional here)."""
    raw = str(text or "").strip()
    if raw.lower().startswith("/route"):
        raw = raw[len("/route"):].strip()
    split = _split_arrow(raw)
    if split is None:
        raise RouteParseError(ROUTE_MISSING_ARROW)
    head, tail = split
    name, source_text = _take_name(head)
    parsed_sources = parse_route_list(source_text)
    parsed_targets = parse_route_list(tail)
    if not parsed_sources.items:
        raise RouteParseError(ROUTE_EMPTY_SOURCES)
    if not parsed_targets.items:
        raise RouteParseError(ROUTE_EMPTY_TARGETS)
    return RouteCommand(
        name=name,
        sources=parsed_sources.items,
        targets=parsed_targets.items,
        source_text=source_text,
        target_text=str(tail).strip(),
    )


def short_id(rule_id: str) -> str:
    """The 8-character form of a task (rule) id used in lists and buttons."""
    return str(rule_id or "")[:SHORT_ID_LENGTH]


def cb_payload(*parts) -> str:
    """Join callback parts with ':' and enforce Telegram's 64-byte limit."""
    payload = ":".join(str(part) for part in parts)
    size = len(payload.encode("utf-8"))
    if size > MAX_CALLBACK_BYTES:
        raise ValueError(
            "callback_data is {size} bytes (limit {limit}): {payload!r}".format(
                size=size, limit=MAX_CALLBACK_BYTES, payload=payload[:48]
            )
        )
    return payload


def join_ids(ids: Iterable[str]) -> str:
    """Render ids as backticked code spans for display."""
    return ", ".join(f"`{item}`" for item in ids)
