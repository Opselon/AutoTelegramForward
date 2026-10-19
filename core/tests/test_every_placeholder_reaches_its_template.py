"""A value a screen formats has to arrive in the template that shows it.

`I18n.t()` is deliberately forgiving: a template it cannot format is returned
as-is, because a half-rendered screen beats an exception in a user's chat. The
cost is that a call site which forgets a placeholder is *silent* — the user
reads `Status in draft: {stat}` or `🌐 Translation: {language}` and nothing in
the logs says why. The key-parity audits cannot see it either: the catalog is
correct, and the leak is in the call.

So the call is what these tests check, statically and at runtime:

  * every `_t(...)`/`_btn(...)`/`t(...)` call in the bot package names a key the
    catalog actually defines (a missing key renders as the raw key — the leak
    that made the `/help` fallback reply `help_text`);
  * every `{placeholder}` in that key's English template is either passed as a
    keyword argument or filled by an explicit `.replace("{name}", …)` on the
    rendered string — the deliberate pattern used where the value may itself
    contain braces;
  * `{name:spec}` is a placeholder too, so a format spec is not mistaken for a
    missing one;
  * at runtime `t()` now *says* what it could not format instead of returning
    braces quietly, while still never raising into a user's render.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

from core.infrastructure.telegram.i18n import PLACEHOLDER, I18n

REPO = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = REPO / "core" / "infrastructure" / "telegram"
CATALOG = json.loads((PACKAGE / "locales" / "translations.json").read_text(encoding="utf-8"))
EN = CATALOG["en"]

#: The call shapes that render catalog copy.
CALLS = {
    "_t", "_btn", "t", "tf", "translate",
    "_toggle_toast", "_toggle_count_toast", "severity_count_line",
}
#: Arguments that carry context rather than a value a template shows.
NOT_A_VALUE = {"lang", "style"}
SKIP_DIRS = {"tests", "locales", "__pycache__"}

FILLED_BY_REPLACE = re.compile(r"""\.replace\(\s*["']\{([a-zA-Z_][a-zA-Z0-9_]*)\}["']""")


def sources():
    for path in sorted(PACKAGE.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path, path.read_text(encoding="utf-8")


def call_name(node: ast.Call):
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def literal_keys(node: ast.Call):
    """Every catalog key the call names in so many words.

    A ternary names two (`_t("ui_ai_enabled" if on else "ui_ai_disabled")`), and
    both are keys that have to exist. Anything else — a variable, a
    concatenation, an f-string — is built at runtime and is not claimed here.
    """
    if not node.args:
        return []
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, ast.IfExp):
        return [b.value for b in (arg.body, arg.orelse)
                if isinstance(b, ast.Constant) and isinstance(b.value, str)]
    return []


def _scan():
    """Every literal-key render call: its key, its keywords, what `.replace` fills."""
    seen = []
    for path, text in sources():
        tree = ast.parse(text)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or call_name(node) not in CALLS:
                continue
            keys = literal_keys(node)
            if not keys:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg} - NOT_A_VALUE
            open_kwargs = any(kw.arg is None for kw in node.keywords) or len(node.args) > 1
            statement = parents.get(node)
            while statement is not None and not isinstance(statement, ast.stmt):
                statement = parents.get(statement)
            src = ast.get_source_segment(text, statement) if statement is not None else ""
            for key in keys:
                seen.append({
                    "path": path.name,
                    "line": node.lineno,
                    "key": key,
                    "kwargs": kwargs,
                    "open": open_kwargs,
                    "replaced": set(FILLED_BY_REPLACE.findall(src or "")),
                })
    return seen


CALLS_SEEN = _scan()


# --------------------------------------------------------------------------- #
# the scanner itself
# --------------------------------------------------------------------------- #


def test_the_scan_reaches_the_call_sites_it_guards():
    """A scanner that finds nothing would pass everything."""
    assert len(CALLS_SEEN) > 400, f"only {len(CALLS_SEEN)} render calls found"
    assert len({c["key"] for c in CALLS_SEEN}) > 300


def test_a_format_spec_is_a_placeholder():
    assert PLACEHOLDER.findall("{delay:.0f} seconds") == ["delay"]
    assert PLACEHOLDER.findall("`{min:.1f}` to `{max:.1f}`") == ["min", "max"]
    assert PLACEHOLDER.findall("{{literal}} and {real}") == ["real"]


def test_the_scanner_reads_both_arms_of_a_ternary():
    """`_t("on" if x else "off")` names two keys, and both must exist."""
    src = 'self._t("ui_ai_enabled" if on else "ui_ai_disabled")'
    call = ast.parse(src).body[0].value
    assert literal_keys(call) == ["ui_ai_enabled", "ui_ai_disabled"]
    assert literal_keys(ast.parse('self._t(key)').body[0].value) == []


def test_the_scanner_understands_the_replace_pattern():
    """`.replace("{cur}", …)` fills a placeholder without a keyword argument."""
    assert FILLED_BY_REPLACE.findall('self._t("k").replace("{cur}", value)') == ["cur"]


# --------------------------------------------------------------------------- #
# every key exists, every placeholder arrives
# --------------------------------------------------------------------------- #


def test_every_render_call_names_a_catalog_key():
    """A key no catalog defines renders as the key itself."""
    unknown = sorted({(c["path"], c["key"]) for c in CALLS_SEEN if c["key"] not in EN})
    assert not unknown, f"keys that would render as raw text: {unknown}"


def test_every_placeholder_in_every_render_call_is_filled():
    unfilled = []
    for call in CALLS_SEEN:
        if call["open"]:
            continue
        needed = set(PLACEHOLDER.findall(EN.get(call["key"], "")))
        missing = needed - call["kwargs"] - call["replaced"]
        if missing:
            unfilled.append(f"{call['path']}:{call['line']} {call['key']} -> {sorted(missing)}")
    assert not unfilled, "placeholders printed to the user: " + "; ".join(unfilled)


def test_the_help_fallback_does_not_reply_with_a_key_name():
    """`/help` when the panel cannot render must send the help body, not `help_text`.

    `help_text` was never a catalog key, so that path replied with the literal
    string "help_text" to anyone whose panel failed to render.
    """
    assert not [c for c in CALLS_SEEN if c["key"] == "help_text"]
    call = [c for c in CALLS_SEEN if c["path"] == "bot_manager.py" and c["key"] == "help"]
    assert len(call) >= 2, "both /help and its fallback render the same body"


def test_the_draft_status_toast_fills_the_template():
    """The template declares `{stat}`; the call passed `status=` and printed it."""
    call = [c for c in CALLS_SEEN if c["key"] == "ui_draft_status_alert"]
    assert len(call) == 1
    assert call[0]["kwargs"] == {"stat"}, call[0]


# --------------------------------------------------------------------------- #
# runtime: it says what it could not format, and still never raises
# --------------------------------------------------------------------------- #


def test_a_missing_placeholder_is_reported_not_silent(caplog):
    i18n = I18n()
    with caplog.at_level("ERROR"):
        rendered = i18n.t("ui_draft_status_alert", lang="en", status="x")
    assert rendered == EN["ui_draft_status_alert"], "the forgiving fallback is unchanged"
    assert "ui_draft_status_alert" in caplog.text
    assert "stat" in caplog.text, "the log must name the placeholder nobody filled"


def test_a_key_no_catalog_defines_is_reported(caplog):
    i18n = I18n()
    with caplog.at_level("ERROR"):
        rendered = i18n.t("definitely_missing_key_xyz", lang="en")
    assert rendered == "definitely_missing_key_xyz", "behaviour is unchanged"
    assert "definitely_missing_key_xyz" in caplog.text


def test_a_filled_template_logs_nothing(caplog):
    i18n = I18n()
    with caplog.at_level("ERROR"):
        rendered = i18n.t("ui_draft_status_alert", lang="en", stat="Active")
    assert rendered == EN["ui_draft_status_alert"].format(stat="Active")
    assert not caplog.records


def test_the_template_without_kwargs_is_used_where_the_caller_fills_it(caplog):
    """The `.replace` pattern is intentional: no kwargs, no complaint."""
    i18n = I18n()
    with caplog.at_level("ERROR"):
        template = i18n.t("ui_weburl_saved", lang="en")
    assert "{url}" in template
    assert not caplog.records


@pytest.mark.parametrize("lang", ["en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it"])
def test_every_locale_formats_the_filled_template(lang):
    i18n = I18n()
    rendered = i18n.t("ui_draft_status_alert", lang=lang, stat="Active")
    assert "{" not in rendered and "}" not in rendered, rendered
