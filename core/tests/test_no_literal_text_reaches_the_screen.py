"""Nothing says anything to a user without going through the catalog.

Eleven batches moved the bot's text into `locales/translations.json`, one leak
class at a time: screen titles, buttons, prompts, alerts, error bodies, values
interpolated into templates, setting names and descriptions, taxonomy slugs,
choice words, refusal reasons, condition tokens, routing reasons. Each batch was
found by hand, and the last one is always the one nobody looked for.

This is the guard that does not expire: an AST scan of every module in this
package for the calls that put text in front of a user — `reply_text`,
`edit_text`, `send_message`, `answer` (the callback toast) and the photo/video/
document variants, plus `_safe_edit` / `_safe_reply` — and a requirement that
the text argument is built from the catalog (`self._t(...)`, `self.t(...)`,
`language_label(...)`, `reason_label(...)`) and not from literals composed at the
call site.

It is deliberately narrow: it reads the *first argument of those calls*, follows
f-strings one level into their interpolations, and stops at any nested catalog
call (the keys and placeholders inside `self._t("ui_x", name=...)` are that
call's business, not the argument's). A literal that reaches a screen any other
way — an inline keyboard label, a dictionary of statuses — is covered by the
existing per-class tests (`test_ui_values_come_from_the_catalog`,
`test_every_placeholder_reaches_its_template`, the cross-language audit); this
one covers the sentences, which is where a leak is most visible to a user.
"""
from __future__ import annotations

import ast
import pathlib
import re

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "infrastructure" / "telegram"

#: Calls whose first argument is text a user reads.
SINKS = {
    "reply_text", "edit_text", "send_message", "edit_message_text",
    "reply_photo", "reply_video", "reply_document", "send_photo",
    "send_video", "send_document", "answer", "reply",
    "_safe_edit", "_safe_reply", "_safe_send",
}

#: Calls that resolve text themselves: whatever they are handed is their own
#: key or placeholder, not the screen's copy.
CATALOG_CALLS = {
    "_t", "t", "tf", "translate", "fill", "translate_text", "language_label",
    "reason_label", "_logic_word", "_choice_label", "_toggle_toast", "_toast",
    "_say",
}
#: Methods that operate on already-resolved text, so their arguments are not
#: copy either (`self._t("ui_x").replace("{wait}", str(wait))`).
PASS_THROUGH = {"format", "replace", "join", "strip", "title"}

#: Text has to contain a letter to be a sentence; a separator is not a leak.
HAS_LETTER = re.compile(r"[A-Za-z]")
ONLY_PUNCTUATION = re.compile(r"^[\W\d_]+$")


def literal_parts(node: ast.AST) -> list:
    """The pieces of text a call site composes itself."""
    out = []

    def walk(current: ast.AST) -> None:
        if isinstance(current, ast.Call):
            name = getattr(current.func, "attr", "") or getattr(current.func, "id", "")
            if name in CATALOG_CALLS or name in PASS_THROUGH or name.endswith("_toast"):
                return
        if isinstance(current, ast.Constant) and isinstance(current.value, str):
            out.append(current.value)
            return
        if isinstance(current, ast.JoinedStr):
            for piece in current.values:
                if isinstance(piece, ast.Constant):
                    if isinstance(piece.value, str):
                        out.append(piece.value)
                else:  # a FormattedValue: look inside the interpolation
                    walk(piece)
            return
        for child in ast.iter_child_nodes(current):
            walk(child)

    walk(node)
    return out


def sinks_in(source: str) -> list:
    """Every text argument in `source` that is not built from the catalog."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        if name not in SINKS:
            continue
        first = node.args[0]
        if isinstance(first, ast.Call):
            inner = getattr(first.func, "attr", "") or getattr(first.func, "id", "")
            if inner in CATALOG_CALLS:
                continue
        if isinstance(first, ast.Name) and first.id.startswith("_"):
            continue
        for piece in literal_parts(first):
            if not HAS_LETTER.search(piece) or ONLY_PUNCTUATION.match(piece):
                continue
            hits.append((node.lineno, name, piece.strip()[:80]))
    return hits


def modules() -> list:
    return sorted(
        path for path in PACKAGE.rglob("*.py") if not path.name.startswith("test_")
    )


# --------------------------------------------------------------------------- #
# The scan itself has to work
# --------------------------------------------------------------------------- #


def test_the_scan_visits_the_package():
    found = modules()
    assert len(found) > 10, f"only {len(found)} module(s) scanned: {found}"


def test_the_scan_finds_the_sinks_it_is_looking_for():
    """A scan that matched no call would pass everything below."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in modules())
    found = len(re.findall(r"\.(reply_text|answer|edit_text|send_message)\(", source))
    assert found > 100, f"only {found} screen calls found in the package"


def test_a_literal_sentence_is_caught():
    source = 'await message.reply_text("Forwarding is now paused for this task")'
    hits = sinks_in(source)
    assert hits and "paused" in hits[0][2], hits


def test_a_literal_inside_an_f_string_is_caught():
    source = 'await message.reply_text(f"Task {rule.id[:8]} saved with no header")'
    hits = sinks_in(source)
    assert hits, "a literal f-string sentence was not caught"
    assert any("no header" in text for _, _, text in hits), hits


def test_a_literals_behind_the_catalog_is_not_caught():
    source = (
        'await message.reply_text(self._t("ui_saved", task=rule.id))\n'
        'await cq.answer(self._t("ui_toggle_on") if on else self._t("ui_toggle_off"))\n'
        'await message.reply_text(self._t("ui_wait").replace("{wait}", str(5)))\n'
    )
    assert sinks_in(source) == []


def test_a_placeholder_or_separator_is_not_caught():
    assert sinks_in('await message.reply_text("──────")') == []
    assert sinks_in('await message.reply_text(" " + "➔")') == []


# --------------------------------------------------------------------------- #
# And the package is clean
# --------------------------------------------------------------------------- #


def test_no_module_says_anything_without_the_catalog():
    offenders = []
    for path in modules():
        for line, call, text in sinks_in(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(PACKAGE.parent.parent)}:{line} {call}({text!r})")
    assert offenders == [], (
        "text reaching a user without the catalog — move it to "
        "locales/translations.json and pass it through `_t`:\n" + "\n".join(offenders)
    )
