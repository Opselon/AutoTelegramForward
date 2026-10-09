"""Comprehensive Tests for AI Configuration UX, Host URL customization, and Live Health Checks."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from core.application.use_cases import AIConfigUseCases
from core.domain.entities import AIConfig
from core.domain.value_objects import AIProviderType
from core.infrastructure.ai.providers import (
    AIProviderFactory,
    OpenAICompatibleProvider,
    _post_json,
)
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState


def test_openai_compatible_base_url_normalization():
    p = OpenAICompatibleProvider()

    # Empty base_url uses default
    cfg = AIConfig(provider=AIProviderType.NINEROUTER, base_url="")
    assert p.base_url(cfg) == "http://sub.legoten.com:4455/v1"

    # Trailing slash is removed
    cfg2 = AIConfig(provider=AIProviderType.OPENAI, base_url="https://api.openai.com/v1/")
    assert p.base_url(cfg2) == "https://api.openai.com/v1"

    # Missing /v1 is automatically appended
    cfg3 = AIConfig(provider=AIProviderType.CUSTOM, base_url="http://sub.legoten.com:4455")
    assert p.base_url(cfg3) == "http://sub.legoten.com:4455/v1"

    # URL ending with /v1 stays as is
    cfg4 = AIConfig(provider=AIProviderType.CUSTOM, base_url="http://sub.legoten.com:4455/v1")
    assert p.base_url(cfg4) == "http://sub.legoten.com:4455/v1"


@pytest.mark.asyncio
async def test_openai_compatible_rewrite_sends_stream_false():
    p = OpenAICompatibleProvider()
    cfg = AIConfig(
        provider=AIProviderType.CUSTOM,
        base_url="http://sub.legoten.com:4455/v1",
        api_key="test-key",
        model="coding",
    )

    with patch("core.infrastructure.ai.providers._post_json", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {
            "choices": [{"message": {"content": "pong response"}}]
        }
        res = await p.rewrite(cfg, "ping")
        assert res == "pong response"
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert kwargs["payload"]["stream"] is False
        assert kwargs["payload"]["model"] == "coding"


@pytest.mark.asyncio
async def test_post_json_sse_stream_fallback():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.side_effect = ValueError("SSE stream")
    mock_resp.text = 'data: {"choices":[{"delta":{"content":"po"}}]}\ndata: {"choices":[{"delta":{"content":"ng"}}]}\ndata: [DONE]\n'

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_client_post:
        mock_client_post.return_value = mock_resp
        data = await _post_json("http://test.url", headers={}, payload={"stream": False})
        assert data["choices"][0]["message"]["content"] == "pong"


@pytest.mark.asyncio
async def test_ai_use_cases_health_check():
    repo = AsyncMock()
    factory = MagicMock()
    provider = AsyncMock()
    provider.check_health.return_value = (True, "pong", 145.2)
    factory.get.return_value = provider

    cfg = AIConfig(
        id="cfg-1",
        name="9Router Test",
        provider=AIProviderType.NINEROUTER,
        model="coding",
    )
    repo.get_by_id.return_value = cfg

    use_cases = AIConfigUseCases(repo, factory)
    ok, reply, latency = await use_cases.health_check("cfg-1")

    assert ok is True
    assert reply == "pong"
    assert latency == 145.2

    # Non-existent config
    repo.get_by_id.return_value = None
    ok2, err2, _ = await use_cases.health_check("non-existent")
    assert ok2 is False
    assert err2 == "config_not_found"


def test_pro_bot_ui_menu_and_detail_rendering():
    ui = ProBotUI.__new__(ProBotUI)
    setattr(ui, "_t", lambda key, **kwargs: key)
    setattr(ui, "_kbd", lambda rows: rows)
    setattr(ui, "_provider_label", lambda key: key.upper())

    cfg = AIConfig(
        id="d63b3267-ceb0-45b2-bf39-7dbdc935a9ba",
        name="9Router Main",
        provider=AIProviderType.NINEROUTER,
        model="coding",
        api_key="1234567890abcdef1234567890",
        base_url="http://sub.legoten.com:4455/v1",
        is_enabled=True,
    )

    # 1. Menu with buttons for each config
    menu_kbd: Any = ui._ai_menu([cfg])
    assert menu_kbd[0][0][1] == f"aid:{cfg.id}"

    # 2. Detail rendering with masked API key and health info
    health_info = {
        "ok": True,
        "latency_ms": 120.4,
        "reply": "pong 🏓",
    }
    text, kbd = ui._render_ai_detail(cfg, health_info=health_info)
    assert "1234567...7890" in text
    assert "http://sub.legoten.com:4455/v1" in text
    assert "120ms" in text
    assert "pong 🏓" in text
    assert "سالم و متصل" in text

    # Verify buttons inside detail view
    kbd_any: Any = kbd
    button_callbacks = [col[1] for row in kbd_any for col in row]
    assert f"ai_hc:{cfg.id}" in button_callbacks
    assert f"ai_sample:{cfg.id}" in button_callbacks
    assert f"ai_eh:{cfg.id}" in button_callbacks
    assert f"ai_em:{cfg.id}" in button_callbacks
    assert f"ai_ek:{cfg.id}" in button_callbacks
    assert f"ai_tog:{cfg.id}" in button_callbacks
    assert f"ai_del_ask:{cfg.id}" in button_callbacks


@pytest.mark.asyncio
async def test_ui_state_base_url_persistence():
    st = UiState(
        step="ai_base_url",
        ai_name="test-ai",
        ai_provider="custom",
        ai_base_url="http://localhost:8000/v1",
    )
    d = st.to_dict()
    assert d["ai_base_url"] == "http://localhost:8000/v1"

    st_restored = UiState.from_dict(d)
    assert st_restored.ai_base_url == "http://localhost:8000/v1"


@pytest.mark.asyncio
async def test_pro_bot_ui_edit_host_flow():
    ui = ProBotUI.__new__(ProBotUI)
    ui._state = MagicMock()
    ui._persist = AsyncMock()
    ui._t = lambda key, **kwargs: key
    ui._kbd = lambda rows: rows
    ui._provider_label = lambda key: key.upper()
    ui._ai = AsyncMock()

    cfg = AIConfig(
        id="cfg-test",
        name="Test AI",
        provider=AIProviderType.CUSTOM,
        model="coding",
        base_url="http://old-url.com/v1",
    )
    ui._ai.get.return_value = cfg
    ui._ai.health_check.return_value = (True, "pong", 50.0)

    # 1. Trigger edit host step
    st = UiState()
    msg = AsyncMock()
    msg.from_user.id = 123
    st.buffer["ai_edit_id"] = "cfg-test"
    st.step = "ai_edit_host"

    await ui._step_ai_edit_host(msg, st, "http://sub.legoten.com:4455/v1")

    assert cfg.base_url == "http://sub.legoten.com:4455/v1"
    ui._ai.update.assert_called_once_with(cfg)
    msg.reply_text.assert_called_once()
    assert "آدرس هاست به‌روزرسانی شد" in msg.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_pro_bot_ui_wizard_base_url_step():
    ui = ProBotUI.__new__(ProBotUI)
    ui._persist = AsyncMock()
    ui._t = lambda key, **kwargs: key
    ui._kbd = lambda rows: rows
    ui._provider_label = lambda key: key.upper()
    ui._model_picker = MagicMock(return_value=None)
    ui._cancel_kbd = MagicMock(return_value=[])

    st = UiState(step="ai_base_url", ai_provider="9router")
    st.buffer["default_base_url"] = "http://sub.legoten.com:4455/v1"

    msg = AsyncMock()
    msg.from_user.id = 123

    # Entering custom URL
    await ui._step_ai_base_url(msg, st, "http://my-host.internal:4455")
    assert st.ai_base_url == "http://my-host.internal:4455"
    assert st.step == "ai_model"
    msg.reply_text.assert_called_once()

