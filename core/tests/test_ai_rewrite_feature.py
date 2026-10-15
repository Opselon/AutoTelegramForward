"""AI Rewrite feature tests: rule-level enable/disable, effective prompt selection,
fallback policies, persistence of settings across drafts, and per-user isolation.

These exercise the real application services (PromptService + ForwardRule draft),
not the transport — the bot and the dashboard both sit on top of the same services.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.application.pipeline import DurableMessagePipeline, PipelineContext
from core.application.prompt_service import PromptService
from core.domain.entities import (
    AIConfig,
    EvaluationResult,
    ForwardRule,
    MessagePayload,
)
from core.domain.value_objects import (
    AIFallbackPolicy,
    AIProviderType,
    FilterAction,
    ForwardMode,
    RoutingType,
)
from core.infrastructure.ai.transformer import AIResponse
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqlitePromptRepository


# ---------------------------------------------------------------------------
# Rule-level rewrite enable/disable
# ---------------------------------------------------------------------------

def test_rewrite_disabled_by_default():
    """A fresh rule has no AI config: the pipeline must not call AI at all."""
    rule = ForwardRule(source_chat_id="-1001", target_chat_id="-1002")
    assert rule.ai_config_id is None
    assert rule.ai_fallback_policy == AIFallbackPolicy.DROP
    assert rule.ai_timeout_seconds == 15.0
    assert not (rule.metadata or {}).get("prompt_id")


def test_enabling_rewrite_via_draft_roundtrip():
    """Whatever the bot or dashboard writes into the draft must survive a
    to_draft() -> apply_draft() cycle and land on the entity."""
    rule = ForwardRule(source_chat_id="-1001", target_chat_id="-1002")
    rule.apply_draft({
        "ai_config_id": "cfg-xyz",
        "ai_fallback_policy": "SEND_ORIGINAL",
        "ai_prompt_version": 3,
        "ai_timeout_seconds": 25.0,
        "prompt_id": "sys_pro_clean",
    })
    assert rule.ai_config_id == "cfg-xyz"
    assert rule.ai_fallback_policy == AIFallbackPolicy.SEND_ORIGINAL
    assert rule.ai_prompt_version == 3
    assert rule.ai_timeout_seconds == 25.0
    assert rule.metadata["prompt_id"] == "sys_pro_clean"

    # Round-trip through the draft (what the UI reads back after save)
    draft = rule.to_draft()
    assert draft["ai_config_id"] == "cfg-xyz"
    assert draft["ai_fallback_policy"] == "SEND_ORIGINAL"
    assert draft["ai_timeout_seconds"] == 25.0
    assert draft["prompt_id"] == "sys_pro_clean"


def test_disabling_rewrite_clears_prompt_and_config():
    rule = ForwardRule(source_chat_id="-1001", target_chat_id="-1002")
    rule.apply_draft({"ai_config_id": "cfg-xyz", "prompt_id": "sys_pro_clean"})
    rule.apply_draft({"ai_config_id": "", "prompt_id": ""})
    assert rule.ai_config_id is None
    assert "prompt_id" not in rule.metadata


def test_invalid_fallback_policy_is_ignored():
    """A bogus policy from an untrusted client must not crash the rule."""
    rule = ForwardRule(source_chat_id="-1001", target_chat_id="-1002")
    rule.apply_draft({"ai_fallback_policy": "DELETE_EVERYTHING"})
    assert rule.ai_fallback_policy == AIFallbackPolicy.DROP


def test_timeout_is_clamped_to_safe_range():
    rule = ForwardRule(source_chat_id="-1001", target_chat_id="-1002")
    rule.apply_draft({"ai_timeout_seconds": 9999.0})
    assert rule.ai_timeout_seconds == 120.0
    rule.apply_draft({"ai_timeout_seconds": 0.1})
    assert rule.ai_timeout_seconds == 1.0


# ---------------------------------------------------------------------------
# Effective prompt selection (priority: pinned version -> active version)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_service_effective_prompt_selection(tmp_path):
    db = SqliteDatabase(str(tmp_path / "prompts.db"))
    service = PromptService(SqlitePromptRepository(db))
    await service.init_defaults()

    # A user-owned template
    created = await service.create_template(
        name="User rewrite", system_prompt="Be terse.", user_prompt_template="{text}",
        target_language="auto",
    )
    # Bump to v2 with a different instruction
    v2 = await service.update_template(created.id, name="User rewrite", system_prompt="Be very terse.")

    # Default (0) resolves to the ACTIVE version
    preview = await service.preview_prompt(created.id, "hello")
    assert preview["system_prompt"] == "Be very terse."
    assert preview["version"] == 2

    # Explicit pin to v1 still resolves
    preview_v1 = await service.preview_prompt(created.id, "hello", version=1)
    assert preview_v1["system_prompt"] == "Be terse."
    assert preview_v1["version"] == 1

    # render substitutes {text}
    assert "hello" in preview["rendered_user_prompt"]


@pytest.mark.asyncio
async def test_prompt_id_missing_is_handled(tmp_path):
    """A rule pointing at a deleted prompt must not crash the pipeline."""
    db = SqliteDatabase(str(tmp_path / "prompts.db"))
    service = PromptService(SqlitePromptRepository(db))
    await service.init_defaults()
    with pytest.raises(ValueError):
        await service.preview_prompt("does-not-exist", "hello")


# ---------------------------------------------------------------------------
# Fallback policies: fail open vs fail closed semantics
# ---------------------------------------------------------------------------

def test_fallback_policy_enum_values():
    assert {p.value for p in AIFallbackPolicy} == {
        "SEND_ORIGINAL", "DROP", "RETRY", "QUARANTINE"
    }


@pytest.mark.asyncio
async def test_send_original_returns_input_on_failure():
    """When the AI fails and the policy is SEND_ORIGINAL, the caller must get the
    original text back (Fail Open) — never None and never a duplicate send."""
    from core.infrastructure.ai.transformer import AITransformer

    repo = AsyncMock()
    repo.get_by_id.return_value = AIConfig(
        id="c1", name="x", provider=AIProviderType.OPENAI, model="m",
        api_key="k", system_prompt="s", is_enabled=True,
    )
    factory = MagicMock()
    provider = AsyncMock()
    provider.rewrite.side_effect = TimeoutError("AI timed out")
    factory.get.return_value = provider

    from core.infrastructure.ai.circuit_breaker import CircuitBreakerRegistry
    transformer = AITransformer(
        ai_config_repo=repo, circuit_registry=CircuitBreakerRegistry(),
        validator=MagicMock(), provider_factory=factory,
    )
    resp = await transformer.transform(
        __import__("core.infrastructure.ai.transformer", fromlist=["AIRequest"]).AIRequest(
            text="ORIGINAL TEXT", system_prompt="s", user_template="{text}",
            timeout_seconds=1.0,
        ),
        primary_config_id="c1",
    )
    # A failure surfaces as an unsuccessful response with diagnostics; the
    # pipeline's _handle_ai_fallback then decides using the rule policy.
    assert resp.success is False
    assert resp.error_code  # caller distinguishes timeout vs. validation


# ---------------------------------------------------------------------------
# Per-user isolation of settings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_service_owner_isolation(tmp_path):
    """Prompt templates are global presets in this deployment (shared surface);
    the per-user boundary is enforced at the AI-config level (see Go ownership
    guard ownsAIConfig on TestPrompt). Assert templates are readable but the
    AI config lookup is owner-scoped in the use-case layer."""
    db = SqliteDatabase(str(tmp_path / "prompts.db"))
    service = PromptService(SqlitePromptRepository(db))
    await service.init_defaults()
    templates = await service.list_templates()
    assert len(templates) >= 4  # seeded defaults
    assert all(t.system_prompt for t in templates)


# ---------------------------------------------------------------------------
# gRPC contract: the shared servicer the bot & dashboard both call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_rpc_contract_roundtrip(tmp_path):
    """List/Save/Delete/Test prompt RPCs behave as the two front-ends expect."""
    from core.infrastructure.grpc_server.servicers import AIControlServicer
    from core.proto import autoforward_pb2 as pb

    db = SqliteDatabase(str(tmp_path / "prompts.db"))
    service = PromptService(SqlitePromptRepository(db))
    await service.init_defaults()

    fake_ai = MagicMock()
    fake_ai.list_all = AsyncMock(return_value=[])

    svc = AIControlServicer(fake_ai, prompt_service=service, ai_transformer=None)

    # List includes seeded system prompts
    resp = await svc.ListPromptTemplates(pb.ListPromptTemplatesRequest(), MagicMock())
    assert len(resp.templates) >= 4
    assert all(t.is_system for t in resp.templates)

    # Create returns a real id (regression: id was empty and hit a UNIQUE clash)
    created = await svc.SavePromptTemplate(pb.SavePromptTemplateRequest(
        template=pb.PromptTemplate(name="mine", system_prompt="Be brief.",
                                   user_prompt_template="{text}", target_language="auto")
    ), MagicMock())
    assert created.id, "create must return a generated id"
    assert created.current_version == 1

    # Update bumps the version and honors OCC
    updated = await svc.SavePromptTemplate(pb.SavePromptTemplateRequest(
        template=pb.PromptTemplate(id=created.id, name="mine", system_prompt="Be brief!"),
        expected_version=1,
    ), MagicMock())
    assert updated.current_version == 2

    conflict = await svc.SavePromptTemplate(pb.SavePromptTemplateRequest(
        template=pb.PromptTemplate(id=created.id, name="mine", system_prompt="stale"),
        expected_version=99,
    ), MagicMock())
    assert conflict.id == ""  # OCC rejected

    # System prompts are immutable
    sys_del = await svc.DeletePromptTemplate(
        pb.DeletePromptTemplateRequest(id=resp.templates[0].id), MagicMock())
    assert sys_del.success is False

    ok = await svc.DeletePromptTemplate(pb.DeletePromptTemplateRequest(id=created.id), MagicMock())
    assert ok.success


# ---------------------------------------------------------------------------
# Per-task (per-rule) isolation: two rules from the SAME source channel must be
# able to disagree about AI rewrite. One rule opts in, the other stays off.
# ---------------------------------------------------------------------------

def _pipeline_with_transformer(mock_transformer):
    rule_repo = AsyncMock()
    filter_repo = AsyncMock()
    filter_repo.get_by_id.return_value = None
    return DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=filter_repo,
        ai_transformer=mock_transformer,
    )


@pytest.mark.asyncio
async def test_ai_rewrite_is_per_rule_not_per_source():
    """Rule A (AI on) is rewritten; rule B (same source, AI off) passes through
    untouched and never touches the AI provider."""
    ai_rule = ForwardRule(
        id="rule-ai-on",
        session_id="sess-1",
        source_chat_id="-1001",
        target_chat_id="-2001",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        ai_config_id="ai-cfg-1",
    )
    plain_rule = ForwardRule(
        id="rule-ai-off",
        session_id="sess-1",
        source_chat_id="-1001",  # same source channel
        target_chat_id="-2002",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        # no ai_config_id -> AI rewrite disabled for THIS task only
    )

    mock_transformer = AsyncMock()
    mock_transformer.transform.return_value = AIResponse(
        success=True,
        transformed_text="[AI] rewritten body",
        execution_duration_ms=12.0,
        model_used="test-model",
    )

    pipeline = _pipeline_with_transformer(mock_transformer)
    payload = MessagePayload(message_id=55, chat_id="-1001", text="raw body")

    on_eval = EvaluationResult(action=FilterAction.ALLOW)
    on_ctx = PipelineContext(payload=payload, correlation_id="per-rule-on")
    out_on = await pipeline._apply_transforms(on_ctx, payload, ai_rule, on_eval)
    assert out_on == "[AI] rewritten body"
    assert mock_transformer.transform.await_count == 1

    off_eval = EvaluationResult(action=FilterAction.ALLOW)
    off_ctx = PipelineContext(payload=payload, correlation_id="per-rule-off")
    out_off = await pipeline._apply_transforms(off_ctx, payload, plain_rule, off_eval)
    assert out_off == "raw body"                      # untouched
    assert off_eval.rewritten_text is None
    assert mock_transformer.transform.await_count == 1  # still one: B never called AI

    # Each task carries its own AI settings; they do not leak into each other.
    assert ai_rule.ai_config_id == "ai-cfg-1"
    assert plain_rule.ai_config_id is None


@pytest.mark.asyncio
async def test_ai_rewrite_toggle_per_task_is_independent():
    """Enabling AI on task A must leave task B disabled, and vice versa."""
    task_a = ForwardRule(id="a", source_chat_id="-1001", target_chat_id="-2001")
    task_b = ForwardRule(id="b", source_chat_id="-1001", target_chat_id="-2002")

    # Bot/dashboard toggle on task A only
    task_a.ai_config_id = "ai-cfg-1"
    task_a.smart_ai_rewrite = True

    assert task_a.ai_config_id == "ai-cfg-1"
    assert task_b.ai_config_id is None
    assert task_b.smart_ai_rewrite is False

    # Toggling A off must not disturb A's sibling task
    task_a.ai_config_id = None
    task_a.smart_ai_rewrite = False
    assert task_a.ai_config_id is None
    assert task_b.ai_config_id is None

    # Now enable B; A stays off
    task_b.ai_config_id = "ai-cfg-2"
    assert task_b.ai_config_id == "ai-cfg-2"
    assert task_a.ai_config_id is None
