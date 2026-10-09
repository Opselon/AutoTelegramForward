import asyncio
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from core.infrastructure.telegram.pv_responder import AIPVResponder, PVResponderConfig
from core.domain.entities import AIConfig, AIProviderType


@pytest.fixture
def temp_config_file(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    import core.infrastructure.telegram.pv_responder as pvr_mod
    monkeypatch.setattr(pvr_mod, "CONFIG_PATH", Path(path))
    yield Path(path)
    if os.path.exists(path):
        os.remove(path)


@pytest.mark.asyncio
async def test_pv_responder_config_persistence(temp_config_file):
    responder = AIPVResponder()
    assert responder.config.enabled is False
    assert responder.config.ignore_bots is True
    assert responder.config.cooldown_seconds == 15

    # Update config
    responder.config.enabled = True
    responder.config.persona_prompt = "لحن خودمونی و باحال"
    responder.config.typing_delay_min = 1.5
    responder.config.typing_delay_max = 3.5
    responder.save_config(responder.config)

    # Reload in fresh instance
    responder2 = AIPVResponder()
    assert responder2.config.enabled is True
    assert responder2.config.persona_prompt == "لحن خودمونی و باحال"
    assert responder2.config.typing_delay_min == 1.5
    assert responder2.config.typing_delay_max == 3.5


@pytest.mark.asyncio
async def test_pv_responder_message_handling(temp_config_file):
    responder = AIPVResponder()
    responder.config.enabled = True
    responder.config.typing_delay_min = 0.05
    responder.config.typing_delay_max = 0.1
    responder.config.cooldown_seconds = 2

    # Mock client and message
    mock_client = AsyncMock()
    mock_client.session_id = "sess_1"

    async def mock_history(*args, **kwargs):
        return
        yield

    mock_client.get_chat_history = MagicMock(return_value=mock_history())

    mock_msg = AsyncMock()
    mock_msg.id = 999
    mock_msg.chat.id = 12345
    mock_msg.chat.type = "private"
    mock_msg.from_user.id = 555
    mock_msg.from_user.is_self = False
    mock_msg.from_user.is_bot = False
    mock_msg.text = "سلام داداش کجایی؟"

    # Mock AI repo returning enabled config
    fake_ai_cfg = AIConfig(
        id="ai-1",
        name="Test AI",
        provider=AIProviderType.OPENAI,
        model="gpt-4o-mini",
        api_key="sk-test",
        is_enabled=True,
    )
    mock_ai_repo = AsyncMock()
    mock_ai_repo.get.return_value = fake_ai_cfg
    mock_ai_repo.list_all.return_value = [fake_ai_cfg]
    responder._ai_repo = mock_ai_repo

    with patch("core.infrastructure.ai.providers.OpenAICompatibleProvider.chat_complete", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = "سلام به روی ماهت، جانم بگو"

        handled = await responder.handle_message(mock_client, mock_msg)
        assert handled is True
        mock_client.send_message.assert_awaited_once_with(12345, "سلام به روی ماهت، جانم بگو")

        # Second message immediately should trigger cooldown
        mock_client.send_message.reset_mock()
        handled2 = await responder.handle_message(mock_client, mock_msg)
        assert handled2 is False
        mock_client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_bot_manager_no_spurious_unknown_command():
    from core.infrastructure.telegram.bot_manager import BotManager

    bm = BotManager.__new__(BotManager)
    bm.ui_step_checker = None
    bm._admin_ids = {12345}
    mock_ui = MagicMock()
    mock_ui._main_menu.return_value = "main_keyboard"
    bm.bot_ui = mock_ui
    bm._login = MagicMock()
    bm._login.pending_for.return_value = None
    bm._awaiting_lang = {}

    mock_msg = AsyncMock()
    mock_msg.chat.id = 12345
    mock_msg.chat.type.value = "private"
    mock_msg.from_user.id = 12345
    mock_msg.text = "یک متن رندوم خارج از دستور"

    # Route text
    await bm._route_text(mock_msg)
    
    # Verify main menu was replied and no error was sent
    mock_msg.reply_text.assert_awaited_once_with(
        "💡 برای مدیریت فوروارد و دسترسی به امکانات ربات، از دکمه‌های زیر استفاده کنید:",
        reply_markup="main_keyboard",
    )
