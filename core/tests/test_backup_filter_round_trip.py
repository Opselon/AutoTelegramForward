"""Filter snapshot round-trip regression tests (v12/v30 fields).

``BackupService`` exported and restored a ``FilterRule`` through
``_filter_dict`` / ``restore_snapshot``, and both sides silently dropped
``regex_block``, ``regex_require`` and ``is_enabled``: the snapshot keys were
never enumerated against the persisted columns. Restoring a backup therefore
wiped both regex lists and re-enabled every filter the owner had deliberately
parked — data loss with no error, because ``FilterRule`` defaulted
``is_enabled`` to ``True`` and the missing lists re-defaulted to ``[]``.

These tests pin the full round trip on a real SQLite database (no mocks):
every persisted column of a filter must survive an export → restore into a
fresh database, including the enable/disable toggle and both regex lists.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from core.application.backup_service import BackupService
from core.domain.entities import FilterRule
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAIConfigRepository,
    SqliteApiCredentialRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqlitePromptRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService

OWNER = 5094837833
# CryptoService derives a key from this via KDF, so a fixed string is fine.
KEY = "atf-backup-round-trip-test-key"


def _build(db: SqliteDatabase) -> BackupService:
    crypto = CryptoService(KEY)
    return BackupService(
        sessions=SqliteSessionRepository(db, crypto),
        rules=SqliteForwardRuleRepository(db),
        filters=SqliteFilterRuleRepository(db),
        ai_configs=SqliteAIConfigRepository(db, crypto),
        credentials=SqliteApiCredentialRepository(db, crypto),
        prompts=SqlitePromptRepository(db),
        crypto=crypto,
    )


def test_every_persisted_filter_column_survives_the_round_trip():
    """Export from one database, restore into another, compare field by field."""
    async def body() -> None:
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            src = SqliteDatabase(str(Path(left) / "src.db"))
            try:
                filters = SqliteFilterRuleRepository(src)
                for i, enabled in enumerate((True, False)):
                    await filters.add(FilterRule(
                        id=f"f-{i}",
                        owner_user_id=OWNER,
                        name=f"filter {i}",
                        whitelist_keywords=["gold"],
                        blacklist_keywords=["spam", "scam"],
                        regex_patterns=["^BUY"],
                        regex_block=["^BLOCKED$"],
                        regex_require=["^REQUIRED$"],
                        allowed_media_types=["photo"],
                        blocked_media_types=["video"],
                        drop_service_messages=False,
                        min_message_length=3,
                        max_message_length=900,
                        is_enabled=enabled,
                    ))
                snapshot = await _build(src).create_snapshot()
            finally:
                src.close()

            exported = snapshot["entities"]["filter_rules"]
            assert len(exported) == 2, exported
            for entry in exported:
                # The v12/v30 columns must be present in the snapshot itself:
                # a key that is never written cannot be restored.
                assert entry["regex_block"] == ["^BLOCKED$"], entry
                assert entry["regex_require"] == ["^REQUIRED$"], entry
                assert entry["is_enabled"] in (True, False), entry

            dst = SqliteDatabase(str(Path(right) / "dst.db"))
            try:
                dst_filters = SqliteFilterRuleRepository(dst)
                stats = await _build(dst).restore_snapshot(
                    snapshot, overwrite_existing=True)
                assert stats["filter_rules"]["imported"] == 2, stats["filter_rules"]
                assert stats["filter_rules"]["failed"] == 0

                for i, enabled in enumerate((True, False)):
                    back = await dst_filters.get_by_id(f"f-{i}")
                    assert back is not None, f"f-{i} not restored"
                    # Field by field, so a future dropped column fails here.
                    assert back.name == f"filter {i}"
                    assert back.whitelist_keywords == ["gold"]
                    assert back.blacklist_keywords == ["spam", "scam"]
                    assert back.regex_patterns == ["^BUY"]
                    assert back.regex_block == ["^BLOCKED$"], (
                        "regex_block lost in the snapshot round trip"
                    )
                    assert back.regex_require == ["^REQUIRED$"], (
                        "regex_require lost in the snapshot round trip"
                    )
                    assert back.allowed_media_types == ["photo"]
                    assert back.blocked_media_types == ["video"]
                    assert back.drop_service_messages is False
                    assert back.min_message_length == 3
                    assert back.max_message_length == 900
                    assert back.owner_user_id == OWNER
                    assert back.is_enabled is enabled, (
                        f"the enable/disable toggle did not survive (got {back.is_enabled},"
                        f" expected {enabled})"
                    )
            finally:
                dst.close()

    asyncio.run(body())


def test_a_parked_filter_stays_disabled_across_backup_and_restore():
    """The reported symptom: a disabled filter came back enabled."""
    async def body() -> None:
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            src = SqliteDatabase(str(Path(left) / "src.db"))
            try:
                filters = SqliteFilterRuleRepository(src)
                await filters.add(FilterRule(
                    id="parked", owner_user_id=OWNER,
                    blacklist_keywords=["spam"], is_enabled=False))
                snapshot = await _build(src).create_snapshot()
            finally:
                src.close()
            dst = SqliteDatabase(str(Path(right) / "dst.db"))
            try:
                dst_filters = SqliteFilterRuleRepository(dst)
                await _build(dst).restore_snapshot(snapshot, overwrite_existing=True)
                back = await dst_filters.get_by_id("parked")
                assert back is not None and back.is_enabled is False, (
                    "restore re-enabled a parked filter — the v30 toggle must round-trip"
                )
                assert back.blacklist_keywords == ["spam"], (
                    "restore lost the stored list"
                )
            finally:
                dst.close()

    asyncio.run(body())


def test_an_older_snapshot_without_the_new_keys_restores_enabled():
    """A pre-v30 snapshot has no is_enabled key: it must default to enabled,
    matching how a pre-v30 database row is healed (never a silent disable)."""
    async def body() -> None:
        with tempfile.TemporaryDirectory() as right:
            legacy = {
                "format": 1,
                "entities": {
                    "filter_rules": [{
                        "id": "legacy-1",
                        "name": "legacy",
                        "whitelist_keywords": [],
                        "blacklist_keywords": ["spam"],
                        "regex_patterns": [],
                        "allowed_media_types": [],
                        "blocked_media_types": [],
                        "drop_service_messages": True,
                        "min_message_length": 0,
                        "max_message_length": 0,
                        "owner_user_id": OWNER,
                        # No regex_block / regex_require / is_enabled keys.
                    }],
                },
                "counts": {"filter_rules": 1},
                "exported_at": 1,
                "source_owner_user_id": OWNER,
            }
            dst = SqliteDatabase(str(Path(right) / "dst.db"))
            try:
                dst_filters = SqliteFilterRuleRepository(dst)
                await _build(dst).restore_snapshot(legacy, overwrite_existing=True)
                back = await dst_filters.get_by_id("legacy-1")
                assert back is not None
                assert back.is_enabled is True, (
                    "a pre-v30 snapshot is a filter that was working: it must restore enabled"
                )
                assert back.regex_block == [] and back.regex_require == []
            finally:
                dst.close()

    asyncio.run(body())
