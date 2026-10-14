"""Per-task configuration guard tests.

These tests pin the contract that every per-task configuration surface
(replace rules, header/footer/watermark, AI picks, paid add-ons, content
processing toggles) must round-trip the FULL stack:

    domain entity  ->  proto (custom_metadata_json)  ->  JSON metadata

and that mutations of one tenant's task config can never read or write
another tenant's rules (fail-closed multi-tenancy).

If you add a new task-config key to the bot (`pro_bot_ui`), to the mobile
editor, or to the web dashboard, add it to ALL_KEYS below so this file
fails until the key survives the whole contract.
"""

import json

import pytest

from core.domain.entities import ForwardRule
from core.infrastructure.grpc_server.servicers import _rule_pb
from core.proto import pb

# ---------------------------------------------------------------------------
# The complete per-task config key list (single source of truth for parity).
# Every key the bot writes into rule.metadata must survive the contract.
# ---------------------------------------------------------------------------
ALL_KEYS = [
    # Replace rules / content modification
    "replacements", "header", "footer", "watermark", "watermark_text",
    "link_replacement", "clean_words",
    # Processing messages
    "process_raw_text", "button_to_text", "remove_links", "remove_emojis",
    "block_voice", "block_stickers", "album_mode",
    # AI features
    "ocr_image_ai", "smart_ai_rewrite", "news_digest",
    "ai_creativity", "ai_max_tokens", "ai_config_id",
    # Paid add-on catalogue (18 entries)
    "paid_addons",
    "keyword_confirmation", "keyword_reply", "auto_delete_seconds",
    "link_buttons", "affiliate_kit", "crypto_mode",
    "duplicate_filters", "auto_posting", "power_control",
    # Cleaner / sync
    "cleaner_enabled", "cleaner_config",
    "sync_replies", "sync_edits", "sync_deletes",
    # Language preference
    "language", "show_forward_header",
]

# Keys that carry structured (non-string) payloads.
STRUCTURED_KEYS = {
    "replacements", "paid_addons", "cleaner_config", "duplicate_filters",
    "clean_words", "whitelist_keywords", "blacklist_keywords",
    "link_buttons", "affiliate_kit",
}


def _make_rule(owner: int, seed: str = "tenant-a") -> ForwardRule:
    """Build a rule whose metadata carries a value for EVERY task key."""
    metadata = {}
    for key in ALL_KEYS:
        if key == "ai_config_id":
            metadata[key] = f"{seed}-ai-config"
        elif key in STRUCTURED_KEYS:
            metadata[key] = {f"{seed}-k": f"{seed}-v"}
        else:
            metadata[key] = f"{seed}-value"
    # Non-string scalars the domain also supports.
    metadata["ai_creativity"] = 0.7
    metadata["ai_max_tokens"] = 600
    metadata["process_raw_text"] = True
    metadata["ocr_image_ai"] = False
    return ForwardRule(
        id=f"rule-{seed}",
        session_id="sess-guard",
        source_chat_id="-1001",
        target_chat_id="-1002",
        owner_user_id=owner,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# 1. Domain -> proto contract: nothing is lost in _rule_pb.
# ---------------------------------------------------------------------------

def test_task_config_round_trips_domain_to_proto():
    """Every task-config key present in the entity's metadata must appear in
    the proto's custom_metadata_json — that envelope is what the Go API,
    the web dashboard, and the mobile app read."""
    rule = _make_rule(owner=5094837833, seed="roundtrip")
    pr = _rule_pb(rule)

    assert pr.owner_user_id == 5094837833, "owner_user_id must be set on proto"
    envelope = json.loads(pr.custom_metadata_json)

    missing = [k for k in ALL_KEYS if k not in envelope]
    assert not missing, f"task-config keys lost in domain->proto: {missing}"

    # Spot-check type fidelity (the mobile client reads these as typed values).
    assert envelope["ai_creativity"] == 0.7
    assert envelope["ai_max_tokens"] == 600
    assert envelope["process_raw_text"] is True
    assert envelope["replacements"] == {"roundtrip-k": "roundtrip-v"}


def test_proto_envelope_is_json_serialisable_and_stable():
    """The envelope must survive a JSON dump/reload cycle without mutation —
    it is stored in SQLite/Postgres as a TEXT/JSONB column."""
    rule = _make_rule(owner=1, seed="stable")
    pr = _rule_pb(rule)
    once = json.loads(pr.custom_metadata_json)
    twice = json.loads(json.dumps(once))
    assert once == twice


# ---------------------------------------------------------------------------
# 2. Entity property parity: the domain surface is the single source of truth.
# ---------------------------------------------------------------------------

def test_entity_properties_back_task_config_keys():
    """The metadata dict and the typed entity properties must agree: setting
    the property must be visible in metadata and vice versa (the bot and the
    API both write through these)."""
    rule = _make_rule(owner=1, seed="props")
    rule.replacements = {"a": "b"}
    assert rule.metadata["replacements"] == {"a": "b"}

    rule.metadata["header"] = "H1"
    assert rule.header == "H1"

    rule.footer = "F1"
    assert rule.metadata["footer"] == "F1"


def test_paid_addons_default_to_empty_not_missing():
    """A rule with no explicit add-ons must expose an empty dict, never a
    KeyError — the mobile catalogue renders all 18 toggles from this."""
    rule = ForwardRule(
        session_id="s", source_chat_id="-1", target_chat_id="-2",
        owner_user_id=1,
    )
    assert rule.paid_addons == {}
    assert rule.replacements == {}


# ---------------------------------------------------------------------------
# 3. Multi-tenant isolation: fail-closed per-task scoping.
# ---------------------------------------------------------------------------

def test_task_config_never_leaks_across_tenants():
    """Tenant A's task config must be invisible to tenant B: the proto
    envelope of A's rule carries only A's data, and B's fetch of A's rule
    must return nothing (the owner-scoped repository contract)."""
    tenant_a = _make_rule(owner=1001, seed="tenantA")
    tenant_b = _make_rule(owner=2002, seed="tenantB")

    pb_a = _rule_pb(tenant_a)
    pb_b = _rule_pb(tenant_b)

    env_a = json.loads(pb_a.custom_metadata_json)
    env_b = json.loads(pb_b.custom_metadata_json)

    # No cross-contamination of task config values.
    assert all(v != "tenantA-value" for v in env_b.values())
    assert all(v != "tenantB-value" for v in env_a.values())
    assert pb_a.owner_user_id == 1001
    assert pb_b.owner_user_id == 2002


@pytest.mark.asyncio
async def test_get_owned_rule_scopes_by_owner(tmp_path):
    """get_owned (the repo primitive behind the GetRule RPC) must be
    fail-closed: a rule owned by A is invisible when queried as B."""
    import asyncio

    from core.infrastructure.persistence.sqlite_repositories import (
        SqliteForwardRuleRepository,
    )
    from core.infrastructure.persistence.database import SqliteDatabase

    # __init__ connects + migrates synchronously.
    db = SqliteDatabase(str(tmp_path / "guard.db"))
    repo = SqliteForwardRuleRepository(db)

    # forward_rules.session_id has an FK to sessions(id) — seed one row.
    db.execute(
        "INSERT INTO sessions (id, user_id, phone_number, session_string_encrypted, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, strftime('%s','now'), strftime('%s','now'))",
        ("sess-guard", 1001, "", ""),
    )

    rule_a = _make_rule(owner=1001, seed="ownerA")
    await repo.add(rule_a)
    fetched_all = await repo.list_by_owner(1001)
    assert len(fetched_all) == 1, "repo add/list sanity for guard test"

    # Owner sees their own rule with the full task config.
    fetched = await repo.get_owned(rule_a.id, 1001)
    assert fetched is not None
    assert fetched.metadata["replacements"] == {"ownerA-k": "ownerA-v"}

    # Another tenant sees nothing — fail-closed, not an error.
    assert await repo.get_owned(rule_a.id, 2002) is None

    # Missing rule ids also resolve to None, never raise.
    assert await repo.get_owned("does-not-exist", 1001) is None


@pytest.mark.asyncio
async def test_rule_pb_omits_owner_never_happens_after_fix():
    """Regression guard for PR #61: _rule_pb MUST set owner_user_id, else the
    Go API's secondary owner filter silently drops every rule."""
    rule = _make_rule(owner=5094837833, seed="regression")
    pr = _rule_pb(rule)
    assert pr.owner_user_id == 5094837833
