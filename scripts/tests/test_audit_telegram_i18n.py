"""Regression tests for the fail-closed Telegram UI translation guard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "audit_telegram_i18n.py"
SPEC = importlib.util.spec_from_file_location("audit_telegram_i18n", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_direct_english_inline_button_is_rejected() -> None:
    source = 'InlineKeyboardButton("Enable forwarding", callback_data="enable")'
    findings = AUDIT.scan_source("ui.py", source)
    assert findings
    assert "InlineKeyboardButton" in findings[0]
    assert "Enable forwarding" in findings[0]


def test_direct_persian_callback_alert_is_rejected() -> None:
    source = 'await query.answer("عملیات ناموفق بود", show_alert=True)'
    findings = AUDIT.scan_source("ui.py", source)
    assert len(findings) == 1
    assert "عملیات ناموفق بود" in findings[0]


def test_direct_text_in_send_message_is_rejected() -> None:
    source = 'await bot.send_message(chat_id, "Your rule was saved successfully")'
    assert AUDIT.scan_source("ui.py", source)


def test_catalog_backed_button_is_allowed() -> None:
    source = 'InlineKeyboardButton(self._t("ui_enable", lang=lang), callback_data="enable")'
    assert AUDIT.scan_source("ui.py", source) == []


def test_catalog_backed_callback_text_is_allowed() -> None:
    source = 'await query.answer(self.i18n.t("ui_saved", lang=lang))'
    assert AUDIT.scan_source("ui.py", source) == []


def test_protocol_tokens_and_callback_ids_are_not_ui_copy() -> None:
    source = (
        'InlineKeyboardButton("BACK", callback_data="adm_users_pg_2")\n'
        'await bot.send_message(chat_id, "https://example.com/status")'
    )
    assert AUDIT.scan_source("ui.py", source) == []


def test_multiline_human_text_is_rejected() -> None:
    source = 'await query.edit_message_text("Settings updated\\nChoose an action below")'
    assert AUDIT.scan_source("ui.py", source)


def test_catalog_key_without_human_copy_is_not_mistaken_for_ui_text() -> None:
    source = 'value = self._t("ui_settings_advanced", lang=lang)'
    assert AUDIT.scan_source("ui.py", source) == []


def test_scanner_discovers_all_ui_files_dynamically() -> None:
    discovered = AUDIT.all_ui_python_files()
    assert discovered, "the Telegram UI scan must discover source files"
    assert discovered == sorted(set(discovered))
    assert all(item.endswith('.py') for item in discovered)
    assert all(item.startswith(AUDIT.UI_ROOTS) for item in discovered)


def test_direct_callback_alert_with_formatted_string_is_rejected() -> None:
    source = 'await query.answer(f"Rule {rule_id} was deleted", show_alert=True)'
    findings = AUDIT.scan_source("ui.py", source)
    assert findings
    assert "untranslated UI literal" in findings[0]


def test_direct_button_label_with_fstring_is_rejected() -> None:
    source = 'InlineKeyboardButton(f"Disable {rule_name}", callback_data="disable")'
    findings = AUDIT.scan_source("ui.py", source)
    assert findings
    assert "InlineKeyboardButton" in findings[0]


def test_callback_payload_is_not_treated_as_visible_copy() -> None:
    source = 'InlineKeyboardButton(self._t("ui_disable", lang=lang), callback_data="disable_rule_42")'
    assert AUDIT.scan_source("ui.py", source) == []


def test_added_line_filter_ignores_legacy_literals_but_catches_new_ones() -> None:
    source = (
        'await bot.send_message(chat_id, "Legacy text")\\n'
        'await bot.send_message(chat_id, "New untranslated text")'
    )
    findings = AUDIT.scan_source("ui.py", source, {2})
    assert len(findings) == 1
    assert "New untranslated text" in findings[0]


def test_full_scan_mode_is_available_for_deep_audits() -> None:
    import inspect

    assert "--all" in inspect.getsource(AUDIT.main)
