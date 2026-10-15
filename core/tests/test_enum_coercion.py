"""Regression guard for the enum-coercion trap in the persistence layer.

``getattr(getattr(rule, "ai_fallback_policy", X), "value", Y)`` looks defensive
but is not: a plain string has no ``.value``, so the hardcoded ``Y`` silently
replaces the caller's value. That is how assigning the string ``"SEND_ORIGINAL"``
persisted ``"DROP"`` — a wrong value written with no error, which then decides
whether a message is dropped or forwarded during an AI provider outage.
"""

import pytest

from core.domain.entities import ForwardRule
from core.domain.value_objects import AIFallbackPolicy, ContentMode
from core.infrastructure.persistence.coercion import enum_str
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteForwardRuleRepository,
)


def test_enum_str_accepts_member_string_and_none():
    assert enum_str(AIFallbackPolicy.SEND_ORIGINAL) == "SEND_ORIGINAL"
    assert enum_str(AIFallbackPolicy.DROP) == "DROP"
    # The case that used to be swallowed by the hardcoded default.
    assert enum_str("SEND_ORIGINAL") == "SEND_ORIGINAL"
    assert enum_str("RETRY") == "RETRY"
    assert enum_str(ContentMode.TEXT_ONLY) == "TEXT_ONLY"
    assert enum_str("TEXT_ONLY") == "TEXT_ONLY"
    # Absent/blank values still fall back to the caller's default.
    assert enum_str(None, "DROP") == "DROP"
    assert enum_str("", "AUTO") == "AUTO"
    assert enum_str("   ", "AUTO") == "AUTO"
    assert enum_str(None) == ""


@pytest.mark.asyncio
async def test_string_fallback_policy_is_persisted_not_coerced_to_default():
    """Assigning the raw string must survive a write/read round trip."""
    db = SqliteDatabase(":memory:")
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES (?, ?, '', '', '', '', 1, 1, 0, 0)",
        ("s1", "+1000"),
    )
    repo = SqliteForwardRuleRepository(db)

    rule = ForwardRule(
        session_id="s1",
        source_chat_id="-100",
        target_chat_id="-200",
        owner_user_id=7,
    )
    created = await repo.add(rule)
    assert enum_str(created.ai_fallback_policy) == "DROP"  # domain default

    # A plain string, exactly as the bot / REST layer would set it.
    created.ai_fallback_policy = "SEND_ORIGINAL"
    await repo.update(created)

    reloaded = await repo.get_by_id(created.id)
    assert enum_str(reloaded.ai_fallback_policy) == "SEND_ORIGINAL", (
        "a string fallback policy was silently rewritten to the default"
    )


@pytest.mark.asyncio
async def test_string_content_mode_is_persisted_not_coerced_to_auto():
    db = SqliteDatabase(":memory:")
    db.execute(
        "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id,"
        " username, first_name, is_active, is_authorized, created_at, updated_at)"
        " VALUES (?, ?, '', '', '', '', 1, 1, 0, 0)",
        ("s1", "+1000"),
    )
    repo = SqliteForwardRuleRepository(db)

    rule = ForwardRule(
        session_id="s1",
        source_chat_id="-100",
        target_chat_id="-200",
        owner_user_id=7,
    )
    created = await repo.add(rule)
    created.content_mode = "TEXT_ONLY"
    await repo.update(created)

    reloaded = await repo.get_by_id(created.id)
    assert enum_str(reloaded.content_mode) == "TEXT_ONLY", (
        "a string content_mode was silently rewritten to AUTO"
    )
