"""Tests for v2.0 Web Dashboard & Telegram Bot Dashboard Full Parity."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from core.domain.entities import ForwardRule
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, CB, UiState
from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES


@pytest.fixture
def mock_pro_bot():
    bot = MagicMock()
    i18n = I18n()
    sessions = AsyncMock()
    sessions.list_all = AsyncMock(return_value=[])
    sessions.list_by_owner = AsyncMock(return_value=[])

    rules = AsyncMock()
    rules.list_all = AsyncMock(return_value=[])
    rules.list_by_owner = AsyncMock(return_value=[])
    rules.get = AsyncMock()
    rules.save = AsyncMock()

    filter_rules = AsyncMock()
    filter_rules.list_all = AsyncMock(return_value=[])
    filter_rules.list_by_owner = AsyncMock(return_value=[])

    ai_configs = AsyncMock()
    pipeline = MagicMock()
    pool = MagicMock()
    login = MagicMock()
    log_client = MagicMock()

    ui = ProBotUI(
        bot=bot,
        i18n=i18n,
        sessions=sessions,
        rules=rules,
        filter_rules=filter_rules,
        ai_configs=ai_configs,
        pipeline=pipeline,
        pool=pool,
        login=login,
        log_client=log_client,
        admin_ids=[123456],
    )
    return ui


@pytest.fixture
def sample_rule():
    return ForwardRule(
        id="42",
        name="thetanews",
        source_chat_id="1542369967",
        target_chat_id="1604203662",
        is_active=True,
        metadata={
            "owner_id": 123456,
            "show_forward_header": True,
            "duplicate_filters": True,
            "sync_edits": True,
            "sync_deletes": True,
            "sync_replies": True,
            "cleaner_enabled": True,
            "cleaner_config": {
                "patterns": ["test_pattern", "promo_link"],
                "remove_urls": True,
            },
            "delay_seconds": 15.0,
            "forward_delay_seconds": 15.0,
            "block_links": True,
            "block_voice": False,
        },
    )


def test_ten_languages_supported_in_bot(mock_pro_bot):
    ui = mock_pro_bot
    markup = ui._lang_menu()
    assert markup is not None
    callback_data_list = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    expected_langs = ["en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it"]
    for lang in expected_langs:
        assert f"lgs:{lang}" in callback_data_list


def test_render_rule_detail_features(mock_pro_bot, sample_rule):
    ui = mock_pro_bot
    text, markup = ui._render_rule_detail(sample_rule)
    
    assert "will not work when show header" in text
    assert "thetanews" in text
    
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "rtg_fwdhdr:42" in callbacks
    assert "rtg_repsync:42" in callbacks
    assert "rtg_dup:42" in callbacks
    assert "rcl:42" in callbacks
    assert "rdly:42" in callbacks
    assert "rfl_bl:42" in callbacks
    assert "rfl_wl:42" in callbacks
    assert "rfl:42" in callbacks
    assert "rtx:42" in callbacks


def test_render_delay_menu(mock_pro_bot, sample_rule):
    ui = mock_pro_bot
    text, markup = ui._render_delay_menu(sample_rule)
    assert "15 ثانیه" in text
    assert "Set Delay" in text or "تأخیر" in text
    
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "rdly_s:42:0" in callbacks
    assert "rdly_s:42:10" in callbacks
    assert "rdly_s:42:30" in callbacks
    assert "rdly_s:42:60" in callbacks
    assert "rdly_c:42" in callbacks
    assert "rd:42" in callbacks


def test_render_cleaner_menu(mock_pro_bot, sample_rule):
    ui = mock_pro_bot
    text, markup = ui._render_cleaner_menu(sample_rule)
    assert "Cleaner" in text or "پاک‌کننده" in text
    
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "rcl_url:42" in callbacks
    assert "rcl_usr:42" in callbacks
    assert "rcl_tag:42" in callbacks
    assert "rd:42" in callbacks


def test_render_filters_menu(mock_pro_bot, sample_rule):
    ui = mock_pro_bot
    text, markup = ui._render_filters_menu(sample_rule)
    assert "Filters" in text or "فیلتر" in text
    
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "rfl_lnk:42" in callbacks
    assert "rfl_vid:42" in callbacks
    assert "rfl_aud:42" in callbacks
    assert "rfl_stk:42" in callbacks
    assert "rfl_txt:42" in callbacks
    assert "rfl_voc:42" in callbacks
    assert "rd:42" in callbacks


def test_render_blacklist_menu(mock_pro_bot, sample_rule):
    sample_rule.blacklist = ["badword", "spam"]
    ui = mock_pro_bot
    text, markup = ui._render_blacklist_menu(sample_rule)
    assert "Blacklist" in text or "لیست سیاه" in text
    assert "badword" in text
    
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "rfl_bl_add:42" in callbacks
    assert "rfl_bl_clr:42" in callbacks
    assert "rd:42" in callbacks


def test_render_whitelist_menu(mock_pro_bot, sample_rule):
    sample_rule.whitelist = ["crypto", "btc"]
    ui = mock_pro_bot
    text, markup = ui._render_whitelist_menu(sample_rule)
    assert "Whitelist" in text or "لیست سفید" in text
    assert "crypto" in text
    
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "rfl_wl_add:42" in callbacks
    assert "rfl_wl_clr:42" in callbacks
    assert "rd:42" in callbacks


def test_render_replace_menu(mock_pro_bot, sample_rule):
    sample_rule.replacements = {"@old": "@new"}
    ui = mock_pro_bot
    text, markup = ui._render_replace_menu(sample_rule)
    assert "Replace" in text or "جایگزینی" in text
    assert "@old" in text
    assert "@new" in text
    
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "rtxa:42" in callbacks
    assert "rtxc:42" in callbacks
    assert "rd:42" in callbacks


@pytest.mark.asyncio
async def test_step_set_delay_valid(mock_pro_bot, sample_rule):
    ui = mock_pro_bot
    ui._owned_rule = AsyncMock(return_value=sample_rule)
    ui._save_owned_rule = AsyncMock()
    
    msg = MagicMock()
    msg.from_user.id = 123456
    msg.reply_text = AsyncMock()
    st = UiState()
    
    await ui._step_set_delay(msg, st, "45", "42")
    assert sample_rule.delay_seconds == 45.0
    ui._save_owned_rule.assert_awaited_once_with(sample_rule, 123456)


@pytest.mark.asyncio
async def test_step_add_bl(mock_pro_bot, sample_rule):
    ui = mock_pro_bot
    ui._owned_rule = AsyncMock(return_value=sample_rule)
    ui._save_owned_rule = AsyncMock()
    
    msg = MagicMock()
    msg.from_user.id = 123456
    msg.reply_text = AsyncMock()
    st = UiState()
    
    await ui._step_add_bl(msg, st, "badword, spammer", "42")
    assert "badword" in sample_rule.blacklist
    assert "spammer" in sample_rule.blacklist
    ui._save_owned_rule.assert_awaited_once_with(sample_rule, 123456)
