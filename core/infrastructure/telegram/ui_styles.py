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

A third drift is the *namespace*. The dashboard's keys (``ui_main_menu_*``)
name the panel a button lives on, not the action it performs, and the rule
table read that namespace as an action: ``ui_main_menu_forward`` matched
``main|menu`` and resolved to ``nav``, so every glyph-less dashboard entry
was one ``decorate`` call away from rendering as a back arrow ("⬅️
Forward"). The surface prefix is stripped before the rules run (see
:data:`SURFACE_PREFIXES`), so a dashboard key inherits the semantics of its
own action — and a new dashboard entry added tomorrow cannot silently turn
into a "back" button.

Because the dashboard is the first screen every user sees, its four
glyph-less entries carry an explicit class (``forward`` / ``copy`` /
``filter`` / ``primary``) and colour marks only the action to take next:
the two create/forward paths in primary, the upgrade path in success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

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
    "SURFACE_PREFIXES",
    "style_for_key",
    "action_part",
    "KEY_STYLE_RULES_COMPILED",
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
    # Dashboard classes. The main menu's own actions had no semantic home, and
    # the generic rules gave them nothing usable: "Forward" is not "add" and
    # "Clone" is not "sync". One class per action keeps the dashboard's four
    # glyph-less entries readable without painting the whole panel.
    "forward": ButtonStyle("forward", "📤", PRIMARY),
    "copy": ButtonStyle("copy", "📋", None),
    "filter": ButtonStyle("filter", "🛡️", None),
}

#: Key prefixes that name the *surface* a button belongs to rather than the
#: action it performs. Stripped before the rule table runs: without this,
#: ``ui_main_menu_{forward,clone}`` matched ``main|menu`` and resolved to
#: ``nav`` — a ⬅️ back arrow on the dashboard's Forward entry. Only the
#: namespace is dropped; the overrides below still match the full key.
SURFACE_PREFIXES: tuple[str, ...] = ("ui_main_menu_",)

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
    "ui_main_menu_forward": "forward",
    "ui_main_menu_clone": "copy",
    "ui_main_menu_filters": "filter",
    # The dashboard's upgrade entry is the one purchase path in the panel:
    # green marks it without painting any of the navigation entries.
    "ui_main_menu_upgrade": "success",
    # "My Telegram API credentials" is the settings surface's main action —
    # without it a new account cannot forward anything at all.
    "ui_settings_creds": "primary",
    # Broadcast is the admin panel's only create/send action.
    "ui_admin_btn_broadcast": "primary",
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
#:
#: 🌐 is deliberately absent. It is both the ``open`` class glyph and the
#: globe this catalog uses for *navigation* copy ("🌐 Language", "🌐 Choose
#: language", the base-URL prompts), so reading it as a primary action
#: painted the language entry blue in every panel that rendered it — the
#: class colour of an ``open`` button now comes from its key
#: (``color_for_key``), never from the authored copy.
AUTHORED_PRIMARY_GLYPHS: frozenset[str] = frozenset(
    {"➕", "🔄", "📤", "📥", "✏️", "💾", "🔗"}
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
#: The rule table above, compiled once. ``re.search`` with a *pattern string*
#: consults a 512-entry cache on every call and re-compiles on a miss; the style
#: layer runs for every button of every rendered panel, so the patterns are
#: compiled here instead — same table, same order, same result.
KEY_STYLE_RULES_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), style) for pattern, style in KEY_STYLE_RULES
)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=4096)
def action_part(key: str) -> str:
    """The action a key performs, with its surface namespace removed.

    ``ui_main_menu_forward`` is the Forward entry *of* the main menu; the
    rules must see ``forward``, not the panel's name, or every dashboard
    entry reads as "main menu" and resolves to the nav class.
    """
    name = (key or "").strip().lower()
    for prefix in SURFACE_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


@lru_cache(maxsize=4096)
def style_for_key(key: str) -> str:
    """Return the semantic style of a catalog key (``neutral`` if unknown)."""
    name = (key or "").strip().lower()
    if name in KEY_STYLE_OVERRIDES:
        return KEY_STYLE_OVERRIDES[name]
    for pattern, style in KEY_STYLE_RULES_COMPILED:
        if pattern.search(action_part(name)):
            return style
    return "neutral"


@lru_cache(maxsize=4096)
def style_glyph(key: str) -> str:
    """Canonical glyph for a catalog key (empty for neutral)."""
    return STYLES.get(style_for_key(key), STYLES["neutral"]).glyph


@lru_cache(maxsize=4096)
def color_for_key(key: str) -> str | None:
    """Background colour for a catalog key (``None`` when unstyled)."""
    return STYLES.get(style_for_key(key), STYLES["neutral"]).color


@lru_cache(maxsize=8192)
def leading_glyph(label: str) -> str:
    """First glyph of a label, canonicalised through the alias table."""
    match = LEADING_GLYPH.match(label or "")
    if not match:
        return ""
    return GLYPH_ALIASES.get(match.group(2), match.group(2))


@lru_cache(maxsize=1024)
def canonical_glyph(glyph: str) -> str:
    """Map an alias glyph to its canonical twin (identity otherwise)."""
    return GLYPH_ALIASES.get(glyph, glyph)


@lru_cache(maxsize=8192)
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
@lru_cache(maxsize=8192)
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


@lru_cache(maxsize=8192)
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
