#!/usr/bin/env python3
"""Fail-closed guard for new hardcoded Telegram UI text.

Audits changed Python UI source against the PR base, rather than a fragile
file:line allowlist. It covers direct strings passed to Telegram render/send
APIs, inline/reply keyboard labels, callback alerts, and button builders.
Existing debt is reported separately by audit_cross_language.py; every newly
added visible literal must use the translation catalog.
"""
from __future__ import annotations

import ast
import os
import re
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_ROOTS = (
    "core/infrastructure/telegram/",
    "core/presentation/telegram/",
    "core/application/telegram/",
)
VISIBLE_CALLS = {
    "answer", "reply", "reply_text", "reply_photo", "reply_document",
    "send_message", "send_photo", "send_document", "edit_text",
    "edit_message_text", "edit_message_caption", "safe_edit",
    "InlineKeyboardButton", "KeyboardButton", "ReplyKeyboardMarkup",
}
VISIBLE_KEYWORDS = {
    "text", "caption", "label", "title", "message", "placeholder",
    "reply_markup",
}
I18N_MARKERS = ("_t(", ".t(", "i18n.", "translate(", "gettext(")
TECHNICAL = re.compile(
    r"^(?:https?://|@[A-Za-z0-9_]{3,}|[A-Z][A-Z0-9_]{2,}|[a-z0-9_]+|\d+(?:\.\d+)?|\s*|[\W_]+)$"
)
RTL = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
FAILURES: list[str] = []


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_translation_expression(source: str, node: ast.AST) -> bool:
    segment = ast.get_source_segment(source, node) or ""
    return any(marker in segment for marker in I18N_MARKERS)


def _human_literal(value: str) -> bool:
    """Identify likely user-facing prose while ignoring protocol tokens/keys."""
    value = value.strip()
    if not value or value.startswith(("http://", "https://", "@")):
        return False
    if RTL.search(value):
        return True
    if TECHNICAL.fullmatch(value):
        return False
    # Natural-language whitespace and sentence punctuation are strong signals.
    return bool(re.search(r"\s", value) and re.search(r"[A-Za-zÀ-ž]", value))


def scan_source(path: str, source: str, changed_lines: set[int] | None = None) -> list[str]:
    """Return direct visible UI literals, optionally limited to added diff lines."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: Python syntax error: {exc.msg}"]
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name not in VISIBLE_CALLS and not (
            name.startswith(("send_", "reply_", "edit_"))
            and name not in {"send", "reply", "edit"}
        ):
            continue
        candidates: list[ast.AST] = []
        if node.args:
            # Pyrogram methods differ: Client.send_message(chat_id, text), while
            # Message.reply_text(text) and CallbackQuery.answer(text) put copy
            # first. Inspect the documented positional slot for each API.
            visible_position = {
                "send_message": 1,
            }.get(name, 0)
            # Client-level edit APIs use (chat_id, message_id, text/caption),
            # while Message/CallbackQuery edit methods take visible text first.
            if name in {"edit_message_text", "edit_message_caption"} and len(node.args) >= 3:
                visible_position = 2
            if name in {"InlineKeyboardButton", "KeyboardButton"}:
                visible_position = 0
            if visible_position < len(node.args):
                candidates.append(node.args[visible_position])
        for kw in node.keywords:
            if kw.arg in VISIBLE_KEYWORDS:
                candidates.append(kw.value)
        for arg in candidates:
            line = getattr(arg, "lineno", getattr(node, "lineno", 1))
            if _is_translation_expression(source, arg):
                continue
            if not isinstance(arg, (ast.Constant, ast.JoinedStr)):
                continue
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                value = arg.value
            else:
                value = ast.get_source_segment(source, arg) or ""
            if not _human_literal(value):
                continue
            line = getattr(arg, "lineno", getattr(node, "lineno", 1))
            if changed_lines is not None and line not in changed_lines:
                continue
            excerpt = value.replace("\n", " ")[:100]
            findings.append(f"{path}:{line}: untranslated UI literal in {name}(): {excerpt!r}")
    return findings


def literal_signatures(source: str) -> set[str]:
    """Extract visible literal call signatures without classifying their language."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    signatures: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name not in VISIBLE_CALLS and not (
            name.startswith(("send_", "reply_", "edit_"))
            and name not in {"send", "reply", "edit"}
        ):
            continue
        candidates: list[ast.AST] = []
        if node.args:
            position = {"send_message": 1}.get(name, 0)
            if name in {"edit_message_text", "edit_message_caption"} and len(node.args) >= 3:
                position = 2
            if position < len(node.args):
                candidates.append(node.args[position])
        for kw in node.keywords:
            if kw.arg in VISIBLE_KEYWORDS:
                candidates.append(kw.value)
        for arg in candidates:
            if not isinstance(arg, (ast.Constant, ast.JoinedStr)):
                continue
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                value = arg.value
            else:
                value = ast.get_source_segment(source, arg) or ""
            excerpt = value.replace("\\n", " ")[:100]
            signatures.add(f"{name}(): {excerpt!r}")
    return signatures


def changed_python_files() -> tuple[list[str], str]:
    base = os.getenv("I18N_AUDIT_BASE", "origin/main")
    # GitHub pull_request supplies the target branch; local runs can set base.
    event_base = os.getenv("GITHUB_BASE_REF")
    if event_base:
        base = f"origin/{event_base}"
    # Prefer the ref already supplied by checkout. Fetch only when absent,
    # and bound network operations so a transient GitHub hiccup cannot hang CI.
    present = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", base],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=5,
    )
    if present.returncode != 0:
        try:
            subprocess.run(
                ["git", "fetch", "--no-tags", "origin", base.removeprefix("origin/")],
                cwd=ROOT, check=False, capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            pass
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", f"{base}...HEAD"],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        # A shallow/local checkout may lack the merge base. Fall back to all
        # UI source files; this is stricter and never silently skips the audit.
        files = [
            str(p.relative_to(ROOT))
            for root in UI_ROOTS if (ROOT / root).exists()
            for p in (ROOT / root).rglob("*.py")
        ]
        return sorted(set(files)), base
    files = [
        item for item in result.stdout.splitlines()
        if item.endswith(".py") and item.startswith(UI_ROOTS)
    ]
    return files, base



def all_ui_python_files() -> list[str]:
    """Discover UI source files at runtime; no hand-maintained file inventory."""
    discovered: set[str] = set()
    for root in UI_ROOTS:
        folder = ROOT / root
        if folder.exists():
            discovered.update(
                str(path.relative_to(ROOT))
                for path in folder.rglob("*.py")
                if path.is_file()
            )
    return sorted(discovered)


def added_line_numbers(path: str, base: str) -> set[int] | None:
    """Return only added source lines so legacy literals do not block unrelated edits."""
    result = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD", "--", path],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    added: set[int] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            added.update(range(start, start + count))
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true', help='scan every Telegram UI Python file')
    args = parser.parse_args()
    files, base = changed_python_files()
    if args.all:
        files = all_ui_python_files()
        base = 'all discovered Telegram UI Python files'
    if not files:
        print(f"Telegram i18n literal guard: PASS (no changed Telegram UI Python files; base={base})")
        return 0
    for relative in files:
        path = ROOT / relative
        if path.exists():
            source = path.read_text(encoding="utf-8")
            if args.all:
                FAILURES.extend(scan_source(relative, source))
            else:
                changed = added_line_numbers(relative, base)
                current_findings = scan_source(relative, source, changed) if changed else scan_source(relative, source)
                # Existing direct literals in the base revision are legacy debt,
                # not regressions. Compare stable call+literal signatures rather
                # than fragile line numbers so moving code does not make CI red.
                baseline_result = subprocess.run(
                    ["git", "show", f"{base}:{relative}"],
                    cwd=ROOT, check=False, capture_output=True, text=True,
                )
                baseline_signatures = set()
                if baseline_result.returncode == 0:
                    baseline_signatures = literal_signatures(baseline_result.stdout)
                FAILURES.extend(
                    item for item in current_findings
                    if ": untranslated UI literal in " not in item
                    or item.split(": untranslated UI literal in ", 1)[1] not in baseline_signatures
                )
    if FAILURES:
        print(f"Telegram i18n literal guard: FAIL ({len(FAILURES)} finding(s))")
        print("\n".join(f"  ✗ {item}" for item in FAILURES))
        print("Use self._t('catalog_key', lang=...) for every user-visible message/button.")
        return 1
    print(f"Telegram i18n literal guard: PASS ({len(files)} changed UI file(s) scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
