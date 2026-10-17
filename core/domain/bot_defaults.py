"""Bot defaults: what an attached bot carries for every task that uses it.

Precedence, in one place
------------------------
For every registered setting, the value a forwarding task actually uses is:

    1. **the task**      — the rule pinned this key itself (it is in the rule's
                           metadata, whether or not the value equals the default)
    2. **the bot**       — the attached bot carries a default for this key
    3. **the registry**  — the canonical default in :mod:`core.domain.rule_settings`

Only *behaviour* settings can be inherited. A bot default may never name a chat,
a route or a schedule (`delivery_transport`, `bot_token_id`, `source_policies`,
`target_policies`, `link_buttons`, `affiliate_kit`, `auto_post_scheduler`,
`power_schedule`, `auto_posting`, `power_control`): those say *which* messages a
task handles and *when*, so inheriting them would silently point one task's
traffic at another task's chats. They are marked ``inheritable=False`` in the
registry and rejected here.

Scoping: defaults are stored per bot token and every read/write goes through the
owner's id, so one tenant's bot defaults can never be read as, or leak into,
another tenant's bot. Two bots belonging to the same tenant inherit
independently — a default is a property of a bot, never of the tenant.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from .rule_settings import (
    SETTING_SPECS,
    SettingError,
    coerce_value,
    pinned_keys,
    read_settings,
)

#: Where an effective value came from.
SOURCE_TASK = "task"
SOURCE_BOT = "bot"
SOURCE_DEFAULT = "default"

SOURCES = (SOURCE_TASK, SOURCE_BOT, SOURCE_DEFAULT)


def validate_defaults(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalise a proposed set of bot defaults.

    Raises :class:`SettingError` for an unknown key, a value the registry would
    refuse, or a setting that names a chat/route/schedule. Storage and wire
    format are both the ordinary registry JSON object.
    """
    if not isinstance(values, Mapping):
        raise SettingError("invalid_type", "defaults must be a JSON object")
    cleaned: Dict[str, Any] = {}
    for key, raw in values.items():
        spec = SETTING_SPECS.get(key)
        if spec is None:
            raise SettingError("unknown_setting", f"unknown setting {key!r}", str(key))
        if not spec.inheritable:
            raise SettingError(
                "not_inheritable",
                f"{key} names a chat, a route or a schedule and cannot be a bot default",
                str(key),
            )
        cleaned[key] = coerce_value(key, raw)
    return cleaned


def merge_defaults(
    current: Mapping[str, Any],
    incoming: Mapping[str, Any],
    *,
    replace_all: bool = False,
    clear: Iterable[str] = (),
) -> Dict[str, Any]:
    """Combine a stored mapping with an incoming one.

    Default is *merge*: a caller sending one changed setting cannot wipe the
    rest. ``replace_all`` states the whole mapping (anything absent is dropped),
    and ``clear`` removes named keys — both explicit, neither implied.
    """
    if replace_all:
        merged: Dict[str, Any] = dict(validate_defaults(incoming))
    else:
        merged = dict(validate_defaults(current))
        merged.update(validate_defaults(incoming))
    for key in clear:
        if key not in SETTING_SPECS:
            raise SettingError("unknown_setting", f"unknown setting {key!r}", str(key))
        merged.pop(key, None)
    return merged


def provenance(rule: Any, defaults: Mapping[str, Any], key: str) -> str:
    """Which layer supplies ``key`` for this rule."""
    if key in pinned_keys(rule):
        return SOURCE_TASK
    spec = SETTING_SPECS.get(key)
    if spec is not None and spec.inheritable and key in defaults:
        return SOURCE_BOT
    return SOURCE_DEFAULT


def effective(rule: Any, defaults: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    """Every setting's effective value for ``rule``, bot defaults applied."""
    defaults = defaults or {}
    values = read_settings(rule)
    pinned = pinned_keys(rule)
    out: Dict[str, Any] = {}
    for key in SETTING_SPECS:
        spec = SETTING_SPECS[key]
        if key in pinned or not spec.inheritable or key not in defaults:
            out[key] = values[key]
        else:
            out[key] = defaults[key]
    return out


def describe(rule: Any, defaults: Mapping[str, Any] | None = None) -> Dict[str, Dict[str, Any]]:
    """Effective values with their source, for "inherited vs task-specific".

    A task-specific value carries ``"pinned": True`` so the caller can tell an
    explicit choice that happens to equal the bot default from an inherited one.
    """
    defaults = defaults or {}
    values = effective(rule, defaults)
    pinned = pinned_keys(rule)
    described: Dict[str, Dict[str, Any]] = {}
    for key, value in values.items():
        source = provenance(rule, defaults, key)
        entry: Dict[str, Any] = {
            "value": value,
            "source": source,
            "default": SETTING_SPECS[key].default,
            "inheritable": SETTING_SPECS[key].inheritable,
        }
        if source == SOURCE_TASK:
            entry["pinned"] = True
        if source == SOURCE_BOT:
            entry["bot_default"] = defaults[key]
        described[key] = entry
    return described


def inherited_count(defaults: Mapping[str, Any], rule: Any = None) -> int:
    """How many of a bot's defaults a rule would actually inherit (audit aid)."""
    pinned = pinned_keys(rule) if rule is not None else set()
    return sum(
        1
        for key in defaults
        if key in SETTING_SPECS and SETTING_SPECS[key].inheritable and key not in pinned
    )
