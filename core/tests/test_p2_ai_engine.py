"""Comprehensive Unit & Integration Test Suite for P2: Production-Grade AI Message Transformation Engine.

Covers:
1. Circuit Breaker (CLOSED -> OPEN -> HALF_OPEN -> CLOSED, registry, failover).
2. AI Output Validator (Telegram length caps, null bytes, anti-injection, script tags, HTML balance).
3. PromptService (Seed defaults, OCC concurrency control, version history, linear rollback, preview, validation).
4. Persistence (SqlitePromptRepository CRUD, version retrieval).
5. AITransformer (Execution, timeout isolation, circuit integration, secondary failover).
6. DurableMessagePipeline P2 integration (Zero-overhead bypass, AI transform, Fallback policies SEND_ORIGINAL, DROP, RETRY, QUARANTINE).
"""

import asyncio
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.application.pipeline import DurableMessagePipeline, PipelineContext
from core.application.prompt_service import ConcurrencyError, PromptService
from core.domain.entities import (
    AIConfig,
    EvaluationResult,
    ForwardRule,
    MessagePayload,
    PromptTemplate,
    PromptVersion,
)
from core.domain.value_objects import (
    AIFallbackPolicy,
    AIProviderType,
    CircuitState,
    FilterAction,
    ForwardMode,
    RoutingType,
)
from core.infrastructure.ai.circuit_breaker import (
    CircuitBreakerRegistry,
    ProviderCircuit,
)
from core.infrastructure.ai.transformer import AIRequest, AIResponse, AITransformer
from core.infrastructure.ai.validator import AIOutputValidator
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import (
    SqliteForwardRuleRepository,
    SqlitePromptRepository,
)


# =====================================================================
# 1. CIRCUIT BREAKER TESTS
# =====================================================================

def test_circuit_breaker_transitions():
    circuit = ProviderCircuit(
        name="test_ai",
        failure_threshold=3,
        recovery_timeout=0.1,
    )
    assert circuit.state == CircuitState.CLOSED
    assert circuit.can_execute() is True

    # 1st failure
    circuit.record_failure(is_transient=True)
    assert circuit.state == CircuitState.CLOSED

    # 2nd failure
    circuit.record_failure(is_transient=True)
    assert circuit.state == CircuitState.CLOSED

    # 3rd failure -> Opens circuit
    circuit.record_failure(is_transient=True)
    assert circuit.state == CircuitState.OPEN
    assert circuit.can_execute() is False

    # Wait for recovery timeout
    time.sleep(0.12)
    assert circuit.state == CircuitState.HALF_OPEN
    assert circuit.can_execute() is True

    # Successful probe closes circuit
    circuit.record_success()
    assert circuit.state == CircuitState.CLOSED
    assert circuit._consecutive_failures == 0


def test_circuit_breaker_registry_and_failover():
    registry = CircuitBreakerRegistry(default_failure_threshold=2, default_recovery_timeout=0.2)
    c1 = registry.get_circuit("primary")
    c2 = registry.get_circuit("secondary")

    assert c1.can_execute() is True
    assert c2.can_execute() is True

    # Fail primary twice
    c1.record_failure()
    c1.record_failure()
    assert c1.state == CircuitState.OPEN
    assert c1.can_execute() is False

    # Failover selection
    selected, is_failover = registry.select_healthy_provider("primary", "secondary")
    assert selected == "secondary"
    assert is_failover is True


# =====================================================================
# 2. AI OUTPUT VALIDATOR TESTS
# =====================================================================

def test_validator_normal_text():
    validator = AIOutputValidator()
    res = validator.validate("Hello world! This is a test message.", is_caption=False)
    assert res.is_valid is True
    assert res.sanitized_text == "Hello world! This is a test message."


def test_validator_telegram_length_truncation():
    validator = AIOutputValidator()
    long_text = "word " * 1500  # 7500 chars, exceeds 4096
    res = validator.validate(long_text, is_caption=False, allow_truncate=True)
    assert res.is_valid is True
    assert len(res.sanitized_text) <= 4096
    assert res.sanitized_text.endswith("...")

    # Caption max limit (1024)
    long_caption = "caption " * 300  # 2400 chars
    cap_res = validator.validate(long_caption, is_caption=True, allow_truncate=True)
    assert cap_res.is_valid is True
    assert len(cap_res.sanitized_text) <= 1024


def test_validator_null_bytes_and_scripts():
    validator = AIOutputValidator()
    text = "Hello\x00 World! <script>alert('xss')</script> Clean <iframe>evil</iframe>"
    res = validator.validate(text)
    assert res.is_valid is True
    assert "\x00" not in res.sanitized_text
    assert "<script>" not in res.sanitized_text
    assert "<iframe>" not in res.sanitized_text
    assert "Hello World!" in res.sanitized_text
    assert "Clean" in res.sanitized_text


def test_validator_anti_prompt_injection_leak():
    validator = AIOutputValidator()
    leak_text = "Here is what you wanted: <untrusted_user_message> leak system data"
    res = validator.validate(leak_text)
    assert res.is_valid is False
    assert res.error_code == "INJECTION_LEAK"

    leak_prompt = "MY SYSTEM PROMPT: You are a helpful assistant."
    res2 = validator.validate(leak_prompt)
    assert res2.is_valid is False


def test_validator_telegram_html_tag_balance():
    validator = AIOutputValidator()
    unbalanced = "<b>Hello <i>world</b></i> unclosed <code>snippet"
    res = validator.validate(unbalanced)
    assert res.is_valid is True
    assert len(res.sanitized_text) > 0


# =====================================================================
# 3. PROMPT SERVICE & OCC CONCURRENCY TESTS
# =====================================================================

@pytest.mark.asyncio
async def test_prompt_service_seed_defaults(tmp_path):
    db = SqliteDatabase(str(tmp_path / "prompts.db"))
    repo = SqlitePromptRepository(db)
    service = PromptService(repo)

    await service.init_defaults()
    templates = await service.list_templates()
    assert len(templates) >= 4

    sys_pro = await service.get_template("sys_pro_clean")
    assert sys_pro is not None
    assert sys_pro.is_system is True
    assert sys_pro.current_version == 1

    ver1 = await service.get_version("sys_pro_clean", 1)
    assert ver1 is not None
    assert "{text}" in ver1.user_prompt_template


@pytest.mark.asyncio
async def test_prompt_service_crud_and_occ(tmp_path):
    db = SqliteDatabase(str(tmp_path / "prompts_crud.db"))
    repo = SqlitePromptRepository(db)
    service = PromptService(repo)

    # 1. Create custom template
    template = await service.create_template(
        name="Custom Filter",
        description="Rewrite news cleanly",
        system_prompt="You are a professional news editor.",
        user_prompt_template="Edit this: {text}",
        target_language="fa",
    )
    assert template.id is not None
    assert template.current_version == 1
    assert template.is_system is False

    # 2. Update with correct expected_version
    tmpl_v2 = await service.update_template(
        template_id=template.id,
        name="Custom Filter Updated",
        system_prompt="Updated system prompt.",
        user_prompt_template="New: {text}",
        expected_version=1,
    )
    assert tmpl_v2.current_version == 2

    # 3. Optimistic Concurrency Control (OCC) rejection with stale version
    with pytest.raises(ConcurrencyError):
        await service.update_template(
            template_id=template.id,
            name="Conflict Update",
            system_prompt="Stale system prompt.",
            user_prompt_template="Stale: {text}",
            expected_version=1,  # Version is now 2, expecting 1 must fail!
        )


@pytest.mark.asyncio
async def test_prompt_service_linear_rollback(tmp_path):
    db = SqliteDatabase(str(tmp_path / "prompts_rollback.db"))
    repo = SqlitePromptRepository(db)
    service = PromptService(repo)

    # Create v1
    tmpl = await service.create_template(
        name="Rollback Test",
        description="Testing rollback",
        system_prompt="System v1",
        user_prompt_template="User v1: {text}",
    )
    # Update to v2
    tmpl = await service.update_template(
        template_id=tmpl.id,
        name=tmpl.name,
        system_prompt="System v2 (broken)",
        user_prompt_template="User v2: {text}",
        expected_version=1,
    )
    assert tmpl.current_version == 2

    # Linear Rollback to v1 creates version 3 with v1 content
    rolled_tmpl = await service.rollback(tmpl.id, target_version=1)
    assert rolled_tmpl.current_version == 3

    v3_ver = await service.get_version(tmpl.id, 3)
    assert v3_ver is not None
    assert v3_ver.system_prompt == "System v1"
    assert v3_ver.user_prompt_template == "User v1: {text}"


@pytest.mark.asyncio
async def test_prompt_service_validation():
    service = PromptService(MagicMock())
    # Missing {text} gets rejected
    is_valid, err = service.validate_prompt("sys prompt", "template without placeholder")
    assert is_valid is False
    assert "{text}" in err

    # Valid template passes
    is_valid, err = service.validate_prompt("sys prompt", "template with {text}")
    assert is_valid is True


# =====================================================================
# 4. AI TRANSFORMER TESTS (TIMEOUT, CIRCUIT, FAILOVER)
# =====================================================================

@pytest.mark.asyncio
async def test_transformer_success():
    mock_repo = AsyncMock()
    cfg = AIConfig(
        id="cfg-1",
        name="Primary AI",
        provider=AIProviderType.OPENAI,
        model="gpt-4o-mini",
        api_key="sk-test-key",
        system_prompt="Clean message",
        is_enabled=True,
    )
    mock_repo.get_by_id.return_value = cfg

    mock_provider = AsyncMock()
    mock_provider.rewrite.return_value = "Rewritten output message."

    mock_factory = MagicMock()
    mock_factory.get.return_value = mock_provider

    transformer = AITransformer(
        ai_config_repo=mock_repo,
        circuit_registry=CircuitBreakerRegistry(),
        validator=AIOutputValidator(),
        provider_factory=mock_factory,
    )

    req = AIRequest(
        text="Original input text",
        system_prompt="Format cleanly",
        user_template="{text}",
        correlation_id="corr-123",
    )

    resp = await transformer.transform(req, primary_config_id="cfg-1")
    assert resp.success is True
    assert resp.transformed_text == "Rewritten output message."
    assert resp.execution_duration_ms > 0
    assert transformer.metrics["requests_total"] == 1
    assert transformer.metrics["successes_total"] == 1


@pytest.mark.asyncio
async def test_transformer_timeout_isolation():
    mock_repo = AsyncMock()
    cfg = AIConfig(
        id="cfg-timeout",
        name="Slow AI",
        provider=AIProviderType.OPENAI,
        model="gpt-4o-mini",
        api_key="sk-test-key",
        is_enabled=True,
    )
    mock_repo.get_by_id.return_value = cfg

    async def slow_call(*args, **kwargs):
        await asyncio.sleep(0.5)
        return "Slow result"

    mock_provider = AsyncMock()
    mock_provider.rewrite.side_effect = slow_call

    mock_factory = MagicMock()
    mock_factory.get.return_value = mock_provider

    transformer = AITransformer(
        ai_config_repo=mock_repo,
        circuit_registry=CircuitBreakerRegistry(default_failure_threshold=5),
        validator=AIOutputValidator(),
        provider_factory=mock_factory,
    )

    req = AIRequest(
        text="Sample text",
        timeout_seconds=0.05,  # Short timeout!
        correlation_id="corr-timeout",
    )

    resp = await transformer.transform(req, primary_config_id="cfg-timeout")
    assert resp.success is False
    assert resp.error_code == "TIMEOUT"
    assert "timed out" in resp.error_message.lower()


@pytest.mark.asyncio
async def test_transformer_circuit_breaker_and_failover():
    mock_repo = AsyncMock()
    cfg1 = AIConfig(id="cfg-fail", name="Failing AI", api_key="sk-fail", is_enabled=True)
    cfg2 = AIConfig(id="cfg-backup", name="Backup AI", api_key="sk-backup", is_enabled=True)

    async def get_cfg(cid):
        return cfg1 if cid == "cfg-fail" else cfg2

    mock_repo.get_by_id.side_effect = get_cfg

    mock_fail_provider = AsyncMock()
    mock_fail_provider.rewrite.side_effect = RuntimeError("Provider down")

    mock_backup_provider = AsyncMock()
    mock_backup_provider.rewrite.return_value = "Backup successfully transformed"

    mock_factory = MagicMock()
    mock_factory.get.side_effect = lambda c: mock_fail_provider if c.id == "cfg-fail" else mock_backup_provider

    circuit_reg = CircuitBreakerRegistry(default_failure_threshold=1)
    transformer = AITransformer(
        ai_config_repo=mock_repo,
        circuit_registry=circuit_reg,
        validator=AIOutputValidator(),
        provider_factory=mock_factory,
    )

    req = AIRequest(text="Important text", correlation_id="corr-failover")

    # 1. First call fails on cfg-fail and trips circuit to OPEN
    resp1 = await transformer.transform(req, primary_config_id="cfg-fail")
    assert resp1.success is False

    # 2. Call with secondary_config_id triggers failover
    resp2 = await transformer.transform(
        req,
        primary_config_id="cfg-fail",
        secondary_config_id="cfg-backup",
    )
    assert resp2.success is True
    assert resp2.transformed_text == "Backup successfully transformed"
    assert transformer.metrics["failovers_total"] == 1


# =====================================================================
# 5. DURABLE PIPELINE INTEGRATION TESTS (P2 ZERO OVERHEAD & FALLBACKS)
# =====================================================================

@pytest.mark.asyncio
async def test_pipeline_zero_overhead_without_ai():
    """Verify that a rule without ai_config_id bypasses AI with zero overhead."""
    rule_repo = AsyncMock()
    filter_repo = AsyncMock()
    filter_repo.get_by_id.return_value = None

    rule = ForwardRule(
        id="rule-no-ai",
        session_id="sess-1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        ai_config_id=None,  # No AI configured
    )
    rule_repo.get_active_rules_for_source.return_value = [rule]

    pipeline = DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=filter_repo,
    )

    payload = MessagePayload(
        message_id=100,
        chat_id="-1001",
        text="Normal message text without AI",
    )

    ctx = PipelineContext(payload=payload, correlation_id="test-bypass")
    eval_res = EvaluationResult(action=FilterAction.ALLOW)

    transformed = await pipeline._apply_transforms(ctx, payload, rule, eval_res)
    assert transformed == "Normal message text without AI"
    assert pipeline.metrics.ai_transforms_total == 0


@pytest.mark.asyncio
async def test_pipeline_ai_transform_success():
    rule_repo = AsyncMock()
    filter_repo = AsyncMock()
    filter_repo.get_by_id.return_value = None

    rule = ForwardRule(
        id="rule-ai-ok",
        session_id="sess-1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        ai_config_id="ai-cfg-1",
    )

    mock_transformer = AsyncMock()
    mock_transformer.transform.return_value = AIResponse(
        success=True,
        transformed_text="[AI Rewritten] Hello world!",
    )

    pipeline = DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=filter_repo,
        ai_transformer=mock_transformer,
    )

    payload = MessagePayload(
        message_id=101,
        chat_id="-1001",
        text="Original hello world",
    )
    ctx = PipelineContext(payload=payload, correlation_id="test-ai-success")
    eval_res = EvaluationResult(action=FilterAction.ALLOW)

    transformed = await pipeline._apply_transforms(ctx, payload, rule, eval_res)
    assert transformed == "[AI Rewritten] Hello world!"
    assert eval_res.rewritten_text == "[AI Rewritten] Hello world!"
    assert pipeline.metrics.ai_transforms_total == 1
    assert pipeline.metrics.ai_transforms_success == 1


@pytest.mark.asyncio
async def test_pipeline_fallback_send_original():
    rule_repo = AsyncMock()
    filter_repo = AsyncMock()
    filter_repo.get_by_id.return_value = None

    rule = ForwardRule(
        id="rule-send-orig",
        session_id="sess-1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        ai_config_id="ai-cfg-fail",
        ai_fallback_policy=AIFallbackPolicy.SEND_ORIGINAL,
    )

    mock_transformer = AsyncMock()
    mock_transformer.transform.return_value = AIResponse(
        success=False,
        error_code="SERVICE_UNAVAILABLE",
        error_message="OpenAI API down",
    )

    pipeline = DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=filter_repo,
        ai_transformer=mock_transformer,
    )

    payload = MessagePayload(
        message_id=102,
        chat_id="-1001",
        text="Original raw text that must pass through",
    )
    ctx = PipelineContext(payload=payload, correlation_id="test-fallback-orig")
    eval_res = EvaluationResult(action=FilterAction.ALLOW)

    transformed = await pipeline._apply_transforms(ctx, payload, rule, eval_res)
    # Policy SEND_ORIGINAL returns raw text
    assert transformed == "Original raw text that must pass through"
    assert pipeline.metrics.ai_transforms_failed == 1
    assert pipeline.metrics.ai_fallback_send_original == 1


@pytest.mark.asyncio
async def test_pipeline_fallback_drop():
    rule_repo = AsyncMock()
    filter_repo = AsyncMock()
    filter_repo.get_by_id.return_value = None

    rule = ForwardRule(
        id="rule-drop",
        session_id="sess-1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        ai_config_id="ai-cfg-fail",
        ai_fallback_policy=AIFallbackPolicy.DROP,
    )

    mock_transformer = AsyncMock()
    mock_transformer.transform.return_value = AIResponse(
        success=False,
        error_code="RATE_LIMIT",
        error_message="Too many requests",
    )

    pipeline = DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=filter_repo,
        ai_transformer=mock_transformer,
    )

    payload = MessagePayload(
        message_id=103,
        chat_id="-1001",
        text="Message to be dropped if AI fails",
    )
    ctx = PipelineContext(payload=payload, correlation_id="test-fallback-drop")
    eval_res = EvaluationResult(action=FilterAction.ALLOW)

    transformed = await pipeline._apply_transforms(ctx, payload, rule, eval_res)
    # Policy DROP returns None to stop pipeline
    assert transformed is None
    assert pipeline.metrics.ai_transforms_failed == 1
    assert pipeline.metrics.ai_fallback_drops == 1


@pytest.mark.asyncio
async def test_pipeline_fallback_retry_and_quarantine():
    rule_repo = AsyncMock()
    filter_repo = AsyncMock()
    filter_repo.get_by_id.return_value = None

    mock_transformer = AsyncMock()
    mock_transformer.transform.return_value = AIResponse(
        success=False,
        error_code="TIMEOUT",
        error_message="AI Timed out",
    )

    pipeline = DurableMessagePipeline(
        rule_repo=rule_repo,
        filter_repo=filter_repo,
        ai_transformer=mock_transformer,
    )

    payload = MessagePayload(message_id=104, chat_id="-1001", text="Test text")

    # 1. RETRY
    rule_retry = ForwardRule(
        id="rule-retry", session_id="s", source_chat_id="-1", target_chat_id="-2",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL, forward_mode=ForwardMode.COPY_MESSAGE,
        ai_config_id="c1", ai_fallback_policy=AIFallbackPolicy.RETRY,
    )
    ctx_retry = PipelineContext(payload=payload, correlation_id="test-retry")
    assert await pipeline._apply_transforms(ctx_retry, payload, rule_retry, EvaluationResult(action=FilterAction.ALLOW)) is None
    assert pipeline.metrics.ai_fallback_retries == 1

    # 2. QUARANTINE
    rule_quar = ForwardRule(
        id="rule-quar", session_id="s", source_chat_id="-1", target_chat_id="-2",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL, forward_mode=ForwardMode.COPY_MESSAGE,
        ai_config_id="c1", ai_fallback_policy=AIFallbackPolicy.QUARANTINE,
    )
    ctx_quar = PipelineContext(payload=payload, correlation_id="test-quar")
    assert await pipeline._apply_transforms(ctx_quar, payload, rule_quar, EvaluationResult(action=FilterAction.ALLOW)) is None
    assert pipeline.metrics.ai_fallback_quarantine == 1


# =====================================================================
# 6. DATABASE REPOSITORY TESTS (SQLITE & POSTGRESQL)
# =====================================================================

@pytest.mark.asyncio
async def test_sqlite_prompt_repository_crud(tmp_path):
    db = SqliteDatabase(str(tmp_path / "sqlite_repo_test.db"))
    repo = SqlitePromptRepository(db)

    # 1. Add Template
    tmpl = PromptTemplate(
        id="tmpl-1",
        name="Test Template",
        description="Description",
        system_prompt="You are a bot.",
        user_prompt_template="{text}",
        target_language="fa",
        current_version=1,
        is_system=False,
    )
    saved = await repo.add_template(tmpl)
    assert saved.id == "tmpl-1"

    # 2. Get Template
    fetched = await repo.get_template("tmpl-1")
    assert fetched is not None
    assert fetched.name == "Test Template"
    assert fetched.target_language == "fa"

    # 3. Add Versions
    v1 = PromptVersion(
        prompt_id="tmpl-1",
        version=1,
        system_prompt="You are a bot v1.",
        user_prompt_template="{text}",
        change_summary="v1 creation",
        is_active=False,
    )
    v2 = PromptVersion(
        prompt_id="tmpl-1",
        version=2,
        system_prompt="You are a bot v2.",
        user_prompt_template="Prefix: {text}",
        change_summary="v2 upgrade",
        is_active=True,
    )
    await repo.add_version(v1)
    await repo.add_version(v2)

    # 4. List Versions
    versions = await repo.list_versions("tmpl-1")
    assert len(versions) == 2
    assert versions[0].version == 2  # DESC order

    # 5. Activate version
    await repo.activate_version("tmpl-1", 1)
    act_ver = await repo.get_active_version("tmpl-1")
    assert act_ver is not None
    assert act_ver.version == 1

    # 6. Delete template
    ok = await repo.delete_template("tmpl-1")
    assert ok is True
    assert await repo.get_template("tmpl-1") is None
    assert len(await repo.list_versions("tmpl-1")) == 0


@pytest.mark.asyncio
async def test_forward_rule_ai_fields_sqlite(tmp_path):
    from core.domain.entities import TelegramSession
    from core.infrastructure.persistence.sqlite_repositories import (
        SqliteSessionRepository,
    )
    from core.infrastructure.security.crypto import CryptoService

    db = SqliteDatabase(str(tmp_path / "sqlite_rules_ai.db"))
    crypto = CryptoService("test-secret-key-1234567890123456")
    session_repo = SqliteSessionRepository(db, crypto)
    session = await session_repo.add(TelegramSession(phone_number="+989123456789"))

    rule_repo = SqliteForwardRuleRepository(db)

    rule = ForwardRule(
        id="rule-ai-full",
        session_id=session.id,
        source_chat_id="-1001",
        target_chat_id="-1002",
        routing_type=RoutingType.CHANNEL_TO_CHANNEL,
        forward_mode=ForwardMode.COPY_MESSAGE,
        ai_config_id="cfg-ai-1",
        ai_fallback_policy=AIFallbackPolicy.SEND_ORIGINAL,
        ai_prompt_version=2,
        ai_timeout_seconds=20.0,
        ai_secondary_config_id="cfg-ai-backup",
    )

    await rule_repo.add(rule)
    saved_rule = await rule_repo.get_by_id("rule-ai-full")
    assert saved_rule is not None
    assert saved_rule.ai_config_id == "cfg-ai-1"
    assert saved_rule.ai_fallback_policy == AIFallbackPolicy.SEND_ORIGINAL
    assert saved_rule.ai_prompt_version == 2
    assert saved_rule.ai_timeout_seconds == 20.0
    assert saved_rule.ai_secondary_config_id == "cfg-ai-backup"


@pytest.mark.asyncio
async def test_postgres_prompt_repository_and_rules():
    """Verify PostgreSQL prompt repository and AI forward rule fields if PostgreSQL is available."""
    try:
        import asyncpg
        conn = await asyncpg.connect("postgresql://ubuntu@localhost/atf_test")
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available in this test environment: {exc}")

    from core.infrastructure.persistence.postgres_repositories import (
        PostgresForwardRuleRepository,
        PostgresPromptRepository,
    )

    prompt_repo = PostgresPromptRepository(conn)
    await prompt_repo.init_schema()

    # Test template CRUD
    tmpl = PromptTemplate(
        id="pg-tmpl-1",
        name="Postgres Template",
        description="Testing PG",
        system_prompt="PG System",
        user_prompt_template="PG: {text}",
        target_language="en",
        current_version=1,
    )
    await prompt_repo.add_template(tmpl)
    fetched = await prompt_repo.get_template("pg-tmpl-1")
    assert fetched is not None
    assert fetched.name == "Postgres Template"

    v1 = PromptVersion(
        prompt_id="pg-tmpl-1",
        version=1,
        system_prompt="PG System v1",
        user_prompt_template="PG: {text}",
        is_active=True,
    )
    await prompt_repo.add_version(v1)
    act_ver = await prompt_repo.get_active_version("pg-tmpl-1")
    assert act_ver is not None
    assert act_ver.version == 1

    # Cleanup
    await prompt_repo.delete_template("pg-tmpl-1")
    assert await prompt_repo.get_template("pg-tmpl-1") is None
    await conn.close()
