"""Forward Every N Messages: the counting rule, in one place.

A task can deliver only every Nth message that passes its filters. Two halves
must agree on what that means, and neither may drift from the other:

* **which message owns a slot** — message 1, N+1, 2N+1 … (`owns_slot`), decided
  from a 1-based count of the messages this task accepted;
* **what a legal N is** — 1 (forward every message, the default and the
  backwards-compatible behaviour) through 1000 (`clamp_forward_every_n`).

The counter itself is not kept here: it is persisted per task (``rule_counters``)
because a restart must continue the sequence rather than replay slot 1. This
module only defines the arithmetic, so the pipeline, the settings registry, the
bot panel and the API validation all read the same bounds.

Counting rules the pipeline relies on:

* only messages that passed every configured filter are counted — a message the
  filter, media or content-policy stage rejected never consumes a slot;
* an **album** is one message. The dispatcher aggregates a media group and hands
  the pipeline one payload, so the album is counted exactly once regardless of
  how many items Telegram sent;
* an **edit** is not a new message: it updates a copy that already had its slot,
  so edits are never counted and never advance the sequence.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "DEFAULT_FORWARD_EVERY_N",
    "MIN_FORWARD_EVERY_N",
    "MAX_FORWARD_EVERY_N",
    "clamp_forward_every_n",
    "owns_slot",
    "slots_until_next",
]

#: 1 means "forward every message", which is also the absence of the feature.
DEFAULT_FORWARD_EVERY_N = 1
MIN_FORWARD_EVERY_N = 1
MAX_FORWARD_EVERY_N = 1000


def clamp_forward_every_n(value: Any) -> int:
    """Normalise any write source to a legal interval.

    Garbage (``None``, ``""``, ``"abc"``, ``nan``) and anything below the minimum
    becomes 1 — the feature off — because failing open keeps forwarding exactly
    as it was before the task configured anything. Values above the maximum are
    capped rather than rejected: a hand-edited row or an older client must not
    silence the task, and 1000 is the documented ceiling.
    """
    if value is None or isinstance(value, bool):
        # A boolean is not an interval: True/False both mean "not configured".
        return DEFAULT_FORWARD_EVERY_N
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_FORWARD_EVERY_N
    if not math.isfinite(number):  # NaN / inf
        return DEFAULT_FORWARD_EVERY_N
    try:
        every_n = int(number)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_FORWARD_EVERY_N
    if every_n < MIN_FORWARD_EVERY_N:
        return DEFAULT_FORWARD_EVERY_N
    return min(every_n, MAX_FORWARD_EVERY_N)


def owns_slot(sequence: int, every_n: int) -> bool:
    """True when the ``sequence``-th accepted message is one of the forwarded ones.

    ``sequence`` is 1-based: with N = 5 the messages 1, 6, 11, … are forwarded.
    A non-positive sequence is never a slot (an unreadable counter must not be
    treated as the first message, which would forward twice in a row).
    """
    if every_n <= MIN_FORWARD_EVERY_N or every_n == DEFAULT_FORWARD_EVERY_N:
        return True
    if sequence <= 0:
        return False
    return (sequence - 1) % every_n == 0


def slots_until_next(sequence: int, every_n: int) -> int:
    """How many more accepted messages arrive before the next forwarded one.

    0 means this sequence owns a slot. ``slots_until_next(2, 5) == 4``: messages
    3, 4, 5 and 6 still have to come, and 6 is forwarded. Used for UI copy
    ("next forward in 4 messages") and for tests; it never decides delivery.
    """
    if every_n <= MIN_FORWARD_EVERY_N or sequence <= 0:
        return 0
    if owns_slot(sequence, every_n):
        return 0
    next_slot = ((sequence - 1) // every_n + 1) * every_n + 1
    return next_slot - sequence
