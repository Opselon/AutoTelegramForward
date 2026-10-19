"""Resolving the settings a task actually runs with.

A task inherits the defaults of the bot it has attached. That single sentence is
the whole rule, and it lives here so the API, the Telegram bot and the
forwarding worker cannot each decide it differently.

Two callers, two scopes
-----------------------
* A **human** path passes ``owner_user_id``, so the lookup goes through the
  owner-scoped read and another tenant's bot is simply not found.
* The **worker** has no request context, but it does have the task, and a task
  knows its owner — so the owner is taken from the argument when given and from
  the rule otherwise. A token id stored on the rule is a routing identifier, and
  an id that names another tenant's bot would otherwise hand that tenant's
  defaults to this task (and, in the worker, deliver through their credential).
  Only a task that knows no owner either (a row written before owners existed)
  falls back to the unscoped read.

Both paths read the same row and the same domain resolver, so what a user sees
on a settings screen is what the forwarder uses.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from ..domain.bot_defaults import describe, effective


async def attached_defaults(rule: Any, tokens: Any, owner_user_id: Optional[int] = None) -> Dict[str, Any]:
    """The defaults of the bot ``rule`` has attached, or ``{}``.

    No attached bot, a missing row, a row that is not this task's owner's, or a
    row whose defaults cannot be read all mean the same thing to a forwarder:
    run on the registry defaults. A failure to read a *convenience* setting must
    never stop a task from forwarding.
    """
    if tokens is None:
        return {}
    token_id = str(getattr(rule, "bot_token_id", "") or "").strip()
    if not token_id:
        return {}
    # The rule is the authority for its own owner: a caller that forgets to pass
    # one must not silently turn a scoped read into an unscoped one.
    owner = int(owner_user_id or 0) or int(getattr(rule, "owner_user_id", 0) or 0)
    try:
        if owner:
            record = await tokens.get_owned(token_id, owner)
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


#: Attribute a materialised copy carries so a save attempt is caught rather than
#: quietly writing a bot's defaults into a task (they would become pins and the
#: task would stop tracking the bot).
INHERITED_MARK = "_inherited_defaults"


async def materialize(rule: Any, tokens: Any) -> Any:
    """``rule`` as the forwarder should see it: bot defaults filled in.

    Returns the rule itself when there is nothing to fill in (no bot attached,
    no defaults, or every setting already the task's own), so the common path
    allocates nothing. Otherwise a copy: the loaded entity must not be modified,
    because a caller that saves it afterwards would turn the inherited values
    into the task's own choices — the exact opposite of inheriting them.

    A default that fails validation (only possible if storage was hand-edited)
    is skipped rather than raised: a convenience setting must never stop a task
    from forwarding.
    """
    from ..domain.rule_settings import SETTING_SPECS, SettingError, apply_settings, pinned_keys

    defaults = await attached_defaults(rule, tokens)
    if not defaults:
        return rule
    pinned = pinned_keys(rule)
    carry = {
        key: value
        for key, value in defaults.items()
        if key in SETTING_SPECS and SETTING_SPECS[key].inheritable and key not in pinned
    }
    if not carry:
        return rule
    try:
        effective_rule = copy.deepcopy(rule)
        apply_settings(effective_rule, carry)
    except SettingError:
        return rule
    except Exception:  # a rule that cannot be copied is not worth failing a forward over
        return rule
    setattr(effective_rule, INHERITED_MARK, sorted(carry))
    return effective_rule
