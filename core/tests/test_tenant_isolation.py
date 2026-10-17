"""Adversarial multi-tenant isolation tests (v12).

These prove, against the REAL sqlite repositories and the REAL BackupService /
gRPC servicers, that tenant A cannot read, overwrite, replay, purge or restore
tenant B's data. They are regression tests for the v12 owner-scoping fix:
before v12 the credential vault and prompt templates were global, and the
dead-letter / delivery-stats endpoints ignored the caller entirely.
"""

import asyncio
import sqlite3
import sys
import tempfile
import os

import pytest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.proto import autoforward_pb2 as pb

from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteApiCredentialRepository,
    SqlitePromptRepository,
    SqliteForwardRuleRepository,
    SqliteSessionRepository,
    SqliteFilterRuleRepository,
    SqliteAIConfigRepository,
)
from core.infrastructure.security.crypto import CryptoService
from core.application.backup_service import BackupService, serialize_bundle, deserialize_bundle
from core.application.prompt_service import PromptService
from core.application.repositories import ApiCredential
from core.application.use_cases import ApiCredentialUseCases
from core.domain.entities import PromptTemplate, TelegramSession, ForwardRule, FilterRule, AIConfig
from core.domain.value_objects import RoutingType, ForwardMode
# module-level import: pyrogram grabs the event loop on import, which raises
# under pytest's asyncio mode when imported lazily inside a test body.
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

TENANT_A = 111
TENANT_B = 222


def _build(path):
    db = SqliteDatabase(path)
    crypto = CryptoService("tenant-iso-test-key")
    return db, crypto, {
        "creds": SqliteApiCredentialRepository(db, crypto),
        "prompts": SqlitePromptRepository(db),
        "rules": SqliteForwardRuleRepository(db),
        "sessions": SqliteSessionRepository(db, crypto),
        "filters": SqliteFilterRuleRepository(db),
        "ai": SqliteAIConfigRepository(db, crypto),
    }


def _seed_two_tenants(path):
    db, crypto, repos = _build(path)

    async def go():
        # Tenant A's secrets
        await repos["creds"].add(ApiCredential(
            id="cred-a", label="A", api_id=1, api_hash="HASH-A",
            is_default=True, created_at=1, updated_at=1, owner_user_id=TENANT_A,
        ))
        # Tenant B's secrets
        await repos["creds"].add(ApiCredential(
            id="cred-b", label="B", api_id=2, api_hash="HASH-B",
            is_default=False, created_at=1, updated_at=1, owner_user_id=TENANT_B,
        ))
        # Tenant A's private prompt
        await repos["prompts"].add_template(PromptTemplate(
            id="pt-a", name="A private", system_prompt="prompt-of-A",
            user_prompt_template="{text}", owner_user_id=TENANT_A,
        ))
        # A shared system prompt
        await repos["prompts"].add_template(PromptTemplate(
            id="pt-sys", name="system", system_prompt="shared",
            user_prompt_template="{text}", is_system=True,
        ))
        # One rule per tenant (a session must exist first: FK constraint)
        await repos["sessions"].add(TelegramSession(
            id="sess-a", phone_number="+1", session_string_encrypted="str-a",
            is_active=True, is_authorized=True, owner_user_id=TENANT_A,
        ))
        await repos["rules"].add(ForwardRule(
            id="rule-a", session_id="sess-a", name="A",
            source_chat_id="-1", target_chat_id="-2",
            routing_type=RoutingType.CHANNEL_TO_CHANNEL,
            forward_mode=ForwardMode.COPY_MESSAGE, owner_user_id=TENANT_A,
        ))
        await repos["rules"].add(ForwardRule(
            id="rule-b", session_id="sess-a", name="B",
            source_chat_id="-3", target_chat_id="-4",
            routing_type=RoutingType.CHANNEL_TO_CHANNEL,
            forward_mode=ForwardMode.COPY_MESSAGE, owner_user_id=TENANT_B,
        ))

    asyncio.run(go())
    return db, crypto, repos


# ---------------------------------------------------------------- credentials

def test_tenant_cannot_list_other_tenants_credentials(tmp_path):
    """list_by_owner must not return another tenant's api_hash."""
    p = str(tmp_path / "iso.db")
    _, _, repos = _seed_two_tenants(p)
    seen = asyncio.run(repos["creds"].list_by_owner(TENANT_A))
    assert [c.id for c in seen] == ["cred-a"]
    # B's secret hash must not be reachable through A's scoped list
    assert all(c.api_hash == "HASH-A" for c in seen)

    seen_b = asyncio.run(repos["creds"].list_by_owner(TENANT_B))
    assert [c.id for c in seen_b] == ["cred-b"]


def test_backup_export_is_scoped_to_owner(tmp_path):
    """A non-admin export must carry only the caller's credentials."""
    p = str(tmp_path / "iso.db")
    _, crypto, repos = _seed_two_tenants(p)
    svc = BackupService(
        sessions=repos["sessions"], rules=repos["rules"], filters=repos["filters"],
        ai_configs=repos["ai"], credentials=repos["creds"], prompts=repos["prompts"],
        crypto=crypto,
    )
    doc = asyncio.run(svc.create_snapshot(TENANT_A))
    ids = [c["id"] for c in doc["entities"]["api_credentials"]]
    hashes = [c.get("api_hash") for c in doc["entities"]["api_credentials"]]
    assert ids == ["cred-a"], f"tenant B credential leaked into A's export: {ids}"
    assert "HASH-B" not in hashes


def test_backup_export_unscoped_returns_everything(tmp_path):
    """The admin (owner=None) path still sees all credentials."""
    p = str(tmp_path / "iso.db")
    _, crypto, repos = _seed_two_tenants(p)
    svc = BackupService(
        sessions=repos["sessions"], rules=repos["rules"], filters=repos["filters"],
        ai_configs=repos["ai"], credentials=repos["creds"], prompts=repos["prompts"],
        crypto=crypto,
    )
    doc = asyncio.run(svc.create_snapshot(None))
    ids = sorted(c["id"] for c in doc["entities"]["api_credentials"])
    assert ids == ["cred-a", "cred-b"]


# ------------------------------------------------------------------ prompts

def test_prompt_list_by_owner_hides_other_tenants(tmp_path):
    p = str(tmp_path / "iso.db")
    _, _, repos = _seed_two_tenants(p)
    seen = asyncio.run(repos["prompts"].list_templates_by_owner(TENANT_A))
    ids = sorted(t.id for t in seen)
    # A's own prompt + the shared system prompt; B's private prompt excluded
    assert ids == ["pt-a", "pt-sys"], ids


def test_prompt_restore_rebinds_to_target_owner(tmp_path):
    """An imported prompt must be re-stamped to the importing tenant."""
    src_p = str(tmp_path / "src.db")
    dst_p = str(tmp_path / "dst.db")
    _, crypto, src_repos = _seed_two_tenants(src_p)
    svc_src = BackupService(
        sessions=src_repos["sessions"], rules=src_repos["rules"], filters=src_repos["filters"],
        ai_configs=src_repos["ai"], credentials=src_repos["creds"], prompts=src_repos["prompts"],
        crypto=crypto,
    )
    doc = asyncio.run(svc_src.create_snapshot(TENANT_A))

    # Fresh destination box with a different master key
    dst_crypto = CryptoService("dst-box-key")
    dst_db, _, dst_repos = _build(dst_p)
    svc_dst = BackupService(
        sessions=dst_repos["sessions"], rules=dst_repos["rules"], filters=dst_repos["filters"],
        ai_configs=dst_repos["ai"], credentials=dst_repos["creds"], prompts=dst_repos["prompts"],
        crypto=dst_crypto,
    )
    stats = asyncio.run(svc_dst.restore_snapshot(doc, target_owner_user_id=TENANT_B))
    assert stats["prompt_templates"]["imported"] == 1

    seen_b = asyncio.run(dst_repos["prompts"].list_templates_by_owner(TENANT_B))
    imported = [t for t in seen_b if t.id == "pt-a"]
    assert imported and imported[0].owner_user_id == TENANT_B, \
        f"imported prompt kept source owner: {imported[0].owner_user_id}"


def test_credential_restore_rebinds_to_target_owner(tmp_path):
    src_p = str(tmp_path / "src.db")
    dst_p = str(tmp_path / "dst.db")
    _, crypto, src_repos = _seed_two_tenants(src_p)
    svc_src = BackupService(
        sessions=src_repos["sessions"], rules=src_repos["rules"], filters=src_repos["filters"],
        ai_configs=src_repos["ai"], credentials=src_repos["creds"], prompts=src_repos["prompts"],
        crypto=crypto,
    )
    doc = asyncio.run(svc_src.create_snapshot(TENANT_A))

    dst_crypto = CryptoService("dst-box-key")
    dst_db, _, dst_repos = _build(dst_p)
    svc_dst = BackupService(
        sessions=dst_repos["sessions"], rules=dst_repos["rules"], filters=dst_repos["filters"],
        ai_configs=dst_repos["ai"], credentials=dst_repos["creds"], prompts=dst_repos["prompts"],
        crypto=dst_crypto,
    )
    stats = asyncio.run(svc_dst.restore_snapshot(doc, target_owner_user_id=TENANT_B))
    assert stats["api_credentials"]["imported"] == 1
    seen_b = asyncio.run(dst_repos["creds"].list_by_owner(TENANT_B))
    assert any(c.id == "cred-a" for c in seen_b)
    assert all(c.api_hash == "HASH-A" for c in seen_b), "secret survived the master_key change"


# --------------------------------------------------------------- DLQ / stats

def _seed_dlq(path, rule_id, owner):
    """Insert a dead-letter row + stats owned by `owner` through `rule_id`."""
    db, crypto, repos = _seed_two_tenants(path)
    # attach the rule to the right owner
    import time as _t
    db.execute(
        "INSERT OR REPLACE INTO dead_letter_queue "
        "(id, job_id, rule_id, source_chat_id, source_message_id, target_chat_id,"
        " payload_json, attempts, last_error, error_category, dead_lettered_at)"
        " VALUES (?,?,?,?,?,?, '{}', 1, 'boom', 'telegram', ?)",
        (f"dlq-{rule_id}", f"job-{rule_id}", rule_id, "-1", 1, "-2", int(_t.time())),
    )
    db.execute(
        "INSERT OR REPLACE INTO rule_stats (rule_id, forwarded, filtered, errors, last_forward_ts)"
        " VALUES (?, 5, 2, 1, 0)",
        (rule_id,),
    )
    db.execute(
        "INSERT OR REPLACE INTO error_log (ts, rule_id, category, error_name, severity, detail, chat_id)"
        " VALUES (?, ?, 'telegram', 'boom', 'ERROR', 'tenant-specific detail', '')",
        (int(_t.time()), rule_id),
    )
    return db, crypto, repos


def test_delivery_stats_scoped_by_owner(tmp_path):
    """GetDeliveryStats with an owner must hide another tenant's rules/errors."""
    from core.infrastructure.grpc_server.servicers import DeliveryControlServicer
    from core.proto import autoforward_pb2 as pb

    p = str(tmp_path / "iso.db")
    db, crypto, repos = _seed_dlq(p, "rule-a", TENANT_A)
    # Give the servicer the repos it needs
    servicer = DeliveryControlServicer(db, repos["rules"])

    # Tenant B asks for stats: must not see any of A's rules or error text
    resp = asyncio.run(servicer.GetDeliveryStats(
        pb.DeliveryStatsRequest(owner_user_id=TENANT_B), None))
    assert all(r.rule_id != "rule-a" for r in resp.rules), "tenant B saw tenant A's rule stats"
    assert all(e.rule_id != "rule-a" for e in resp.errors), "tenant B saw tenant A's error text"

    # Tenant A sees its own
    resp_a = asyncio.run(servicer.GetDeliveryStats(
        pb.DeliveryStatsRequest(owner_user_id=TENANT_A), None))
    assert any(r.rule_id == "rule-a" for r in resp_a.rules)
    assert any(e.rule_id == "rule-a" for e in resp_a.errors)


def test_dlq_list_scoped_by_owner(tmp_path):
    from core.infrastructure.grpc_server.servicers import DeliveryControlServicer
    from core.proto import autoforward_pb2 as pb

    p = str(tmp_path / "iso.db")
    db, crypto, repos = _seed_dlq(p, "rule-a", TENANT_A)
    servicer = DeliveryControlServicer(db, repos["rules"])

    resp_b = asyncio.run(servicer.ListDeadLetter(
        pb.ListDeadLetterRequest(owner_user_id=TENANT_B), None))
    assert list(resp_b.entries) == [], "tenant B saw tenant A's dead-letter row"

    resp_a = asyncio.run(servicer.ListDeadLetter(
        pb.ListDeadLetterRequest(owner_user_id=TENANT_A), None))
    assert any(e.rule_id == "rule-a" for e in resp_a.entries)


def test_dlq_replay_denied_for_other_owner(tmp_path):
    from core.infrastructure.grpc_server.servicers import DeliveryControlServicer
    from core.proto import autoforward_pb2 as pb

    p = str(tmp_path / "iso.db")
    db, crypto, repos = _seed_dlq(p, "rule-a", TENANT_A)
    servicer = DeliveryControlServicer(db, repos["rules"])

    resp = asyncio.run(servicer.ReplayDeadLetter(
        pb.ReplayDeadLetterRequest(id="dlq-rule-a", owner_user_id=TENANT_B), None))
    assert not resp.success, "tenant B replayed tenant A's dead-letter job"
    # The entry must still be present (not consumed)
    rows = db.query_all("SELECT id FROM dead_letter_queue WHERE id=?", ("dlq-rule-a",))
    assert len(rows) == 1

    # Tenant A can replay its own
    resp_a = asyncio.run(servicer.ReplayDeadLetter(
        pb.ReplayDeadLetterRequest(id="dlq-rule-a", owner_user_id=TENANT_A), None))
    assert resp_a.success


def test_dlq_purge_denied_for_other_owner(tmp_path):
    from core.infrastructure.grpc_server.servicers import DeliveryControlServicer
    from core.proto import autoforward_pb2 as pb

    p = str(tmp_path / "iso.db")
    db, crypto, repos = _seed_dlq(p, "rule-a", TENANT_A)
    servicer = DeliveryControlServicer(db, repos["rules"])

    asyncio.run(servicer.PurgeDeadLetter(
        pb.PurgeDeadLetterRequest(owner_user_id=TENANT_B), None))
    rows = db.query_all("SELECT id FROM dead_letter_queue WHERE id=?", ("dlq-rule-a",))
    assert len(rows) == 1, "tenant B purged tenant A's dead-letter queue"

    asyncio.run(servicer.PurgeDeadLetter(
        pb.PurgeDeadLetterRequest(owner_user_id=TENANT_A), None))
    rows = db.query_all("SELECT id FROM dead_letter_queue WHERE id=?", ("dlq-rule-a",))
    assert len(rows) == 0


# ------------------------------------------------- corrupted / hostile input

def test_restore_rejects_malformed_bundle(tmp_path):
    p = str(tmp_path / "iso.db")
    _, crypto, repos = _seed_two_tenants(p)
    svc = BackupService(
        sessions=repos["sessions"], rules=repos["rules"], filters=repos["filters"],
        ai_configs=repos["ai"], credentials=repos["creds"], prompts=repos["prompts"],
        crypto=crypto,
    )
    # An unknown format must raise, not silently import garbage.
    with pytest.raises(ValueError):
        asyncio.run(svc.restore_snapshot({"format": "unknown", "entities": {}}))


def test_restore_empty_bundle_is_safe(tmp_path):
    p = str(tmp_path / "iso.db")
    _, crypto, repos = _seed_two_tenants(p)
    svc = BackupService(
        sessions=repos["sessions"], rules=repos["rules"], filters=repos["filters"],
        ai_configs=repos["ai"], credentials=repos["creds"], prompts=repos["prompts"],
        crypto=crypto,
    )
    bundle = serialize_bundle({"format": 2, "exported_at": 1, "source_owner_user_id": None, "entities": {}})
    stats = asyncio.run(svc.restore_snapshot(deserialize_bundle(bundle)))
    assert all(v["imported"] == 0 for v in stats.values())


def test_get_template_preserves_owner(tmp_path):
    """Regression: get_template() rebuilt the entity by hand and dropped
    owner_user_id, so the DeletePromptTemplate ownership guard in
    servicers.py compared against 0 and let tenant B delete tenant A's prompt.
    """
    db = SqliteDatabase(str(tmp_path / "iso.db"))
    repo = SqlitePromptRepository(db)
    t = PromptTemplate(
        id="reg-owner", name="reg", system_prompt="s", user_prompt_template="{text}",
        owner_user_id=TENANT_A, is_system=False, created_at=1, updated_at=1,
    )
    asyncio.run(repo.add_template(t))
    got = asyncio.run(repo.get_template("reg-owner"))
    assert got is not None
    assert got.owner_user_id == TENANT_A, "get_template lost owner_user_id"


def test_delete_prompt_denied_cross_tenant(tmp_path):
    """Same regression at the service boundary: tenant B must not be able to
    delete a template owned by tenant A through DeletePromptTemplate."""
    from core.infrastructure.grpc_server.servicers import AIControlServicer
    from core.proto import autoforward_pb2 as pb

    db = SqliteDatabase(str(tmp_path / "iso.db"))
    prompts = SqlitePromptRepository(db)
    svc = AIControlServicer(None, PromptService(prompts))
    t = PromptTemplate(
        id="reg-del", name="reg", system_prompt="s", user_prompt_template="{text}",
        owner_user_id=TENANT_A, is_system=False, created_at=1, updated_at=1,
    )
    asyncio.run(prompts.add_template(t))
    res = asyncio.run(svc.DeletePromptTemplate(
        pb.DeletePromptTemplateRequest(id="reg-del", owner_user_id=TENANT_B), None))
    assert res.success is False
    still = asyncio.run(prompts.get_template("reg-del"))
    assert still is not None and still.owner_user_id == TENANT_A


def test_legacy_ownership_reconciliation_single_tenant(tmp_path):
    """Regression: on a real single-tenant install the pre-v12 credential and
    prompt rows carried owner_user_id=0, so the new owner-scoped queries hid
    the owner's own data from them. With exactly one genuine account the legacy
    rows must be rebound to that owner."""
    p = str(tmp_path / "recon.db")
    db = SqliteDatabase(p)
    crypto = CryptoService("recon-key")
    creds = SqliteApiCredentialRepository(db, crypto)
    prompts = SqlitePromptRepository(db)
    cred = ApiCredential(
        id="legacy-cred", label="legacy", api_id=1, api_hash="hash-secret",
        proxy="", is_default=True, created_at=1, updated_at=1,
    )
    asyncio.run(creds.add(cred))
    # add exactly one genuine human account
    db.execute(
        "INSERT INTO web_accounts (user_id, username, password_hash, salt, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (777, "the-owner", "x", "s", 1, 1),
    )
    # re-open so the additive + reconciliation path runs again
    db.close()
    db2 = SqliteDatabase(p)
    got = asyncio.run(SqliteApiCredentialRepository(db2, crypto).get_default_by_owner(777))
    assert got is not None, "legacy credential was not rebound to the sole owner"
    assert got.id == "legacy-cred"
    db2.close()


def test_legacy_ownership_reconciliation_multi_tenant_is_noop(tmp_path):
    """With several real accounts the heuristic must stay neutral: legacy rows
    remain in the shared system scope (owner 0) and no tenant silently inherits
    the others' credentials."""
    p = str(tmp_path / "recon2.db")
    db = SqliteDatabase(p)
    crypto = CryptoService("recon-key")
    creds = SqliteApiCredentialRepository(db, crypto)
    asyncio.run(creds.add(ApiCredential(
        id="legacy-shared", label="legacy", api_id=2, api_hash="shared-secret",
        proxy="", is_default=True, created_at=1, updated_at=1,
    )))
    db.execute(
        "INSERT INTO web_accounts (user_id, username, password_hash, salt, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (111, "a", "x", "s", 1, 1),
    )
    db.execute(
        "INSERT INTO web_accounts (user_id, username, password_hash, salt, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (222, "b", "x", "s", 1, 1),
    )
    db.close()
    db2 = SqliteDatabase(p)
    # The legacy row must still be in the shared scope, not bound to tenant 111.
    row = db2.query_one("SELECT owner_user_id FROM api_credentials WHERE id = ?", ("legacy-shared",))
    assert row is not None and int(row["owner_user_id"]) == 0, (
        "multi-tenant install silently rebound shared credentials to one tenant"
    )
    db2.close()


def test_bot_created_credential_is_stamped(tmp_path):
    """Regression: ApiCredentialUseCases.create did not accept owner_user_id,
    so a credential added through the bot's guided flow landed in the shared
    system scope (owner 0) and was invisible to the owner-scoped export."""
    db = SqliteDatabase(str(tmp_path / "stamp.db"))
    repo = SqliteApiCredentialRepository(db, CryptoService("stamp-key"))
    uc = ApiCredentialUseCases(repo)
    cred = asyncio.run(uc.create(
        label="via-bot", api_id=7, api_hash="h", is_default=True,
        owner_user_id=TENANT_A,
    ))
    assert cred.owner_user_id == TENANT_A
    mine = asyncio.run(repo.list_by_owner(TENANT_A))
    assert [c.id for c in mine] == [cred.id]
    other = asyncio.run(repo.list_by_owner(TENANT_B))
    assert other == [], "tenant B saw tenant A's bot-created credential"


def test_use_case_owned_mutations_deny_cross_tenant(tmp_path):
    """v12.1: the Telegram bot resolves entities by callback id, so IDOR
    guards must live at the use-case layer — one enforcement point for every
    caller, not only the HTTP/gRPC boundary."""
    import time
    from core.application.use_cases import (
        ForwardRuleUseCases, FilterRuleUseCases, AIConfigUseCases)
    from core.domain.value_objects import RoutingType, ForwardMode
    from core.infrastructure.persistence.sqlite_repositories import (
        SqliteFilterRuleRepository, SqliteAIConfigRepository)

    db = SqliteDatabase(str(tmp_path / "uc.db"))
    from core.domain.entities import TelegramSession
    asyncio.run(SqliteSessionRepository(db, CryptoService("uc-key")).add(TelegramSession(
        id="s", phone_number="+1", session_string_encrypted="x",
        is_active=True, is_authorized=True, owner_user_id=TENANT_A)))
    rules_uc = ForwardRuleUseCases(SqliteForwardRuleRepository(db))
    filt_uc = FilterRuleUseCases(SqliteFilterRuleRepository(db))
    ai_uc = AIConfigUseCases(SqliteAIConfigRepository(db, CryptoService("uc-key")), None)

    rule = asyncio.run(rules_uc.create(ForwardRule(
        id="uc-rule", session_id="s", name="n", source_chat_id="-1", target_chat_id="-2",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL, forward_mode=ForwardMode.COPY_MESSAGE,
        owner_user_id=TENANT_A, created_at=1, updated_at=1)))
    filt = asyncio.run(filt_uc.create(FilterRule(id="uc-f", name="f", owner_user_id=TENANT_A)))
    cfg = asyncio.run(ai_uc.create(AIConfig(id="uc-c", name="c", owner_user_id=TENANT_A)))

    # reads
    assert asyncio.run(rules_uc.get_owned("uc-rule", TENANT_A)) is not None
    assert asyncio.run(rules_uc.get_owned("uc-rule", TENANT_B)) is None
    assert asyncio.run(filt_uc.get_owned("uc-f", TENANT_B)) is None
    assert asyncio.run(ai_uc.get_owned("uc-c", TENANT_B)) is None
    # admin path (None) still resolves
    assert asyncio.run(rules_uc.get_owned("uc-rule", None)) is not None

    # deletes
    with pytest.raises(PermissionError):
        asyncio.run(rules_uc.delete_owned("uc-rule", TENANT_B))
    with pytest.raises(PermissionError):
        asyncio.run(filt_uc.delete_owned("uc-f", TENANT_B))
    with pytest.raises(PermissionError):
        asyncio.run(ai_uc.delete_owned("uc-c", TENANT_B))
    # victims intact
    assert asyncio.run(rules_uc.get_owned("uc-rule", TENANT_A)) is not None
    assert asyncio.run(filt_uc.get_owned("uc-f", TENANT_A)) is not None
    assert asyncio.run(ai_uc.get_owned("uc-c", TENANT_A)) is not None

    # legitimate owner deletes still work
    assert asyncio.run(rules_uc.delete_owned("uc-rule", TENANT_A)) is True
    assert asyncio.run(filt_uc.delete_owned("uc-f", TENANT_A)) is True
    assert asyncio.run(ai_uc.delete_owned("uc-c", TENANT_A)) is True


def test_ai_and_credential_guards_deny_cross_tenant(tmp_path):
    """v12.1: AI execution paths and credential mutation must refuse a forged
    id belonging to another tenant. `health_check`/`test_rewrite` consume
    another tenant's API key, and `set_default` flips shared default state."""
    from core.application.use_cases import (
        AIConfigUseCases, ApiCredentialUseCases)
    from core.application.repositories import ApiCredential
    from core.infrastructure.persistence.sqlite_repositories import (
        SqliteAIConfigRepository, SqliteApiCredentialRepository)

    db = SqliteDatabase(str(tmp_path / "guard.db"))
    ai_uc = AIConfigUseCases(SqliteAIConfigRepository(db, CryptoService("k")), None)
    cred_uc = ApiCredentialUseCases(SqliteApiCredentialRepository(db, CryptoService("k")))

    cfg = asyncio.run(ai_uc.create(AIConfig(id="g-cfg", name="g",
                                            owner_user_id=TENANT_A)))
    cred = asyncio.run(cred_uc.create("g", 1, "hash", owner_user_id=TENANT_A))

    # AI reads + execution
    assert asyncio.run(ai_uc.get_owned("g-cfg", TENANT_B)) is None
    assert asyncio.run(ai_uc.get_owned("g-cfg", TENANT_A)) is not None
    assert asyncio.run(ai_uc.health_check_owned("g-cfg", TENANT_B)) == \
        (False, "config_not_found", 0.0)
    assert asyncio.run(ai_uc.test_rewrite_owned("g-cfg", "hi", TENANT_B)) == \
        (False, "config_not_found")
    with pytest.raises(PermissionError):
        asyncio.run(ai_uc.update_owned(
            AIConfig(id="g-cfg", owner_user_id=TENANT_B), TENANT_B))

    # credential mutations
    assert asyncio.run(cred_uc.get_owned(cred.id, TENANT_B)) is None
    with pytest.raises(PermissionError):
        asyncio.run(cred_uc.set_default_owned(cred.id, TENANT_B))
    with pytest.raises(PermissionError):
        asyncio.run(cred_uc.delete_owned(cred.id, TENANT_B))

    # owner still works; admin scope (None) preserved
    assert asyncio.run(cred_uc.set_default_owned(cred.id, None)) is not None
    assert asyncio.run(cred_uc.set_default_owned(cred.id, TENANT_A)) is not None
    assert asyncio.run(cred_uc.get_owned(cred.id, None)) is not None


def test_bot_scope_uid_maps_admin_and_tenant():
    """v12.1: the bot's owner-scope helper must keep the legacy global scope
    for admins (None) and an explicit tenant id otherwise, so single-tenant
    installs keep working while multi-tenant reads stay scoped."""
    # imported at module level: pyrogram grabs the event loop on import,
    # which raises under pytest's asyncio mode when done lazily here.
    ui = ProBotUI.__new__(ProBotUI)
    ui._admin_ids = set()
    assert ui._scope_uid(5094837833) is None

    # explicit admin list => admin keeps global scope, others are scoped
    ui._admin_ids = {5094837833}
    assert ui._scope_uid(5094837833) is None
    assert ui._scope_uid(999001) == 999001


# ---------------------------------------------------------- v12.2 servicers
# The gRPC layer must refuse by-id mutations and secret reads when the caller
# is not the owner. This is the enforcement point the HTTP layer cannot
# provide on its own (list-then-filter is client-side filtering).


def _servicers(path):
    """Build real servicers over a two-tenant sqlite db."""
    from core.infrastructure.grpc_server import servicers as S
    from core.application.use_cases import (
        SessionUseCases, ForwardRuleUseCases, FilterRuleUseCases, AIConfigUseCases,
    )
    db, crypto, repos = _seed_two_tenants(path)
    sess_uc = SessionUseCases(repos["sessions"])
    rules_uc = ForwardRuleUseCases(repos["rules"])
    filters_uc = FilterRuleUseCases(repos["filters"])
    ai_uc = AIConfigUseCases(repos["ai"], provider_factory=None)
    rs = S.ForwardRuleControlServicer(rules_uc)
    fs = S.FilterControlServicer(filters_uc)
    ais = S.AIControlServicer(ai_uc)
    ss = S.SessionControlServicer(sess_uc, pool=None)
    return db, repos, rs, fs, ais, ss


class _Ctx:
    """Minimal stand-in for a grpc.ServicerContext (no network needed)."""

    async def abort(self, code, details):
        raise RuntimeError(f"aborted: {code} {details}")


def test_grpc_delete_rule_denied_cross_tenant(tmp_path):
    db, repos, rs, *_ = _servicers(tmp_path / "s1.db")
    # Tenant B tries to delete A's rule
    resp = asyncio.run(rs.DeleteRule(
        pb.DeleteRuleRequest(id="rule-a", owner_user_id=TENANT_B), _Ctx()))
    assert not resp.success
    # rule still exists
    assert asyncio.run(repos["rules"].get_by_id("rule-a")) is not None
    # owner can delete
    resp = asyncio.run(rs.DeleteRule(
        pb.DeleteRuleRequest(id="rule-a", owner_user_id=TENANT_A), _Ctx()))
    assert resp.success
    db.close()


def test_grpc_pause_resume_denied_cross_tenant(tmp_path):
    _, repos, rs, *_ = _servicers(tmp_path / "s2.db")
    r = asyncio.run(rs.PauseRule(
        pb.PauseRuleRequest(rule_id="rule-a", owner_user_id=TENANT_B), _Ctx()))
    assert not r.success
    rule = asyncio.run(repos["rules"].get_by_id("rule-a"))
    assert not rule.is_paused
    # owner pause works
    r = asyncio.run(rs.PauseRule(
        pb.PauseRuleRequest(rule_id="rule-a", owner_user_id=TENANT_A), _Ctx()))
    assert r.success
    assert asyncio.run(repos["rules"].get_by_id("rule-a")).is_paused
    # owner resume works
    r = asyncio.run(rs.ResumeRule(
        pb.PauseRuleRequest(rule_id="rule-a", owner_user_id=TENANT_A), _Ctx()))
    assert r.success
    assert not asyncio.run(repos["rules"].get_by_id("rule-a")).is_paused


def test_grpc_update_rule_denied_cross_tenant(tmp_path):
    db, repos, rs, *_ = _servicers(tmp_path / "s3.db")
    # Tenant B tries to hijack A's rule. Use a persisted field
    # (target_chat_id) since rule name/description are not schema columns.
    foreign = pb.ForwardRule(id="rule-a", target_chat_id="-999", owner_user_id=TENANT_B)
    out = asyncio.run(rs.UpdateRule(pb.UpdateRuleRequest(rule=foreign), _Ctx()))
    # returns empty rule (denied), DB untouched
    assert out == pb.ForwardRule()
    assert asyncio.run(repos["rules"].get_by_id("rule-a")).target_chat_id == "-2"
    # owner update works
    own = pb.ForwardRule(id="rule-a", target_chat_id="-777", owner_user_id=TENANT_A)
    out = asyncio.run(rs.UpdateRule(pb.UpdateRuleRequest(rule=own), _Ctx()))
    assert out.target_chat_id == "-777"
    db.close()


def test_grpc_backup_session_denied_cross_tenant(tmp_path):
    _, repos, _, _, _, ss = _servicers(tmp_path / "s4.db")
    # B asks for A's session string
    r = asyncio.run(ss.BackupSession(
        pb.BackupSessionRequest(session_id="sess-a", owner_user_id=TENANT_B), _Ctx()))
    assert not r.success
    assert r.encrypted_session_data == ""
    # owner gets it
    r = asyncio.run(ss.BackupSession(
        pb.BackupSessionRequest(session_id="sess-a", owner_user_id=TENANT_A), _Ctx()))
    assert r.success
    assert r.encrypted_session_data == "str-a"


def test_grpc_terminate_session_denied_cross_tenant(tmp_path):
    _, repos, _, _, _, ss = _servicers(tmp_path / "s5.db")
    r = asyncio.run(ss.TerminateSession(
        pb.TerminateSessionRequest(session_id="sess-a", owner_user_id=TENANT_B), _Ctx()))
    assert not r.success
    assert r.message != "terminated"
    # session survived
    assert asyncio.run(repos["sessions"].get_by_id("sess-a")) is not None
    # owner terminates
    r = asyncio.run(ss.TerminateSession(
        pb.TerminateSessionRequest(session_id="sess-a", owner_user_id=TENANT_A), _Ctx()))
    assert r.success and r.message == "terminated"
    assert asyncio.run(repos["sessions"].get_by_id("sess-a")) is None


def test_grpc_test_rule_denied_cross_tenant(tmp_path):
    _, repos, rs, *_ = _servicers(tmp_path / "s6.db")
    r = asyncio.run(rs.TestRule(
        pb.TestRuleRequest(rule_id="rule-a", sample_text="hello",
                           owner_user_id=TENANT_B), _Ctx()))
    assert not r.matched
    assert r.match_reason == "rule_not_found"
    # owner can dry-run (still may not match, but the rule must be loaded)
    r = asyncio.run(rs.TestRule(
        pb.TestRuleRequest(rule_id="rule-a", sample_text="hello",
                           owner_user_id=TENANT_A), _Ctx()))
    assert r.match_reason != "rule_not_found"


def test_first_run_without_session_renders_onboarding():
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI, CB
    from core.infrastructure.telegram.i18n import I18n

    ui = ProBotUI.__new__(ProBotUI)
    ui.i18n = I18n()
    ui.i18n.current = "en"
    ui._web_url = "https://dashboard.example.com"

    fake_user = MagicMock()
    fake_user.first_name = "Alice"
    fake_user.last_name = ""
    fake_user.username = "alice_test"

    text, kbd = ui._render_welcome_onboarding(111222, user=fake_user)
    assert "Alice" in text
    assert "Connect Now" in text or "connect" in text
    assert "Scott" not in text
    assert "5989354154" not in text
    assert "+989" not in text

    buttons = [btn for row in kbd.inline_keyboard for btn in row]
    btn_texts = [b.text for b in buttons]
    assert any("Connect Now" in t for t in btn_texts)
    assert not any("Get App" in t for t in btn_texts)
    assert any("Support" in t for t in btn_texts)


def test_pv_responder_multi_tenant_isolation(tmp_path, monkeypatch):
    from core.infrastructure.telegram.pv_responder import AIPVResponder
    import core.infrastructure.telegram.pv_responder as pvr_mod
    monkeypatch.setattr(pvr_mod, "CONFIG_PATH", tmp_path / "global_pv.json")

    responder = AIPVResponder()
    cfg_a = responder.get_config_for_user(TENANT_A)
    assert cfg_a.enabled is False

    cfg_a.enabled = True
    cfg_a.persona_prompt = "Prompt for Tenant A"
    responder.save_config_for_user(TENANT_A, cfg_a)

    cfg_b = responder.get_config_for_user(TENANT_B)
    assert cfg_b.enabled is False
    assert cfg_b.persona_prompt != "Prompt for Tenant A"

    responder_new = AIPVResponder()
    reloaded_a = responder_new.get_config_for_user(TENANT_A)
    assert reloaded_a.enabled is True
    assert reloaded_a.persona_prompt == "Prompt for Tenant A"

    reloaded_b = responder_new.get_config_for_user(TENANT_B)
    assert reloaded_b.enabled is False


@pytest.mark.asyncio
async def test_dashboard_scoped_self_only_multi_lang():
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI
    from core.infrastructure.telegram.i18n import I18n
    from core.infrastructure.telegram.pv_responder import AIPVResponder

    ui = ProBotUI.__new__(ProBotUI)
    ui.i18n = I18n()
    ui._admin_ids = {TENANT_A}
    ui._accounts = None
    ui._metrics = None
    ui._rule_stats = None
    ui._users = None
    ui._web_url = ""

    ui._sessions = AsyncMock()
    ui._sessions.list_by_owner = AsyncMock(return_value=[])

    ui._rules = AsyncMock()
    ui._rules.list_by_owner = AsyncMock(return_value=[])

    ui._filters = AsyncMock()
    ui._filters.list_by_owner = AsyncMock(return_value=[])

    ui._ai = AsyncMock()
    ui._ai.list_by_owner = AsyncMock(return_value=[])

    ui._pv_responder = AIPVResponder()

    fake_b = MagicMock()
    fake_b.first_name = "Bob"
    fake_b.last_name = "Builder"
    fake_b.username = "bob_b"

    # Language is resolved per-user from the user's own Telegram client
    # language_code — never from the process-wide i18n.current, which is
    # shared across all tenants.
    def client(lang: str) -> MagicMock:
        u = MagicMock()
        u.first_name = "Bob"
        u.last_name = "Builder"
        u.username = "bob_b"
        u.language_code = lang
        return u

    # In English
    text_en = await ui._main_text(TENANT_B, user=client("en"))
    assert "Bob Builder" in text_en
    assert str(TENANT_B) in text_en
    assert "Connected phone: `None`" in text_en
    assert "Currently active tasks: **0**" in text_en
    assert "Scott" not in text_en
    assert "+989" not in text_en

    # In Persian
    text_fa = await ui._main_text(TENANT_B, user=client("fa"))
    assert "Bob Builder" in text_fa
    assert "شماره متصل: `None`" in text_fa
    assert "Scott" not in text_fa

    # In Russian
    text_ru = await ui._main_text(TENANT_B, user=client("ru"))
    assert "Bob Builder" in text_ru
    assert "Подключенный номер: `None`" in text_ru

    # In Chinese
    text_zh = await ui._main_text(TENANT_B, user=client("zh"))
    assert "Bob Builder" in text_zh
    assert "已绑手机：`None`" in text_zh
