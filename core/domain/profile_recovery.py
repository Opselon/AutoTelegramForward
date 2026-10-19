"""Profile recovery helpers — what "Reset config" means, in one place.

The ``/profile`` screen offers two distinct actions and they must not be
confused:

*Restart setup* only clears the UI conversation state (``UiState``). Rules,
sessions and settings survive, so the owner re-walks onboarding without losing
a working account.

*Reset config* clears per-task settings — headers, footers, the cleaner, the
content filters and blocked words — for the owner's rules. It must never touch
the rule identity, its source/target routing, its active flag, its owning
session, or Telegram itself. Keeping the decision here (instead of inline in a
callback) is what makes it testable without a bot, and what stops the screen's
promise and the code from drifting apart.
"""

from __future__ import annotations

from typing import Any, Dict, List

#: Metadata keys that hold per-task settings rather than routing/identity.
#: Listed for documentation and for callers that need to reason about the
#: blob; the reset itself goes through the property setters below.
RESETTABLE_METADATA_KEYS = (
    "replacements",
    "cleaner_config",
    "cleaner_enabled",
    "clean_usernames",
    "clean_hashtags",
    "clean_phones",
    "clean_empty_lines",
    "clean_buttons",
    "remove_links",
    "remove_emojis",
    "clean_custom_words",
    "block_links",
    "block_voice",
    "block_stickers",
    "block_video",
    "block_audio",
    "block_text",
    "allowed_media_types",
)

#: Rule attributes reset to their defaults. Ids, owner, session, source/target,
#: ``is_active`` and the filter-rule link are deliberately absent: resetting
#: settings must not silently disable, re-route or delete a task.
RESETTABLE_ATTRS: Dict[str, Any] = {
    "custom_header": "",
    "custom_footer": "",
    "replacements": {},
    "max_time_edit": 86400,
    "cleaner_enabled": False,
    "cleaner_config": {},
    "clean_usernames": False,
    "clean_hashtags": False,
    "clean_phones": False,
    "clean_empty_lines": False,
    "clean_buttons": False,
    "remove_links": False,
    "remove_emojis": False,
    "clean_custom_words": [],
    "block_links": False,
    "block_voice": False,
    "block_stickers": False,
    "block_video": False,
    "block_audio": False,
    "block_text": False,
    "allowed_media_types": [],
    "auto_delete_seconds": 0,
}

#: Values that must survive a reset. Named explicitly so a future refactor that
#: widens ``RESETTABLE_ATTRS`` cannot quietly drop them.
PRESERVED_ATTRS = (
    "id",
    "owner_user_id",
    "session_id",
    "source_chat_id",
    "target_chat_id",
    "is_active",
    "filter_rule_id",
    "bot_token_id",
)


def reset_rule_settings(rule: Any) -> List[str]:
    """Reset the documented per-task settings on ``rule`` in place.

    Returns the names that actually changed, so the caller can log a truthful
    count and a test can prove the reset reached every setting. Only attributes
    that exist on this rule type are touched (some deployments carry an older
    entity), and ``metadata`` keeps every key that is not a setting blob.
    """
    changed: List[str] = []
    for attr, default in RESETTABLE_ATTRS.items():
        if not hasattr(rule, attr):
            continue
        current = getattr(rule, attr)
        if current != default:
            changed.append(attr)
        setattr(rule, attr, list(default) if isinstance(default, list) else default)

    # Every setting above is a property whose setter persists into `metadata`
    # (cleaner flags, filters, replacements, allowed types). Setting the
    # default therefore *is* the reset — popping the keys afterwards only made
    # the setters write them back, which made a second reset look like work.
    # Routing/entitlement keys (transport, bot binding, feature group) are
    # never in `RESETTABLE_ATTRS` and are left untouched.
    return changed
