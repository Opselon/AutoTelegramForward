"""Tests for Telegram Bot Main Menu Redesign & Dashboard (v17)."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, CB
from core.infrastructure.telegram.i18n import I18n


@pytest.fixture
def mock_pro_bot_ui():
    bot = MagicMock()
    i18n = I18n()
    sessions = AsyncMock()
    sessions.list_all = AsyncMock(return_value=[])
    sessions.list_by_owner = AsyncMock(return_value=[])

    rules = AsyncMock()
    rules.list_all = AsyncMock(return_value=[])
    rules.list_by_owner = AsyncMock(return_value=[])

    filter_rules = AsyncMock()
    filter_rules.list_all = AsyncMock(return_value=[])
    filter_rules.list_by_owner = AsyncMock(return_value=[])

    ai_configs = AsyncMock()
    pipeline = MagicMock()
    pipeline.stats.processed = 10
    pipeline.stats.forwarded = 8
    pipeline.stats.filtered = 2
    pipeline.stats.rewritten = 0
    pipeline.stats.errors = 0
    pipeline.stats.started_at = 1000

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


@pytest.mark.asyncio
async def test_main_text_formatting_english(mock_pro_bot_ui):
    ui = mock_pro_bot_ui
    ui.i18n.current = "en"

    fake_user = MagicMock()
    fake_user.first_name = "Mr Scott"
    fake_user.last_name = "BAUER"
    fake_user.username = "scottbauer"

    fake_rule = MagicMock()
    fake_rule.is_active = True
    fake_rule.replacements = MagicMock(return_value={"old": "new"})
    ui._rules.list_all.return_value = [fake_rule]
    ui._rules.list_by_owner.return_value = [fake_rule]

    fake_filter = MagicMock()
    fake_filter.blacklist_keywords = ["bad1", "bad2"]
    fake_filter.whitelist_keywords = ["good1"]
    ui._filters.list_all.return_value = [fake_filter]
    ui._filters.list_by_owner.return_value = [fake_filter]

    fake_sess = MagicMock()
    fake_sess.is_active = True
    fake_sess.phone_number = "989141830237"
    ui._sessions.list_all.return_value = [fake_sess]
    ui._sessions.list_by_owner.return_value = [fake_sess]

    text = await ui._main_text(5989354154, user=fake_user)

    assert "👋 Welcome back, **Mr Scott BAUER**!" in text
    assert "📊 Service status: Running ✅" in text
    assert "📋 Active Tasks: 1" in text
    assert "• Blocked keywords: 2" in text
    assert "• Allowed keywords: 1" in text
    assert "• Replace rules: 1" in text
    assert "Contact @Capxi" in text
    assert "👤 YOUR ACCOUNT" in text
    assert "Mr Scott BAUER" in text
    assert "5989354154" in text
    assert "+989141830237" in text
    assert "VIP 995" in text
    assert "💎 SERVICE PLAN" in text
    assert "📋 FORWARDING TASKS" in text
    assert "👥 YOUR SESSIONS" in text
    assert "💰 CREDITS BALANCE" in text


@pytest.mark.asyncio
async def test_main_text_formatting_persian(mock_pro_bot_ui):
    ui = mock_pro_bot_ui
    ui.i18n.current = "fa"

    fake_user = MagicMock()
    fake_user.first_name = "Scott"
    fake_user.last_name = ""
    fake_user.username = None

    text = await ui._main_text(123456, user=fake_user)

    assert "خوش آمدید" in text
    assert "وضعیت سرویس: **در حال اجرا** ✅" in text
    assert "@Capxi" in text
    assert "حساب کاربری" in text
    assert "قوانین فعال" in text


def test_main_menu_buttons_structure(mock_pro_bot_ui):
    ui = mock_pro_bot_ui
    ui.i18n.current = "en"

    kbd = ui._main_menu()
    rows = kbd.inline_keyboard

    # Check row count
    assert len(rows) == 7

    # Row 1: Forward | Spam
    assert rows[0][0].text == "Forward" and rows[0][0].callback_data == CB["rules"]
    assert rows[0][1].text == "Spam" and rows[0][1].callback_data == CB["filters"]

    # Row 2: Publish | Clone
    assert rows[1][0].text == "Publish" and rows[1][0].callback_data == CB["rule_add"]
    assert rows[1][1].text == "Clone" and rows[1][1].callback_data == CB["ai"]

    # Row 3: Settings | Browse Addons
    assert rows[2][0].text == "⚙️ Settings" and rows[2][0].callback_data == CB["settings"]
    assert rows[2][1].text == "🧩 Browse Addons" and rows[2][1].callback_data == CB["dashboard"]

    # Row 4: Upgrade | Stats
    assert rows[3][0].text == "💎 Upgrade" and rows[3][0].callback_data == CB["upgrade"]
    assert rows[3][1].text == "📊 Statistics" and rows[3][1].callback_data == CB["stats"]

    # Row 5: Documentation | Language
    assert rows[4][0].text == "📖 Documentation ↗" and rows[4][0].callback_data == CB["help"]
    assert rows[4][1].text == "🌐 Language" and rows[4][1].callback_data == CB["lang"]

    # Row 6: Support (URL to @Capxi)
    assert rows[5][0].text == "🆘 Support (@Capxi)"
    assert rows[5][0].url == "https://t.me/Capxi"

    # Row 7: Disconnect / Sessions
    assert rows[6][0].text == "🔌 Disconnect" and rows[6][0].callback_data == CB["sessions"]

    # Ensure "Get App" is NOT present anywhere
    for row in rows:
        for btn in row:
            assert "Get App" not in btn.text


def test_upgrade_text(mock_pro_bot_ui):
    ui = mock_pro_bot_ui
    ui.i18n.current = "en"
    en_text = ui._upgrade_text()
    assert "VIP Features" in en_text
    assert "@Capxi" in en_text

    ui.i18n.current = "fa"
    fa_text = ui._upgrade_text()
    assert "اشتراک ویژه" in fa_text
    assert "@Capxi" in fa_text
