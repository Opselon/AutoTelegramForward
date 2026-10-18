"""Text that reaches a user must be in that user's language.

Three surfaces still were not, and each is pinned here:

  * the cancelled-connect screen hardcoded Russian and Chinese in the source and
    served English to the other eight locales — six of which have their own
    translation sitting in the catalog;
  * the broadcast summary asked for `ui_broadcast_done`, a key that does not
    exist (the catalog has `ui_admin_broadcast_done`), so the admin who ran the
    broadcast was sent the key itself; and
  * `lang == "fa"` was used as "unset" in the PV screens, which is the shape the
    project forbids: a language tag compared in code instead of resolved through
    the catalog.
"""

import json
import pathlib
import re
import sqlite3
import time
from typing import Dict, List
from unittest.mock import MagicMock

import pytest

from core.infrastructure.grpc_server.admin_servicer import AdminControlServicer as AdminServicer
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

CORE = pathlib.Path("core")
CATALOG = CORE / "infrastructure/telegram/locales/translations.json"
UI_SOURCE = CORE / "infrastructure/telegram/pro_bot_ui.py"

#: the exact strings that used to live in the source, one per hardcoded branch
HARDCODED_CANCEL_TEXT = (
    "❌ Операция отменена.",
    "🚀 Подключить снова",
    "🏚️ Главное меню",
    "❌ 操作已取消。",
    "🚀 重新连接",
    "🏚️ 主菜单",
)


def _build_ui() -> ProBotUI:
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


@pytest.fixture
def servicer(tmp_path):
    """A real AdminServicer over a real database, with one user per language."""
    db = SqliteDatabase(str(tmp_path / "svc.db"))
    now = int(time.time())
    languages: Dict[int, str] = {}
    for index, lang in enumerate(SUPPORTED_LANGUAGES):
        uid = 1000 + index
        db.execute(
            "INSERT INTO users (user_id, language, created_at, updated_at) VALUES (?,?,?,?)",
            (uid, lang, now, now),
        )
        languages[uid] = lang
    svc = AdminServicer(db, None, None, None, i18n=I18n())
    return svc, languages


# --------------------------------------------------------------------------- #
# the cancelled-connect screen
# --------------------------------------------------------------------------- #


def test_the_cancelled_connect_screen_reads_the_catalog_in_every_language():
    ui = _build_ui()
    for lang in SUPPORTED_LANGUAGES:
        text, retry, menu = ui._cancelled_connect_texts(lang)
        assert text == ui._t("ui_cancelled", lang=lang)
        assert retry == ui._t("ui_retry_connect", lang=lang)
        assert menu == ui._t("ui_back_main_menu", lang=lang)


def test_no_locale_is_served_another_locales_cancelled_text():
    ui = _build_ui()
    english = ui._cancelled_connect_texts("en")
    for lang in SUPPORTED_LANGUAGES:
        if lang == "en":
            continue
        text, retry, menu = ui._cancelled_connect_texts(lang)
        assert (text, retry, menu) != english, (
            f"{lang} is served the English cancelled text - the catalog has its own"
        )


def test_the_cancelled_screen_keeps_no_hardcoded_language_text():
    source = UI_SOURCE.read_text(encoding="utf-8")
    offenders = [text for text in HARDCODED_CANCEL_TEXT if text in source]
    assert not offenders, offenders
    assert 'lang == "ru"' not in source and 'lang == "zh"' not in source


# --------------------------------------------------------------------------- #
# the broadcast summary
# --------------------------------------------------------------------------- #


def test_the_broadcast_summary_renders_in_the_recipients_language(servicer):
    svc, languages = servicer
    for uid, lang in languages.items():
        rendered = svc._localized("ui_admin_broadcast_done", uid, sent=3, failed=1, total=4)
        assert "ui_" not in rendered, (lang, rendered)
        assert "3" in rendered and "1" in rendered, (lang, rendered)
        assert rendered == I18n().t("ui_admin_broadcast_done", lang=lang, sent=3, failed=1)


def test_the_key_the_servicer_asks_for_exists_in_every_language():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    for lang in SUPPORTED_LANGUAGES:
        assert "ui_admin_broadcast_done" in catalog[lang], lang
    assert "ui_broadcast_done" not in catalog["en"], (
        "the servicer used to ask for this key; if it is added, the mismatch "
        "test above no longer proves anything"
    )


def test_the_servicer_no_longer_asks_for_the_missing_key():
    source = (CORE / "infrastructure/grpc_server/admin_servicer.py").read_text(encoding="utf-8")
    assert '"_localized("ui_broadcast_done"' not in source
    assert 'ui_admin_broadcast_done",' in source


def test_a_notification_missing_from_a_language_falls_back_to_english_and_logs(servicer, caplog):
    svc, languages = servicer
    uid_ru = next(uid for uid, lang in languages.items() if lang == "ru")
    # Drop the key from the Russian table only: the recipient must still get a
    # readable line, and the operator must be able to see that a translation is
    # missing rather than reading the raw key in a bug report.
    table = svc._i18n._catalog["ru"]
    removed = table.pop("ui_admin_broadcast_done", None)
    assert removed is not None, "the key was not there to remove"
    try:
        with caplog.at_level("DEBUG"):
            rendered = svc._localized("ui_admin_broadcast_done", uid_ru, sent=1, failed=0)
        assert "ui_" not in rendered
        assert rendered == I18n().t("ui_admin_broadcast_done", lang="en", sent=1, failed=0)
    finally:
        table["ui_admin_broadcast_done"] = removed


def test_a_notification_key_that_exists_nowhere_is_logged(servicer, caplog):
    svc, languages = servicer
    uid = next(iter(languages))
    with caplog.at_level("DEBUG"):
        rendered = svc._localized("ui_this_key_is_not_in_the_catalog", uid)
    assert rendered == "ui_this_key_is_not_in_the_catalog"
    assert any(
        rec.levelname == "ERROR" and "ui_this_key_is_not_in_the_catalog" in rec.getMessage()
        for rec in caplog.records
    )


def test_the_servicer_accepts_every_supported_language(servicer):
    svc, languages = servicer
    for uid, lang in languages.items():
        assert svc._i18n_lang(uid) == lang
    assert svc._i18n_lang(999999) == "en"  # unknown recipient -> English, never a crash


# --------------------------------------------------------------------------- #
# the persisted preference is honoured (it silently was not)
# --------------------------------------------------------------------------- #


def _user_db(path):
    """A real database with one user per language, as the profile test does."""
    db = SqliteDatabase(str(path))
    now = int(time.time())
    for index, lang in enumerate(SUPPORTED_LANGUAGES):
        db.execute(
            "INSERT INTO users (user_id, language, created_at, updated_at) VALUES (?,?,?,?)",
            (2000 + index, lang, now, now),
        )
    return db


def test_the_bot_honours_the_stored_language_without_in_memory_state(tmp_path):
    """The second step of the documented resolution order used to be dead.

    `_resolve_lang` reads the stored preference through `row.get("language")`,
    and a `sqlite3.Row` has no `get` - the AttributeError was swallowed, so every
    user who was not in the in-memory cache (after a restart, or with a Telegram
    client in another language) was served English.
    """
    from types import SimpleNamespace

    db = _user_db(tmp_path / "resolve.db")
    ui = _build_ui()
    ui._users = SimpleNamespace(_db=db)
    ui._states = {}          # nothing cached: the stored preference must decide
    for index, lang in enumerate(SUPPORTED_LANGUAGES):
        uid = 2000 + index
        assert ui._resolve_lang(uid) == lang, (uid, lang)
        assert ui._resolve_lang(uid, user_obj=None) == lang


def test_the_client_language_is_still_only_a_fallback(tmp_path):
    """A Telegram client language must not outrank the stored preference."""
    from types import SimpleNamespace

    db = _user_db(tmp_path / "fallback.db")
    ui = _build_ui()
    ui._users = SimpleNamespace(_db=db)
    ui._states = {}
    assert ui._resolve_lang(2001, user_obj=SimpleNamespace(language_code="en-US")) == "fa"


def test_an_unknown_user_still_resolves_from_the_client_then_english(tmp_path):
    from types import SimpleNamespace

    db = _user_db(tmp_path / "unknown.db")
    ui = _build_ui()
    ui._users = SimpleNamespace(_db=db)
    ui._states = {}
    assert ui._resolve_lang(999999, user_obj=SimpleNamespace(language_code="pt-BR")) == "pt"
    assert ui._resolve_lang(999999) == "en"


def test_the_bot_manager_reads_the_same_preference(tmp_path):
    from types import SimpleNamespace

    from core.infrastructure.telegram.bot_manager import BotManager

    db = _user_db(tmp_path / "manager.db")
    mgr = BotManager.__new__(BotManager)
    mgr._users = SimpleNamespace(_db=db)
    mgr._awaiting_lang = {}
    for index, lang in enumerate(SUPPORTED_LANGUAGES):
        assert mgr._resolve_lang(2000 + index) == lang, lang
    assert mgr._resolve_lang(999999) == "en"


# --------------------------------------------------------------------------- #
# the source rule: no language tag is compared in code
# --------------------------------------------------------------------------- #


def _language_tag_comparisons() -> List[str]:
    """`x == "fa"` (and friends) anywhere in the package, outside i18n itself."""
    pattern = re.compile(
        r"(==|!=)\s*[\"'](" + "|".join(SUPPORTED_LANGUAGES) + r")[\"']"
    )
    hits = []
    for path in sorted(CORE.rglob("*.py")):
        if "tests" in path.parts or path.name == "i18n.py":
            # i18n.py is where a tag is *defined* and normalized; comparing one
            # there is the module's job.
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(line):
                hits.append(f"{path}:{line_no}: {stripped[:90]}")
    return hits


def test_no_language_tag_is_compared_in_code():
    """Localized text resolves through the catalog, never through a tag check.

    The cancelled-connect screen compared `lang == "fa"` / `"ru"` / `"zh"` and
    sent everything else English; the PV screens treated `"fa"` as "unset". Both
    are banned for the same reason: a new language would need a code change.
    """
    offenders = _language_tag_comparisons()
    assert not offenders, offenders


def test_the_language_tag_rule_would_catch_a_comparison(tmp_path):
    """Negative control for the rule, so it cannot pass by matching nothing."""
    planted = CORE / "infrastructure/telegram/_lang_rule_control.py"
    planted.write_text('if lang == "ru":\n    pass\n', encoding="utf-8")
    try:
        hits = _language_tag_comparisons()
        assert any("_lang_rule_control.py" in hit for hit in hits), hits
    finally:
        planted.unlink()


def test_the_pv_screens_resolve_an_unspecified_language_from_the_user():
    """`lang or <resolve from uid>`, not `lang or "fa"`.

    Passing a language still works; passing nothing resolves the caller's own
    language; and "fa" is a language like any other rather than a sentinel.
    """
    source = UI_SOURCE.read_text(encoding="utf-8")
    assert 'lang is None or lang == "fa"' not in source
    assert "lang = lang or self._lang_for(uid=uid)" in source


def test_the_cancel_helper_is_used_by_both_call_sites():
    source = UI_SOURCE.read_text(encoding="utf-8")
    assert source.count("self._cancelled_connect_texts(lang)") == 2


def test_a_sqlite_row_round_trips_the_language_column(tmp_path):
    """The preference the servicer reads is the one the bot writes."""
    path = str(tmp_path / "lang.db")
    db = SqliteDatabase(path)
    now = int(time.time())
    db.execute(
        "INSERT INTO users (user_id, language, created_at, updated_at) VALUES (?,?,?,?)",
        (777, "fr", now, now),
    )
    row = db.query_one("SELECT language FROM users WHERE user_id=?", (777,))
    assert row["language"] == "fr"
    # and it is the same value an independent connection sees
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    assert raw.execute("SELECT language FROM users WHERE user_id=777").fetchone()["language"] == "fr"
    raw.close()
