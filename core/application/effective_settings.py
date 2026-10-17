"""Resolving the settings a task actually runs with.

A task inherits the defaults of the bot it has attached. That single sentence is
the whole rule, and it lives here so the API, the Telegram bot and the
forwarding worker cannot each decide it differently.

Two callers, two scopes
-----------------------
* A **human** path passes ``owner_user_id``, so the lookup goes through the
  owner-scoped read and another tenant's bot is simply not found.
* The **worker** has no request context — it resolves a token by id, exactly as
  it does for delivery. It never passes an owner and never receives one.

Both paths read the same row and the same domain resolver, so what a user sees
on a settings screen is what the forwarder uses.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..domain.bot_defaults import describe, effective


async def attached_defaults(rule: Any, tokens: Any, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
    """The defaults of the bot ``rule`` has attached, or ``{}``.

    No attached bot, a missing row, or a row whose defaults cannot be read all
    mean the same thing to a forwarder: run on the registry defaults. A failure
    to read a *convenience* setting must never stop a task from forwarding.
    """
    if tokens is None:
        return {}
    token_id = str(getattr(rule, "bot_token_id", "") or "").strip()
    if not token_id:
        return {}
    try:
        if owner_user_id:
            record = await tokens.get_owned(token_id, int(owner_user_id))
        else:
            record = await tokens.get_by_id(token_id)
    except Exception:
        return {}
    defaults = getattr(record, "defaults", None) if record is not None else None
    return dict(defaults) if isinstance(defaults, dict) else {}


async def resolve_rule(
    rule: Any, tokens: Any, owner_user_id: Optional[int] = None
) -> Dict[str, Any]:
    """The complete effective settings for ``rule``, inheritance applied."""
    return effective(rule, await attached_defaults(rule, tokens, owner_user_id))


async def describe_rule(
    rule: Any, tokens: Any, owner_user_id: Optional[int] = None
) -> Dict[str, Dict[str, Any]]:
    """Every effective setting with its source, for a client to render."""
    return describe(rule, await attached_defaults(rule, tokens, owner_user_id))
