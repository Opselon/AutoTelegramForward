#!/usr/bin/env python3
"""Fail-closed gate: user-visible Dart copy that never reaches the mobile catalog.

The Flutter client is 1:1 across the 10 locales in ``mobile/lib/l10n/``, yet a
couple of dozen widgets still carry their labels inline (``Text('Retry')``,
``labelText: 'Task Name'``), so those screens render English for every user.
This gate fails the build on any *new* one, and tolerates today's debt through a
content-keyed baseline (``scripts/.mobile_ui_copy_baseline.txt``).

Scanned: ``mobile/lib/**/*.dart`` excluding ``mobile/lib/l10n/`` (the catalog is
translations, not copy). Flagged:

* ``Text('…')`` / ``Text("…")`` / ``const Text('…')``;
* string literals assigned to the display arguments ``labelText``, ``hintText``,
  ``tooltip``, ``label``, ``title``, ``semanticLabel`` (plus ``helperText`` and
  ``errorText``, same surface);
* interpolated literals count — ``'Sender: ${r.senderType}'`` is copy, because
  the static part is what the user reads.

Not flagged, with the reason:

* catalog lookups — ``Text(t.get('key'))`` passes no literal at all, and a
  literal that is a *key* never sits directly after ``Text(`` or ``label:``;
* technical tokens: URLs and hostnames (``https://atf.example.com``), file
  paths and routes, dates/UUIDs/hex colours, MIME types, the brand
  ``AutoTelegramForward``, format placeholders (``{source_title}``), field
  names (``filter_rule_id``), log levels/service names (``ERROR``, ``core``),
  single lowercase identifiers, and shell commands.

The matcher is deliberately a reader, not a bare regex: it walks Dart's quoting
(``'``/``"``, triple quotes, ``r'…'`` raw strings, ``\\`` escapes) so an
apostrophe inside prose (``'Doesn\\'t work'``) cannot desynchronize it.

No Flutter/Dart SDK and no third-party package is required: CI's python job has
neither.

Baseline format is shared with ``scripts/ui_copy_baseline.py`` (the reference
implementation): one ``path|normalized text`` entry per tolerated occurrence,
``#`` comments allowed, whitespace-normalized so inserting a line above an
offender does not redden CI.

Usage (from the repository root):
    python3 scripts/audit_mobile_ui_copy.py                  # report + exit 1 on new copy
    python3 scripts/audit_mobile_ui_copy.py --report         # also list baselined hits
    python3 scripts/audit_mobile_ui_copy.py --update-baseline
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ui_copy_baseline import (  # noqa: E402  (path set up above)
    Offender,
    default_baseline,
    run_gate,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIB = "mobile/lib"
L10N_DIR = "l10n"

# A literal directly after one of these is display copy. `Text(` is anchored on a
# word boundary, so `SelectableText(`/`RichText(` do not match; `t.get(` after the
# paren means the value is a catalog lookup, not a literal, and simply never matches.
DISPLAY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bText\s*\(\s*"), "Text()"),
    (
        re.compile(
            r"\b(?:labelText|hintText|helperText|errorText|tooltip|label|title|subtitle|"
            r"semanticLabel|message|description|emptyTitle|emptySubtitle|errorTitle|"
            r"errorMessage|successMessage|confirmLabel|cancelLabel|dialogTitle|"
            r"headerText|footerText)\s*:\s*"
        ),
        "display argument",
    ),
)

BRAND = "AutoTelegramForward"
INTERPOLATION = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
OTHER_SCRIPT = re.compile(r"[\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0750-\u077f\u4e00-\u9fff]")
HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

TECHNICAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:https?://|mailto:|tel:|www\.)", re.IGNORECASE),
    re.compile(r"^[a-z0-9_.\-+]+@[a-z0-9.\-]+\.[a-z]{2,}$", re.IGNORECASE),
    re.compile(r"^(?:application|text|image|audio|video|multipart)/[a-z0-9.+-]+$", re.IGNORECASE),
    re.compile(
        r"^(?:systemctl|sudo|docker|npm|npx|pip|python3?|pytest|git|curl|cd|ls|export|"
        r"flutter|dart|go)\s",
    ),
    re.compile(r"^[\d\s.,:%+\-/]+$"),
    re.compile(
        r"^(?:[\d.]+(?:px|rem|em|%|vh|vw|s|ms|fr)?|#[0-9a-f]{3,8}|rgba?\(|hsla?\(|"
        r"calc\(|var\(|url\()",
        re.IGNORECASE,
    ),
    re.compile(r"^/[A-Za-z0-9_{}./\-]*$"),  # route / absolute path
    re.compile(r"^[a-z0-9_{}.\-]+(?:/[a-z0-9_{}.\-]+)+$"),  # relative path
    re.compile(
        r"^[A-Za-z0-9_-]+\.(?:dart|tsx?|jsx?|json|ya?ml|md|py|go|sh|css|html|svg|png|jpe?g)$",
        re.IGNORECASE,
    ),
    re.compile(r"^\{[A-Za-z0-9_]+\}$"),  # {source_title}
    re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$"),  # identifier / slug / field name
    re.compile(r"^[A-Z][A-Z0-9_]*$"),  # log level / constant
    re.compile(r"^[A-Z][A-Z0-9]*(?:[-_/][A-Z0-9]+)+$"),  # AES-GCM
    re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*)+$"),  # camelCase identifier
    re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$"),  # dotted identifier
    re.compile(r"^[\w.$]+\([^)]*\)$"),  # code fragment
    re.compile(r"FFFFFFFF|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE),
)


def normalize(text: str) -> str:
    return " ".join(text.split())


def _strip_interpolation(text: str) -> str:
    return INTERPOLATION.sub(" ", text)


def _has_word_run(text: str) -> bool:
    if LATIN_WORD.search(text):
        return True
    if not OTHER_SCRIPT.search(text):
        return False
    return len(HAS_LETTER.findall(text)) >= 2


def is_technical(text: str) -> bool:
    """True when the literal is a token, not copy (each rule is documented above)."""
    raw = normalize(text)
    if not raw:
        return True
    if raw == BRAND:
        return True
    # Decoration in front of a token (`'❌ ERROR'`) is not prose.
    value = re.sub(r"^[^\w{]+", "", raw, flags=re.UNICODE)
    if not value:
        return True
    if not _has_word_run(value):
        return True
    if any(pattern.match(value) for pattern in TECHNICAL_PATTERNS):
        # A single capitalized word with no separator (`Save`, `Retry`) is copy;
        # a lowercase identifier (`core`, `atf_lang`) is not.
        bare = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
        if bare.match(value) and re.match(r"^[A-Z][a-z]+$", value) and " " not in value:
            return False
        return True
    return False


def is_copy(text: str) -> bool:
    """Copy = at least one readable word in the static part, and no technical rule."""
    static = _strip_interpolation(text)
    if not static.strip():
        return False
    # Judge the static segments individually: `BOT ${x}` is a token plus a value,
    # `${n} tasks` is a sentence.
    for segment in static.split("\n"):
        segment = segment.strip()
        if not segment:
            continue
        if _has_word_run(segment) and not is_technical(segment):
            return True
    return False


def _skip_interpolation(source: str, index: int) -> int:
    """Return the index just past the ``${…}`` starting at ``index`` (a ``$``)."""
    depth = 0
    cursor = index + 1
    while cursor < len(source):
        char = source[cursor]
        if char == "{":
            depth += 1
            cursor += 1
        elif char == "}":
            depth -= 1
            cursor += 1
            if depth == 0:
                return cursor
        elif char in "'\"":
            # `${t.get('key')}` — the inner quotes belong to the interpolation,
            # not to the string that contains it.
            nested = read_string(source, cursor)
            cursor = nested[1] if nested else cursor + 1
        else:
            cursor += 1
    return cursor


def read_string(source: str, start: int) -> tuple[str, int, bool] | None:
    """Read a Dart string literal at ``start``; return (content, end, raw)."""
    index = start
    raw = False
    if index < len(source) and source[index] in "rR":
        raw = True
        index += 1
    if index >= len(source) or source[index] not in "'\"":
        return None
    quote = source[index]
    triple = source.startswith(quote * 3, index)
    delimiter = quote * 3 if triple else quote
    index += len(delimiter)
    collected: list[str] = []
    while index < len(source):
        char = source[index]
        if char == "\\" and not raw:
            collected.append(source[index : index + 2])
            index += 2
            continue
        if char == "$" and not raw and source.startswith("${", index):
            end = _skip_interpolation(source, index)
            collected.append(source[index:end])
            index = end
            continue
        if source.startswith(delimiter, index):
            return "".join(collected), index + len(delimiter), raw
        if not triple and char == "\n":
            return None  # unterminated on this line: not a literal
        collected.append(char)
        index += 1
    return None


def literals_in_argument(source: str, start: int) -> list[tuple[int, str]]:
    """Literals at nesting depth 0 of the expression starting at ``start``.

    The region ends at the first top-level comma or closing bracket, so for
    ``Text(…)`` only the data argument is examined — ``style: TextStyle(
    fontFamily: 'Vazirmatn')`` stays out of it — and a nested ``t.get('key')``
    is at depth 1, i.e. never a candidate.
    """
    found: list[tuple[int, str]] = []
    depth = 0
    index = start
    while index < len(source):
        char = source[index]
        if char in "'\"" or (
            char in "rR" and index + 1 < len(source) and source[index + 1] in "'\""
        ):
            literal = read_string(source, index)
            if literal is None:
                index += 1
                continue
            content, end, _raw = literal
            if depth == 0:
                found.append((index, content))
            index = end
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return found
            depth -= 1
        elif char == "," and depth == 0:
            return found
        index += 1
    return found


def scan_source(path: str, source: str) -> list[Offender]:
    """Return every display literal in one Dart source file."""
    seen: set[int] = set()
    offenders: list[Offender] = []
    for pattern, _label in DISPLAY_PATTERNS:
        for match in pattern.finditer(source):
            for offset, content in literals_in_argument(source, match.end()):
                if offset in seen or not is_copy(content):
                    continue
                seen.add(offset)
                line = source.count("\n", 0, offset) + 1
                column = offset - (source.rfind("\n", 0, offset) + 1) + 1
                offenders.append(
                    Offender(path=path, line=line, text=normalize(content), column=column)
                )
    offenders.sort(key=lambda item: (item.line, item.column))
    return offenders


def scan_lib(lib: Path, root: Path) -> tuple[list[Offender], int]:
    """Scan every Dart file under ``lib`` except the ``l10n`` catalog directory."""
    offenders: list[Offender] = []
    files = sorted(
        path
        for path in lib.rglob("*.dart")
        if L10N_DIR not in path.relative_to(lib).parts
    )
    for file in files:
        source = file.read_text(encoding="utf-8", errors="replace")
        rel_path = file.relative_to(root).as_posix()
        offenders.extend(scan_source(rel_path, source))
    offenders.sort(key=lambda item: (item.path, item.line, item.column))
    return offenders, len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed gate for unlocalized Dart UI copy.")
    parser.add_argument("--root", default=str(ROOT), help="repository root (default: this repo)")
    parser.add_argument("--lib", default="", help=f"mobile library dir (default: <root>/{DEFAULT_LIB})")
    parser.add_argument("--baseline", default="", help="baseline file (default: scripts/.<surface>_…txt)")
    parser.add_argument("--update-baseline", action="store_true", help="rewrite the baseline (count printed)")
    parser.add_argument("--report", action="store_true", help="list every offender, baselined or not")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    lib = Path(args.lib).resolve() if args.lib else root / DEFAULT_LIB
    baseline = Path(args.baseline).resolve() if args.baseline else default_baseline(root, "mobile")
    if not lib.exists():
        print(f"::error::mobile library not found at {lib}")
        return 2

    offenders, file_count = scan_lib(lib, root)
    label = baseline.relative_to(root).as_posix() if baseline.is_relative_to(root) else str(baseline)
    print(f"mobile UI-copy gate: {file_count} Dart file(s) scanned under "
          f"{lib.relative_to(root).as_posix() if lib.is_relative_to(root) else lib} (l10n/ excluded)")
    return run_gate(offenders, baseline, label, args.update_baseline, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
