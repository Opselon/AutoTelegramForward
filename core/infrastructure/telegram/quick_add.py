"""Parse the `/forward add` argument line into a typed command.

Grammar (the arrow may be ``->``, ``→`` or ``⟶``)::

    /forward add LABEL SRC[,SRC...] -> DST[,DST...] [--filter[=value] ...]

* ``LABEL`` is one token: letters, digits, ``_`` and ``-`` (max 50).
* Lists use the shared route-list contract (ASCII or Persian comma, ``@name``,
  ``t.me/...`` links) so quick add and the wizard cannot disagree.
* Per-task filters are ``--`` flags, listed by :data:`FLAG_HELP`. They are
  parsed here and applied by :class:`core.application.quick_add.QuickAddService`
  through the entity's own setters — the parser never writes to a task.

The parser is pure: no Telegram types, no I/O, no translation. ``code`` on the
error is what the surface maps to a catalog key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from core.application.quick_add import (
    BLOCKABLE,
    ONLY_MODES,
    QuickAddError,
    TaskFilters,
    validate_label,
)

from .route_lists import ASCII_ARROW, UNICODE_ARROWS, looks_like_peer, parse_route_list

#: Bounds that keep a pasted command from growing one rule's JSON unbounded.
MAX_WORDS = 50
MAX_DELAY_SECONDS = 86_400
MAX_EVERY_N = 10_000

#: Flag name -> (value required, short description key). The description keys
#: are catalog keys, so help stays localized like every other screen.
FLAG_HELP: Tuple[Tuple[str, bool], ...] = (
    ("only", True),
    ("block", True),
    ("keywords", True),
    ("deny", True),
    ("delay", True),
    ("every", True),
)


class QuickAddParseError(ValueError):
    """The argument line cannot be read; ``code`` names the reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class QuickAddCommand:
    """The raw tokens of one quick-add command, before peers are resolved."""

    label: str
    sources: List[str]
    targets: List[str]
    filters: TaskFilters


def _split_arrow(text: str) -> Tuple[str, str] | None:
    for arrow in (ASCII_ARROW, *UNICODE_ARROWS):
        index = text.find(arrow)
        if index >= 0:
            return text[:index], text[index + len(arrow):]
    return None


def _strip_command(text: str) -> str:
    raw = str(text or "").strip()
    first, _, rest = raw.partition(" ")
    head = first.split("@", 1)[0].lower()
    if head != "/forward":
        # The UI always passes the whole message; a bare argument line is
        # accepted too so the parser can be exercised without a framework.
        word, _, tail = raw.partition(" ")
        if word.lower() != "add":
            raise QuickAddParseError("not_add")
        return tail.strip()
    args = rest.strip()
    word, _, tail = args.partition(" ")
    if word.lower() != "add":
        raise QuickAddParseError("not_add")
    return tail.strip()


def _words(raw: str) -> List[str]:
    return [chunk.strip() for chunk in str(raw or "").replace("\u060c", ",").split(",")
            if chunk.strip()]


def _bounded(values: Sequence[str], flag: str) -> Tuple[str, ...]:
    unique: List[str] = []
    seen = set()
    for value in values:
        word = value.strip()
        if not word:
            continue
        if len(word) > 64:
            raise QuickAddParseError("bad_filter_value", f"{flag}={word[:16]}…")
        if word.lower() not in seen:
            seen.add(word.lower())
            unique.append(word)
    if not unique:
        raise QuickAddParseError("bad_filter_value", flag)
    if len(unique) > MAX_WORDS:
        raise QuickAddParseError("bad_filter_value", f"{flag}>{MAX_WORDS}")
    return tuple(unique)


def _int_value(flag: str, raw: str, maximum: int) -> int:
    text = str(raw or "").strip()
    if not text.isdigit():
        raise QuickAddParseError("bad_filter_value", f"{flag}={text}")
    value = int(text)
    if value <= 0 or value > maximum:
        raise QuickAddParseError("bad_filter_value", f"{flag}={text}")
    return value


def _parse_flags(tokens: Sequence[str]) -> TaskFilters:
    """Read ``--name[=value]`` tokens; unknown or malformed ones are refused."""
    block: List[str] = []
    keywords: List[str] = []
    deny: List[str] = []
    only = ""
    delay = 0
    every = 0
    seen: Dict[str, int] = {}
    for token in tokens:
        if not token.startswith("--"):
            continue
        name, has_value, value = token[2:].partition("=")
        name = name.strip().lower()
        if name not in dict(FLAG_HELP):
            raise QuickAddParseError("unknown_filter", token[:32])
        if not has_value or not value.strip():
            raise QuickAddParseError("bad_filter_value", token[:32])
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            # One flag, comma-separated values: a repeated flag is ambiguous
            # input, not an accumulator (the list contract owns joining).
            raise QuickAddParseError("duplicate_filter", "--" + name)
        if name == "block":
            for entry in _words(value):
                item = entry.lower()
                if item not in BLOCKABLE:
                    raise QuickAddParseError("bad_filter_value", f"block={entry}")
                if item not in block:
                    block.append(item)
        elif name == "only":
            mode = value.strip().lower()
            if mode not in ONLY_MODES:
                raise QuickAddParseError("bad_filter_value", f"only={mode}")
            only = mode
        elif name == "keywords":
            keywords.extend(_bounded(_words(value), "--keywords"))
        elif name == "deny":
            deny.extend(_bounded(_words(value), "--deny"))
        elif name == "delay":
            delay = _int_value("--delay", value, MAX_DELAY_SECONDS)
        elif name == "every":
            every = _int_value("--every", value, MAX_EVERY_N)
    try:
        return TaskFilters(
            block=tuple(block),
            only=only,
            keywords=tuple(dict.fromkeys(keywords)),
            deny=tuple(dict.fromkeys(deny)),
            delay=delay,
            every=every,
        )
    except QuickAddError as exc:  # a rule the value layer refuses
        raise QuickAddParseError(exc.code, exc.detail) from exc


def parse_quick_add(text: str) -> QuickAddCommand:
    """Parse one ``/forward add`` line (the leading command is optional)."""
    body = _strip_command(text)
    if not body:
        raise QuickAddParseError("missing_arguments")
    split = _split_arrow(body)
    if split is None:
        raise QuickAddParseError("missing_arrow")
    head, tail = split
    head_text = head.strip()
    if not head_text:
        raise QuickAddParseError("missing_label")
    if "--" in head_text.split():
        raise QuickAddParseError("unknown_filter", "before-arrow")

    # The label is everything before the first chat-like token, so an unquoted
    # multi-word label reaches the validator (which says no) instead of being
    # silently swallowed into the source list. A quoted label is taken verbatim.
    if head_text[0] in ('"', "'", "`"):
        quote = head_text[0]
        end = head_text.find(quote, 1)
        label_raw = head_text[1:] if end == -1 else head_text[1:end]
        sources_text = "" if end == -1 else head_text[end + 1:].strip()
    else:
        tokens = head_text.split()
        split_at = next((i for i, token in enumerate(tokens) if looks_like_peer(token)), len(tokens))
        label_raw = " ".join(tokens[:split_at])
        sources_text = " ".join(tokens[split_at:])
    try:
        label = validate_label(label_raw)
    except QuickAddError as exc:
        raise QuickAddParseError(exc.code, exc.detail) from exc

    sources = parse_route_list(sources_text.strip()).items
    tail_tokens = tail.split()
    filters = _parse_flags([token for token in tail_tokens if token.startswith("--")])
    target_text = " ".join(token for token in tail_tokens if not token.startswith("--"))
    targets = parse_route_list(target_text).items
    if not sources:
        raise QuickAddParseError("empty_sources")
    if not targets:
        raise QuickAddParseError("empty_targets")
    for token in (*sources, *targets):
        if not looks_like_peer(token):
            raise QuickAddParseError("bad_peer", token)
    return QuickAddCommand(label=label, sources=sources, targets=targets, filters=filters)
