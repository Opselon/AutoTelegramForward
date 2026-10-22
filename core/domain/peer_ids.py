"""Peer ids: the one canonical answer to \"which chat is this token?\".

Telegram presents the same channel under two numeric spellings —
``-1001234567890`` (Bot API) and ``1234567890`` (the raw unique id) — plus
``@usernames`` and ``t.me`` links. Every surface that dedupes, matches a
selection or detects a self-loop uses the functions here, so two spellings of
one chat can never disagree again.
"""

from __future__ import annotations

import re
from typing import Tuple

#: ``-100…`` Bot-API spelling and the raw unique id name one chat.
UNIQUE_ID_MATCH = re.compile(r"^(?:-100)?(?P<raw>\d{1,20})$")

#: Link prefixes stripped before matching, longest first.
LINK_PREFIXES: Tuple[str, ...] = (
    "https://t.me/",
    "http://t.me/",
    "https://telegram.me/",
    "http://telegram.me/",
    "t.me/",
    "telegram.me/",
)


def strip_link_prefix(token: str) -> str:
    """Drop a t.me / telegram.me prefix from one entry (keeps ``@name``)."""
    text = str(token or "").strip()
    low = text.lower()
    for prefix in LINK_PREFIXES:
        if low.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def unique_peer_key(token: str) -> str:
    """Key that collides both spellings of one numeric id.

    ``-1001234567890`` and ``1234567890`` are one chat; a username or a
    ``t.me`` link is spelling-sensitive (lower-cased) like before.
    """
    stripped = strip_link_prefix(token).lstrip("@").strip().lower()
    match = UNIQUE_ID_MATCH.match(stripped)
    return match.group("raw") if match else stripped


def peer_key(token: str) -> str:
    """Alias of :func:`unique_peer_key` kept for the route editor's spelling."""
    return unique_peer_key(token)


#: A Telegram chat id as a user types it: digits, optionally behind a minus.
CHAT_ID_MATCH = re.compile(r"^-?\d{1,20}$")


def looks_like_chat_id(token: str) -> bool:
    """True only for a numeric chat id (`-100…`, `-288…`, `1234567890`).

    Stricter than :func:`looks_like_peer` on purpose: the direct-entry field
    accepts ids, so a username or a link typed there is reported as not
    understood instead of being sent to Telegram as an id.
    """
    return bool(CHAT_ID_MATCH.match(str(token or "").strip()))


def looks_like_peer(token: str) -> bool:
    """True when an entry plausibly names a chat rather than a task name."""
    text = str(token or "").strip()
    if not text:
        return False
    if text.startswith(("@", "-", "+")):
        return True
    low = text.lower()
    if any(low.startswith(prefix) for prefix in LINK_PREFIXES):
        return True
    return text.isdigit()
