"""No user-visible label may be a catalog key.

`I18n.t` answers an unknown key with the key itself. That is the right answer for
a log line and the wrong one for a screen: it is how a user came to read
`ui_login_fail_no_pending_login` in the login flow, and how they would read
`ui_reg_group_media2` if the registry grew a group nobody translated, or
`ui_ai_policy_short_something` if a task's UI draft held a policy value the
catalog does not know.

Every key a screen builds from a variable has to be checked against the catalog
before it is rendered, and every key a screen names literally has to exist. These
tests hold both ends: the catalog is swept against the literals in the source,
and the AST rule catches a hand-built key that nothing checks.
"""

import ast
import inspect
import json
import pathlib
import re
from typing import Callable, Dict
from unittest.mock import MagicMock

import pytest

from core.domain.value_objects import AIFallbackPolicy
from core.infrastructure.telegram import registry_editor
from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES
from core.infrastructure.telegram.pro_bot_ui import ProBotUI
from core.infrastructure.telegram.registry_editor import (
    GROUP_ORDER,
    SETTING_SPECS,
    RegistryEditor,
)

CORE = pathlib.Path("core")
#: functions that answer "is this key in the catalog?" — the only acceptable
#: guard before rendering a key that was built rather than named.
GUARD_CALLS = {"contains", "_has"}


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


# --------------------------------------------------------------------------- #
# the catalog is 1:1 and complete for the keys the registry and UI can build
# --------------------------------------------------------------------------- #


def test_all_ten_catalogs_have_exactly_the_same_keys():
    catalog = json.loads(
        (CORE / "infrastructure/telegram/locales/translations.json").read_text(encoding="utf-8")
    )
    assert set(catalog) == set(SUPPORTED_LANGUAGES)
    assert len(catalog) == 10
    english = set(catalog["en"])
    assert english, "the English catalog is empty"
    for lang, table in catalog.items():
        assert set(table) == english, (
            lang,
            sorted(english - set(table))[:5],
            sorted(set(table) - english)[:5],
        )


@pytest.mark.parametrize("policy", [p.value.lower() for p in AIFallbackPolicy])
def test_every_ai_fallback_policy_has_a_label_in_every_language(policy):
    catalog = I18n()._catalog
    key = f"ui_ai_policy_short_{policy}"
    for lang in SUPPORTED_LANGUAGES:
        assert key in catalog[lang], f"{key} missing from {lang}"


def test_every_registry_group_has_a_title_in_every_language():
    catalog = I18n()._catalog
    groups = {spec.group for spec in SETTING_SPECS.values()} | set(GROUP_ORDER)
    assert groups, "the registry defines no groups"
    for group in sorted(groups):
        key = f"ui_reg_group_{group}"
        for lang in SUPPORTED_LANGUAGES:
            assert key in catalog[lang], f"{key} missing from {lang}"


# --------------------------------------------------------------------------- #
# a key built from a variable is guarded
# --------------------------------------------------------------------------- #


def test_an_unknown_ai_policy_renders_no_label_and_is_logged(caplog):
    ui = _build_ui()
    with caplog.at_level("DEBUG"):
        assert ui._ai_policy_text("DROP") == ui._t("ui_ai_policy_short_drop")
        assert ui._ai_policy_text("something_new") == ""
    assert not any("ui_ai_policy_short" in rec.getMessage() for rec in caplog.records)
    assert any(
        rec.levelname == "ERROR" and "something_new" in rec.getMessage()
        for rec in caplog.records
    )


def test_an_unknown_registry_group_renders_a_slug_not_a_key(caplog):
    editor = RegistryEditor(translate=I18n().t, save=lambda r: None)
    with caplog.at_level("DEBUG"):
        assert editor._group_label("media") == I18n().t("ui_reg_group_media")
        fallback = editor._group_label("media_two")
    assert fallback == "Media Two"
    assert "ui_reg_group" not in fallback
    assert any(
        rec.levelname == "ERROR" and "ui_reg_group_media_two" in rec.getMessage()
        for rec in caplog.records
    )


def test_the_group_label_detects_a_missing_key_without_a_contains_callback():
    """The fallback guard: `t` echoes the key, and that is the signal."""
    editor = RegistryEditor(translate=lambda key, **kw: key, save=lambda r: None)
    assert editor._group_label("media") == "Media"


# --------------------------------------------------------------------------- #
# the source-level rules
# --------------------------------------------------------------------------- #


def _translation_literals() -> Dict[str, str]:
    """Every ``_t("literal")`` in the package, mapped to where it was found."""
    found: Dict[str, str] = {}
    pattern = re.compile(r'_t\(\s*"([a-zA-Z0-9_]+)"')
    for path in sorted(CORE.rglob("*.py")):
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            line = source[: match.start()].count("\n") + 1
            found.setdefault(match.group(1), f"{path}:{line}")
    return found


def test_every_literal_key_in_the_source_exists_in_the_catalog():
    """A literal key is a typo away from rendering itself; check them all."""
    catalog = I18n()._catalog["en"]
    missing = {key: where for key, where in _translation_literals().items() if key not in catalog}
    assert not missing, missing


def test_the_sweep_actually_looked_at_keys():
    """A guard against the sweep silently matching nothing."""
    literals = _translation_literals()
    assert len(literals) > 200, len(literals)


def test_interpolated_keys_are_only_built_where_the_catalog_is_checked():
    """The AST rule, applied to the whole package, not just the login screens.

    A function that builds a translation key from a value must be able to ask
    whether the key exists; one that cannot is one raw-key leak away from the
    screen it renders.
    """
    unguarded = []
    for path in sorted(CORE.rglob("*.py")):
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "_t(f" not in source and "_t(\n" not in source:
            continue
        tree = ast.parse(source)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            builds = [
                node
                for node in ast.walk(func)
                if isinstance(node, ast.JoinedStr)
                and any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and re.match(r"^ui_[a-z_]*$", part.value)
                    for part in node.values
                )
            ]
            if not builds:
                continue
            guarded = any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in GUARD_CALLS
                for call in ast.walk(func)
            )
            if not guarded:
                unguarded.append(f"{path}:{func.name}")
    assert not unguarded, unguarded


def test_the_ast_rule_would_catch_a_hand_built_key(tmp_path):
    """Negative control for the rule itself: it must flag an unguarded builder."""
    bad = CORE / "infrastructure/telegram/_ast_rule_control.py"
    bad.write_text(
        "def render(self, code):\n"
        '    return self._t(f"ui_login_fail_{code}")\n',
        encoding="utf-8",
    )
    try:
        tree = ast.parse(bad.read_text(encoding="utf-8"))
        flagged = []
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            builds = any(
                isinstance(node, ast.JoinedStr)
                and any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and re.match(r"^ui_[a-z_]*$", part.value)
                    for part in node.values
                )
                for node in ast.walk(func)
            )
            if builds:
                flagged.append(func.name)
        assert flagged == ["render"]
    finally:
        bad.unlink()


def test_the_guarded_helpers_are_the_only_builders():
    """Named, so deleting a guard shows up as a failing expectation."""
    builders = []
    for module, names in (
        (inspect.getmodule(ProBotUI), ["_login_fail_text", "_ai_policy_text"]),
        (registry_editor, ["_group_label"]),
    ):
        for name in names:
            source = inspect.getsource(getattr(_class_of(module, name), name))
            assert 'f"ui_' in source, f"{name} no longer builds a key - rule may be stale"
            assert "_t(" in source, f"{name} no longer renders a key"
            builders.append(name)
    assert sorted(builders) == ["_ai_policy_text", "_group_label", "_login_fail_text"]


def _class_of(module, method: str) -> Callable:
    for value in vars(module).values():
        if inspect.isclass(value) and hasattr(value, method):
            return value
    raise AssertionError(f"no class in {module} defines {method}")
