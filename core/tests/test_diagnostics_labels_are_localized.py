"""The logs, debug and diagnostics screens name things in the user's language.

Severities (`warn`), diagnostic categories (`forward`) and per-task counters
(`skipped`) are stored as slugs, and every screen printed them verbatim: an
English word in the middle of a Persian sentence. The same screens also printed
`recoverable=True` — a Python boolean, in English, to a user who does not read
either — and the debug screen's own labels ("queue depth", "errors 24h") were
written straight into the handler.

The severities and categories are our own closed vocabularies (`errors.py`), so
they are catalog copy: `ui_diag_sev_*`, `ui_diag_cat_*`, `ui_counter_*`. A slug
this build does not know — an older row, a store that grew a category — is shown
unchanged rather than hidden, and a *known* slug with no translation logs an
ERROR.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.infrastructure.telegram import diagnostics
from core.infrastructure.telegram.errors import CATEGORIES, SEVERITIES
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, _BOT_RENDER_LANGUAGE

CORE = Path(__file__).resolve().parents[1]
CATALOG_PATH = CORE / "infrastructure" / "telegram" / "locales" / "translations.json"
LOCALES = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")
PERSIAN = re.compile(r"[\u0600-\u06ff]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
HAN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

SEV_KEYS = {f"ui_diag_sev_{sev}" for sev in SEVERITIES}
CAT_KEYS = {f"ui_diag_cat_{cat}" for cat in CATEGORIES}
COUNTER_KEYS = {f"ui_counter_{key}" for key in diagnostics._STAT_KEYS}
SCREEN_KEYS = {"ui_debug_queue_depth", "ui_debug_errors_24h", "ui_perf_window",
               "ui_logs_recoverable"}


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def translate(locale: str):
    def _t(key, **params):
        text = I18n().t(key, lang=locale, **params)
        assert text and text != key, f"{key} is missing from the {locale} catalog"
        return text

    return _t


class FakeErrorLog:
    """The two reads the log screens make."""

    def __init__(self, rows=None, counts=None):
        self.rows = rows if rows is not None else [
            {"severity": "error", "error_name": "FloodWait", "category": "forward",
             "detail": "420 FLOOD_WAIT_60", "recoverable": True},
            {"severity": "auth", "error_name": "AuthKeyUnregistered", "category": "auth",
             "detail": "", "recoverable": False},
        ]
        self.counts = counts if counts is not None else {"ERROR": 2, "warn": 1}

    async def recent(self, limit=10, severity=None):
        return list(self.rows)[: int(limit)]

    async def counts_by_severity(self, since_ts=0):
        return dict(self.counts)


def build_ui(locale: str = "fa", *, error_log=None, metrics=None, dispatcher=None):
    ui = ProBotUI(
        bot=MagicMock(), i18n=I18n(), sessions=MagicMock(), rules=MagicMock(),
        filter_rules=MagicMock(), ai_configs=MagicMock(),
        pipeline=SimpleNamespace(stats=SimpleNamespace(started_at=1_000_000, processed=120,
                                                      errors=3)),
        pool=SimpleNamespace(all_clients=lambda: []), login=MagicMock(),
        log_client=MagicMock(), admin_ids=[111], error_log=error_log,
        metrics=metrics, dispatcher=dispatcher,
    )
    _BOT_RENDER_LANGUAGE.set(locale)
    return ui


@pytest.fixture(autouse=True)
def _the_render_language_does_not_outlive_the_test():
    """`_BOT_RENDER_LANGUAGE` is a process-wide ContextVar, not a fixture.

    `build_ui()` below sets it so a screen renders in the language under test.
    The production wrapper resets it in a `finally`; a test has no such wrapper,
    so without this the chosen language — "fa" for the RTL cases — survives into
    every later module and renders their text in Persian. That is the same leak
    the render-language rule exists to prevent, just inside the test process.

    The context is cleared on the way in as well, so a language leaked by an
    earlier module cannot decide what an assertion in this file reads, and the
    token restored on the way out leaves the process exactly as it was found.
    """
    token = _BOT_RENDER_LANGUAGE.set(None)
    try:
        yield
    finally:
        _BOT_RENDER_LANGUAGE.reset(token)


def test_build_ui_renders_in_the_language_under_test():
    """`build_ui` chooses a language by setting the render context.

    Which is exactly why the fixture above exists: this value would otherwise
    still be here for the next test and the next module to render in.
    """
    ui = build_ui("fa")
    assert ui is not None
    assert _BOT_RENDER_LANGUAGE.get(None) == "fa"


# --------------------------------------------------------------------------- #
# the catalog carries the vocabularies
# --------------------------------------------------------------------------- #


def test_every_vocabulary_has_a_catalog_key():
    cat = catalog()["en"]
    missing = sorted((SEV_KEYS | CAT_KEYS | COUNTER_KEYS | SCREEN_KEYS) - set(cat))
    assert missing == [], f"no catalog entry for: {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_label_is_present_in_every_locale(locale: str):
    cat = catalog()
    keys = SEV_KEYS | CAT_KEYS | COUNTER_KEYS | SCREEN_KEYS
    absent = sorted(k for k in keys if not str(cat.get(locale, {}).get(k, "")).strip())
    assert absent == [], f"missing in {locale}: {absent}"


def test_the_english_labels_are_the_vocabularies_they_name():
    """The English screen keeps the operator's own slugs, so it cannot drift."""
    en = catalog()["en"]
    for sev in SEVERITIES:
        assert en[f"ui_diag_sev_{sev}"] == sev
    for cat in CATEGORIES:
        assert en[f"ui_diag_cat_{cat}"] == cat


@pytest.mark.parametrize("locale,pattern", [("fa", PERSIAN), ("ru", CYRILLIC), ("zh", HAN)])
def test_the_labels_are_written_in_that_locale_s_script(locale: str, pattern):
    cat = catalog()[locale]
    untranslated = sorted(
        key for key in SEV_KEYS | CAT_KEYS | COUNTER_KEYS | SCREEN_KEYS
        if not pattern.search(cat[key])
    )
    assert untranslated == [], f"still Latin in {locale}: {untranslated}"


@pytest.mark.parametrize("locale", ["fa", "ru", "zh"])
def test_the_labels_are_not_the_english_ones(locale: str):
    cat = catalog()
    same = sorted(k for k in SEV_KEYS | CAT_KEYS | COUNTER_KEYS | SCREEN_KEYS
                  if cat[locale][k] == cat["en"][k])
    assert same == [], f"{locale} left these in English: {same}"


def test_no_vocabulary_is_one_english_blob():
    """Loanwords are legitimate; a wholly untranslated table is not."""
    cat = catalog()
    for key in sorted(SEV_KEYS | CAT_KEYS | COUNTER_KEYS | SCREEN_KEYS):
        values = {cat[locale][key] for locale in LOCALES}
        assert len(values) >= 5, f"{key} reads the same in {len(values)} distinct values"


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def report() -> dict:
    return {
        "errors": {
            "total": 3,
            "counts": {"ERROR": 2, "warn": 1},
            "recent": [
                {"category": "forward", "error_name": "FloodWait", "rule_id": "rule-1",
                 "correlation_id": "abc123"},
            ],
        },
        "tasks": [{"rule_id": "rule-1", "counters": {"forwarded": 12, "skipped": 1}}],
    }


@pytest.mark.parametrize("locale", LOCALES)
def test_the_report_names_severities_and_categories_in_the_user_language(locale: str):
    body = diagnostics.render_report(translate(locale), report())
    cat = catalog()[locale]
    assert cat["ui_diag_sev_error"] in body
    assert cat["ui_diag_sev_warn"] in body
    assert cat["ui_diag_cat_forward"] in body
    assert cat["ui_counter_forwarded"] in body
    if locale != "en":
        for english in ("ERROR", "warn", "forward", "forwarded", "skipped"):
            assert english not in body, f"{locale} kept {english!r} in the report"


def test_the_report_still_names_the_error_it_cannot_translate():
    """A TDLib class name is the token support asks for — it stays."""
    body = diagnostics.render_report(translate("fa"), report())
    assert "FloodWait" in body


def test_an_unknown_counter_is_shown_as_stored():
    line = diagnostics.stat_line(translate("fa"), {"queued_by_policy": 4})
    assert "queued_by_policy 4" in line


# --------------------------------------------------------------------------- #
# one log row
# --------------------------------------------------------------------------- #


def test_a_log_row_translates_the_category_and_drops_the_boolean():
    row = {"severity": "error", "error_name": "FloodWait", "category": "forward"}
    line = diagnostics.error_line(translate("fa"), row)
    assert catalog()["fa"]["ui_diag_cat_forward"] in line
    assert "FloodWait" in line
    assert "recoverable" not in line and "True" not in line and "False" not in line


def test_a_retryable_row_says_so_in_the_user_language():
    row = {"severity": "warn", "error_name": "FloodWait", "category": "forward"}
    marked = diagnostics.error_line(translate("fa"), row, retryable=True)
    plain = diagnostics.error_line(translate("fa"), row, retryable=False)
    assert catalog()["fa"]["ui_logs_recoverable"] in marked
    assert catalog()["fa"]["ui_logs_recoverable"] not in plain


def test_an_unknown_category_is_shown_as_stored():
    row = {"severity": "error", "error_name": "X", "category": "rate_limit"}
    assert "rate_limit" in diagnostics.error_line(translate("fa"), row)


def test_a_known_category_without_a_translation_logs_and_keeps_the_slug(caplog):
    def t(key, **params):
        return key if key == "ui_diag_cat_forward" else translate("fa")(key, **params)

    with caplog.at_level(logging.ERROR, logger="atf.botui.diagnostics"):
        assert diagnostics.category_label(t, "forward") == "forward"
    assert any(rec.levelname == "ERROR" and "ui_diag_cat_forward" in rec.getMessage()
               for rec in caplog.records)


def test_a_known_category_without_a_translation_is_never_silent():
    """Conversely, an unknown slug must not spam the log."""
    lines: list = []

    class Rec(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Rec()
    logging.getLogger("atf.botui.diagnostics").addHandler(handler)
    try:
        assert diagnostics.category_label(translate("fa"), "rate_limit") == "rate_limit"
    finally:
        logging.getLogger("atf.botui.diagnostics").removeHandler(handler)
    assert lines == []


def test_the_severity_glyph_survives_the_store_s_own_casing():
    assert diagnostics.severity_icon("ERROR") == diagnostics.SEVERITY_ICON["error"]
    assert diagnostics.severity_icon("warn") == diagnostics.SEVERITY_ICON["warn"]
    assert diagnostics.severity_icon("something_new") == "❓"


def test_both_stores_spellings_of_one_severity_read_the_same():
    """The pipeline writes `warn`, the dashboard's table writes `WARNING`."""
    fa = translate("fa")
    assert diagnostics.severity_label(fa, "WARNING") == diagnostics.severity_label(fa, "warn")
    assert diagnostics.severity_icon("WARNING") == diagnostics.SEVERITY_ICON["warn"]
    assert diagnostics.severity_label(fa, "CRITICAL") == diagnostics.severity_label(fa, "fatal")


def test_the_hourly_breakdown_line_is_localized():
    line = diagnostics.severity_count_line(translate("fa"), "ERROR", 2)
    assert catalog()["fa"]["ui_diag_sev_error"] in line
    assert "ERROR" not in line
    assert line.endswith(": 2")


# --------------------------------------------------------------------------- #
# the screens the handlers render
# --------------------------------------------------------------------------- #


def test_the_debug_screen_labels_its_own_numbers_in_the_user_language():
    ui = build_ui("fa", error_log=FakeErrorLog(),
                  dispatcher=SimpleNamespace(queue_size=lambda: 7))
    body = run(ui._debug_body())
    cat = catalog()["fa"]
    assert cat["ui_debug_queue_depth"] in body
    assert cat["ui_debug_errors_24h"] in body
    assert "queue depth" not in body and "errors 24h" not in body


def test_the_performance_screen_labels_its_window_in_the_user_language():
    metrics = SimpleNamespace(hourly=AsyncMock())
    metrics.hourly.return_value = [{"forwarded": 5}, {"forwarded": 7}]
    ui = build_ui("fa", metrics=metrics)
    body = run(ui._perf_body())
    assert catalog()["fa"]["ui_perf_window"] in body
    assert "24h:" not in body


def test_the_handlers_render_rows_through_the_translated_renderer():
    source = (CORE / "infrastructure" / "telegram" / "pro_bot_ui.py").read_text(
        encoding="utf-8")
    assert "recoverable={bool(" not in source
    assert "SEV_ICON.get(" not in source
    assert "diagnostics.error_line(self._t, r)" in source
    assert "diagnostics.severity_count_line(self._t, sev, n)" in source
    assert 'f"📦 queue depth:' not in source
    assert 'f"⚠️ errors 24h:' not in source
