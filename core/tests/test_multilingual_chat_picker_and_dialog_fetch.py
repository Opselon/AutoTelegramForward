import pytest
from unittest.mock import AsyncMock, MagicMock
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState
from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
import json


def test_translations_catalog_1_to_1_parity():
    """Verify that every single language has the exact same set of translation keys."""
    i18n = I18n()
    catalog = i18n._catalog
    for lang in SUPPORTED_LANGUAGES:
        assert lang in catalog, f"Missing language {lang} in catalog"

    ref_keys = set(catalog["en"].keys())
    assert len(ref_keys) >= 345, f"Expected at least 345 keys, got {len(ref_keys)}"

    for lang in SUPPORTED_LANGUAGES:
        lang_keys = set(catalog[lang].keys())
        missing = ref_keys - lang_keys
        extra = lang_keys - ref_keys
        assert not missing, f"Language {lang} is missing keys: {missing}"
        assert not extra, f"Language {lang} has unexpected extra keys: {extra}"
        assert len(lang_keys) == len(ref_keys), f"Key count mismatch for {lang}"


def test_resolve_lang_db_preference():
    """User language in database takes precedence over bot default."""
    ui = object.__new__(ProBotUI)
    ui._states = {}
    ui._users = MagicMock()
    ui._users._db = MagicMock()
    ui._users._db.query_one.return_value = {"language": "en"}
    ui.i18n = MagicMock()
    ui.i18n.current = "fa"

    resolved = ui._resolve_lang(uid=5094837833)
    assert resolved == "en"


def test_render_chat_picker_multilingual_english():
    """Chat picker with English locale renders English headers and buttons."""
    i18n = I18n()
    ui = object.__new__(ProBotUI)
    ui.i18n = i18n
    ui._states = {}
    ui._users = None
    ui._t = lambda key, lang=None, **kw: i18n.t(key, lang=lang or "en", **kw)
    ui._kbd = lambda rows: rows
    ui._resolve_lang = lambda uid=None, user_obj=None: "en"

    st = UiState(
        step="rule_source",
        lang="en",
        user_id=5094837833,
        buffer={
            "chats": [
                {"id": "-100123", "title": "News Channel", "emoji": "📢", "kind": "channel", "username": "news"},
            ],
            "page": 0,
        },
    )

    text, kbd = ui._render_chat_picker(st, role="source", page=0, uid=5094837833)
    assert "Select Source Channel or Group" in text
    assert "Page 1 of 1" in text
    assert "News Channel" in text

    button_labels = [btn[0] for row in kbd for btn in row]
    assert any("Refresh List" in lbl for lbl in button_labels)
    assert any("Cancel" in lbl for lbl in button_labels)


def test_render_chat_picker_multilingual_persian():
    """Chat picker with Persian locale renders Persian headers and buttons."""
    i18n = I18n()
    ui = object.__new__(ProBotUI)
    ui.i18n = i18n
    ui._states = {}
    ui._users = None
    ui._t = lambda key, lang=None, **kw: i18n.t(key, lang=lang or "fa", **kw)
    ui._kbd = lambda rows: rows
    ui._resolve_lang = lambda uid=None, user_obj=None: "fa"

    st = UiState(
        step="rule_source",
        lang="fa",
        user_id=12345,
        buffer={
            "chats": [
                {"id": "-100123", "title": "کانال خبری", "emoji": "📢", "kind": "channel", "username": "news"},
            ],
            "page": 0,
        },
    )

    text, kbd = ui._render_chat_picker(st, role="source", page=0, uid=12345)
    assert "انتخاب کانال یا گروه مبدأ" in text
    assert "صفحه 1 از 1" in text
    assert "کانال خبری" in text

    button_labels = [btn[0] for row in kbd for btn in row]
    assert any("تازه‌سازی لیست" in lbl for lbl in button_labels)
    assert any("انصراف" in lbl for lbl in button_labels)


def test_render_chat_picker_fallback_localized():
    """Fallback text when no dialogs available renders in active language."""
    i18n = I18n()
    ui = object.__new__(ProBotUI)
    ui.i18n = i18n
    ui._states = {}
    ui._users = None
    ui._t = lambda key, lang=None, **kw: i18n.t(key, lang=lang or "en", **kw)
    ui._kbd = lambda rows: rows
    ui._resolve_lang = lambda uid=None, user_obj=None: "en"

    st = UiState(
        step="rule_source",
        lang="en",
        user_id=5094837833,
        buffer={"chats": [], "page": 0},
    )

    text, kbd = ui._render_chat_picker(st, role="source", page=0, uid=5094837833)
    assert "Source dialog list not found automatically" in text
    assert "Please enter numeric ID" in text


@pytest.mark.asyncio
async def test_fetch_dialogs_admin_fallback():
    """Admin users without owner-specific sessions fall back to list_all()."""
    ui = object.__new__(ProBotUI)
    ui._is_admin = lambda uid: True
    ui._resolve_lang = lambda uid=None: "en"
    ui._t = lambda key, **kw: key

    session_mock = MagicMock()
    session_mock.id = "sess_001"
    session_mock.is_active = True
    session_mock.is_authorized = True
    session_mock.session_string = ""
    session_mock.session_string_encrypted = ""

    ui._sessions = MagicMock()
    ui._sessions.list_by_owner = AsyncMock(return_value=[])
    ui._sessions.list_all = AsyncMock(return_value=[session_mock])

    client_mock = MagicMock()
    client_mock.is_connected = True

    dialog_mock = MagicMock()
    dialog_mock.chat.id = -100999
    dialog_mock.chat.title = "Admin Channel"
    dialog_mock.chat.type = "channel"
    dialog_mock.chat.username = "admin_chan"

    async def async_iter(*args, **kwargs):
        yield dialog_mock

    client_mock.get_dialogs = MagicMock(return_value=async_iter())

    ui._pool = MagicMock()
    ui._pool.get.return_value = client_mock

    chats = await ui._fetch_dialogs(owner_user_id=5094837833)
    assert len(chats) == 1
    assert chats[0]["id"] == "-100999"
    assert chats[0]["title"] == "Admin Channel"
    assert chats[0]["kind"] == "channel"
