"""Feature detection against the installed pyrogram's documented surface.

Why this exists
---------------
The pinned release (pyrogram 2.0.106) predates ``message_thread_id`` on the
high-level ``forward_messages`` / ``copy_message`` / ``copy_media_group`` /
``send_message`` methods — the parameter is a Bot-API concept that official
pyrogram's MTProto layer never grew before the project's final release, while
forks and the Bot API itself do accept it. Passing it anyway raises
``TypeError``, so a task pointed at a forum topic failed **every** delivery
at the call boundary (both the live dispatcher and the Go Worker's runtime
adapter) — a configuration the panels offered and validated never survived
contact with the documented client surface.

The helpers below inspect the *live* client's method signatures once and
thread the kwarg through only when the installed pyrogram actually accepts
it. The documented surface decides — not the fork's. On the pinned release a
topic-targeted message is delivered to the chat's General topic (the same
behaviour as passing nothing); on a fork that grew the parameter, topic
targeting works unchanged.
"""

from __future__ import annotations

import inspect
import logging
import weakref
from typing import Any, Dict, Iterable, List, Union

logger = logging.getLogger("atf.pyrogram.compat")

__all__ = ["supports_message_thread", "thread_kwargs"]

#: One probe result per live client, per method name. Weak so a closed
#: session (a re-login replaces the Client object) cannot pin its probe.
_PROBES: "weakref.WeakKeyDictionary[Any, Dict[str, bool]]" = weakref.WeakKeyDictionary()


def _probe(client: Any, method_name: str) -> bool:
    try:
        method = getattr(client, method_name)
        return "message_thread_id" in inspect.signature(method).parameters
    except (AttributeError, TypeError, ValueError):
        # No such method / not introspectable: treat as unsupported rather
        # than letting the send path raise.
        return False


def supports_message_thread(client: Any, method_name: str) -> bool:
    """True when this client's ``method_name`` accepts ``message_thread_id``.

    Probed once per client and cached — the signature of a running Client
    does not change under it.
    """
    try:
        per_client = _PROBES.setdefault(client, {})
    except TypeError:
        # Not weak-referenceable: probe uncached (a fake in tests).
        return _probe(client, method_name)
    if method_name not in per_client:
        per_client[method_name] = _probe(client, method_name)
        if not per_client[method_name]:
            logger.info(
                "pyrogram %s does not accept message_thread_id — topic targeting "
                "degrades to the chat's General topic for this client",
                method_name,
            )
    return per_client[method_name]


def thread_kwargs(
    client: Any,
    method_names: Union[str, Iterable[str]],
    topic_id: Any,
) -> Dict[str, int]:
    """The kwarg dict for topic targeting, or ``{}`` when unsupported.

    ``method_names`` may name one method or several when one built dict is
    spread over more than one call (the durable queue builds one
    ``thread_kwargs`` and forwards it into forward/copy/send alike). With a
    list, the kwarg is included only when **every** named method accepts it —
    partial support would deliver some messages to the topic and some to
    General, which is worse than a consistent default.
    """
    tid = int(topic_id or 0)
    if tid <= 0:
        return {}
    names: List[str] = [method_names] if isinstance(method_names, str) else list(method_names)
    if not names:
        return {}
    if all(supports_message_thread(client, name) for name in names):
        return {"message_thread_id": tid}
    return {}
