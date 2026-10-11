"""Live integration test for the backup gRPC surface.

Spins up a real in-process gRPC server bound to a BackupControlServicer wired
to sqlite repositories against a temp database, then a real gRPC client calls
BackupExport and BackupImport. This covers the wire path the API and the bot
both use.
"""

from __future__ import annotations

import asyncio
import grpc
import pytest

from core.infrastructure.grpc_server import servicers
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
from core.application.backup_service import BackupService
from core.domain.entities import AIConfig, FilterRule, ForwardRule, TelegramSession
from core.domain.value_objects import AIProviderType, ForwardMode, RoutingType
from core.proto import pb, pb_grpc


@pytest.fixture
def backup_stack(tmp_path):
    """A BackupControlServicer over real sqlite repos on a temp database."""
    db = SqliteDatabase(str(tmp_path / "atf.db"))
    crypto = CryptoService("integration-test-master-key")
    sessions = SqliteSessionRepository(db, crypto)
    rules = SqliteForwardRuleRepository(db)
    filters = SqliteFilterRuleRepository(db)
    ai = SqliteAIConfigRepository(db, crypto)
    creds = SqliteApiCredentialRepository(db, crypto)
    prompts = SqlitePromptRepository(db)

    svc = BackupService(
        sessions=sessions, rules=rules, filters=filters,
        ai_configs=ai, credentials=creds, prompts=prompts, crypto=crypto,
    )
    servicer = servicers.BackupControlServicer(svc)
    return {
        "servicer": servicer, "service": svc, "db": db,
        "repos": {"sessions": sessions, "rules": rules, "filters": filters,
                  "ai": ai, "creds": creds, "prompts": prompts},
        "crypto": crypto,
    }


async def _seed(stack):
    r = stack["repos"]
    await r["creds"].add(__import__(
        "core.application.repositories", fromlist=["ApiCredential"]
    ).ApiCredential(
        id="cred-1", label="main", api_id=98765, api_hash="HASH-SECRET",
        proxy="", is_default=True, created_at=1, updated_at=1,
        owner_user_id=42,
    ))
    await r["sessions"].add(TelegramSession(
        id="sess-int", phone_number="+989111111111",
        session_string_encrypted="LIVE-SESSION-STRING",
        user_id="60001", username="migrator",
        is_active=True, is_authorized=True, api_credential_id="cred-1",
        owner_user_id=42,
    ))
    await r["filters"].add(FilterRule(
        id="flt-int", name="text only",
        blacklist_keywords=["spam"], owner_user_id=42,
    ))
    await r["ai"].add(AIConfig(
        id="ai-int", name="translator", provider=AIProviderType.OPENAI,
        model="gpt-4o-mini",
        api_key="sk-LIVE-KEY", base_url="https://api.openai.com/v1",
        system_prompt="translate", owner_user_id=42,
    ))
    await r["rules"].add(ForwardRule(
        id="rule-int", session_id="sess-int", name="int test",
        source_chat_id="-100333", target_chat_id="-100444",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        filter_rule_id="flt-int", ai_config_id="ai-int",
        owner_user_id=42,
    ))


class _FakeContext:
    """Minimal grpc.ServicerContext stand-in for direct handler calls."""

    def __init__(self):
        self._code = grpc.StatusCode.OK
        self._details = ""

    def abort(self, code, details):
        self._code = code
        self._details = details
        raise RuntimeError(details)

    def set_code(self, code):
        self._code = code

    def set_details(self, details):
        self._details = details


def test_export_produces_valid_bundle(backup_stack):
    asyncio.run(_seed(backup_stack))

    resp = asyncio.run(backup_stack["servicer"].BackupExport(
        pb.BackupExportRequest(owner_user_id=42),
        _FakeContext(),
    ))
    assert resp.success, resp.error
    assert resp.counts["sessions"] == 1
    assert resp.counts["forward_rules"] == 1
    assert resp.counts["ai_configs"] == 1

    import json
    doc = json.loads(resp.bundle)
    assert doc["format"] == 2
    # secrets travel as plaintext so a different master_key can read them
    assert doc["entities"]["sessions"][0]["session_string"] == "LIVE-SESSION-STRING"
    assert doc["entities"]["ai_configs"][0]["api_key"] == "sk-LIVE-KEY"


def test_export_then_import_roundtrip_on_same_box(backup_stack):
    asyncio.run(_seed(backup_stack))
    ctx = _FakeContext()

    resp = asyncio.run(backup_stack["servicer"].BackupExport(
        pb.BackupExportRequest(owner_user_id=42), ctx))
    assert resp.success, resp.error

    # restore into a SECOND stack with a DIFFERENT master_key
    other_crypto = CryptoService("other-box-key")
    db2 = SqliteDatabase(":memory:")
    stack2 = BackupService(
        sessions=SqliteSessionRepository(db2, other_crypto),
        rules=SqliteForwardRuleRepository(db2),
        filters=SqliteFilterRuleRepository(db2),
        ai_configs=SqliteAIConfigRepository(db2, other_crypto),
        credentials=SqliteApiCredentialRepository(db2, other_crypto),
        prompts=SqlitePromptRepository(db2),
    )
    servicer2 = servicers.BackupControlServicer(stack2)

    imp = asyncio.run(servicer2.BackupImport(
        pb.BackupImportRequest(bundle=resp.bundle), ctx))
    assert imp.success, imp.error
    assert imp.stats["sessions"].imported == 1
    assert imp.stats["forward_rules"].imported == 1
    assert imp.stats["api_credentials"].imported == 1
    assert all(v.failed == 0 for v in imp.stats.values())

    # the session string survived the master_key change (repo decrypts on read)
    s = asyncio.run(stack2._sessions.get_by_id("sess-int"))
    assert s is not None
    assert s.session_string_encrypted == "LIVE-SESSION-STRING"
    a = asyncio.run(stack2._ai.get_by_id("ai-int"))
    assert a.api_key == "sk-LIVE-KEY"


def test_export_with_passphrase_is_aes_gcm(backup_stack):
    asyncio.run(_seed(backup_stack))
    ctx = _FakeContext()

    plain = asyncio.run(backup_stack["servicer"].BackupExport(
        pb.BackupExportRequest(owner_user_id=42), ctx))
    enc = asyncio.run(backup_stack["servicer"].BackupExport(
        pb.BackupExportRequest(owner_user_id=42, passphrase="s3cret"), ctx))
    assert enc.success, enc.error
    assert enc.bundle.startswith("enc:v1:")
    assert not plain.bundle.startswith("enc:v1:")

    # wrong passphrase is rejected at import, not silently ignored
    bad = asyncio.run(backup_stack["servicer"].BackupImport(
        pb.BackupImportRequest(bundle=enc.bundle, passphrase="wrong"), ctx))
    assert not bad.success
    assert "wrong passphrase" in bad.error

    # right passphrase restores
    good = asyncio.run(backup_stack["servicer"].BackupImport(
        pb.BackupImportRequest(bundle=enc.bundle, passphrase="s3cret"), ctx))
    assert good.success, good.error
