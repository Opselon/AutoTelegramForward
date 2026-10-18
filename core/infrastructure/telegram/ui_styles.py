"""Canonical button styles for the Telegram bot UI.

A Telegram inline button has two styles, and both drift when every call
site picks its own:

* the **background colour** (``keyboardButtonStyle``, layer 225 — see
  :mod:`core.infrastructure.telegram.button_style`): primary / success /
  danger. Only a semantic action class may carry one, otherwise every
  panel turns into noise;
* the **leading glyph**. Its only job is decoration, and hand-rolled
  prefixes drifted badly: the same catalog key rendered as ``"✖ Cancel"``,
  ``"❌ Cancel"`` and — since the catalog value already carries a glyph —
  ``"❌ ❌ Cancel"``, in the same bot, in every language.

Styles are therefore derived from the **catalog key** of the button, never
from the language: there is no per-locale branching in this module. The
glyph is applied *after* the catalog lookup, so all 10 locales render the
same decoration, and a label that already carries a glyph is never
decorated twice (the helpers are idempotent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The background-colour names of ``button_style`` (their wire encoding lives
#: there). Redeclared instead of imported on purpose: importing
#: ``button_style`` pulls in ``pyrogram.raw``, which calls
#: ``asyncio.get_event_loop()`` at import time and therefore cannot be
#: imported by tooling (the cross-language audit) or on a Python where that
#: call no longer auto-creates a loop. ``core/tests/test_ui_styles.py`` pins
#: these three strings against ``button_style`` so the pair cannot drift.
PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"

__all__ = [
    "ButtonStyle",
    "STYLES",
    "style_for_key",
    "style_glyph",
    "color_for_key",
    "color_for_label",
    "style_label",
    "decorate",
    "leading_glyph",
    "canonical_glyph",
    "registered_glyphs",
    "GLYPH_TOKEN",
]


@dataclass(frozen=True)
class ButtonStyle:
    """One semantic button class: its canonical glyph and background."""

    name: str
    glyph: str
    color: str | None = None


#: The style vocabulary. ``neutral`` deliberately carries no glyph and no
#: background: inventing decoration for a button nobody styled changes the
#: whole panel for no semantic reason.
STYLES: dict[str, ButtonStyle] = {
    "primary": ButtonStyle("primary", "➕", PRIMARY),
    "success": ButtonStyle("success", "✅", SUCCESS),
    "danger": ButtonStyle("danger", "❌", DANGER),
    "warning": ButtonStyle("warning", "⚠️", None),
    "nav": ButtonStyle("nav", "⬅️", None),
    "refresh": ButtonStyle("refresh", "🔄", PRIMARY),
    "info": ButtonStyle("info", "ℹ️", None),
    "open": ButtonStyle("open", "🌐", PRIMARY),
    "neutral": ButtonStyle("neutral", "", None),
}

#: Drift observed in production: one action, two glyphs. Aliases are
#: canonicalised to the glyph of their style.
GLYPH_ALIASES: dict[str, str] = {
    "✖": "❌",
    "✗": "❌",
    "⛔": "❌",
    "🚫": "❌",
    "↩": "⬅️",
    "🔙": "⬅️",
    "⟵": "⬅️",
    "⬅": "⬅️",
    "🔁": "🔄",
    "♻": "🔄",
    "ⓘ": "ℹ️",
    "🛈": "ℹ️",
    "ℹ": "ℹ️",
}

#: Curated overrides where the key name alone is ambiguous.
KEY_STYLE_OVERRIDES: dict[str, str] = {
    "ui_back": "nav",
    "ui_admin_next": "nav",
    "ui_refresh": "refresh",
    "ui_login_retry": "refresh",
    "ui_webpass_reset_btn": "refresh",
    "ui_dashboard_open": "open",
    "ui_logs_recent": "info",
    "ui_logs_stats": "info",
    "ui_main_menu_publish": "primary",
    "ui_main_menu_forward": "primary",
    "ui_main_menu_clone": "refresh",
    "ui_main_menu_filters": "info",
}

#: Key-token -> style, evaluated in order, first match wins. Matching the
#: catalog key (not the copy) keeps the rule set language-agnostic.
KEY_STYLE_RULES: tuple[tuple[str, str], ...] = (
    (
        "cancel|delete|remove|clear|reset|revoke|purge|logout|disconnect|unlink|block"
        "|clean|strip|wipe|flush",
        "danger",
    ),
    ("enable|confirm|save|apply|done|accept|approve|verify", "success"),
    ("disable|overwrite|warning|expire|danger|force", "warning"),
    ("add|new|create|import|upload|link|login|connect|clone|copy|publish", "primary"),
    ("back|return|main|menu|close|exit|next|prev", "nav"),
    ("refresh|reload|retry|sync", "refresh"),
    ("help|info|about|status|detail|logs|stats|perf|debug|show|view|health", "info"),
)

#: Glyphs that carry a background when they were authored in the catalog
#: instead of being applied by a style. Everything else stays unstyled.
AUTHORED_PRIMARY_GLYPHS: frozenset[str] = frozenset(
    {"➕", "🔄", "🌐", "📤", "📥", "✏️", "💾", "🔗"}
)

#: The characters that count as a leading glyph. Published because the
#: cross-language audit matches exactly the same set — two private copies had
#: already drifted and both missed U+2139 ("ℹ️"), so an "ℹ️ Settings" label
#: read as undecorated copy and got a second glyph glued in front of it.
#: Covers the pictographs/emoji planes, symbols, arrows and enclosed
#: alphanumerics (U+203C-U+24FF plus U+2580-U+2BFF: ⓘ, ℹ, ✔, ⏻, ◀), each
#: optionally followed by the emoji selector. Box drawing (U+2500-U+257F) is
#: deliberately excluded: "━━━" is a separator rule, not a decoration, and
#: treating it as one made every separator read as a doubled glyph.
GLYPH_TOKEN = (
    r"(?:[\U0001F000-\U0001FAFF\u203C-\u24FF\u2580-\u2BFF]\uFE0F?)"
)
#: Optional leading whitespace, a glyph, then its optional trailing space.
#: The whitespace is captured, not skipped: a catalog value may open with a
#: newline (``"\n⚠️ *Reply, Edict …*"`` is appended to a bigger panel), and
#: dropping it both moved the text and left the glyph looking absent.
LEADING_GLYPH = re.compile(rf"^(\s*)({GLYPH_TOKEN})\s*")
#: The same glyph repeated ("❌ ❌ Cancel" — style prefix + catalog glyph).
REPEATED_GLYPH = re.compile(rf"^(\s*)({GLYPH_TOKEN})(?:\s*\2)+\s*")
#: Any glyph anywhere, used by the style audit.
ANY_GLYPH = re.compile(GLYPH_TOKEN)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def style_for_key(key: str) -> str:
    """Return the semantic style of a catalog key (``neutral`` if unknown)."""
    name = (key or "").strip().lower()
    if name in KEY_STYLE_OVERRIDES:
        return KEY_STYLE_OVERRIDES[name]
    for pattern, style in KEY_STYLE_RULES:
        if re.search(pattern, name):
            return style
    return "neutral"


def style_glyph(key: str) -> str:
    """Canonical glyph for a catalog key (empty for neutral)."""
    return STYLES.get(style_for_key(key), STYLES["neutral"]).glyph


def color_for_key(key: str) -> str | None:
    """Background colour for a catalog key (``None`` when unstyled)."""
    return STYLES.get(style_for_key(key), STYLES["neutral"]).color


def leading_glyph(label: str) -> str:
    """First glyph of a label, canonicalised through the alias table."""
    match = LEADING_GLYPH.match(label or "")
    if not match:
        return ""
    return GLYPH_ALIASES.get(match.group(2), match.group(2))


def canonical_glyph(glyph: str) -> str:
    """Map an alias glyph to its canonical twin (identity otherwise)."""
    return GLYPH_ALIASES.get(glyph, glyph)


def color_for_label(label: str) -> str | None:
    """Background colour implied by a rendered label's leading glyph.

    Lets the render funnel style buttons it did not build from a key:
    every destructive button is red whatever assembled it. A glyph
    outside the table stays unstyled rather than guessing a colour.
    """
    glyph = leading_glyph(label)
    if not glyph:
        return None
    if glyph == STYLES["danger"].glyph:
        return DANGER
    if glyph == STYLES["success"].glyph:
        return SUCCESS
    if glyph in AUTHORED_PRIMARY_GLYPHS:
        return PRIMARY
    return None


# --------------------------------------------------------------------------- #
# decoration
# --------------------------------------------------------------------------- #
def style_label(label: str) -> str:
    """Normalise the decoration of a rendered label.

    Collapses a doubled glyph and canonicalises a drifted glyph. A label
    with no glyph is returned untouched — this never invents decoration
    for a button the author left plain, and it never touches the copy
    itself, so it is safe for all 10 locales.
    """
    if not label:
        return label
    collapsed = REPEATED_GLYPH.match(label)
    if collapsed:
        pad, glyph = collapsed.group(1), canonical_glyph(collapsed.group(2))
        return f"{pad}{glyph} {label[collapsed.end():].lstrip()}"
    first = LEADING_GLYPH.match(label)
    if not first:
        return label
    pad, glyph = first.group(1), first.group(2)
    canonical = canonical_glyph(glyph)
    if canonical == glyph:
        return label
    return f"{pad}{canonical} {label[first.end():].lstrip()}"


def decorate(key: str, label: str, style: str | None = None) -> str:
    """Apply the canonical style glyph of ``key`` to a resolved label.

    Idempotent: the result always carries exactly one glyph.
    """
    styled = style_label(label)
    if leading_glyph(styled):
        return styled
    glyph = STYLES.get(style or style_for_key(key), STYLES["neutral"]).glyph
    if not glyph:
        return styled
    if not styled:
        return glyph
    pad = styled[: len(styled) - len(styled.lstrip())]
    return f"{pad}{glyph} {styled.lstrip()}"


def registered_glyphs() -> frozenset[str]:
    """Every glyph this module may render, canonical and alias alike."""
    canonical = {s.glyph for s in STYLES.values()} | set(AUTHORED_PRIMARY_GLYPHS)
    return frozenset((canonical | set(GLYPH_ALIASES) | set(GLYPH_ALIASES.values())) - {""})
