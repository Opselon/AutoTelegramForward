"""What a user is shown when a login fails.

The login failure path builds its message key from a value the user cannot see:

    self._t(f"ui_login_fail_{key}", error=detail)

Three things were wrong with it, all of them user-visible:

  * five of the codes `LoginFlowManager` returns had no catalog entry, and
    `I18n.t` answers an unknown key with the key itself — so the chat showed
    the literal string `ui_login_fail_no_pending_login`;
  * `detail` carries internal text (`send_failed:TimeoutError: ...`), and the
    generic key interpolated it straight into the message; and
  * `ui_login_fail_send_failed`, `ui_login_code_error`,
    `ui_login_code_verify_error` and `ui_login_2fa_verify_error` interpolated
    `{error}`/`{result}`/`{exc}` — the last one asked for `{err}`, which no
    caller supplies, so the user saw the placeholder `{err}` itself.

The guard is `I18n.contains`: the caller asks whether the key exists and falls
back to the generic line, so a code nobody has written a message for can never
reach the chat as a raw key. These tests pin that, the completeness of the
catalog against the codes the code actually returns, and the "no Persian in a
non-Persian locale" invariant that the wrong-language values violated.
"""

import ast
import inspect
import re
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.infrastructure.telegram import login_flow
from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

CATALOG = Path("core/infrastructure/telegram/locales/translations.json")

#: results that mean the step worked; they are not failures and need no message
SUCCESS_CODES = {"send_code", "send_password", "login_success"}

#: Persian-only graphemes. Used to catch a value written in the wrong language
#: (six locales shipped Persian text for four login keys before this).
PERSIAN_GRAPHEMES = set("یگچپژکیهؤئإأآۀٔءيھ") | set("۰۱۲۳۴۵۶۷۸۹")

#: keys whose whole job is to tell the user what went wrong, so they must be a
#: complete sentence: a placeholder here is either internal detail or a bug.
LOGIN_FAILURE_KEYS = re.compile(r"^ui_login_(fail|code_error|code_verify_error|2fa_verify_error)")


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


def failure_codes_returned_by_login_flow() -> set:
    """Every failure code in the module, read from the source.

    Read from the source on purpose: a new `return "some_new_code"` is exactly
    the change that reintroduces the raw-key leak, and it has to fail here.
    """
    source = inspect.getsource(login_flow)
    codes = set(re.findall(r'return "([a-z_]+)"', source))
    codes |= set(re.findall(r'return f"([a-z_]+):', source))
    return {c for c in codes if c not in SUCCESS_CODES}


# --------------------------------------------------------------------------- #
# the catalog covers every failure the user can hit
# --------------------------------------------------------------------------- #


def test_every_login_failure_code_has_a_message_in_all_ten_languages():
    i18n = I18n()
    codes = failure_codes_returned_by_login_flow()
    assert codes, "no codes found - the extraction regex has drifted"
    for code in sorted(codes):
        key = f"ui_login_fail_{code}"
        for lang in SUPPORTED_LANGUAGES:
            assert key in i18n._catalog[lang], f"{key} missing from {lang}"


def test_the_codes_that_used_to_render_as_raw_keys_are_covered():
    """The five the audit found, named so a regression is unmistakable."""
    i18n = I18n()
    for code in (
        "no_pending_login",
        "auth_expired",
        "invalid_code",
        "too_many_attempts",
        "invalid_password",
    ):
        key = f"ui_login_fail_{code}"
        assert key in i18n._catalog["en"], f"{key} is missing again"


#: only cooldown and flood_wait may interpolate — they show the seconds the
#: user has to wait; every other line is a complete sentence.
LOGIN_KEYS_ALLOWED_TO_INTERPOLATE = ("ui_login_fail_cooldown", "ui_login_fail_flood_wait")


def test_no_login_failure_message_contains_a_placeholder():
    """A failure line is a sentence, not a template: nothing to interpolate."""
    i18n = I18n()
    offenders = []
    for lang, table in i18n._catalog.items():
        for key, value in table.items():
            if not LOGIN_FAILURE_KEYS.match(key):
                continue
            if key in LOGIN_KEYS_ALLOWED_TO_INTERPOLATE:
                assert "{error}" in value, f"{lang}:{key} must show the seconds: {value!r}"
                assert set(re.findall(r"{(\w+)}", value)) == {"error"}, f"{lang}:{key}"
                continue
            if "{" in value:
                offenders.append(f"{lang}:{key}={value!r}")
    assert not offenders, offenders


def test_the_cooldown_seconds_still_reach_the_user():
    """`cooldown:30` shows the user the number — guarded from both directions."""
    ui = _build_ui()
    text = ui._login_fail_text("cooldown", "30")
    assert "30" in text and "{error}" not in text


def test_no_non_persian_locale_carries_persian_text():
    """The invariant the wrong-language values broke, checked catalog-wide.

    Six locales shipped Persian for `ui_login_fail_invalid_phone`,
    `ui_login_fail_cooldown`, `ui_login_fail_flood_wait` and
    `ui_login_fail_send_failed`; a user reading Spanish got Persian.
    """
    i18n = I18n()
    offenders = []
    for lang, table in i18n._catalog.items():
        if lang == "fa":
            continue
        for key, value in table.items():
            if any(ch in PERSIAN_GRAPHEMES for ch in value):
                offenders.append(f"{lang}:{key}={value[:40]!r}")
    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# the resolver: a missing message is a generic message, never a raw key
# --------------------------------------------------------------------------- #


def test_an_unknown_code_falls_back_to_the_generic_line():
    ui = _build_ui()
    text = ui._login_fail_text("code_nobody_wrote_a_message_for", "TimeoutError: boom")
    assert text == ui._t("ui_login_fail")
    assert "ui_login_fail" not in text
    assert "code_nobody_wrote_a_message_for" not in text
    assert "TimeoutError" not in text and "boom" not in text


def test_a_known_code_uses_its_own_message():
    ui = _build_ui()
    for code in ("invalid_phone", "no_pending_login", "too_many_attempts"):
        assert ui._login_fail_text(code) == ui._t(f"ui_login_fail_{code}")
        assert ui._login_fail_text(code) != ui._t("ui_login_fail")


def test_the_internal_detail_is_never_shown(caplog):
    """`send_failed:TimeoutError: ...` is for the operator, not the user."""
    ui = _build_ui()
    with caplog.at_level("DEBUG"):
        known = ui._login_fail_text("send_failed", "TimeoutError: connection reset")
        unknown = ui._login_fail_text("brand_new_code", "TimeoutError: connection reset")
    for text in (known, unknown):
        assert "TimeoutError" not in text and "connection reset" not in text
        assert "send_failed" not in text and "brand_new_code" not in text


def test_the_detail_is_logged_when_the_message_falls_back(caplog):
    """A code with no message still has to be diagnosable from the log."""
    ui = _build_ui()
    with caplog.at_level("DEBUG"):
        ui._login_fail_text("brand_new_code", "TimeoutError: connection reset")
    assert any("brand_new_code" in rec.getMessage() for rec in caplog.records)
    assert any("TimeoutError" in rec.getMessage() for rec in caplog.records)


def test_an_unknown_code_is_reported_in_the_log(caplog):
    ui = _build_ui()
    with caplog.at_level("DEBUG"):
        ui._login_fail_text("brand_new_code", "detail")
    assert any(
        rec.levelname == "ERROR" and "brand_new_code" in rec.getMessage()
        for rec in caplog.records
    )


def test_the_message_follows_the_users_language():
    ui = _build_ui()
    assert ui._login_fail_text("invalid_phone", lang="es") == ui._t(
        "ui_login_fail_invalid_phone", lang="es"
    )
    assert ui._login_fail_text("nope", lang="es") == ui._t("ui_login_fail", lang="es")
    assert ui._t("ui_login_fail_invalid_phone", lang="fa") != ui._t(
        "ui_login_fail_invalid_phone", lang="en"
    )


# --------------------------------------------------------------------------- #
# the call sites cannot go back to building keys by hand
# --------------------------------------------------------------------------- #


def test_a_hand_built_login_key_is_only_ever_guarded_by_a_catalog_check():
    """Building `ui_login_fail_<code>` is allowed in exactly one shape.

    A call site that builds the key and renders it is how the raw-key leak
    happened; the resolver is allowed to build it only because it asks the
    catalog first (`contains`) and falls back to the generic line. So: any
    function that interpolates the key must also consult `contains`, and the
    login screens must route through the resolver.
    """
    source = inspect.getsource(ProBotUI)
    tree = ast.parse(textwrap.dedent(source))

    builders, unguarded = [], []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        builds = any(
            isinstance(node, ast.JoinedStr)
            and any(
                isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                and "ui_login_fail_" in part.value
                for part in node.values
            )
            for node in ast.walk(func)
        )
        if not builds:
            continue
        builders.append(func.name)
        guarded = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "contains"
            for call in ast.walk(func)
        )
        if not guarded:
            unguarded.append(func.name)

    assert unguarded == [], f"a login key is built and rendered without a catalog check: {unguarded}"
    assert builders == ["_login_fail_text"], builders
    assert "_login_fail_text(" in source


@pytest.mark.parametrize(
    "key",
    [
        "ui_login_fail",
        "ui_login_send_fail",
        "ui_login_fail_send_failed",
        "ui_login_code_error",
        "ui_login_code_verify_error",
        "ui_login_2fa_verify_error",
    ],
)
def test_the_reworded_keys_are_placeholder_free_in_every_language(key):
    catalog = I18n()._catalog
    for lang, table in catalog.items():
        assert key in table, f"{key} missing from {lang}"
        assert "{" not in table[key], f"{lang}:{key} still interpolates something"
        assert table[key].strip(), f"{lang}:{key} is empty"
