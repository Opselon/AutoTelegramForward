"""Identity redaction for log lines.

Loggers and status strings throughout the core identify a Telegram session by
its phone number or its @username — both are personally identifiable, and both
were being written verbatim. This module is the single place that turns them
into something safe to log: a phone keeps its country prefix and shows four
digits, a username keeps its first two characters.

Everything here is deterministic and dependency-free, so it can be called from
any logger call site without changing the call's shape.
"""

from __future__ import annotations

import re
from typing import Any

#: A phone number as the core stores it: optional ``+``, 7..16 digits.
_PHONE_RE = re.compile(r"^\+?(\d{7,16})$")

_REDACTED = "[REDACTED]"


def mask_phone(phone: Any) -> str:
    """Return a log-safe rendering of ``phone``.

    ``+989121234567`` becomes ``+9891****4567``: the country prefix and the
    last four digits stay, the middle is asterisks. Anything that is not a
    phone number (an empty value, a placeholder like ``"unknown"``) is
    replaced wholesale, because a non-phone string in a phone field is
    unstructured data and must not be assumed benign.
    """
    s = "" if phone is None else str(phone)
    m = _PHONE_RE.match(s.strip())
    if not m:
        return _REDACTED
    digits = m.group(1)
    keep = min(4, len(digits) - 1)
    prefix = "+" if s.strip().startswith("+") else ""
    return f"{prefix}{digits[:keep]}****{digits[-4:]}"


def mask_username(username: Any) -> str:
    """Return a log-safe rendering of a Telegram @username.

    Keeps the first two characters (enough to tell two accounts apart in a
    log, not enough to identify one), replaces the rest with asterisks.
    """
    s = "" if username is None else str(username)
    s = s.strip().lstrip("@")
    if not s:
        return _REDACTED
    if len(s) <= 2:
        return s[0] + "*"
    return s[:2] + "*" * min(len(s) - 2, 12)


def mask_label(*candidates: Any) -> str:
    """Pick the first non-empty identity candidate and mask it.

    Session boot identifies a live account by username, then first name, then
    phone number. Whatever is actually present must be masked the same way,
    and an account with none of them is reported as redacted rather than as
    nothing (which reads as a successful boot of an unnamed session).
    """
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue
        if _PHONE_RE.match(text):
            return mask_phone(text)
        return mask_username(text)
    return _REDACTED


def preview_text(text: Any, limit: int = 30) -> str:
    """A short, non-sensitive preview of an arbitrary string.

    Used for user-supplied text whose content is untrusted; the preview is
    bounded so a long message cannot flood a log line.
    """
    s = "" if text is None else str(text)
    if len(s) > limit:
        return s[:limit] + "..."
    return s


__all__ = [
    "mask_label",
    "mask_phone",
    "mask_username",
    "preview_text",
]
