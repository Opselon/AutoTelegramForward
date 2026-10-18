"""Regression tests for the canonical Telegram button style layer."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.infrastructure.telegram import ui_styles
from core.infrastructure.telegram.button_style import DANGER, PRIMARY, SUCCESS
from core.infrastructure.telegram.i18n import SUPPORTED_LANGUAGES, I18n

CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "telegram"
    / "locales"
    / "translations.json"
)

BUTTON_STYLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "telegram"
    / "button_style.py"
)


def _catalog() -> dict[str, dict[str, str]]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# registry integrity
# --------------------------------------------------------------------------- #
def test_every_styled_button_class_has_a_distinct_glyph_and_colour() -> None:
    glyphs = [s.glyph for s in ui_styles.STYLES.values() if s.glyph]
    assert glyphs, "the style registry must render at least one glyph"
    assert len(glyphs) == len(set(glyphs)), "two styles share one glyph"
    colours = [s.color for s in ui_styles.STYLES.values() if s.color]
    assert set(colours) <= {PRIMARY, SUCCESS, DANGER}, (
        "only the three layer-225 colours may be used"
    )


def test_aliases_point_at_registered_glyphs() -> None:
    canonical = set(ui_styles.registered_glyphs())
    assert set(ui_styles.GLYPH_ALIASES.values()) <= canonical


def test_alias_keys_are_not_canonical_glyphs() -> None:
    canonical = {s.glyph for s in ui_styles.STYLES.values()}
    assert not (set(ui_styles.GLYPH_ALIASES) & canonical)


def test_key_overrides_only_reference_registered_styles() -> None:
    assert set(ui_styles.KEY_STYLE_OVERRIDES.values()) <= set(ui_styles.STYLES)


def test_style_rules_are_ordered_and_compile() -> None:
    for pattern, style in ui_styles.KEY_STYLE_RULES:
        assert style in ui_styles.STYLES, f"{style} is not a registered style"
        re.compile(pattern)


# --------------------------------------------------------------------------- #
# style resolution
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("ui_back", "nav"),
        ("ui_cancel", "danger"),
        ("ui_delete_rule", "danger"),
        ("ui_refresh", "refresh"),
        ("ui_login_retry", "refresh"),
        ("ui_dashboard_open", "open"),
        ("ui_filter_add", "primary"),
        ("ui_login", "primary"),
        ("ui_clean_btn_hashtags", "danger"),
        ("ui_pv_btn_status", "info"),
        ("ui_ai_btn_enable", "success"),
        ("ui_ai_btn_disable", "warning"),
        ("ui_migrate_overwrite", "warning"),
        ("ui_rule_btn_topic", "neutral"),
    ],
)
def test_style_for_key(key: str, expected: str) -> None:
    assert ui_styles.style_for_key(key) == expected


def test_background_colour_follows_the_action_class() -> None:
    assert ui_styles.color_for_key("ui_cancel") == DANGER
    assert ui_styles.color_for_key("ui_draft_btn_save") == SUCCESS
    assert ui_styles.color_for_key("ui_ai_add") == PRIMARY
    # Navigation and information are never painted.
    assert ui_styles.color_for_key("ui_back") is None
    assert ui_styles.color_for_key("ui_logs_stats") is None


def test_unknown_key_is_neutral_and_gets_no_invented_glyph() -> None:
    assert ui_styles.style_for_key("ui_something_nobody_mapped") == "neutral"
    assert ui_styles.decorate("ui_something_nobody_mapped", "Whatever") == "Whatever"


# --------------------------------------------------------------------------- #
# decoration: applied once, canonical, language-neutral
# --------------------------------------------------------------------------- #
def test_decorate_applies_the_canonical_glyph_once() -> None:
    assert ui_styles.decorate("ui_back", "Back") == "⬅️ Back"
    # idempotent for call sites that still prefix by hand
    assert ui_styles.decorate("ui_back", "⬅️ Back") == "⬅️ Back"


def test_decorate_canonicalises_drifted_source_glyphs() -> None:
    assert ui_styles.decorate("ui_cancel", "✖ Cancel") == "❌ Cancel"
    assert ui_styles.decorate("ui_back", "↩ Back") == "⬅️ Back"
    assert ui_styles.decorate("ui_retry", "🔁 Try Again") == "🔄 Try Again"
    assert ui_styles.style_label("🔁 Try Again") == "🔄 Try Again"


def test_decorate_collapses_a_doubled_glyph() -> None:
    # The catalog value already carries "❌ Cancel"; the hand-rolled prefix
    # rendered "❌ ❌ Cancel" to every user in every language.
    assert ui_styles.decorate("ui_cancel", "❌ ❌ Cancel") == "❌ Cancel"
    assert ui_styles.style_label("❌ ❌ Cancel") == "❌ Cancel"
    assert ui_styles.style_label("❌ Cancel") == "❌ Cancel"


def test_authored_catalog_glyph_wins() -> None:
    label = "🩺 Health check (Ping)"
    assert ui_styles.decorate("ui_ai_btn_health", label) == label
    assert ui_styles.style_label("📤 Export backup (plain)") == "📤 Export backup (plain)"


def test_style_label_never_invents_decoration_for_plain_labels() -> None:
    for label in ("1", "0", "⌫", "Main Menu", "🔗 https://x.example"):
        assert ui_styles.style_label(label) == label


def test_colour_derived_from_a_rendered_label() -> None:
    assert ui_styles.color_for_label("❌ Cancel") == DANGER
    assert ui_styles.color_for_label("✅ Saved") == SUCCESS
    assert ui_styles.color_for_label("➕ Add rule") == PRIMARY
    assert ui_styles.color_for_label("⬅️ Back") is None
    assert ui_styles.color_for_label("7") is None


def test_glyph_and_colour_are_identical_in_every_locale() -> None:
    i18n = I18n(str(CATALOG_PATH))
    keys = ("ui_back", "ui_cancel", "ui_refresh", "ui_login_retry", "ui_dashboard_open")
    for key in keys:
        rendered = {
            ui_styles.decorate(key, i18n.t(key, lang=lang)).split(" ", 1)[0]
            for lang in SUPPORTED_LANGUAGES
        }
        assert len(rendered) == 1, f"{key} renders different glyphs per locale: {rendered}"
        assert len({ui_styles.color_for_key(key) for _ in SUPPORTED_LANGUAGES}) == 1


def test_no_language_branching_in_the_style_layer() -> None:
    source = Path(ui_styles.__file__).read_text(encoding="utf-8")
    for lang in SUPPORTED_LANGUAGES:
        assert f'== "{lang}"' not in source
        assert f"'{lang}'" not in source


def test_colour_names_cannot_drift_from_the_wire_layer() -> None:
    """``ui_styles`` redeclares the three colour names so the style layer stays
    importable without the Telegram runtime (the cross-language audit reads it).

    They are written by hand in ``button_style``; this test keeps the pair in
    lock-step in both directions.
    """
    assert (ui_styles.PRIMARY, ui_styles.SUCCESS, ui_styles.DANGER) == (
        PRIMARY,
        SUCCESS,
        DANGER,
    )
    wire_source = BUTTON_STYLE_PATH.read_text(encoding="utf-8")
    for name, value in (("PRIMARY", PRIMARY), ("SUCCESS", SUCCESS), ("DANGER", DANGER)):
        assert f'{name} = "{value}"' in wire_source
    style_source = Path(ui_styles.__file__).read_text(encoding="utf-8")
    assert "from .button_style import" not in style_source
    assert "import pyrogram" not in style_source
