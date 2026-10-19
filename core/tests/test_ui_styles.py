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


def test_navigation_copy_is_not_read_as_a_primary_action() -> None:
    """🌐 is the globe of "🌐 Language", not a call to action.

    The authored-glyph heuristic painted every language entry blue because
    🌐 was listed as a primary glyph and is also the ``open`` class glyph.
    A class colour comes from the key (``color_for_key``), never from copy.
    """
    assert "🌐" not in ui_styles.AUTHORED_PRIMARY_GLYPHS
    assert ui_styles.color_for_label("🌐 Language") is None
    assert ui_styles.color_for_label("🌐 Choose language") is None
    # the open class still carries its colour, from the key
    assert ui_styles.color_for_key("ui_dashboard_open") == PRIMARY


def test_the_dashboard_namespace_never_resolves_to_the_back_class() -> None:
    """`ui_main_menu_forward` matched main|menu and read as "back" (⬅️ Forward)."""
    keys = [
        key
        for table in _catalog().values()
        for key in table
        if key.startswith("ui_main_menu_")
    ]
    assert keys, "the dashboard keyspace disappeared from the catalog"
    for key in keys:
        assert ui_styles.style_for_key(key) != "nav", key
        assert ui_styles.style_glyph(key) not in ("⬅️", "🔙"), key


def test_only_the_surface_namespace_is_stripped_before_matching() -> None:
    assert ui_styles.action_part("ui_main_menu_forward") == "forward"
    assert ui_styles.action_part("ui_back_main_menu") == "ui_back_main_menu"
    # a real back button keeps the navigation class…
    assert ui_styles.style_for_key("ui_back_main_menu") == "nav"
    # …and the stripped dashboard entry keeps its own action class
    assert ui_styles.style_for_key("ui_main_menu_forward") == "forward"
    assert ui_styles.style_for_key("ui_main_menu_filters") == "filter"
    assert ui_styles.style_for_key("ui_main_menu_clone") == "copy"


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


# --------------------------------------------------------------------------- #
# glyph class + catalog-wide invariants
# --------------------------------------------------------------------------- #
GLYPH_MATCH = re.compile(r"^(" + ui_styles.GLYPH_TOKEN + ")")
REPEATED = re.compile(r"^(" + ui_styles.GLYPH_TOKEN + r")(?:\s*\1)+")


def test_every_alias_glyph_is_matched_by_the_glyph_class() -> None:
    """An alias the pattern cannot match is dead code — the drift it names
    survives silently (U+2139 "ℹ" was exactly that)."""
    for alias in ui_styles.GLYPH_ALIASES:
        assert GLYPH_MATCH.match(alias), f"alias {alias!r} is not recognised as a glyph"


def _typography(char: str) -> bool:
    """True for punctuation the class deliberately ignores.

    Bullets, ellipses, curly quotes, fullwidth brackets and box-drawing rules
    are layout, not decoration: "• item" and "━━━━" must stay glyph-free.
    """
    code = ord(char)
    return (
        char.isalnum()
        or code < 0x2000
        or 0x2000 <= code <= 0x203B  # general punctuation
        or 0x2500 <= code <= 0x257F  # box drawing (separator rules)
        or 0xFF00 <= code <= 0xFFEF  # fullwidth and halfwidth forms
    )


def test_glyph_class_covers_every_catalog_authored_glyph() -> None:
    """No catalog value may start with a glyph the class cannot see: that is
    how "ⓘ How does this work?" ended up with a second glyph glued in front."""
    invisible = []
    for lang, table in _catalog().items():
        for key, value in table.items():
            if not isinstance(value, str) or not value.strip():
                continue
            head = value.lstrip()[0]
            if _typography(head):
                continue
            if not GLYPH_MATCH.match(value.lstrip()):
                invisible.append((lang, key, value[:40]))
    assert invisible == [], f"undetected leading glyphs: {invisible[:5]}"


def test_no_catalog_value_starts_with_two_glyphs() -> None:
    doubled = [
        (lang, key, value[:40])
        for lang, table in _catalog().items()
        for key, value in table.items()
        if isinstance(value, str) and REPEATED.match(value.lstrip())
    ]
    assert doubled == [], f"doubled decoration in the catalog: {doubled[:5]}"


def test_decoration_never_doubles_and_is_idempotent_catalog_wide() -> None:
    """The whole catalog, all 10 locales: decorating a label can add at most
    one glyph, and decorating its result again changes nothing."""
    for lang, table in _catalog().items():
        for key, value in table.items():
            if not isinstance(value, str) or not value.strip():
                continue
            authored = ui_styles.leading_glyph(value)
            once = ui_styles.decorate(key, value)
            assert not REPEATED.match(once), f"{lang}/{key} doubles its glyph: {once[:40]!r}"
            assert ui_styles.decorate(key, once) == once, f"{lang}/{key} is not idempotent"
            # Only a glyph the style layer *added* has to be a registered one;
            # an authored glyph may legitimately be any pictograph (🤖, 👋).
            if not authored and once != value:
                added = ui_styles.leading_glyph(once)
                assert added in ui_styles.registered_glyphs(), (lang, key, added)


def test_button_keys_have_one_glyph_in_every_locale() -> None:
    """A key rendered as a button must look the same in all 10 locales."""
    catalog = _catalog()
    keys = sorted(k for k in catalog["en"] if k.endswith("_btn") or "_btn_" in k)
    assert keys, "no button keys found"
    for key in keys:
        glyphs = {ui_styles.leading_glyph(catalog[lang].get(key, "")) for lang in SUPPORTED_LANGUAGES}
        assert len(glyphs) == 1, f"{key} renders {glyphs} across locales"


def test_a_label_opening_with_a_newline_keeps_it_and_is_not_doubled() -> None:
    """Regression: "\n⚠️ *Reply … will not work*" is a message block appended to
    a bigger panel. The glyph was invisible to the pattern, so a second one was
    glued on *and* the leading newline was stripped away."""
    label = "\n⚠️ *Reply, Edit, Replace, Translate will not work when show header*"
    assert ui_styles.leading_glyph(label) == "⚠️"
    assert ui_styles.style_label(label) == label
    assert ui_styles.decorate("ui_rule_detail_header_warning", label) == label
    plain = "\n**Nothing here yet**"
    assert ui_styles.decorate("ui_rule_detail_header_warning", plain) == (
        "\n⚠️ **Nothing here yet**"
    )


def test_a_separator_rule_is_not_decoration() -> None:
    """Box drawing is layout: "━━━━" must not read as a repeated glyph."""
    for rule in ("━━━━━━━━━━━━━━━━━━━━", "────────────────────"):
        assert ui_styles.leading_glyph(rule) == ""
        assert ui_styles.style_label(rule) == rule
        assert ui_styles.decorate("ui_sep", rule) == rule
