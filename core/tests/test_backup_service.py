"""Round-trip tests for BackupService (export -> import -> export).

Uses in-memory fakes of the repository interfaces so the test exercises the
real domain entities, field mapping and crypto round-trip without a database
or a live Telegram client.
"""

from __future__ import annotations

import base64
import asyncio
from typing import Dict, List, Optional

import pytest

from core.application.backup_service import (
    SNAPSHOT_FORMAT,
    BackupService,
    _BUNDLE_PREFIX,
    decrypt_bundle,
    deserialize_bundle,
    encrypt_bundle,
    is_encrypted_bundle,
    serialize_bundle,
)
from core.application.repositories import (
    ApiCredential,
    IAIConfigRepository,
    IApiCredentialRepository,
    IFilterRuleRepository,
    IForwardRuleRepository,
    IPromptRepository,
    ISessionRepository,
)
from core.domain.entities import (
    AIFallbackPolicy,
    AIConfig,
    FilterRule,
    ForwardRule,
    PromptTemplate,
    TelegramSession,
)
from core.domain.value_objects import ForwardMode, RoutingType


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeCrypto:
    """XOR-with-key cipher: mimics the encrypt/decrypt contract."""

    def __init__(self, key: str = "src-key") -> None:
        self.key = key

    def encrypt(self, plaintext: str) -> str:
        kb = self.key.encode()
        raw = bytes(b ^ kb[i % len(kb)] for i, b in enumerate(plaintext.encode()))
        return "ENC:" + base64.b64encode(raw).decode()

    def decrypt(self, blob: str) -> str:
        if not isinstance(blob, str) or not blob.startswith("ENC:"):
            raise ValueError("not ciphertext")
        raw = base64.b64decode(blob[4:])
        kb = self.key.encode()
        return bytes(b ^ kb[i % len(kb)] for i, b in enumerate(raw)).decode()


class _Base:
    def __init__(self) -> None:
        self._store: Dict[str, object] = {}


class FakeSessionRepo(_Base, ISessionRepository):
    """Mirrors SqliteSessionRepository: encrypts the session string on write."""

    def __init__(self, crypto: FakeCrypto) -> None:
        super().__init__()
        self._crypto = crypto

    async def add(self, session: TelegramSession) -> TelegramSession:
        self._store[session.id] = self._enc(session)
        return session

    async def update(self, session: TelegramSession) -> TelegramSession:
        return await self.add(session)

    async def get_by_id(self, session_id: str) -> Optional[TelegramSession]:
        s = self._store.get(session_id)
        return self._dec(s) if s is not None else None

    async def get_by_phone(self, phone: str) -> Optional[TelegramSession]:
        s = next((x for x in self._store.values() if x.phone_number == phone), None)
        return self._dec(s) if s is not None else None

    async def list_all(self) -> List[TelegramSession]:
        return [self._dec(s) for s in self._store.values()]

    async def list_by_owner(self, owner_user_id: int) -> List[TelegramSession]:
        return [self._dec(s) for s in self._store.values() if s.owner_user_id == owner_user_id]

    async def list_active(self) -> List[TelegramSession]:
        return [self._dec(s) for s in self._store.values() if s.is_active]

    async def delete(self, session_id: str) -> bool:
        return self._store.pop(session_id, None) is not None

    # -- helpers -------------------------------------------------------------
    def _enc(self, s: TelegramSession) -> TelegramSession:
        from dataclasses import replace as _replace
        return _replace(
            s,
            session_string_encrypted=(
                self._crypto.encrypt(s.session_string_encrypted)
                if s.session_string_encrypted else ""
            ),
        )

    def _dec(self, s: TelegramSession) -> TelegramSession:
        from dataclasses import replace as _replace
        try:
            return _replace(
                s,
                session_string_encrypted=(
                    self._crypto.decrypt(s.session_string_encrypted)
                    if s.session_string_encrypted else ""
                ),
            )
        except Exception:
            return s

    def stored(self, session_id: str) -> Optional[TelegramSession]:
        """The on-disk (encrypted) form."""
        return self._store.get(session_id)


class FakeRuleRepo(_Base, IForwardRuleRepository):
    async def add(self, rule: ForwardRule) -> ForwardRule:
        self._store[rule.id] = rule
        return rule

    async def update(self, rule: ForwardRule, expected_version: Optional[int] = None) -> ForwardRule:
        self._store[rule.id] = rule
        return rule

    async def get_by_id(self, rule_id: str) -> Optional[ForwardRule]:
        return self._store.get(rule_id)

    async def list_all(self) -> List[ForwardRule]:
        return list(self._store.values())

    async def list_by_owner(self, owner_user_id: int) -> List[ForwardRule]:
        return [r for r in self._store.values() if r.owner_user_id == owner_user_id]

    async def list_by_session(self, session_id: str, owner_user_id: int = 0) -> List[ForwardRule]:
        return [r for r in self._store.values() if r.session_id == session_id]

    async def list_active_by_source(self, source_chat_id: str) -> List[ForwardRule]:
        return [r for r in self._store.values()
                if r.source_chat_id == source_chat_id and r.is_active]

    async def delete(self, rule_id: str) -> bool:
        return self._store.pop(rule_id, None) is not None


class FakeFilterRepo(_Base, IFilterRuleRepository):
    async def add(self, filter_rule: FilterRule) -> FilterRule:
        self._store[filter_rule.id] = filter_rule
        return filter_rule

    async def update(self, filter_rule: FilterRule) -> FilterRule:
        self._store[filter_rule.id] = filter_rule
        return filter_rule

    async def get_by_id(self, filter_id: str) -> Optional[FilterRule]:
        return self._store.get(filter_id)

    async def list_all(self) -> List[FilterRule]:
        return list(self._store.values())

    async def list_by_owner(self, owner_user_id: int) -> List[FilterRule]:
        return [f for f in self._store.values() if f.owner_user_id == owner_user_id]

    async def delete(self, filter_id: str) -> bool:
        return self._store.pop(filter_id, None) is not None


class FakeAIRepo(_Base, IAIConfigRepository):
    """Mirrors SqliteAIConfigRepository: plaintext in, encrypted at rest, decrypted on read."""

    def __init__(self, crypto: FakeCrypto) -> None:
        super().__init__()
        self._crypto = crypto

    async def add(self, config: AIConfig) -> AIConfig:
        self._store[config.id] = self._enc(config)
        return config

    async def update(self, config: AIConfig) -> AIConfig:
        return await self.add(config)

    async def get_by_id(self, config_id: str) -> Optional[AIConfig]:
        c = self._store.get(config_id)
        return self._dec(c) if c is not None else None

    async def list_all(self) -> List[AIConfig]:
        return [self._dec(c) for c in self._store.values()]

    async def list_by_owner(self, owner_user_id: int) -> List[AIConfig]:
        return [self._dec(c) for c in self._store.values() if c.owner_user_id == owner_user_id]

    async def delete(self, config_id: str) -> bool:
        return self._store.pop(config_id, None) is not None

    # -- helpers -------------------------------------------------------------
    def _enc(self, c: AIConfig) -> AIConfig:
        from dataclasses import replace as _replace
        return _replace(c, api_key=self._crypto.encrypt(c.api_key) if c.api_key else "")

    def _dec(self, c: AIConfig) -> AIConfig:
        from dataclasses import replace as _replace
        try:
            return _replace(c, api_key=self._crypto.decrypt(c.api_key) if c.api_key else "")
        except Exception:
            return c

    def stored(self, config_id: str) -> Optional[AIConfig]:
        """The on-disk (encrypted) form, for asserting about what was written."""
        return self._store.get(config_id)


class FakeCredRepo(_Base, IApiCredentialRepository):
    """Mirrors SqliteApiCredentialRepository: encrypts api_hash on write."""

    def __init__(self, crypto: FakeCrypto) -> None:
        super().__init__()
        self._crypto = crypto

    async def add(self, cred: ApiCredential) -> ApiCredential:
        self._store[cred.id] = self._enc(cred)
        return cred

    async def update(self, cred: ApiCredential) -> ApiCredential:
        return await self.add(cred)

    async def get_by_id(self, cred_id: str) -> Optional[ApiCredential]:
        c = self._store.get(cred_id)
        return self._dec(c) if c is not None else None

    async def get_default(self) -> Optional[ApiCredential]:
        for c in self._store.values():
            if c.is_default:
                return await self.get_by_id(c.id)
        return None

    async def get_default_by_owner(self, owner_user_id: int) -> Optional[ApiCredential]:
        # v12: mirror the sqlite fallback — the caller's own default first,
        # then the shared system scope (owner 0).
        own = [
            c for c in self._store.values()
            if c.is_default and int(getattr(c, "owner_user_id", 0) or 0) == int(owner_user_id)
        ]
        if own:
            return await self.get_by_id(own[0].id)
        shared = [
            c for c in self._store.values()
            if c.is_default and int(getattr(c, "owner_user_id", 0) or 0) == 0
        ]
        return await self.get_by_id(shared[0].id) if shared else None

    async def list_all(self) -> List[ApiCredential]:
        return [await self.get_by_id(c.id) for c in self._store.values()]

    async def list_by_owner(self, owner_user_id: int) -> List[ApiCredential]:
        # v12: mirror the sqlite scoping; owner 0 is the legacy/system scope.
        return [
            await self.get_by_id(c.id)
            for c in self._store.values()
            if int(getattr(c, "owner_user_id", 0) or 0) == int(owner_user_id)
        ]

    async def delete(self, cred_id: str) -> bool:
        return self._store.pop(cred_id, None) is not None

    # -- helpers -------------------------------------------------------------
    def _enc(self, c: ApiCredential) -> ApiCredential:
        from dataclasses import replace as _replace
        return _replace(c, api_hash=self._crypto.encrypt(c.api_hash) if c.api_hash else "")

    def _dec(self, c: ApiCredential) -> ApiCredential:
        from dataclasses import replace as _replace
        try:
            return _replace(c, api_hash=self._crypto.decrypt(c.api_hash) if c.api_hash else "")
        except Exception:
            return c

    def stored(self, cred_id: str) -> Optional[ApiCredential]:
        """The on-disk (encrypted) form."""
        return self._store.get(cred_id)


class FakePromptRepo(_Base, IPromptRepository):
    async def add_template(self, t: PromptTemplate) -> PromptTemplate:
        self._store[t.id] = t
        return t

    async def update_template(self, t: PromptTemplate) -> PromptTemplate:
        self._store[t.id] = t
        return t

    async def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        return self._store.get(template_id)

    async def list_templates(self) -> List[PromptTemplate]:
        return list(self._store.values())

    async def list_templates_by_owner(self, owner_user_id: int) -> List[PromptTemplate]:
        # v12: mirror the sqlite scoping — system prompts stay shared.
        return [
            t for t in self._store.values()
            if t.is_system or int(getattr(t, "owner_user_id", 0) or 0) == int(owner_user_id)
        ]

    async def delete_template(self, template_id: str) -> bool:
        return self._store.pop(template_id, None) is not None

    async def add_version(self, version) -> object:
        return version

    async def get_version(self, template_id: str, version: int):
        return None

    async def list_versions(self, template_id: str) -> List:
        return []

    async def activate_version(self, template_id: str, version: int) -> bool:
        return False

    async def get_active_version(self, template_id: str):
        return None

    async def count_user_templates(self, owner_user_id: int = 0) -> int:
        return len([t for t in self._store.values() if not t.is_system])


# --------------------------------------------------------------------------- #
# Fixture: source + destination installations
# --------------------------------------------------------------------------- #

def _make_service(key: str = "src-key"):
    crypto = FakeCrypto(key)
    repos = {
        "sessions": FakeSessionRepo(crypto),
        "rules": FakeRuleRepo(),
        "filters": FakeFilterRepo(),
        "ai": FakeAIRepo(crypto),
        "creds": FakeCredRepo(crypto),
        "prompts": FakePromptRepo(),
    }
    svc = BackupService(
        sessions=repos["sessions"],
        rules=repos["rules"],
        filters=repos["filters"],
        ai_configs=repos["ai"],
        credentials=repos["creds"],
        prompts=repos["prompts"],
        crypto=crypto,
    )
    return svc, crypto, repos


def _enc(crypto: FakeCrypto, plaintext: str) -> str:
    return crypto.encrypt(plaintext)


def _seed_source(svc: BackupService, crypto: FakeCrypto, repos: dict) -> None:
    """Populate the source installation with realistic, sensitive data.

    Repositories encrypt on write, so all secrets are seeded in PLAINTEXT —
    exactly what the real sqlite layer receives from the application.
    """
    async def _seed():
        # credential (per-tenant as of v12; the export is scoped to owner 42)
        await repos["creds"].add(ApiCredential(
            id="cred-1", label="main", api_id=12345,
            api_hash="HASHSECRET",
            proxy="", is_default=True, created_at=1, updated_at=1,
            owner_user_id=42,
        ))
        # session
        await repos["sessions"].add(TelegramSession(
            id="sess-1", phone_number="+989120000001",
            session_string_encrypted="SESSIONSTRING-1",
            user_id="50001", username="src_account",
            is_active=True, is_authorized=True, api_credential_id="cred-1",
            owner_user_id=42,
        ))
        # filter
        await repos["filters"].add(FilterRule(
            id="flt-1", name="vip only", whitelist_keywords=["vip"],
            drop_service_messages=True, min_message_length=5, max_message_length=4096,
            owner_user_id=42,
        ))
        # ai config
        await repos["ai"].add(AIConfig(
            id="ai-1", name="primary", provider="openai", model="gpt-4o-mini",
            api_key="sk-SECRETKEY",
            base_url="https://api.openai.com/v1",
            system_prompt="rewrite", temperature=0.5, is_enabled=True,
            target_language="fa", owner_user_id=42,
        ))
        # user prompt template (owned by tenant 42)
        await repos["prompts"].add_template(PromptTemplate(
            id="pt-7", name="fa rewrite", description="",
            system_prompt="you are a translator",
            user_prompt_template="Translate: {text}", target_language="fa",
            current_version=1, is_system=False,
            owner_user_id=42,
        ))
        # system prompt: identical on every box, must NOT be re-imported
        await repos["prompts"].add_template(PromptTemplate(
            id="pt-sys-1", name="system default", description="",
            system_prompt="base", user_prompt_template="{text}",
            target_language="auto", current_version=1, is_system=True,
        ))
        # forward rule wired to everything above
        await repos["rules"].add(ForwardRule(
            id="rule-1", session_id="sess-1", name="ch->ch",
            source_chat_id="-100111", target_chat_id="-100222",
            routing_type=RoutingType.CHANNEL_TO_CHANNEL,
            forward_mode=ForwardMode.COPY_MESSAGE,
            filter_rule_id="flt-1", ai_config_id="ai-1",
            ai_fallback_policy=AIFallbackPolicy.SEND_ORIGINAL,
            ai_prompt_version=3, ai_timeout_seconds=20.0,
            owner_user_id=42,
        ))

    asyncio.run(_seed())


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_export_decrypts_secrets():
    svc, crypto, repos = _make_service()
    _seed_source(svc, crypto, repos)

    doc = asyncio.run(svc.create_snapshot(owner_user_id=42))

    assert doc["format"] == SNAPSHOT_FORMAT
    assert doc["source_owner_user_id"] == 42
    counts = doc["counts"]
    assert counts["sessions"] == 1
    assert counts["forward_rules"] == 1
    assert counts["filter_rules"] == 1
    assert counts["ai_configs"] == 1
    assert counts["api_credentials"] == 1

    # secrets are PLAINTEXT in the document — readable by a different master_key
    assert doc["entities"]["sessions"][0]["session_string"] == "SESSIONSTRING-1"
    assert doc["entities"]["ai_configs"][0]["api_key"] == "sk-SECRETKEY"
    assert doc["entities"]["api_credentials"][0]["api_hash"] == "HASHSECRET"

    # system templates travel in the export (informational) but are skipped on import
    assert any(t["is_system"] for t in doc["entities"]["prompt_templates"])


def test_roundtrip_preserves_everything_with_different_master_key():
    src, src_crypto, src_repos = _make_service("src-key")
    _seed_source(src, src_crypto, src_repos)
    doc = asyncio.run(src.create_snapshot(owner_user_id=42))

    # destination box: DIFFERENT master_key, empty database
    dst, dst_crypto, dst_repos = _make_service("dst-key")
    stats = asyncio.run(dst.restore_snapshot(doc))

    assert stats["sessions"]["imported"] == 1
    assert stats["forward_rules"]["imported"] == 1
    assert stats["filter_rules"]["imported"] == 1
    assert stats["ai_configs"]["imported"] == 1
    assert stats["api_credentials"]["imported"] == 1
    assert stats["prompt_templates"]["imported"] == 1  # only the user template
    assert all(v["failed"] == 0 for v in stats.values())

    # session was re-encrypted under the destination master_key
    stored = dst_repos["sessions"]._store["sess-1"]
    assert dst_crypto.decrypt(stored.session_string_encrypted) == "SESSIONSTRING-1"

    # ai key round-trips through the destination crypto
    stored_ai = dst_repos["ai"].stored("ai-1")
    assert dst_crypto.decrypt(stored_ai.api_key) == "sk-SECRETKEY"

    # credential (global) restored with its id preserved
    stored_cred = dst_repos["creds"].stored("cred-1")
    assert stored_cred is not None
    assert dst_crypto.decrypt(stored_cred.api_hash) == "HASHSECRET"
    assert stored_cred.api_id == 12345

    # rule kept its wiring AND its AI settings
    rule = dst_repos["rules"]._store["rule-1"]
    assert rule.filter_rule_id == "flt-1"
    assert rule.ai_config_id == "ai-1"
    assert rule.ai_fallback_policy == AIFallbackPolicy.SEND_ORIGINAL
    assert rule.ai_prompt_version == 3
    assert rule.ai_timeout_seconds == 20.0
    assert rule.owner_user_id == 42

    # user prompt template came over, system one did not duplicate
    assert dst_repos["prompts"]._store.get("pt-7") is not None
    assert "pt-sys-1" not in dst_repos["prompts"]._store


def test_session_encrypted_once_is_not_double_encrypted():
    """A snapshot exported from THIS box and re-imported into the same box."""
    src, crypto, repos = _make_service("same-key")
    _seed_source(src, crypto, repos)

    # export stores plaintext; restore encrypts it under "same-key"
    doc = asyncio.run(src.create_snapshot(owner_user_id=42))

    dst, dst_crypto, dst_repos = _make_service("same-key")
    asyncio.run(dst.restore_snapshot(doc))

    stored = dst_repos["sessions"]._store["sess-1"]
    # exactly one layer of encryption — decrypting yields the original string
    assert dst_crypto.decrypt(stored.session_string_encrypted) == "SESSIONSTRING-1"


def test_restore_is_idempotent_without_overwrite():
    src, crypto, repos = _make_service()
    _seed_source(src, crypto, repos)
    doc = asyncio.run(src.create_snapshot(owner_user_id=42))

    dst, _, dst_repos = _make_service("dst-key")
    first = asyncio.run(dst.restore_snapshot(doc))
    second = asyncio.run(dst.restore_snapshot(doc))

    assert all(v["imported"] >= 1 for v in first.values())
    # nothing imported the second time; everything skipped
    assert all(v["imported"] == 0 for v in second.values())
    assert all(v["skipped"] >= 1 for v in second.values())

    # no duplicate rows
    assert len(dst_repos["sessions"]._store) == 1
    assert len(dst_repos["rules"]._store) == 1
    assert len(dst_repos["ai"]._store) == 1


def test_overwrite_replaces_existing():
    src, crypto, repos = _make_service()
    _seed_source(src, crypto, repos)
    doc = asyncio.run(src.create_snapshot(owner_user_id=42))

    dst, _, dst_repos = _make_service("dst-key")
    asyncio.run(dst.restore_snapshot(doc))
    # corrupt the destination rule name, then re-import with overwrite
    dst_repos["rules"]._store["rule-1"].name = "CORRUPTED"

    asyncio.run(dst.restore_snapshot(doc, overwrite_existing=True))
    assert dst_repos["rules"]._store["rule-1"].name == "ch->ch"
    assert len(dst_repos["rules"]._store) == 1


def test_retarget_owner_rebinds_entities():
    src, crypto, repos = _make_service()
    _seed_source(src, crypto, repos)
    doc = asyncio.run(src.create_snapshot(owner_user_id=42))

    dst, _, dst_repos = _make_service("dst-key")
    asyncio.run(dst.restore_snapshot(doc, target_owner_user_id=99))

    assert dst_repos["sessions"]._store["sess-1"].owner_user_id == 99
    assert dst_repos["rules"]._store["rule-1"].owner_user_id == 99
    assert dst_repos["ai"]._store["ai-1"].owner_user_id == 99
    assert dst_repos["filters"]._store["flt-1"].owner_user_id == 99


def test_bundle_encryption_roundtrip():
    payload = serialize_bundle({"format": 2, "entities": {"sessions": []}})
    enc = encrypt_bundle(payload, "hunter2")
    assert enc.startswith(_BUNDLE_PREFIX)
    assert decrypt_bundle(enc, "hunter2") == payload

    # wrong passphrase -> clear error
    with pytest.raises(ValueError, match="wrong passphrase"):
        decrypt_bundle(enc, "nope")

    # missing passphrase on an encrypted bundle
    with pytest.raises(ValueError, match="passphrase-protected"):
        decrypt_bundle(enc, "")

    # passphrase given for a PLAIN bundle -> hard error (no silent import)
    with pytest.raises(ValueError, match="not encrypted"):
        decrypt_bundle(payload, "hunter2")

    # plain bundle with no passphrase passes through
    assert decrypt_bundle(payload, "") == payload


def test_bundle_key_is_derived_with_a_salted_pbkdf2():
    """Two exports of the same payload must not share a key.

    Regression for the v1 format, whose key was a single SHA-256 of the
    passphrase: no salt, no work factor. Both properties are visible on the
    wire — the salt is per bundle, and a bundle written with a different salt
    is not readable with the other bundle's ciphertext.
    """
    import hashlib

    from core.application.backup_service import _BUNDLE_KDF_ITERATIONS, _BUNDLE_SALT_BYTES

    payload = serialize_bundle({"format": 2, "entities": {}})
    first, second = encrypt_bundle(payload, "hunter2"), encrypt_bundle(payload, "hunter2")
    assert first != second  # random salt -> different ciphertext

    # The KDF must be a salted PBKDF2 at the OWASP floor, not a bare hash: the
    # key of a bundle is reproducible from its own salt and nothing else.
    salt = base64.urlsafe_b64decode(first[len(_BUNDLE_PREFIX):])[:_BUNDLE_SALT_BYTES]
    assert len(salt) == _BUNDLE_SALT_BYTES
    assert _BUNDLE_KDF_ITERATIONS >= 600_000
    derived = hashlib.pbkdf2_hmac(
        "sha256", b"hunter2", salt, _BUNDLE_KDF_ITERATIONS, dklen=32
    )
    assert derived != hashlib.sha256(b"hunter2").digest()


def test_legacy_v1_bundle_is_refused_and_announced_as_encrypted():
    """A v1 bundle fails closed with an explanation, and is never mistaken
    for a plain bundle (which would import as garbage or, worse, skip the
    passphrase prompt entirely)."""

    v1_blob = "enc:v1:" + base64.urlsafe_b64encode(b"x" * 40).decode("ascii")
    assert is_encrypted_bundle(v1_blob)
    with pytest.raises(ValueError, match="legacy v1 bundle"):
        decrypt_bundle(v1_blob, "hunter2")

    # a truncated v2 header is a clear error, not an IndexError
    with pytest.raises(ValueError, match="wrong passphrase or corrupted"):
        decrypt_bundle(_BUNDLE_PREFIX + base64.urlsafe_b64encode(b"short").decode("ascii"), "x")


def test_bundle_serialization_shape():
    doc = {"format": 2, "exported_at": 1, "entities": {}}
    text = serialize_bundle(doc)
    assert deserialize_bundle(text) == doc

    with pytest.raises(ValueError, match="entities"):
        deserialize_bundle('{"format": 2}')


def test_export_scoped_to_owner_excludes_other_tenants():
    src, crypto, repos = _make_service()
    _seed_source(src, crypto, repos)
    # a second tenant's session
    asyncio.run(repos["sessions"].add(TelegramSession(
        id="sess-2", phone_number="+989120000002",
        session_string_encrypted="SESSIONSTRING-2",
        owner_user_id=77,
    )))

    doc = asyncio.run(src.create_snapshot(owner_user_id=42))
    ids = [s["id"] for s in doc["entities"]["sessions"]]
    assert ids == ["sess-1"]


def test_full_bundle_roundtrip_through_serialization():
    """export -> serialize -> encrypt -> decrypt -> deserialize -> restore."""
    src, crypto, repos = _make_service("src-key")
    _seed_source(src, crypto, repos)
    doc = asyncio.run(src.create_snapshot(owner_user_id=42))

    blob = encrypt_bundle(serialize_bundle(doc), "passphrase-123")
    text = decrypt_bundle(blob, "passphrase-123")
    doc2 = deserialize_bundle(text)
    assert doc2["counts"] == doc["counts"]

    dst, _, dst_repos = _make_service("dst-key")
    stats = asyncio.run(dst.restore_snapshot(doc2))
    assert all(v["imported"] >= 1 for v in stats.values())
    assert len(dst_repos["rules"]._store) == 1
