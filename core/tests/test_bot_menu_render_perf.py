"""Hot-path budget for the bot's menu render.

The dashboard is rebuilt on every ``/start``, every menu callback and every
return-to-menu button, and each build renders ~20 catalog keys through
``I18n.t()`` plus the style funnel for each button. The published budget
(``docs/ui-ux-system.md`` §5) is **menu build ≤ 2 ms**.

These tests measure the real builders against the real catalog — no stub
``_t``, no fixture copy of the keyboard — so a regression that reintroduces a
per-button regex pass, a second catalog lookup per key, or a SQLite read per
render fails here instead of showing up as a laggy bot.

The timing assertions are deliberately loose enough for CI noise (the budget
is 2 ms and the loop reports the median of 300 builds) but tight enough that
the *old* shape of the code — which recompiled style patterns, re-resolved the
language from the database and formatted every placeholder on each call —
cannot pass them.
"""
from __future__ import annotations

import time

import pytest

from core.infrastructure.telegram.i18n import SUPPORTED_LANGUAGES, I18n
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState
from core.infrastructure.telegram.ui_styles import (
    action_part,
    color_for_label,
    decorate,
    style_for_key,
    style_label,
)

ADMIN_UID = 5094837833
CLIENT_UID = 5989354154

#: The published budget, in milliseconds.
MENU_BUILD_BUDGET_MS = 2.0


class _CountingDB:
    """A database whose only job is to count reads."""

    def __init__(self, language: str | None) -> None:
        self.language = language
        self.reads = 0

    def query_one(self, sql, params=()):  # noqa: ANN001 - mirrors the real API
        self.reads += 1
        if self.language is None:
            return None
        return {"language": self.language}


class _Users:
    def __init__(self, db: _CountingDB) -> None:
        self._db = db


def _state(lang: str) -> UiState:
    st = UiState()
    st.lang = lang
    return st


def _ui(*, db: _CountingDB | None = None) -> ProBotUI:
    """A ProBotUI wired to the *real* render path.

    ``_t`` and ``_resolve_lang`` are left as the real bound methods; only the
    collaborators a menu build never touches are stubbed out.
    """
    i18n = I18n()
    ui = object.__new__(ProBotUI)
    ui.i18n = i18n
    ui._states = {}
    ui._users = _Users(db) if db is not None else None
    ui._db = None
    ui._admin_ids = {ADMIN_UID}
    ui._admin_service = None
    return ui


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _time_builds(ui: ProBotUI, uid: int, *, repeats: int = 300) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        markup = ui._main_menu(uid)
        samples.append((time.perf_counter() - started) * 1000.0)
    assert markup.inline_keyboard
    return _median(samples)


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_menu_build_is_within_budget_in_every_language(lang):
    ui = _ui()
    ui._states[CLIENT_UID] = _state(lang)

    # One warm build (catalog table + style memo) then measure.
    ui._main_menu(CLIENT_UID)
    median_ms = _time_builds(ui, CLIENT_UID)

    assert median_ms < MENU_BUILD_BUDGET_MS, (
        f"main menu build for lang={lang} took {median_ms:.3f}ms "
        f"(budget {MENU_BUILD_BUDGET_MS}ms)"
    )


def test_admin_menu_build_is_within_budget():
    ui = _ui()
    ui._states[ADMIN_UID] = _state("en")
    ui._main_menu(ADMIN_UID)
    assert _time_builds(ui, ADMIN_UID) < MENU_BUILD_BUDGET_MS


def test_repeated_renders_do_not_query_the_database():
    """The language resolution is memoized; a render costs zero reads.

    ``_resolve_lang`` falls back to a SQLite ``SELECT`` when the user has no
    in-memory state — which is exactly the case for a menu built from a bare
    uid. Before memoization every such build paid a query.
    """
    db = _CountingDB("fa")
    ui = _ui(db=db)

    ui._main_menu(CLIENT_UID)
    after_first = db.reads
    assert after_first == 1, "first render should resolve the language once"

    for _ in range(50):
        ui._main_menu(CLIENT_UID)

    assert db.reads == after_first, (
        f"menu build hit the database {db.reads - after_first} extra times "
        "across 50 renders"
    )


def test_language_choice_is_reflected_on_the_next_render():
    """A stale memo would render the *previous* language until restart."""
    db = _CountingDB("fa")
    ui = _ui(db=db)

    assert ui._resolve_lang(CLIENT_UID) == "fa"
    ui.remember_language(CLIENT_UID, "ru")
    assert ui._resolve_lang(CLIENT_UID) == "ru"

    # And the same memo is what a fresh render reads.
    assert ui._main_menu(CLIENT_UID) is not None
    assert ui._resolve_lang(CLIENT_UID) == "ru"


def test_catalog_table_is_built_once_per_language():
    i18n = I18n()
    first = i18n.table("fa")
    assert i18n.table("fa") is first
    assert i18n.table("fa-IR") is first, "regional tags resolve to the base locale"
    # English keys are merged in, so a missing translation is one dict hit
    # instead of a second lookup, and no merged value is empty.
    assert set(i18n.table("en")) <= set(first)
    assert all(first[key] for key in i18n.table("en"))


def test_empty_translation_falls_back_to_english():
    """The old lookup was ``table.get(k) or english.get(k)``.

    A merged table must not turn an empty locale value into an empty button
    label — that would blank the button in that language only.
    """
    i18n = I18n()
    i18n._catalog["fa"] = dict(i18n._catalog["fa"])
    key = "ui_cancel"
    i18n._catalog["fa"][key] = ""
    i18n._tables.pop("fa", None)

    assert i18n.t(key, lang="fa") == i18n.table("en")[key]


def test_formatted_translations_are_cached_but_identical():
    i18n = I18n()
    first = i18n.t("login_success", lang="fa", phone="123")
    second = i18n.t("login_success", lang="fa", phone="123")
    assert first == second
    assert "123" in second
    # A different placeholder value must not be served from the cache.
    assert "999" in i18n.t("login_success", lang="fa", phone="999")


def test_unknown_key_still_renders_the_key():
    i18n = I18n()
    assert i18n.t("definitely_missing_key_xyz", lang="ru") == "definitely_missing_key_xyz"


def test_style_helpers_are_memoized_but_stable():
    for helper in (style_for_key, action_part, style_label, color_for_label):
        helper.cache_clear()
    assert style_for_key("ui_cancel") == style_for_key("ui_cancel")
    assert style_for_key.cache_info().hits >= 1

    first = decorate("ui_cancel", "❌ Cancel")
    second = decorate("ui_cancel", "❌ Cancel")
    assert first == second
    assert decorate.cache_info().hits >= 1


def test_catalog_lookup_throughput():
    """A coarse guard on the lookup itself (10k keys in well under a second)."""
    i18n = I18n()
    keys = ["ui_cancel", "ui_back", "ui_main_menu_forward", "ui_done", "ui_refresh"]
    i18n.t(keys[0], lang="fa")
    started = time.perf_counter()
    for _ in range(2000):
        for key in keys:
            i18n.t(key, lang="fa")
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"10k catalog lookups took {elapsed:.3f}s"
