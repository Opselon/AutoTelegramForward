#!/usr/bin/env python3
"""Deterministic i18n audit for the Telegram bot.

Why this exists
---------------
The repository already had four gates: a 1:1 key/placeholder audit, a
change-scoped literal guard, a mobile copy gate and a web copy gate. Each one
answers a narrow question, and the questions they do *not* answer are exactly
the ones that reach users:

  * a keyboard label built as a tuple literal (`_kbd([[("Delete", "x")]])`)
    never passes through the visible-call list, so the literal guard is blind;
  * an f-string that mixes prose with a translated variable is a literal to a
    human and an opaque ``JoinedStr`` to a regex;
  * a key referenced from a helper parameter looks unused to a literal-only
    scan, and a key *built* from a prefix looks defined to it;
  * catalog values can be present, placeholder-correct and still untranslated.

This module is the single audit that answers all of them in one pass, with
stable finding classes, machine-readable output and a content-keyed baseline so
the existing debt can be pinned without hiding new debt.

Finding classes (stable identifiers, used by the JSON output and the tests):

    CATALOG_PARITY              key sets differ between the reference and a locale
    EMPTY_VALUE                 a key renders an empty string in some locale
    INVALID_CHARACTER           U+FFFD (a destroyed emoji) in a catalog value
    PLACEHOLDER_MISMATCH        placeholder names or format specs differ
    MISSING_KEY                 a literal key used in code that no catalog defines
    DYNAMIC_KEY_UNVERIFIED      a key assembled from a prefix with no static family
    UNUSED_KEY                  a catalog key no source file mentions
    HARDCODED_USER_TEXT         prose literal reaching a user-facing call
    GLOBAL_LANGUAGE_STATE       process-wide `i18n.current` read on a render path
    UNWRAPPED_HANDLER           an update handler not bound to a request locale
    CALLBACK_DATA_FROM_TEXT     callback_data built from translated text
    VALUE_LEAK                  a locale's value holds another language's script
    UNTRANSLATED_VALUE          a locale's value is the English prose verbatim

Severity is ``critical`` for anything a user can see as the wrong language or
as a raw key, ``major`` for catalog defects that surface on some paths, and
``minor`` for dead weight.

Usage::

    python3 scripts/i18n_audit.py                     # human report
    python3 scripts/i18n_audit.py --json out.json     # machine report
    python3 scripts/i18n_audit.py --update-baseline   # re-pin known debt
    python3 scripts/i18n_audit.py --fail-on critical  # default

Exit codes: 0 clean (or only baselined findings), 1 findings above the
threshold, 2 the audit itself could not run.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]

REFERENCE_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ["en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it"]
LATIN_LOCALES = {"en", "es", "fr", "id", "it", "pt", "uz"}
RTL_LOCALES = {"fa"}
CJK_LOCALES = {"zh"}
CYRILLIC_LOCALES = {"ru"}

SCRIPT_RTL = re.compile(r"[\u0590-\u05ff\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")
SCRIPT_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
SCRIPT_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
SCRIPT_LATIN_WORD = re.compile(r"\b[A-Za-z]{3,}\b")
PLACEHOLDER = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)(?:!([a-zA-Z]))?(?::([^}]+))?\}(?!\})")
U_FFFD = "\ufffd"
KEY_SHAPE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
TOKEN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
TECHNICAL_TOKEN = re.compile(
    r"^(?:https?://|tg://|@\w+|[A-Z][A-Z0-9_]{1,}|[a-z0-9_.:/+-]+|\d+(?:[.,]\d+)?|\s*|[\W_]+)$"
)

#: Calls whose arguments reach a user's screen.
VISIBLE_CALLS = {
    "answer", "reply", "reply_text", "reply_photo", "reply_document", "reply_animation",
    "reply_video", "reply_audio", "reply_sticker",
    "send_message", "send_photo", "send_document", "send_animation", "send_video",
    "send_audio", "send_voice", "send_sticker", "send_cached_media", "send_media_group",
    "edit_text", "edit_message_text", "edit_message_caption", "edit_message_media",
    "edit_message_reply_markup", "edit_reply_markup", "safe_edit", "edit_caption",
    "InlineKeyboardButton", "KeyboardButton", "copy_message",
}
TEXT_KEYWORDS = {"text", "caption", "label", "title", "placeholder", "message", "question"}
KEY_CALLS = {"_t", "t", "_btn", "btn", "translate", "_label", "_toggle_label"}
BUTTON_ROW_HELPERS = {"_kbd", "_row", "_menu", "_grid", "_keypad", "_cancel_kbd"}

#: Terms that are identical in every language by definition. A value made only
#: of these is not an untranslated string; excluding them here (rather than
#: pinning them in a baseline) keeps a real leak beside them visible.
NEUTRAL_TOKENS = {
    "telegram", "telegram premium", "api", "id", "vip", "ai", "gpt", "openai",
    "html", "json", "markdown", "url", "urls", "uuid", "ip", "ttl", "db", "sql",
    "mp3", "mp4", "pdf", "csv", "gif", "jpeg", "png", "webp", "ok", "sms", "2fa",
    "otp", "bot", "bots", "web", "webhook", "webhooks", "proxy", "socks5", "tor",
    "torrc", "sha", "jwt", "cpu", "ram", "dns", "http", "https", "tls", "ssh",
    "vpn", "bg", "vip", "pro", "usd", "eur", "rub", "irr", "star", "stars", "ton", "crypto",
    "binance", "coinbase", "kraken", "bybit", "okx", "kucoin",
}


@dataclass
class Finding:
    kind: str
    severity: str
    path: str
    line: int
    detail: str
    evidence: str = ""
    locales: Tuple[str, ...] = ()
    hint: str = ""

    @property
    def fingerprint(self) -> str:
        """Content key: stable across line moves and reformatting."""
        basis = f"{self.kind}|{self.path}|{self.detail}|{self.evidence}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        row = asdict(self)
        row["locales"] = list(self.locales)
        row["fingerprint"] = self.fingerprint
        return row


@dataclass
class AuditResult:
    findings: List[Finding] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)

    def of(self, kind: str) -> List[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out

    def above(self, threshold: str) -> List[Finding]:
        order = {"minor": 0, "major": 1, "critical": 2}
        floor = order[threshold]
        return [f for f in self.findings if order[f.severity] >= floor]


# --------------------------------------------------------------------------- #
# catalog layer
# --------------------------------------------------------------------------- #
def load_catalog(root: Path) -> Dict[str, Dict[str, str]]:
    path = root / "core/infrastructure/telegram/locales/translations.json"
    with open(path, encoding="utf-8") as fh:
        catalog = json.load(fh)
    if not isinstance(catalog, dict) or REFERENCE_LANGUAGE not in catalog:
        raise ValueError(f"{path} has no '{REFERENCE_LANGUAGE}' table")
    return {lang: table for lang, table in catalog.items() if isinstance(table, dict)}


def catalog_languages(catalog: Dict[str, Dict[str, str]]) -> List[str]:
    return sorted(catalog)


def registered_languages(root: Path) -> List[str]:
    """The locale list `i18n.py` accepts, read without importing pyrogram."""
    path = root / "core/infrastructure/telegram/i18n.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "SUPPORTED_LANGUAGES":
            try:
                return sorted(ast.literal_eval(node.value))
            except ValueError:
                return []
    return []


def audit_catalog(catalog: Dict[str, Dict[str, str]], root: Optional[Path] = None) -> List[Finding]:
    findings: List[Finding] = []
    reference = catalog[REFERENCE_LANGUAGE]
    ref_keys = set(reference)

    if root is not None:
        registered = registered_languages(root)
        if registered and registered != catalog_languages(catalog):
            findings.append(Finding(
                kind="LOCALE_REGISTRY_DRIFT", severity="critical",
                path="core/infrastructure/telegram/i18n.py", line=0,
                detail=f"i18n.py accepts {registered}, catalog has {catalog_languages(catalog)}",
                hint="a locale in one list and not the other is unreachable or unrenderable",
            ))

    for lang, table in sorted(catalog.items()):
        missing = sorted(ref_keys - set(table))
        extra = sorted(set(table) - ref_keys)
        if missing or extra:
            findings.append(Finding(
                kind="CATALOG_PARITY", severity="critical",
                path="core/infrastructure/telegram/locales/translations.json", line=0,
                detail=f"{lang}: {len(missing)} missing, {len(extra)} extra",
                evidence=", ".join((missing + extra)[:8]), locales=(lang,),
                hint="add/remove the key in every locale in the same commit",
            ))
        for key in sorted(ref_keys & set(table)):
            value = table[key]
            if not str(value).strip():
                findings.append(Finding(
                    kind="EMPTY_VALUE", severity="major",
                    path="core/infrastructure/telegram/locales/translations.json", line=0,
                    detail=f"{lang}: {key}", evidence="", locales=(lang,),
                    hint="an empty value silently falls back to English mid-screen",
                ))
            if U_FFFD in str(value):
                findings.append(Finding(
                    kind="INVALID_CHARACTER", severity="critical",
                    path="core/infrastructure/telegram/locales/translations.json", line=0,
                    detail=f"{lang}: {key}", evidence=str(value)[:60], locales=(lang,),
                    hint="a destroyed emoji renders as a black diamond; re-encode the value",
                ))
            if lang == REFERENCE_LANGUAGE:
                continue
            if _placeholder_signature(value) != _placeholder_signature(reference[key]):
                findings.append(Finding(
                    kind="PLACEHOLDER_MISMATCH", severity="critical",
                    path="core/infrastructure/telegram/locales/translations.json", line=0,
                    detail=f"{lang}: {key}",
                    evidence=f"{sorted(_placeholder_signature(value))} != "
                             f"{sorted(_placeholder_signature(reference[key]))}",
                    locales=(lang,),
                    hint="names, format specs and conversions must match the reference",
                ))
            leak = _value_leak(lang, value, reference[key])
            if leak:
                findings.append(Finding(
                    kind=leak, severity="major",
                    path="core/infrastructure/telegram/locales/translations.json", line=0,
                    detail=f"{lang}: {key}", evidence=str(value)[:70], locales=(lang,),
                    hint="translate the value; identical prose is a fallback nobody sees",
                ))
    return findings


def _placeholder_signature(value: str) -> Tuple[Tuple[str, str, str], ...]:
    return tuple(sorted((m[0], m[1] or "", m[2] or "") for m in PLACEHOLDER.findall(str(value))))


def _neutral(text: str) -> bool:
    stripped = PLACEHOLDER.sub(" ", text)
    words = [w for w in re.findall(r"[^\W\d_]+", stripped, re.UNICODE) if len(w) >= 2]
    if not words:
        return True
    return all(w.lower() in NEUTRAL_TOKENS for w in words)


def _value_leak(lang: str, value: str, english: str) -> str:
    """Classify a locale value that carries the wrong language."""
    text = str(value)
    if _neutral(text):
        return ""
    if lang in LATIN_LOCALES and SCRIPT_RTL.search(text):
        return "VALUE_LEAK"
    if lang in LATIN_LOCALES and SCRIPT_CYRILLIC.search(text):
        return "VALUE_LEAK"
    if lang in LATIN_LOCALES and SCRIPT_CJK.search(text):
        return "VALUE_LEAK"
    if lang in RTL_LOCALES and SCRIPT_CJK.search(text):
        return "VALUE_LEAK"
    if lang in CJK_LOCALES and SCRIPT_RTL.search(text):
        return "VALUE_LEAK"
    if lang in CYRILLIC_LOCALES and SCRIPT_RTL.search(text):
        return "VALUE_LEAK"
    # same prose as English: no script difference to catch it, so compare
    if text.strip() == str(english).strip() and len(SCRIPT_LATIN_WORD.findall(PLACEHOLDER.sub(" ", text))) >= 2:
        return "UNTRANSLATED_VALUE"
    return ""


# --------------------------------------------------------------------------- #
# source layer
# --------------------------------------------------------------------------- #
def ui_files(root: Path) -> List[Path]:
    roots = [
        root / "core/infrastructure/telegram",
        root / "core/presentation/telegram",
        root / "core/application/telegram",
    ]
    files: List[Path] = []
    for base in roots:
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return [p for p in files if p.name != "i18n.py"]


def is_test_file(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_")


def is_prose(value: str) -> bool:
    """True when a literal reads like a sentence a human would see."""
    text = value.strip()
    if not text or TECHNICAL_TOKEN.match(text):
        return False
    if not re.search(r"[A-Za-z\u0600-\u06ff\u0400-\u04ff\u4e00-\u9fff]", text):
        return False
    unstyled = re.sub(r"^[^\w\u0600-\u06ff]+", "", text)
    if not unstyled:
        return False
    words = re.findall(r"[^\W\d_]{2,}", unstyled, re.UNICODE)
    if len(words) >= 2:
        return True
    return bool(re.search(r"[.!?…:]\s*$", unstyled)) and len(unstyled) > 6


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def literal_text(node: Optional[ast.AST]) -> Tuple[str, bool]:
    """(text, is_static) for a string-valued expression.

    A ``JoinedStr`` contributes only its constant parts; the boolean says
    whether the whole expression was static. Reporting the constant part of an
    f-string is the point: ``f"Deleted {n} messages"`` reads as English prose
    even though ``n`` is dynamic.
    """
    if node is None:
        return "", False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts), False
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left, _ = literal_text(node.left)
        right, _ = literal_text(node.right)
        return left + right, False
    return "", False


def _shallow_parts(node: ast.AST) -> List[ast.AST]:
    """The expression's own string-bearing parts, without deep recursion."""
    parts: List[ast.AST] = [node]
    if isinstance(node, ast.JoinedStr):
        parts.extend(node.values)
    elif isinstance(node, ast.BinOp):
        parts.extend([node.left, node.right])
    elif isinstance(node, ast.Call):
        parts.extend(node.args)
        parts.extend(kw.value for kw in node.keywords)
    return parts


_SEGMENT_CACHE: Dict[int, Tuple[str, bytes, List[int]]] = {}


def _segment(source: str, node: ast.AST) -> str:
    """The exact source text of ``node``.

    ``ast.get_source_segment`` re-splits the entire file on every call, which
    costs minutes on a 12k-line module with thousands of nodes. The line starts
    are computed once per source and offsets are byte offsets (they are, which
    matters for every line that carries an emoji).
    """
    key = id(source)
    cached = _SEGMENT_CACHE.get(key)
    if cached is None or cached[0] is not source:
        raw = source.encode("utf-8")
        starts = [0]
        for index, byte in enumerate(raw):
            if byte == 0x0A:
                starts.append(index + 1)
        cached = (source, raw, starts)
        _SEGMENT_CACHE[key] = cached
    _, raw, starts = cached
    lineno = getattr(node, "lineno", 0) or 0
    end_lineno = getattr(node, "end_lineno", lineno) or lineno
    if lineno <= 0 or end_lineno > len(starts):
        return ""
    start = starts[lineno - 1] + (getattr(node, "col_offset", 0) or 0)
    end = starts[end_lineno - 1] + (getattr(node, "end_col_offset", 0) or 0)
    try:
        return raw[start:end].decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def _expression_text(source: str, node: ast.AST) -> str:
    return _segment(source, node)


def _resolves_through_catalog(expr: str) -> bool:
    return any(marker in expr for marker in ("_t(", ".t(", "_btn(", "translate(", "gettext("))


class SourceAudit(ast.NodeVisitor):
    """Walks one module and collects key references and literal leaks."""

    def __init__(self, path: Path, source: str, catalog: Dict[str, Dict[str, str]]):
        self.path = path
        self.source = source
        self.reference = catalog[REFERENCE_LANGUAGE]
        self.findings: List[Finding] = []
        self.keys_used: Set[str] = set()
        self.dynamic_prefixes: Set[str] = set()
        self.handlers = 0
        self.wrapped_handlers = 0
        self._translated_names: Set[str] = set()

    # -- helpers -------------------------------------------------------- #
    def _add(self, kind: str, severity: str, node: ast.AST, detail: str,
             evidence: str = "", hint: str = "") -> None:
        self.findings.append(Finding(
            kind=kind, severity=severity,
            path=str(self.path), line=getattr(node, "lineno", 0),
            detail=detail, evidence=evidence[:120], hint=hint,
        ))

    def _note_key(self, value: str, node: ast.AST) -> None:
        if not KEY_SHAPE.match(value):
            return
        self.keys_used.add(value)
        if value not in self.reference:
            self._add(
                "MISSING_KEY", "critical", node, value,
                evidence=_expression_text(self.source, node)[:80],
                hint="t() answers an unknown key with the key itself — a raw key in the chat",
            )

    def _check_visible_expression(self, node: Optional[ast.AST], api: str) -> None:
        if node is None:
            return
        expr = _expression_text(self.source, node)
        if _resolves_through_catalog(expr):
            # The label is translated, but a literal tail can still leak:
            # `_t("x") + " (English note)"` is half-translated. Only the
            # expression's own operands are inspected — walking the whole
            # subtree once per visible argument is quadratic in nesting depth
            # and this file nests keyboard builders five levels deep.
            for child in _shallow_parts(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    text = child.value
                    if is_prose(text) and not self._is_key_literal(text):
                        self._add("HARDCODED_USER_TEXT", "critical", child,
                                  f"{api}: concatenated literal", text)
            return
        text, _ = literal_text(node)
        if is_prose(text):
            self._add("HARDCODED_USER_TEXT", "critical", node, f"{api}: literal", text,
                      hint="route the string through self._t(key) and add the key to all locales")

    def _is_key_literal(self, text: str) -> bool:
        return bool(KEY_SHAPE.match(text) and text in self.reference)

    # -- traversal ------------------------------------------------------ #
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node) -> None:
        decorators = [ast.get_source_segment(self.source, d) or "" for d in node.decorator_list]
        registered = any(".on_" in d for d in decorators)
        wrapped = any("_localized_handler" in d for d in decorators)
        if registered:
            self.handlers += 1
            # A handler can be locale-safe without the decorator: resolving the
            # language itself and passing `lang=` on every call is just as
            # isolated. Only a handler that *also* has a context-dependent
            # translation call is a leak, so count those instead of the
            # decorator alone.
            unbound = self._calls_without_lang(node)
            if wrapped or not unbound:
                self.wrapped_handlers += 1
            else:
                self._add("UNWRAPPED_HANDLER", "critical", node, node.name,
                          evidence=f"{unbound} call(s) without lang=, e.g. line {unbound[0]}"
                          if isinstance(unbound, list) else "",
                          hint="bind the handler with @self._localized_handler (or pass lang= "
                               "on every _t call) so it renders in the requesting user's language")
        self.generic_visit(node)

    def _calls_without_lang(self, node: ast.AST) -> List[int]:
        lines: List[int] = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = call_name(child.func)
            if name not in {"_t", "t", "_btn", "btn", "translate"}:
                continue
            if not any(kw.arg == "lang" for kw in child.keywords):
                lines.append(child.lineno)
        return lines

    def visit_Assign(self, node: ast.Assign) -> None:
        # `text = self._t("key")` then `callback_data=text` is translated text
        # used as an identifier; remember the names that hold translations.
        for target in node.targets:
            if isinstance(target, ast.Name) and _resolves_through_catalog(_expression_text(self.source, node.value)):
                self._translated_names.add(target.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "current" and isinstance(node.value, ast.Attribute) and node.value.attr == "i18n":
            self._add("GLOBAL_LANGUAGE_STATE", "critical", node, "i18n.current",
                      hint="process-wide: one user's choice renders for every other user")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        # `{"text": ..., "callback_data": ...}` — the dict form of a button.
        for key, value in zip(node.keys or [], node.values or []):
            if isinstance(key, ast.Constant) and key.value in {"callback_data", "data"}:
                expr = _expression_text(self.source, value)
                if _resolves_through_catalog(expr):
                    self._add("CALLBACK_DATA_FROM_TEXT", "critical", value, "callback_data",
                              evidence=expr[:80],
                              hint="callback data is matched after a language change; keep it ASCII")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = call_name(node.func)

        if name in KEY_CALLS and node.args:
            first = node.args[0]
            text, static = literal_text(first)
            if static:
                self._note_key(text, first)
            elif isinstance(first, ast.JoinedStr):
                prefix = _fstring_prefix(first)
                if prefix:
                    self.dynamic_prefixes.add(prefix)
                self._add("DYNAMIC_KEY_UNVERIFIED", "major", node, prefix or "<dynamic>",
                          evidence=_expression_text(self.source, first)[:80],
                          hint="enumerate the possible keys and assert each exists, or use contains()")

        if name in VISIBLE_CALLS:
            for arg in node.args:
                self._check_visible_expression(arg, name)
            for kw in node.keywords:
                if kw.arg in TEXT_KEYWORDS:
                    self._check_visible_expression(kw.value, f"{name}({kw.arg})")

        if name in BUTTON_ROW_HELPERS and node.args:
            self._check_button_rows(node.args[0])

        # callback_data must be an identifier, never a translation
        for kw in node.keywords:
            if kw.arg in {"callback_data", "data"}:
                expr = _expression_text(self.source, kw.value)
                if _resolves_through_catalog(expr):
                    self._add("CALLBACK_DATA_FROM_TEXT", "critical", node, kw.arg,
                              evidence=expr[:80],
                              hint="callback data is matched after a language change; keep it ASCII")
                elif isinstance(kw.value, ast.Name) and kw.value.id in self._translated_names:
                    self._add("CALLBACK_DATA_FROM_TEXT", "critical", node, kw.arg,
                              evidence=expr[:80],
                              hint="callback data is matched after a language change; keep it ASCII")
        self.generic_visit(node)

    def _check_button_rows(self, node: ast.AST) -> None:
        """Flag literal labels inside `_kbd([[("Label", data)]])`."""
        for child in ast.walk(node):
            if not isinstance(child, ast.Tuple) or len(child.elts) < 2:
                continue
            label = child.elts[0]
            if not isinstance(label, ast.Constant) or not isinstance(label.value, str):
                continue
            if len(child.elts) > 1:
                payload = _expression_text(self.source, child.elts[1])
                if _resolves_through_catalog(payload):
                    self._add("CALLBACK_DATA_FROM_TEXT", "critical", child.elts[1],
                              "keyboard tuple payload", evidence=payload[:80],
                              hint="callback data is matched after a language change; keep it ASCII")
            text = label.value
            if KEY_SHAPE.match(text) or text in self.reference:
                continue
            if is_prose(text):
                self._add("HARDCODED_USER_TEXT", "critical", label,
                          "keyboard label", text,
                          hint="use self._btn(\"key\") so every locale gets its own label")


def _fstring_prefix(node: ast.JoinedStr) -> str:
    if not node.values:
        return ""
    first = node.values[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        text = first.value
        if text.endswith("_") and len(text) > 2:
            return text
    return ""


def audit_sources(root: Path, catalog: Dict[str, Dict[str, str]]) -> Tuple[List[Finding], Dict[str, object]]:
    findings: List[Finding] = []
    used: Set[str] = set()
    prefixes: Set[str] = set()
    all_literals: Set[str] = set()
    handlers = wrapped = 0
    parsed = 0
    sources: Dict[str, str] = {}

    files = ui_files(root)
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        sources[str(path)] = source
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            findings.append(Finding(
                kind="PARSE_ERROR", severity="critical", path=str(path),
                line=getattr(exc, "lineno", 0) or 0, detail=str(exc)[:120],
            ))
            continue
        parsed += 1
        visitor = SourceAudit(path, source, catalog)
        visitor.visit(tree)
        findings.extend(visitor.findings)
        used |= visitor.keys_used
        prefixes |= visitor.dynamic_prefixes
        handlers += visitor.handlers
        wrapped += visitor.wrapped_handlers
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                all_literals.add(node.value)

    reference = set(catalog[REFERENCE_LANGUAGE])
    # Any literal mention counts as a reference: keys are also forwarded through
    # helper parameters (`self._panel(title_key="ui_x")`) which a `_t`-only scan
    # would call unused.
    mentioned = {key for key in reference if key in all_literals}
    used |= mentioned
    # The catalog is shared: the dashboard generates its setting labels from it
    # and the CLI validates against it. A key mentioned there is not dead, even
    # when no bot module reads it.
    shared = reference_mentions(root, reference)
    used |= shared

    dynamic_families = {key for key in reference if any(key.startswith(p) for p in prefixes)}
    used |= dynamic_families

    for key in sorted(reference - used):
        findings.append(Finding(
            kind="UNUSED_KEY", severity="minor",
            path="core/infrastructure/telegram/locales/translations.json", line=0,
            detail=key, hint="dead weight in ten locales; remove it in one commit or wire it up",
        ))

    return findings, {
        "files": len(sources),
        "parsed": parsed,
        "handlers": handlers,
        "wrapped_handlers": wrapped,
        "keys_referenced": len(used),
        "keys_dynamic_families": len(dynamic_families),
        "dynamic_prefixes": sorted(prefixes),
    }


def reference_mentions(root: Path, reference: Set[str]) -> Set[str]:
    """Keys the bot does not read but another surface still renders.

    The Telegram catalog is also the dashboard's setting-label source and the
    CLI's validation target, so a key can be live without appearing in any bot
    module. Reading those files is not enough: the dashboard constructs labels
    from registry data, so its *generated* artifacts are scanned too.
    """
    skip = {"node_modules", "dist", "build", ".venv", "__pycache__", "locales", "l10n"}
    suffixes = {".py", ".ts", ".tsx", ".js", ".dart", ".json"}
    hits: Set[str] = set()
    # the dashboard generates setting labels from the catalog
    for base in ("core", "scripts", "atfcli", "web/frontend/src", "mobile/lib"):
        for path in (root / base).rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if any(tag in path.parts for tag in skip):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            hits |= set(TOKEN.findall(text)) & reference
    return hits


def audit(root: Path) -> AuditResult:
    catalog = load_catalog(root)
    findings = audit_catalog(catalog, root)
    source_findings, stats = audit_sources(root, catalog)
    findings.extend(source_findings)
    stats["catalog_keys"] = len(catalog[REFERENCE_LANGUAGE])
    stats["locales"] = len(catalog)
    return AuditResult(findings=findings, stats=stats)


# --------------------------------------------------------------------------- #
# baseline
# --------------------------------------------------------------------------- #
def load_baseline(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {entry["fingerprint"]: entry for entry in data.get("entries", []) if "fingerprint" in entry}


def split_baselined(findings: List[Finding], baseline: Dict[str, dict]) -> Tuple[List[Finding], List[Finding]]:
    fresh: List[Finding] = []
    known: List[Finding] = []
    for finding in findings:
        entry = baseline.get(finding.fingerprint)
        if entry is None:
            fresh.append(finding)
        else:
            known.append(finding)
    return fresh, known


def write_baseline(path: Path, findings: List[Finding]) -> int:
    entries = []
    for finding in sorted(findings, key=lambda f: (f.kind, f.path, f.detail)):
        entries.append({
            "fingerprint": finding.fingerprint,
            "kind": finding.kind,
            "path": finding.path,
            "detail": finding.detail,
            "evidence": finding.evidence,
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "entries": entries}, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return len(entries)


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def human_report(result: AuditResult, fresh: List[Finding], known: List[Finding],
                 threshold: str = "critical") -> str:
    counts = result.counts()
    failing = _at_or_above(fresh, threshold) if threshold != "none" else []
    below = [f for f in fresh if f not in failing]
    lines = ["I18N AUDIT", ""]
    lines.append(
        f"  {result.stats['files']} UI modules, {result.stats['handlers']} handlers "
        f"({result.stats['wrapped_handlers']} locale-bound), "
        f"{result.stats['keys_referenced']}/{result.stats['catalog_keys']} keys referenced, "
        f"{result.stats['locales']} locales"
    )
    if counts:
        lines.append("")
        for kind in sorted(counts):
            lines.append(f"  {kind:26s} {counts[kind]:5d}")
    else:
        lines.append("  no findings")
    if failing:
        lines.append("")
        lines.append("NEW (failing):")
        for finding in failing[:40]:
            where = f"{finding.path}:{finding.line}" if finding.line else finding.path
            suffix = f"  [{', '.join(finding.locales)}]" if finding.locales else ""
            lines.append(f"  {finding.severity:8s} {finding.kind:24s} {where}  {finding.detail}{suffix}")
            if finding.evidence:
                lines.append(f"           {finding.evidence}")
    if known:
        lines.append("")
        lines.append(f"BASELINED (known debt, not failing): {len(known)}")
        for finding in known[:10]:
            lines.append(f"  {finding.kind:24s} {finding.path}  {finding.detail}")
        if len(known) > 10:
            lines.append(f"  … {len(known) - 10} more")
    if below:
        lines.append("")
        lines.append(f"NEW below the '{threshold}' threshold (reported, not failing): {len(below)}")
        for finding in below[:10]:
            lines.append(f"  {finding.kind:24s} {finding.path}  {finding.detail}")
        if len(below) > 10:
            lines.append(f"  … {len(below) - 10} more")
    lines.append("")
    if failing:
        lines.append(f"FAIL: {len(failing)} finding(s) at or above '{threshold}' are not baselined")
    else:
        lines.append(f"PASS: no unbaselined finding at or above '{threshold}'"
                     + (f" ({len(known)} baselined)" if known else ""))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic i18n audit for the Telegram bot")
    parser.add_argument("--root", default=str(ROOT), help="repository root (default: auto-detected)")
    parser.add_argument("--json", dest="json_path", help="write the machine-readable report here")
    parser.add_argument("--baseline", default="scripts/i18n_baseline.json",
                        help="content-keyed baseline of known findings")
    parser.add_argument("--update-baseline", action="store_true",
                        help="re-pin every current finding as known debt")
    parser.add_argument("--fail-on", choices=["minor", "major", "critical", "none"], default="critical",
                        help="exit 1 when a NEW finding at or above this severity exists")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        result = audit(root)
    except Exception as exc:  # noqa: BLE001
        print(f"i18n audit could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = root / baseline_path

    if args.update_baseline:
        count = write_baseline(baseline_path, result.findings)
        print(f"baseline written: {count} findings -> {baseline_path}")
        return 0

    baseline = load_baseline(baseline_path)
    fresh, known = split_baselined(result.findings, baseline)

    if args.json_path:
        payload = {
            "stats": result.stats,
            "counts": result.counts(),
            "findings": [f.as_dict() for f in result.findings],
            "fresh": [f.fingerprint for f in fresh],
            "baselined": [f.fingerprint for f in known],
        }
        Path(args.json_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                                        encoding="utf-8")

    print(human_report(result, fresh, known, args.fail_on))

    if args.fail_on == "none":
        return 0
    return 1 if _at_or_above(fresh, args.fail_on) else 0


def _at_or_above(findings: Iterable[Finding], threshold: str) -> List[Finding]:
    order = {"minor": 0, "major": 1, "critical": 2}
    floor = order[threshold]
    return [f for f in findings if order[f.severity] >= floor]


if __name__ == "__main__":
    sys.exit(main())
