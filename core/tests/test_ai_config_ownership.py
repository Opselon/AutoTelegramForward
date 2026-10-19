"""Part 2 STEER 10 — an AI config id is a selector, never proof of ownership.

A task's rewriting config is named by an id that arrives from a callback payload,
an imported backup, or a hand-edited row. The engine used to resolve it with an
unscoped read (`IAIConfigRepository.get_by_id`), so a rule whose ``ai_config_id``
pointed at another tenant's config would spend **that tenant's provider,
base_url and API key** to rewrite the first tenant's messages.

The fixes are owner-scoped reads everywhere the engine touches a config, and a
refusal that says which of the three cases it is (foreign / ownerless / deleted)
because each needs a different fix. Proved here against the real
``SqliteAIConfigRepository`` and the real ``AITransformer``; the end-to-end case
runs the real ``DurableMessagePipeline`` and asserts what each tenant receives.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

import pytest

from core.application.pipeline import DurableMessagePipeline
from core.domain.entities import AIConfig, ForwardRule, MessagePayload, TelegramSession
from core.infrastructure.ai.circuit_breaker import CircuitBreakerRegistry
from core.infrastructure.ai.transformer import AIRequest, AITransformer
from core.infrastructure.ai.validator import AIOutputValidator
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteAIConfigRepository,
    SqliteFilterRuleRepository,
    SqliteForwardRuleRepository,
    SqliteMessageMapRepository,
    SqliteProcessedMessageRepository,
    SqliteSessionRepository,
)
from core.infrastructure.security.crypto import CryptoService

OWNER_A = 5094837833
OWNER_B = 5989354154
SOURCE = "-1004400615270"
TARGET_A = "-1003915313919"
TARGET_B = "-1003915313920"


class RecordingProvider:
    """Reports which config it was asked to spend."""

    def __init__(self) -> None:
        self.used: list[tuple[str, str, str]] = []

    async def rewrite(self, config: AIConfig, text: str) -> str:
        self.used.append((config.id, config.api_key, config.base_url))
        # Never echo the input: the transformer passes the fully-built prompt
        # (delimiters included), and echoing it back trips the output validator.
        return f"rewritten-by-{config.id}"


class RecordingFactory:
    def __init__(self, provider: RecordingProvider) -> None:
        self._provider = provider

    def get(self, config: AIConfig) -> RecordingProvider:
        return self._provider


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str, str]] = []

    async def enqueue_delivery(self, rule, payload, target_chat_id, text,
                               evaluation=None, job_kind="forward",
                               correlation_id="", branding=None):
        self.jobs.append((rule.id, str(target_chat_id), text))
        return True

    @property
    def texts(self) -> list[str]:
        return [job[2] for job in self.jobs]


def make_config(cfg_id: str, owner: int, api_key: str) -> AIConfig:
    return AIConfig(id=cfg_id, name=cfg_id, api_key=api_key,
                    base_url="https://api.example.com/v1", is_enabled=True,
                    owner_user_id=owner)


def make_rule(rule_id: str, owner: int, target: str, config_id: str) -> ForwardRule:
    rule = ForwardRule(
        id=rule_id, session_id="sess-a", source_chat_id=SOURCE, target_chat_id=target,
        is_active=True, owner_user_id=owner, ai_config_id=config_id,
        ai_fallback_policy="SEND_ORIGINAL",
    )
    rule.duplicate_filters = False
    # Both switches the engine gates on, set through the rule's own props.
    rule.smart_ai_rewrite = True
    rule.paid_addons = {"ai_rewrite": True}
    return rule


@pytest.fixture
def ws():
    with tempfile.TemporaryDirectory() as tmp:
        db = SqliteDatabase(os.path.join(tmp, "ai_owner.db"))
        try:
            yield db
        finally:
            db.close()


def run(coro):
    return asyncio.run(coro)


def _seed(db) -> None:
    async def go():
        crypto = CryptoService("test-key")
        configs = SqliteAIConfigRepository(db, crypto)
        await configs.add(make_config("cfg-a", OWNER_A, "sk-tenant-a"))
        await configs.add(make_config("cfg-b", OWNER_B, "sk-tenant-b"))
        await configs.add(make_config("cfg-orphan", 0, "sk-nobody"))
        sessions = SqliteSessionRepository(db, crypto)
        for owner in (OWNER_A, OWNER_B):
            await sessions.add(TelegramSession(
                id=f"sess-{owner}", phone_number="+100****0000", is_active=True,
                is_authorized=True, owner_user_id=owner,
            ))
        rules = SqliteForwardRuleRepository(db)
        rule_a = make_rule("rule-a", OWNER_A, TARGET_A, "cfg-a")
        rule_a.session_id = f"sess-{OWNER_A}"
        rule_b = make_rule("rule-b", OWNER_B, TARGET_B, "cfg-a")  # names A's config
        rule_b.session_id = f"sess-{OWNER_B}"
        await rules.add(rule_a)
        await rules.add(rule_b)

    run(go())


# --------------------------------------------------------------------------- #
# The repository: owner-scoped reads fail closed
# --------------------------------------------------------------------------- #
def test_the_repository_returns_a_config_only_to_its_owner(ws):
    _seed(ws)
    configs = SqliteAIConfigRepository(ws, CryptoService("test-key"))

    async def go():
        owned = await configs.get_owned("cfg-a", OWNER_A)
        assert owned is not None and owned.id == "cfg-a"
        assert await configs.get_owned("cfg-a", OWNER_B) is None, "a foreign config was returned"
        assert await configs.get_owned("cfg-b", OWNER_A) is None, "a foreign config was returned"
        assert await configs.get_owned("cfg-missing", OWNER_A) is None
        # An ownerless legacy row belongs to no tenant: it may not be claimed.
        assert await configs.get_owned("cfg-orphan", OWNER_A) is None, "an ownerless config was claimed"

    run(go())


# --------------------------------------------------------------------------- #
# The transformer: the credential is never spent
# --------------------------------------------------------------------------- #
def _transformer(db, provider) -> AITransformer:
    return AITransformer(
        ai_config_repo=SqliteAIConfigRepository(db, CryptoService("test-key")),
        circuit_registry=CircuitBreakerRegistry(),
        validator=AIOutputValidator(),
        provider_factory=RecordingFactory(provider),
    )


def test_a_tenants_rule_cannot_spend_another_tenants_config(ws, caplog):
    _seed(ws)
    provider = RecordingProvider()
    req = AIRequest(text="hello", correlation_id="corr-foreign")

    with caplog.at_level(logging.WARNING):
        resp = run(_transformer(ws, provider).transform(
            req, primary_config_id="cfg-a", owner_user_id=OWNER_B,
        ))

    assert resp.success is False, resp
    assert resp.error_code == "INVALID_CONFIG", resp
    assert provider.used == [], f"a foreign credential was spent: {provider.used}"
    refusal = "\n".join(r.getMessage() for r in caplog.records)
    assert "[AI:CONFIG_REFUSED]" in refusal, refusal
    assert "not by rule owner" in refusal, refusal


def test_a_rule_ownerless_config_is_refused_with_its_own_reason(ws, caplog):
    """Distinguishable from a boundary violation: it needs a different fix."""
    _seed(ws)
    provider = RecordingProvider()
    req = AIRequest(text="hello", correlation_id="corr-orphan")

    with caplog.at_level(logging.WARNING):
        resp = run(_transformer(ws, provider).transform(
            req, primary_config_id="cfg-orphan", owner_user_id=OWNER_A,
        ))

    assert resp.success is False
    assert provider.used == []
    refusal = "\n".join(r.getMessage() for r in caplog.records)
    assert "has no owner" in refusal, refusal


def test_the_owner_still_rewrites_with_its_own_config(ws):
    """The control: the boundary must not have been drawn around everyone."""
    _seed(ws)
    provider = RecordingProvider()
    req = AIRequest(text="hello", correlation_id="corr-own")

    resp = run(_transformer(ws, provider).transform(
        req, primary_config_id="cfg-a", owner_user_id=OWNER_A,
    ))

    assert resp.success is True, resp
    assert resp.transformed_text == "rewritten-by-cfg-a", resp.transformed_text
    assert provider.used and provider.used[0][0] == "cfg-a", provider.used


# --------------------------------------------------------------------------- #
# End to end: what each tenant actually receives
# --------------------------------------------------------------------------- #
def test_the_pipeline_rewrites_only_with_the_rules_own_config(ws, caplog):
    """Two tasks, one message: the owned config rewrites, the borrowed one does not."""
    _seed(ws)
    provider = RecordingProvider()
    queue = RecordingQueue()

    pipeline = DurableMessagePipeline(
        rule_repo=SqliteForwardRuleRepository(ws),
        filter_repo=SqliteFilterRuleRepository(ws),
        queue_manager=queue,
        msg_map_repo=SqliteMessageMapRepository(ws),
        processed_repo=SqliteProcessedMessageRepository(ws),
        ai_transformer=_transformer(ws, provider),
        ai_repo=SqliteAIConfigRepository(ws, CryptoService("test-key")),
    )

    with caplog.at_level(logging.WARNING):
        run(pipeline.process(
            MessagePayload(message_id=77, chat_id=SOURCE, text="original text"),
            trigger="NEW_MESSAGE",
        ))

    delivered = {job[0]: job[2] for job in queue.jobs}
    # Both tasks forward (SEND_ORIGINAL): a refused rewrite must never cost the
    # owner a delivery — it just delivers the text it was given.
    assert "rule-a" in delivered and "rule-b" in delivered, queue.jobs
    assert delivered["rule-a"] == "rewritten-by-cfg-a", delivered
    assert delivered["rule-b"] == "original text", (
        "a task was rewritten with another tenant's config: " + str(delivered["rule-b"])
    )
    assert [c for c, _, _ in provider.used] == ["cfg-a"], provider.used
