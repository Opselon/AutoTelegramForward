"""Who is eligible for an automatic reply.

Targeting is deliberately a pure function of (mode, list, chat): no clocks, no
I/O, no randomness. That is what makes "reply to everyone except these" testable
and what keeps the same message from being handled differently on two replicas.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ...domain.autoreply import ContactTarget, IncomingMessage, TargetMode, normalize_chat_id


def _find(targets: Iterable[ContactTarget], chat_id: str) -> Optional[ContactTarget]:
    wanted = normalize_chat_id(chat_id)
    for target in targets or []:
        if target.chat_id == wanted:
            return target
    return None


def is_targeted(mode: TargetMode, targets: Iterable[ContactTarget],
                message: IncomingMessage) -> tuple[bool, str]:
    """Return ``(allowed, reason)``.

    ``reason`` is the machine-readable explanation (``target_everyone``,
    ``chat_muted``, ``chat_not_selected``, ``chat_excepted``, ``chat_enabled``)
    that lands in the structured log, so a user asking "why didn't it answer"
    gets an answer rather than a guess.
    """
    if message.chat_type != "private":
        return False, "not_private"
    entry = _find(targets, message.chat_id)
    mode = TargetMode.coerce(mode)

    if mode is TargetMode.EVERYONE:
        if entry is not None and not entry.is_enabled:
            return False, "chat_muted"
        return True, "target_everyone"

    if mode is TargetMode.ALL_EXCEPT:
        # The list is the exception list: an enabled row means "do not answer
        # this one". An absent chat is answered.
        if entry is not None and entry.is_enabled:
            return False, "chat_excepted"
        return True, "target_all_except"

    # SELECTED_ONLY / MANUAL: only an explicitly enabled chat is answered.
    if entry is not None and entry.is_enabled:
        return True, "chat_enabled"
    return False, "chat_not_selected"


__all__ = ["is_targeted"]
