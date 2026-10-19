"""Words that reach a screen must come from the catalog, not from the source.

Key parity was green while nine locales still read English. The words were not
keys at all: they were source literals interpolated *into* a catalog template —
``plan = "VIP Pro"`` fed to ``ui_main_plan``, ``remaining = "0 days"`` fed to
``ui_main_remaining``, ``display_name = "Valued User"`` fed to the greeting,
``"... delay={n}s"`` appended to every rule row, ``"AF Route Lab Source"`` used
as a chat-name fallback, ``f"{name} (Copy)"`` written into a duplicated rule's
name. The catalog never sees them, so no parity, placeholder or cross-leak check
can: only these renders can.

Every case below renders a real screen through the real catalog and asserts the
Persian render carries no English residue from the source, and that the English
render still reads English.
"""

import ast
import io
import json
import pathlib
import re
import tokenize
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.domain.entities import ForwardRule
from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState

CORE = pathlib.Path("core")
UI_SOURCE = CORE / "infrastructure/telegram/pro_bot_ui.py"
CATALOG = json.loads(
    (CORE / "infrastructure/telegram/locales/translations.json").read_text(encoding="utf-8")
)

#: every key this batch introduced, or moved a hardcoded value onto
BATCH_KEYS = (
    "ui_ai_test_error_body",
    "ui_ai_test_unknown_error",
    "ui_plan_free",
    "ui_plan_pro",
    "ui_plan_status_active",
    "ui_plan_status_none",
    "ui_rule_copy_of",
    "ui_rule_new_copy",
    "ui_rule_source_fallback",
    "ui_rule_target_fallback",
    "ui_rules_row_meta",
    "ui_secs_value",
    "ui_topic_panel_title",
    "ui_user_fallback_id",
    "ui_user_fallback_name",
)

#: literals that used to sit in the source and reach a user's screen
RETIRED_LITERALS = (
    "VIP Pro",
    "0 days",
    "Valued User",
    "AF Route Lab Source",
    "AF Route Lab Target",
    "New Rule (Copy)",
    "**Set up Forwarding Topic**",
    "mode=",
)


def _string_literals() -> list[str]:
    """Every string literal in the UI source except comments and docstrings.

    Comments and docstrings name the retired literals on purpose — they explain
    the defect — so a raw ``in source`` check would flag the explanation itself.
    A docstring is never rendered; a literal can be.
    """
    source = UI_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstring_nodes.add(id(body[0].value))
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_nodes:
                continue
            values.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            values.append(
                "".join(
                    part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    else "{}"
                    for part in node.values
                )
            )
    return values


def _user(lang: str, name: str = "Bob") -> MagicMock:
    u = MagicMock()
    u.language_code = lang
    u.first_name = name
    u.last_name = ""
    u.username = None
    return u


def _main_ui(rules: Any = None) -> ProBotUI:
    """A ProBotUI whose collaborators answer empty, for `_main_text`."""
    sessions = AsyncMock()
    sessions.list_all = AsyncMock(return_value=[])
    sessions.list_by_owner = AsyncMock(return_value=[])
    rules_repo = AsyncMock()
    rules_repo.list_all = AsyncMock(return_value=[])
    rules_repo.list_by_owner = AsyncMock(return_value=list(rules or []))
    filters = AsyncMock()
    filters.list_all = AsyncMock(return_value=[])
    filters.list_by_owner = AsyncMock(return_value=[])
    ui = ProBotUI(
        bot=MagicMock(), i18n=I18n(), sessions=sessions, rules=rules_repo,
        filter_rules=filters, ai_configs=AsyncMock(), pipeline=MagicMock(),
        pool=MagicMock(), login=MagicMock(), log_client=MagicMock(),
        admin_ids=[111],
    )
    if rules:
        rules_repo.list_by_owner = AsyncMock(return_value=list(rules))
    return ui


def _fa_ui() -> ProBotUI:
    """A UI wired to the real catalog whose only tenant picked Persian."""
    ui = object.__new__(ProBotUI)
    ui.i18n = I18n()
    ui._states = {5094837833: UiState(lang="fa")}
    ui._users = None
    ui._t = ProBotUI._t.__get__(ui, ProBotUI)
    ui._btn = ProBotUI._btn.__get__(ui, ProBotUI)
    ui._fallback_display_name = ProBotUI._fallback_display_name.__get__(ui, ProBotUI)
    ui._addon_label = ProBotUI._addon_label.__get__(ui, ProBotUI)
    ui._lang_for = ProBotUI._lang_for.__get__(ui, ProBotUI)
    ui._kbd = ProBotUI._kbd.__get__(ui, ProBotUI)
    ui._rules_menu = ProBotUI._rules_menu.__get__(ui, ProBotUI)
    ui._render_rules_list = ProBotUI._render_rules_list.__get__(ui, ProBotUI)
    ui._render_rule_detail = ProBotUI._render_rule_detail.__get__(ui, ProBotUI)
    ui._render_advanced_settings = ProBotUI._render_advanced_settings.__get__(ui, ProBotUI)
    return ui


def _rule(**kw: Any) -> ForwardRule:
    params: dict[str, Any] = dict(
        id="rule-i18n-1",
        name="",
        session_id="session-1",
        owner_user_id=5094837833,
        source_chat_id="-1001",
        target_chat_id="-1002",
        source_chat_name="",
        target_chat_name="",
        is_active=True,
    )
    params.update(kw)
    return ForwardRule(**params)


# --------------------------------------------------------------------------- #
# the keys exist, 1:1, with the English placeholders
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key", BATCH_KEYS)
def test_every_key_of_the_batch_exists_in_every_locale(key: str) -> None:
    assert set(CATALOG) == set(SUPPORTED_LANGUAGES)
    for lang in SUPPORTED_LANGUAGES:
        assert CATALOG[lang].get(key, "").strip(), f"{key} missing from {lang}"


@pytest.mark.parametrize("key", BATCH_KEYS)
def test_every_batch_key_keeps_the_english_placeholders(key: str) -> None:
    english = set(re.findall(r"\{(\w+)\}", CATALOG["en"][key]))
    for lang in SUPPORTED_LANGUAGES:
        assert set(re.findall(r"\{(\w+)\}", CATALOG[lang][key])) == english, (lang, key)


@pytest.mark.parametrize("key", BATCH_KEYS)
def test_no_batch_value_carries_another_locale_script(key: str) -> None:
    """A Persian value under the English key renders Persian to every English user."""
    for lang in ("en", "es", "fr", "it", "pt"):
        assert not re.search(r"[\u0600-\u06ff]", CATALOG[lang][key]), (lang, key)
    for lang in ("zh",):
        assert not re.search(r"[\u0400-\u04ff]", CATALOG[lang][key]), (lang, key)
    for table in CATALOG.values():
        assert "\ufffd" not in table[key], key


# --------------------------------------------------------------------------- #
# the main menu: plan, remaining time, and the missing-name fallbacks
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_free_plan_line_is_localized() -> None:
    ui = _main_ui()
    fa = await ui._main_text(111, user=_user("fa"))
    assert CATALOG["fa"]["ui_plan_free"] in fa
    assert CATALOG["fa"]["ui_plan_status_none"] in fa
    for residue in ("Free", "0 days", "Active", "Valued User"):
        assert residue not in fa, f"{residue!r} leaked into the fa plan block"

    en = await ui._main_text(111, user=_user("en"))
    assert CATALOG["en"]["ui_plan_free"] in en
    assert CATALOG["en"]["ui_plan_status_none"] in en


@pytest.mark.asyncio
async def test_the_pro_plan_line_is_localized() -> None:
    ui = _main_ui(rules=[_rule()])
    fa = await ui._main_text(111, user=_user("fa"))
    assert CATALOG["fa"]["ui_plan_status_active"] in fa
    assert CATALOG["fa"]["ui_plan_status_none"] not in fa

    en = await ui._main_text(111, user=_user("en"))
    assert CATALOG["en"]["ui_plan_pro"] in en
    assert CATALOG["en"]["ui_plan_status_active"] in en


@pytest.mark.asyncio
async def test_the_placeholder_fields_of_the_main_menu_are_localized() -> None:
    """'None' for the active model and the phone number was an English literal."""
    ui = _main_ui()
    fa = await ui._main_text(111, user=_user("fa"))
    assert CATALOG["fa"]["ui_none_label"] in fa
    assert CATALOG["fa"]["ui_none_label"] != CATALOG["en"]["ui_none_label"]


@pytest.mark.asyncio
async def test_an_unnamed_user_is_greeted_in_the_language_they_chose() -> None:
    ui = _main_ui()
    nameless = _user("fa", "")
    nameless.first_name = ""
    nameless.last_name = ""
    nameless.username = None

    fa = await ui._main_text(111, user=nameless)
    assert CATALOG["fa"]["ui_user_fallback_id"].format(uid=111) in fa
    assert "User 111" not in fa and "Valued User" not in fa

    # a second tenant with an english client, whose state must stay its own
    en_user = _user("en", "")
    en_user.first_name = ""
    en_user.last_name = ""
    en_user.username = None
    en = await ui._main_text(222, user=en_user)
    assert CATALOG["en"]["ui_user_fallback_id"].format(uid=222) in en


def test_the_onboarding_greeting_uses_the_localized_fallback_name() -> None:
    ui = _fa_ui()
    nameless = _user("fa", "")
    nameless.first_name = ""
    nameless.last_name = ""
    nameless.username = None

    text, _kbd = ui._render_welcome_onboarding(5094837833, nameless)
    assert CATALOG["fa"]["ui_user_fallback_id"].format(uid=5094837833) in text
    assert "Valued User" not in text and "User 5094837833" not in text


def test_a_user_id_of_zero_falls_back_to_the_localized_generic_name() -> None:
    ui = _fa_ui()
    assert ui._fallback_display_name(0, "fa") == CATALOG["fa"]["ui_user_fallback_name"]
    assert ui._fallback_display_name(0, "en") == CATALOG["en"]["ui_user_fallback_name"]


# --------------------------------------------------------------------------- #
# rule rows, cards and the advanced panel
# --------------------------------------------------------------------------- #

def test_the_rule_row_meta_line_is_localized() -> None:
    ui = _fa_ui()
    text, _kbd = ui._render_rules_list([_rule()], page=0, page_size=5)
    assert CATALOG["fa"]["ui_rules_row_meta"].format(mode="COPY_MESSAGE", delay="0") in text
    assert "mode=" not in text, "the fa rule row still reads the English meta line"


def test_the_card_falls_back_to_localized_chat_labels() -> None:
    """A rule with neither a chat name nor a chat id is the only case the
    fallback is reachable — the literal it used to hold ("AF Route Lab Source")
    is what a tenant read in all ten locales."""
    ui = _fa_ui()
    text, _kbd = ui._render_rule_detail(
        _rule(source_chat_id="", target_chat_id="", source_chat_name="", target_chat_name="")
    )
    assert CATALOG["fa"]["ui_rule_source_fallback"] in text
    assert CATALOG["fa"]["ui_rule_target_fallback"] in text
    assert "AF Route Lab" not in text and "Source chat" not in text


def test_a_named_chat_is_never_replaced_by_the_fallback() -> None:
    ui = _fa_ui()
    text, _kbd = ui._render_rule_detail(
        _rule(source_chat_name="News Channel", target_chat_name="Target Group")
    )
    assert "News Channel" in text and "Target Group" in text
    assert CATALOG["fa"]["ui_rule_source_fallback"] not in text


def test_the_advanced_panel_localizes_chat_labels_and_second_values() -> None:
    ui = _fa_ui()
    text, _kbd = ui._render_advanced_settings(
        _rule(source_chat_id="", target_chat_id="", source_chat_name="", target_chat_name="")
    )
    assert CATALOG["fa"]["ui_rule_source_fallback"] in text
    assert CATALOG["fa"]["ui_secs_value"].format(sec="0") in text
    assert "0 s" not in text and " s" not in text


# --------------------------------------------------------------------------- #
# the source no longer carries the literals
# --------------------------------------------------------------------------- #

def test_the_retired_literals_are_gone_from_the_ui_source() -> None:
    literals = _string_literals()
    for retired in RETIRED_LITERALS:
        offenders = [value for value in literals if retired in value]
        assert not offenders, f"{retired!r} is a hardcoded visible string again: {offenders[:2]}"


def test_the_ui_source_no_longer_builds_a_copy_name_from_a_literal() -> None:
    source = UI_SOURCE.read_text(encoding="utf-8")
    assert '(Copy)"' not in source
    assert "ui_rule_copy_of" in source and "ui_rule_new_copy" in source


def test_the_source_no_longer_inlines_an_english_plan_word() -> None:
    literals = set(_string_literals())
    for word in ("VIP Pro", "Free", "0 days", "Active"):
        assert word not in literals, f"{word!r} is inlined into a screen again"
