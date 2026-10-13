import pytest
from core.domain.entities import ForwardRule
from core.infrastructure.telegram.pro_bot_ui import ProBotUI


def test_forward_rule_parity_properties():
    rule = ForwardRule(
        id="rule-test-1",
        session_id="session-1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        source_chat_name="AF Route Lab Source",
        target_chat_name="AF Route Lab Target",
    )

    # Defaults
    assert rule.topic_id is None
    assert rule.debug_mode is False
    assert rule.sender_type == "Personal Telegram Account"
    assert rule.feature_group == "all"
    assert rule.duplicate_filters is False
    assert rule.crypto_mode is False
    assert rule.auto_posting is False
    assert rule.power_control is False
    assert rule.process_raw_text is False
    assert rule.button_to_text is False
    assert rule.ocr_image_ai is False
    assert rule.smart_ai_rewrite is False
    assert rule.news_digest is False
    assert rule.ai_creativity == 0.7
    assert rule.ai_max_tokens == 600
    assert isinstance(rule.paid_addons, dict)

    # Mutations via properties
    rule.topic_id = 42
    rule.debug_mode = True
    rule.sender_type = "Bot Account"
    rule.feature_group = "crypto"
    rule.duplicate_filters = True
    rule.crypto_mode = True
    rule.auto_posting = True
    rule.power_control = True
    rule.process_raw_text = True
    rule.button_to_text = True
    rule.ocr_image_ai = True
    rule.smart_ai_rewrite = True
    rule.news_digest = True
    rule.paid_addons = {"channel_comments": True, "every_n": False}

    assert rule.topic_id == 42
    assert rule.debug_mode is True
    assert rule.sender_type == "Bot Account"
    assert rule.feature_group == "crypto"
    assert rule.duplicate_filters is True
    assert rule.crypto_mode is True
    assert rule.auto_posting is True
    assert rule.power_control is True
    assert rule.process_raw_text is True
    assert rule.button_to_text is True
    assert rule.ocr_image_ai is True
    assert rule.smart_ai_rewrite is True
    assert rule.news_digest is True
    assert rule.paid_addons["channel_comments"] is True
    assert rule.paid_addons["every_n"] is False


@pytest.mark.asyncio
async def test_bot_ui_task_overview_rendering():
    ui = object.__new__(ProBotUI)
    ui._render_rule_detail = ProBotUI._render_rule_detail.__get__(ui, ProBotUI)
    ui._kbd = ProBotUI._kbd.__get__(ui, ProBotUI)
    ui._t = lambda key, **kw: key

    rule = ForwardRule(
        id="af_route_lab_b_12345",
        name="af_route_lab_b",
        session_id="session-1",
        source_chat_id="-1001",
        target_chat_id="-1002",
        source_chat_name="AF Route Lab Source",
        target_chat_name="AF Route Lab Target",
        is_active=True,
    )
    rule.topic_id = 99
    rule.debug_mode = False
    rule.sender_type = "Personal Telegram Account"
    rule.feature_group = "all"

    text, kbd = ui._render_rule_detail(rule)

    # Verify Task Overview Content
    assert "af_route_lab_b" in text
    assert "Task Overview" in text
    assert "RUNNING 🟢" in text
    assert "AF Route Lab Source" in text
    assert "AF Route Lab Target" in text
    assert "Personal Telegram Account" in text
    assert "PAID ADDON" in text

    # Verify 4 Colored Action Buttons
    cbs = [btn.callback_data for row in kbd.inline_keyboard for btn in row]
    assert f"res:{rule.id}" in cbs   # Edit Source (Green)
    assert f"ret:{rule.id}" in cbs   # Edit Target (Red)
    assert f"rtpc:{rule.id}" in cbs  # Set up Forwarding Topic (Blue)
    assert f"radv:{rule.id}" in cbs  # Advanced Settings (Dark)

    # Verify Chips and Addons
    assert f"rgrp:{rule.id}:all" in cbs
    assert f"rgrp:{rule.id}:crypto" in cbs
    assert f"radon_all:{rule.id}" in cbs


@pytest.mark.asyncio
async def test_bot_ui_advanced_settings_and_all_addons():
    ui = object.__new__(ProBotUI)
    ui._render_advanced_settings = ProBotUI._render_advanced_settings.__get__(ui, ProBotUI)
    ui._render_all_addons = ProBotUI._render_all_addons.__get__(ui, ProBotUI)
    ui._kbd = ProBotUI._kbd.__get__(ui, ProBotUI)
    ui._t = lambda key, **kw: key

    rule = ForwardRule(
        id="rule_adv_123",
        name="af_route_lab_b",
        session_id="session-1",
        source_chat_id="-1001",
        target_chat_id="-1002",
    )
    rule.duplicate_filters = True
    rule.crypto_mode = True
    rule.ocr_image_ai = True
    rule.smart_ai_rewrite = True

    # 1. Advanced Settings View
    adv_text, adv_kbd = ui._render_advanced_settings(rule)
    assert "Filters (?)" in adv_text
    assert "Schedules (?)" in adv_text
    assert "Time Forward (?)" in adv_text
    assert "Processing Messages (?)" in adv_text
    assert "Modify Content (?)" in adv_text
    assert "AI Features" in adv_text
    assert "Creativity 0.7 · Max Tokens 600" in adv_text

    adv_cbs = [btn.callback_data for row in adv_kbd.inline_keyboard for btn in row]
    assert f"rtg_dup:{rule.id}" in adv_cbs
    assert f"rtg_crypto:{rule.id}" in adv_cbs
    assert f"rtg_raw:{rule.id}" in adv_cbs
    assert f"rtg_btn2txt:{rule.id}" in adv_cbs
    assert f"rtg_ocr:{rule.id}" in adv_cbs
    assert f"rtg_ai:{rule.id}" in adv_cbs
    assert f"rd:{rule.id}" in adv_cbs  # Back to Task Overview

    # 2. All 18 Addons Catalogue
    add_text, add_kbd = ui._render_all_addons(rule)
    assert "PAID ADDON Catalogue — 18 Modular Extensions" in add_text
    add_cbs = [btn.callback_data for row in add_kbd.inline_keyboard for btn in row]
    assert f"radon_tg:{rule.id}:comments" in add_cbs
    assert f"radon_tg:{rule.id}:webhook" in add_cbs
    assert f"radon_tg:{rule.id}:crypto" in add_cbs
    assert f"radon_tg:{rule.id}:ocr_ai" in add_cbs
    assert f"rd:{rule.id}" in add_cbs
