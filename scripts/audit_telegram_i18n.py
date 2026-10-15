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
    r"^(?:https?://|@[A-Za-z0-9_]{3,}|[A-Z][A-Z0-9_]{2,}|"
    r"[a-z_][a-z0-9_]*(?:_[a-z0-9_]+)*|\\d+(?:\\.\\d+)?|"
    r"\\s*|[\\W_]+)$"
)
RTL = re.compile(r"[\\u0600-\\u06ff\\u0750-\\u077f\\u08a0-\\u08ff]")
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
    """Return direct visible UI literals; optionally inspect only added diff lines."""
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
            # Telegram send/reply/edit APIs put their visible body first.
            if name not in {"InlineKeyboardButton", "KeyboardButton"}:
                candidates.append(node.args[0])
        if name in {"InlineKeyboardButton", "KeyboardButton"} and node.args:
            candidates.append(node.args[0])
        for kw in node.keywords:
            if kw.arg in VISIBLE_KEYWORDS:
                candidates.append(kw.value)
        for arg in candidates:
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


def changed_python_files() -> tuple[list[str], str]:
    base = os.getenv("I18N_AUDIT_BASE", "origin/main")
    # GitHub pull_request supplies the target branch; local runs can set base.
    event_base = os.getenv("GITHUB_BASE_REF")
    if event_base:
        base = f"origin/{event_base}"
    subprocess.run(
        ["git", "fetch", "--no-tags", "origin", base.removeprefix("origin/")],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", f"{base}...HEAD"],
        cwd=ROOT, check=False, capture_output=True, text=True,
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


def added_line_numbers(path: str, base: str) -> set[int]:
    """Return line numbers introduced by the PR; avoid failing on legacy debt."""
    result = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD", "--", path],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return set()
    added: set[int] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        added.update(range(start, start + count))
    return added


def main() -> int:
    files, base = changed_python_files()
    if not files:
        print(f"Telegram i18n literal guard: PASS (no changed Telegram UI Python files; base={base})")
        return 0
    for relative in files:
        path = ROOT / relative
        if path.exists():
            changed = added_line_numbers(relative, base)
            if not changed:
                print(f"WARNING: no added-line diff available for {relative}; scanning full file")
                FAILURES.extend(scan_source(relative, path.read_text(encoding="utf-8")))
            else:
                FAILURES.extend(scan_source(relative, path.read_text(encoding="utf-8"), changed))
    if FAILURES:
        print(f"Telegram i18n literal guard: FAIL ({len(FAILURES)} finding(s))")
        print("\n".join(f"  ✗ {item}" for item in FAILURES))
        print("Use self._t('catalog_key', lang=...) for every user-visible message/button.")
        return 1
    print(f"Telegram i18n literal guard: PASS ({len(files)} changed UI file(s) scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
