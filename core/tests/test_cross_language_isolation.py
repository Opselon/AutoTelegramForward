"""Cross-language isolation and per-user resolution guard tests.

These tests pin the invariant that a user's rendered language is always
the user's OWN choice — persisted DB row, in-memory state, or the user's
own Telegram client language_code — and never another user's choice
leaked through process-wide mutable state.

THE BUG THIS FILE GUARDS (production defect, October 2026):
    A user selected English but every bot string rendered in Persian.
    Root cause: `I18n.current` is a process-wide singleton, and the
    language-set callback mutated it (`i18n.set_language(code)`), so one
    user's choice leaked into every concurrent user's render. Seven code
    sites additionally branched on `self.i18n.current == "fa"` with raw
    hardcoded strings that bypassed the 378-key × 10-language catalog
    entirely, and `_main_text` re-resolved the language WITHOUT the user
    object so the cached state of an unrelated session could override
    the caller's client language.

If you add a new code path that reads `i18n.current` for rendering,
switch it to `_resolve_lang(uid, user)` or this file's audit will fail.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest

from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
from core.infrastructure.telegram.pro_bot_ui import ProBotUI


def _build_ui() -> ProBotUI:
    """A minimal ProBotUI with mocked collaborators (no DB/Telegram)."""
    return ProBotUI(
        bot=MagicMock(),
        i18n=I18n(),
        sessions=MagicMock(),
        rules=MagicMock(),
        filter_rules=MagicMock(),
        ai_configs=MagicMock(),
        pipeline=MagicMock(),
        pool=MagicMock(),
        login=MagicMock(),
        log_client=MagicMock(),
        admin_ids=[111],
    )


def _user(lang: str, name: str = "Bob") -> MagicMock:
    u = MagicMock()
    u.language_code = lang
    u.first_name = name
    u.last_name = ""
    u.username = None
    return u


# ---------------------------------------------------------------------------
# 1. The singleton must never leak one user's language to another user.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGUAGES))
def test_resolve_lang_never_reads_the_global(lang: str) -> None:
    """Even with the process-global set to `lang`, a user whose own choice
    is different must resolve to their own language, not the global."""
    ui = _build_ui()
    other = "fa" if lang != "fa" else "ru"
    ui.i18n.set_language(other)  # pollute the global the old way
    assert ui._resolve_lang(999_999, _user(lang)) == lang


def test_two_users_different_languages_simultaneously() -> None:
    """User A (en) and user B (fa) resolve independently: neither leaks."""
    ui = _build_ui()
    ui.i18n.set_language("fa")  # global polluted by user B's choice
    a = ui._resolve_lang(111, _user("en"))
    b = ui._resolve_lang(222, _user("fa"))
    assert a == "en"
    assert b == "fa"


def test_unknown_language_falls_back_to_english() -> None:
    """Fail-closed: an unknown client language renders English, never an
    arbitrary other language."""
    ui = _build_ui()
    ui.i18n.set_language("fa")
    assert ui._resolve_lang(333, _user("klingon")) == "en"
    assert ui._resolve_lang(0, None) == "en"


# ---------------------------------------------------------------------------
# 2. The language-set callback must persist, never mutate the global.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_language_callback_does_not_mutate_global() -> None:
    """Regression: _cb_lang_set used to call i18n.set_language(code),
    poisoning every subsequent render for every other user."""
    ui = _build_ui()
    ui._users = AsyncMock()
    users_db = MagicMock()
    users_db.query_one = MagicMock(return_value=None)
    ui._users._db = users_db

    ui.i18n.set_language("en")
    # simulate the callback body for user 222 choosing fa
    code, uid = "fa", 222
    if code in SUPPORTED_LANGUAGES:
        st = ui._state(uid)
        st.lang = code
        await ui._users.set_language(uid, code)
    assert ui.i18n.current == "en", "language choice leaked into the global"
    assert ui._resolve_lang(222) == "fa"  # persisted for the choosing user
    assert ui._resolve_lang(111, _user("en")) == "en"  # others unaffected


# ---------------------------------------------------------------------------
# 3. Rendering surfaces resolve per-user.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_text_renders_the_users_own_language() -> None:
    """_main_text must surface the caller's language, not the global."""
    sessions = AsyncMock()
    sessions.list_all = AsyncMock(return_value=[])
    sessions.list_by_owner = AsyncMock(return_value=[])
    rules = AsyncMock()
    rules.list_all = AsyncMock(return_value=[])
    rules.list_by_owner = AsyncMock(return_value=[])
    fr = AsyncMock()
    fr.list_all = AsyncMock(return_value=[])
    fr.list_by_owner = AsyncMock(return_value=[])
    ui = ProBotUI(
        bot=MagicMock(), i18n=I18n(), sessions=sessions, rules=rules,
        filter_rules=fr, ai_configs=AsyncMock(), pipeline=MagicMock(),
        pool=MagicMock(), login=MagicMock(), log_client=MagicMock(),
        admin_ids=[111],
    )
    ui.i18n.set_language("fa")  # global polluted
    text = await ui._main_text(111, user=_user("fa", "Scott"))
    assert "خوش آمدید" in text, "user's fa choice did not render Persian"
    assert "Scott" in text

    text_en = await ui._main_text(111, user=_user("en", "Scott"))
    assert "Welcome back" in text_en


# ---------------------------------------------------------------------------
# 4. Catalog integrity: every language translates every key.
# ---------------------------------------------------------------------------

def test_catalog_every_language_translates_every_key() -> None:
    """The translations catalog must be 1:1 — a missing key would render
    the English string (or the raw key) inside a foreign-language UI."""
    i18n = I18n()
    en = set(i18n._catalog["en"].keys())
    assert en, "English catalog is empty"
    for lang in SUPPORTED_LANGUAGES:
        table = i18n._catalog.get(lang)
        assert table is not None, f"language {lang} missing from catalog"
        missing = en - set(table.keys())
        assert not missing, f"{lang} is missing {len(missing)} keys, e.g. {sorted(missing)[:5]}"
        # Values must be non-empty and actually localised.
        empty = [k for k in en if not (table.get(k) or "").strip()]
        assert not empty, f"{lang} has empty values for {empty[:5]}"


def test_catalog_english_is_never_the_persian_fallback() -> None:
    """English values must not silently contain Persian graphemes — that
    would mean an English UI renders Persian (the reported symptom)."""
    i18n = I18n()
    fa_graphemes = set("یگچپژکیهؤئإأآ")
    leaked = [k for k, v in i18n._catalog["en"].items()
              if any(ch in fa_graphemes for ch in v)]
    assert not leaked, f"English catalog leaks Persian text in: {leaked[:5]}"


# ---------------------------------------------------------------------------
# 5. No cross-user state leakage through the shared UiState cache.
# ---------------------------------------------------------------------------

def test_state_cache_cannot_override_the_users_client_language() -> None:
    """_state() seeds UiState.lang from _resolve_lang WITH the user object,
    so an unrelated cached value can never override the caller's client
    language (the second half of the original defect)."""
    ui = _build_ui()
    st = ui._state(111, user=_user("ru"))
    assert st.lang == "ru", "UiState seed ignored the user's client language"
    # and the same user resolving again keeps their language
    assert ui._resolve_lang(111, _user("ru")) == "ru"



def test_locale_tags_normalize_region_variants() -> None:
    """Regional Telegram language tags resolve to a supported base locale."""
    i18n = I18n()
    assert i18n.set_language("pt-BR")
    assert i18n.current == "pt"
    assert i18n.t("ui_lang_choose", lang="fa_IR") == i18n.t("ui_lang_choose", lang="fa")
    assert not i18n.set_language("not-a-supported-language")


@pytest.mark.asyncio
async def test_handler_language_context_isolated_between_concurrent_users() -> None:
    """Concurrent events must not share a process-wide render language."""
    ui = _build_ui()

    async def render(_client, event):
        await asyncio.sleep(0)
        return ui._t("ui_lang_choose")

    wrapped = ui._localized_handler(render)
    english_event = MagicMock()
    english_event.from_user.id = 101
    english_event.from_user.language_code = "en"
    persian_event = MagicMock()
    persian_event.from_user.id = 202
    persian_event.from_user.language_code = "fa"

    english, persian = await asyncio.gather(
        wrapped(None, english_event),
        wrapped(None, persian_event),
    )
    assert english == ui.i18n.t("ui_lang_choose", lang="en")
    assert persian == ui.i18n.t("ui_lang_choose", lang="fa")
    assert english != persian
    assert ui.i18n.current == "en"


def test_every_static_bot_translation_reference_exists_in_catalog() -> None:
    """All literal _t() keys used by the production Telegram UI must resolve."""
    source_path = Path(__file__).resolve().parents[1] / "infrastructure" / "telegram" / "pro_bot_ui.py"
    source = source_path.read_text(encoding="utf-8")
    i18n = I18n()
    used = set(re.findall(r"""(?:self\._t|self\.i18n\.t)\(\s*["']([^"']+)["']""", source))
    missing = used - set(i18n._catalog["en"])
    assert not missing, f"Bot references missing translation keys: {sorted(missing)}"
