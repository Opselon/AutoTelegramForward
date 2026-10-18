"""Regression tests for the cross-language audit (the multilingual tester).

Each test pins one defect class the upgraded tester must catch. The last two
run the audit against the real repository, so the gate cannot silently rot
into a no-op.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/audit_cross_language.py"
SPEC = importlib.util.spec_from_file_location("audit_cross_language", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


@pytest.fixture(autouse=True)
def _isolated_audit_state():
    """Keep each test's findings out of the module-level result lists."""
    saved_failures = list(AUDIT.failures)
    saved_notes = list(AUDIT.notes)
    AUDIT.failures.clear()
    AUDIT.notes.clear()
    yield AUDIT.failures
    AUDIT.failures[:] = saved_failures
    AUDIT.notes[:] = saved_notes


# --------------------------------------------------------------------------- #
# catalog rules
# --------------------------------------------------------------------------- #
def test_matching_catalogs_pass() -> None:
    reference = {"ui_cancel": "❌ Cancel", "ui_main": "Main {n}"}
    AUDIT.check("bot", "fa", dict(reference), reference)
    assert AUDIT.failures == []


def test_missing_key_fails() -> None:
    reference = {"ui_cancel": "❌ Cancel", "ui_new": "New"}
    AUDIT.check("bot", "fa", {"ui_cancel": "❌ لغو"}, reference)
    assert AUDIT.failures and "missing" in AUDIT.failures[0]


def test_extra_key_fails() -> None:
    reference = {"ui_cancel": "❌ Cancel"}
    AUDIT.check("bot", "fa", {"ui_cancel": "❌ لغو", "ui_ghost": "شبح"}, reference)
    assert AUDIT.failures and "extra" in AUDIT.failures[0]


def test_empty_value_fails() -> None:
    reference = {"ui_cancel": "❌ Cancel"}
    AUDIT.check("bot", "de", {"ui_cancel": "   "}, reference)
    assert AUDIT.failures and "empty" in AUDIT.failures[0]


def test_placeholder_mismatch_fails() -> None:
    reference = {"ui_main": "Main {n}"}
    AUDIT.check("bot", "fa", {"ui_main": "اصلی"}, reference)
    assert AUDIT.failures and "placeholder" in AUDIT.failures[0]


def test_english_catalog_with_persian_text_fails() -> None:
    reference = {"ui_start": "شروع", "ui_cancel": "Cancel"}
    AUDIT.check("bot", "en", reference, reference)
    assert AUDIT.failures and "Persian" in AUDIT.failures[0]


def test_endonym_key_may_hold_rtl_text_in_english() -> None:
    reference = {"lang_native_fa": "فارسی"}
    AUDIT.check("bot", "en", reference, reference)
    assert AUDIT.failures == []


def test_translated_locale_may_hold_rtl_text() -> None:
    reference = {"ui_cancel": "Cancel"}
    AUDIT.check("bot", "fa", {"ui_cancel": "لغو"}, reference)
    assert AUDIT.failures == []


# --------------------------------------------------------------------------- #
# client-side leak detection
# --------------------------------------------------------------------------- #
def test_digit_normaliser_is_not_copy() -> None:
    line = '_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")'
    assert AUDIT._has_non_reference_letters(line) is False


def test_arabic_comma_separator_is_not_copy() -> None:
    line = 'kws = [k.strip() for k in raw.replace("،", ",").split(",")]'
    assert AUDIT._has_non_reference_letters(line) is False


def test_persian_word_is_copy() -> None:
    assert AUDIT._has_non_reference_letters('x = "انصراف"') is True


def test_cyrillic_word_is_copy() -> None:
    """The old Persian-only scan let a Russian branch ship to every locale."""
    assert AUDIT._has_non_reference_letters('label = "Отмена"') is True


# --------------------------------------------------------------------------- #
# language branching
# --------------------------------------------------------------------------- #
def test_language_branch_is_reported() -> None:
    findings = AUDIT.language_branch_findings(
        'if lang == "fa":\n    label = "انصراف"\n', "ui.py"
    )
    assert len(findings) == 1
    assert "'fa'" in findings[0]


def test_multiline_language_branch_is_reported() -> None:
    findings = AUDIT.language_branch_findings('elif language == "ru":\n', "ui.py")
    assert findings


def test_non_language_comparison_is_ignored() -> None:
    assert AUDIT.language_branch_findings('if step == "fa":\n', "ui.py") == []
    assert AUDIT.language_branch_findings("if lang == other:\n", "ui.py") == []


# --------------------------------------------------------------------------- #
# button labels
# --------------------------------------------------------------------------- #
def test_hardcoded_button_label_is_reported() -> None:
    source = 'InlineKeyboardButton("Enable forwarding", callback_data="enable")'
    findings, total, resolved, _native = AUDIT.button_label_findings(source, "ui.py")
    assert findings and "Enable forwarding" in findings[0]
    assert (total, resolved) == (1, 0)


def test_catalog_button_label_is_accepted() -> None:
    source = 'rows.append([(self._btn("ui_cancel"), "cancel")])'
    findings, total, resolved, _native = AUDIT.button_label_findings(source, "ui.py")
    assert findings == []
    assert (total, resolved) == (1, 1)


def test_keypad_digit_and_callback_id_are_not_copy() -> None:
    source = 'rows.append([("1", "pad:1"), ("yes", "adm_yes")])'
    findings, _total, _resolved, _native = AUDIT.button_label_findings(source, "ui.py")
    assert findings == []


def test_language_picker_endonym_is_exempt() -> None:
    source = 'rows.append([("🇷🇺 Русский (RU)", "lang:ru")])'
    findings, total, resolved, native = AUDIT.button_label_findings(source, "ui.py")
    assert findings == []
    assert native == 1
    assert resolved == 0


# --------------------------------------------------------------------------- #
# button decoration
# --------------------------------------------------------------------------- #
CATALOG = {"en": {"ui_cancel": "❌ Cancel", "ui_back": "⬅️ Back", "ui_plain": "Plain"}}


def test_drifted_glyph_is_reported() -> None:
    source = 'rows.append([("✖ " + self._t("ui_cancel"), "cancel")])'
    findings = AUDIT.button_style_findings(source, CATALOG, "ui.py")
    assert findings and "drifted glyph" in findings[0]


def test_doubled_glyph_is_reported() -> None:
    """`"❌ " + _t(key)` renders the glyph the catalog value already carries."""
    source = 'rows.append([("❌ " + self._t("ui_cancel"), "cancel")])'
    findings = AUDIT.button_style_findings(source, CATALOG, "ui.py")
    assert findings and "twice" in findings[0]


def test_unregistered_glyph_is_reported() -> None:
    source = 'rows.append([("🧩 " + self._t("ui_plain"), "plain")])'
    findings = AUDIT.button_style_findings(source, CATALOG, "ui.py")
    assert findings and "unregistered" in findings[0]


def test_canonical_helper_is_accepted() -> None:
    source = 'rows.append([(self._btn("ui_cancel"), "cancel")])'
    assert AUDIT.button_style_findings(source, CATALOG, "ui.py") == []


def test_decoration_parity_across_locales_is_enforced() -> None:
    catalog = {
        "en": {"ui_cancel": "❌ Cancel"},
        "fa": {"ui_cancel": "لغو"},
        "ru": {"ui_cancel": "❌ Отмена"},
    }
    findings = AUDIT.decoration_parity_findings(catalog, {"ui_cancel"})
    assert len(findings) == 1
    assert "fa" in findings[0]


# --------------------------------------------------------------------------- #
# callback copy
# --------------------------------------------------------------------------- #
def test_literal_callback_toast_is_reported() -> None:
    findings = AUDIT.callback_copy_findings('await cq.answer("Saved!")', "ui.py")
    assert findings and "Saved!" in findings[0]


def test_catalog_callback_toast_is_accepted() -> None:
    assert AUDIT.callback_copy_findings('await cq.answer(self._t("ui_saved"))', "ui.py") == []


def test_numeric_callback_answer_is_not_copy() -> None:
    assert AUDIT.callback_copy_findings('await cq.answer(f"{digits}")', "ui.py") == []


# --------------------------------------------------------------------------- #
# the gate, against the real repository
# --------------------------------------------------------------------------- #
def test_real_repository_catalogs_are_one_to_one() -> None:
    catalog = json.loads(AUDIT.BOT_CATALOG.read_text(encoding="utf-8"))
    assert set(catalog) == set(AUDIT.SUPPORTED_LANGUAGES)
    reference = set(catalog["en"])
    for lang, table in catalog.items():
        assert set(table) == reference, f"{lang} key set differs from English"


def test_input_alias_keys_exist_in_every_locale() -> None:
    """The cancel/clear/default matchers read their words from the catalog."""
    catalog = json.loads(AUDIT.BOT_CATALOG.read_text(encoding="utf-8"))
    for key in (
        "ui_input_cancel_aliases",
        "ui_input_clear_aliases",
        "ui_input_default_aliases",
    ):
        for lang in AUDIT.SUPPORTED_LANGUAGES:
            value = catalog[lang].get(key, "")
            assert value.strip(), f"{key} missing for {lang}"
            assert not re.search(r",\s*,|^,|,$", value), f"{key}/{lang} malformed list"


def test_repository_passes_the_gate() -> None:
    assert AUDIT.main([]) == 0


def test_repository_passes_the_gate_with_the_report() -> None:
    assert AUDIT.main(["--report"]) == 0
