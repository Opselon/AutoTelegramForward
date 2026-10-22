"""Stable rule selectors: one parser for every command and callback surface.

A rule has an opaque, stable id (a UUID for a forwarding task, a 16-hex token
for a replace rule) that never changes when rules are renamed, reordered or
deleted. Users still recognise rules by name, by chat or by the first characters
of the id they copied from a list, so every surface has to accept all of them —
and all of them have to interpret the same token the same way. That is what this
module owns:

* :func:`parse_selector` classifies a raw token (exact id, explicit ``#prefix``,
  or free text) without touching a repository;
* :func:`resolve_rule` resolves it against values the caller already scoped to
  the owner, so a token can never reach another tenant's rule: a foreign id is
  simply absent from the candidate list and answers ``not_found``;
* :func:`match_rule` answers the list search box with the same precedence.

An **ordinal is not a selector**. Rules are listed in execution order, and that
order changes whenever a priority changes, so ``2`` is never read as "the second
rule". A bare number is only matched when it is part of the rule's own id or name.

Callers get a :class:`SelectorError` with a stable ``code`` — never a bare
``None`` — so a handler can explain *why* nothing matched (empty, ambiguous,
not found) instead of printing one generic refusal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence, Tuple

#: A full id, with or without the ``#`` people add when they mean "this is an
#: id". Both id shapes the project issues are accepted: uuid4 canonical/hex for
#: forwarding tasks, and the 16-hex token used for replace rules.
_ID_FULL = re.compile(
    r"\A#?(?P<value>"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{32}"
    r"|[0-9a-f]{16}"
    r")\Z",
    re.IGNORECASE,
)

#: A prefix is only a selector when the user marks it as one (``#a1b2c3d4``).
#: Without the marker, "abcd" is an ordinary word and must stay matchable text.
_ID_PREFIX = re.compile(r"\A#(?P<value>[0-9a-f]{4,31})\Z", re.IGNORECASE)

ID = "id"
PREFIX = "prefix"
TEXT = "text"

#: Stable refusal codes. Handlers map them to catalog keys; they are part of the
#: contract and must not be renamed casually.
EMPTY = "empty"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"

#: Below this length a substring search matches half the account; the resolver
#: refuses it instead of guessing.
MIN_TEXT = 2


@dataclass(frozen=True)
class Selector:
    """A parsed selector: what the user typed, and how to look it up."""

    raw: str
    kind: str
    value: str

    @property
    def is_id(self) -> bool:
        return self.kind in (ID, PREFIX)


class SelectorError(ValueError):
    """A selector that names no single rule.

    ``candidates`` carries display labels (never ids of foreign rules) so an
    ambiguous answer can show what to disambiguate between.
    """

    def __init__(self, code: str, detail: str = "", candidates: Sequence[str] = ()) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.candidates: Tuple[str, ...] = tuple(candidates)


def parse_selector(raw: Any) -> Selector:
    """Classify a token. Raises :class:`SelectorError` for an empty one."""
    text = str(raw or "").strip().strip("`'\"")
    if not text:
        raise SelectorError(EMPTY, "empty selector")
    match = _ID_FULL.match(text)
    if match:
        return Selector(raw=text, kind=ID, value=match.group("value").lower())
    match = _ID_PREFIX.match(text)
    if match:
        return Selector(raw=text, kind=PREFIX, value=match.group("value").lower())
    return Selector(raw=text, kind=TEXT, value=text)


def _field(rule: Any, name: str) -> str:
    if isinstance(rule, dict):
        return str(rule.get(name) or "")
    return str(getattr(rule, name, "") or "")


def rule_id(rule: Any) -> str:
    return _field(rule, "id")


def rule_label(rule: Any, *, width: int = 8) -> str:
    """``name · #id`` — the label every list and report uses.

    Names are not unique (two tasks may share one), so the short id travels with
    the name everywhere a human has to pick or report one.
    """
    rid = rule_id(rule)
    name = _field(rule, "name") or _field(rule, "source") or _field(rule, "source_chat_name")
    short = f"#{rid[:width]}" if rid else ""
    if name and short:
        return f"{name} · {short}"
    return name or short or "—"


def match_rule(rule: Any, query: str) -> bool:
    """The list search box: id (full or ``#prefix``), name, source or target."""
    text = str(query or "").strip()
    if not text:
        return True
    try:
        selector = parse_selector(text)
    except SelectorError:
        return True
    if selector.kind == ID:
        return rule_id(rule).lower() == selector.value
    if selector.kind == PREFIX:
        return rule_id(rule).lower().startswith(selector.value)
    low = selector.value.casefold()
    fields = (
        _field(rule, "name"),
        _field(rule, "source"),
        _field(rule, "to"),
        _field(rule, "source_chat_name"),
        _field(rule, "source_chat_id"),
        _field(rule, "target_chat_id"),
    )
    return any(low in value.casefold() for value in fields if value)


def _names(rule: Any) -> Iterable[str]:
    for name in ("name", "source", "source_chat_name"):
        value = _field(rule, name).strip()
        if value:
            yield value


def resolve_rule(rules: Sequence[Any], raw: Any) -> Any:
    """Resolve ``raw`` against owner-scoped ``rules``; raise :class:`SelectorError`.

    Precedence is deliberate and exhaustive: an id is matched before any text,
    so a rule whose *name* looks like a hex id can never shadow the real one.
    """
    selector = parse_selector(raw)
    rows = list(rules)

    if selector.kind == ID:
        for rule in rows:
            if rule_id(rule).lower() == selector.value:
                return rule
        raise SelectorError(NOT_FOUND, f"no rule with id {selector.value}")

    if selector.kind == PREFIX:
        hits = [rule for rule in rows if rule_id(rule).lower().startswith(selector.value)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise SelectorError(AMBIGUOUS, "prefix matches several rules",
                                [rule_label(rule) for rule in hits[:5]])
        raise SelectorError(NOT_FOUND, f"no rule id starts with {selector.value}")

    needle = selector.value.casefold()
    # Exact, case-insensitive name/chat first: typing a full name must never be
    # refused as "ambiguous" because another name happens to contain it.
    exact = [rule for rule in rows if any(n.casefold() == needle for n in _names(rule))]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SelectorError(AMBIGUOUS, "several rules share that name",
                            [rule_label(rule) for rule in exact[:5]])

    if len(needle) < MIN_TEXT:
        raise SelectorError(NOT_FOUND, "selector too short")

    # Peer ids and usernames, as listed by the forwarding surfaces.
    for field_name in ("source_chat_id", "source_username"):
        hits = [rule for rule in rows if _field(rule, field_name).casefold() == needle]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise SelectorError(AMBIGUOUS, "several rules use that chat",
                                [rule_label(rule) for rule in hits[:5]])

    hits = [
        rule for rule in rows
        if any(needle in name.casefold() for name in _names(rule))
    ]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise SelectorError(AMBIGUOUS, "that text matches several rules",
                            [rule_label(rule) for rule in hits[:5]])
    raise SelectorError(NOT_FOUND, f"no rule matches {selector.value!r}")


def resolve_optional(rules: Sequence[Any], raw: Any) -> Optional[Any]:
    """Like :func:`resolve_rule`, but an empty token means "nothing picked"."""
    try:
        return resolve_rule(rules, raw)
    except SelectorError as exc:
        if exc.code == EMPTY:
            return None
        raise


def describe_failure(exc: SelectorError) -> str:
    """A one-line, id-free explanation for logs and structured errors."""
    if exc.code == AMBIGUOUS:
        return "ambiguous: " + ", ".join(exc.candidates) if exc.candidates else "ambiguous"
    return exc.code
