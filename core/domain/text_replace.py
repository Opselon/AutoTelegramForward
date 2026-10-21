"""Per-task text replacements with options: literal or regex, case, whole word.

Why this module exists
----------------------
A task's ``metadata["replacements"]`` used to be a flat ``{old: new}`` map and
``FilterEngine.replace_text`` did ``str.replace`` (with a case-insensitive
fallback). That covers "discount 10% -> discount 50%" and nothing else: no
regular expressions, no way to say "only this exact case", no way to say "the
whole word, not a substring of a longer one", and no way to name an entry so a
text command can address it later.

The storage shape is deliberately backward compatible:

* ``"old": "new"``                — a plain literal entry (legacy rows keep working,
                                    including the smart-case fallback: an exact
                                    match wins, otherwise a case-insensitive pass).
* ``"old": {"to": "new", ...}``   — an entry with options. ``mode`` is
                                    ``literal`` or ``regex``, ``flags`` is a
                                    subset of ``imsxcw`` and ``name`` is an
                                    optional human label.

Everything that reads or writes replacements goes through this module, so the
engine, the bot panel, the text commands and the dashboard cannot disagree
about what a stored entry means.

Safety: a user-typed pattern is compiled through the same guarded compiler the
content policy uses (length cap, nested-quantifier refusal). An unusable
pattern is *skipped* — it never corrupts or drops the message it was meant to
edit.
"""

from __future__ import annotations

import logging
import hashlib
import re
from dataclasses import dataclass, replace as dataclass_replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core.domain.content_policy import compile_user_pattern

logger = logging.getLogger("atf.domain.text_replace")

MODE_LITERAL = "literal"
MODE_REGEX = "regex"
MODES: Tuple[str, ...] = (MODE_LITERAL, MODE_REGEX)

#: Flags a user may attach to an entry.
#:   i = ignore case, c = force case-sensitive literal (opt out of smart case),
#:   w = whole word, m/s/x = regex flags (multiline/dotall/verbose).
FLAG_CHARS = "icwmsx"
_REGEX_ONLY_FLAGS = "msx"

#: Bounds. A replacement source is a word or a short pattern, not a program.
MAX_ENTRIES = 100
MAX_PATTERN_LEN = 300
MAX_REPLACEMENT_LEN = 2000
MAX_NAME_LEN = 40

#: Execution order. Every entry carries one; a higher number runs first, the
#: same direction as `ForwardRule.priority`, so a user who understands one
#: number understands both. The default is the value every entry written before
#: this field existed behaves as: same priority, therefore insertion order.
DEFAULT_PRIORITY = 100
MIN_PRIORITY = 0
MAX_PRIORITY = 1000

#: Delimiters accepted in the free-text form (the panel's "old -> new" input).
DELIMITERS: Tuple[str, ...] = ("->", "=>", "➔", "—>", "→")
#: ":" is accepted for literal entries only: a regex may legitimately contain
#: one (``(?:...)``), so splitting a pattern on the first colon would corrupt it.
_LITERAL_EXTRA_DELIMITERS: Tuple[str, ...] = (":",)


def error_key(code: str) -> str:
    """Catalog key explaining a rejected entry, from its stable code."""
    return _ERROR_KEYS.get(code, "ui_rep_err_unknown")


class ReplaceError(ValueError):
    """A rejected replacement entry, carrying a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


#: ReplaceError code -> catalog key that explains it to a user. Defined next to
#: the codes themselves so a new refusal cannot be added without an explanation.
_ERROR_KEYS = {
    "empty_source": "ui_rep_err_empty_source",
    "pattern_too_long": "ui_rep_err_pattern_too_long",
    "replacement_too_long": "ui_rep_err_replacement_too_long",
    "invalid_regex": "ui_rep_err_invalid_regex",
    "too_many_entries": "ui_rep_err_too_many",
    "unterminated_quote": "ui_rep_err_unterminated_quote",
    "unknown_option": "ui_rep_err_unknown_option",
    "missing_source": "ui_rep_err_missing_source",
    "missing_delimiter": "ui_rep_err_missing_delimiter",
    "missing_flags": "ui_rep_err_unknown_option",
    "name_too_long": "ui_rep_err_name_too_long",
    "unknown_mode": "ui_rep_err_invalid_regex",
    "priority_out_of_range": "ui_rep_err_priority",
}


@dataclass(frozen=True)
class ReplaceEntry:
    """One find/replace pair, with the options that decide how it matches."""

    source: str
    to: str = ""
    mode: str = MODE_LITERAL
    flags: str = ""
    whole_word: bool = False
    name: str = ""
    #: Whether the entry runs at all. A disabled rule stays stored (so it can be
    #: switched back on) but the engine skips it.
    enabled: bool = True
    #: Higher runs first; equal priorities keep insertion order.
    priority: int = DEFAULT_PRIORITY
    #: Stable identifier of the *rule* this entry is a copy of. A rule assigned
    #: to three tasks is three entries sharing one id, which is what lets the
    #: manager show and edit it as one row. Empty on entries written before the
    #: manager existed; those are addressed by their source text.
    rid: str = ""

    # -- derived -------------------------------------------------------- #
    @property
    def is_regex(self) -> bool:
        return self.mode == MODE_REGEX

    @property
    def ignore_case(self) -> bool:
        return "i" in self.flags

    @property
    def case_sensitive(self) -> bool:
        return "c" in self.flags

    @property
    def is_plain(self) -> bool:
        """True when the entry needs no options at all (legacy storage shape)."""
        return (
            self.mode == MODE_LITERAL
            and not self.flags
            and not self.whole_word
            and not self.name
            and self.enabled
            and self.priority == DEFAULT_PRIORITY
        )

    # -- storage -------------------------------------------------------- #
    def to_value(self) -> Any:
        """The JSON-safe value to store under ``metadata["replacements"]``."""
        if self.is_plain:
            return self.to
        out: Dict[str, Any] = {"to": self.to}
        if self.mode != MODE_LITERAL:
            out["mode"] = self.mode
        if self.flags:
            out["flags"] = self.flags
        if self.whole_word:
            out["whole_word"] = True
        if self.name:
            out["name"] = self.name
        if not self.enabled:
            out["enabled"] = False
        if self.priority != DEFAULT_PRIORITY:
            out["priority"] = self.priority
        return out

    def to_rule_value(self, rid: Optional[str] = None) -> Dict[str, Any]:
        """The value stored when the entry is keyed by its rule id.

        The source travels in the value because the map key is no longer the
        text to find: two rules may legitimately search for the same string with
        different replacements, and one of them being renamed must not move.
        """
        out: Dict[str, Any] = {
            "id": str(rid or self.rid or ""),
            "source": self.source,
            "to": self.to,
        }
        if self.mode != MODE_LITERAL:
            out["mode"] = self.mode
        if self.flags:
            out["flags"] = self.flags
        if self.whole_word:
            out["whole_word"] = True
        if self.name:
            out["name"] = self.name
        if not self.enabled:
            out["enabled"] = False
        if self.priority != DEFAULT_PRIORITY:
            out["priority"] = self.priority
        return out

    # -- display -------------------------------------------------------- #
    def badge(self) -> str:
        """Short technical marker for a list line: ``re``, ``re/im``, ``lit``."""
        if self.is_regex:
            flags = "".join(ch for ch in self.flags if ch in FLAG_CHARS)
            return "re/" + flags if flags else "re"
        flags = "".join(ch for ch in self.flags if ch in FLAG_CHARS)
        return "lit/" + flags if flags else "lit"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def clamp_priority(value: Any) -> int:
    """A priority as stored: an int inside ``MIN_PRIORITY..MAX_PRIORITY``.

    Clamped on read as well as on write, so a hand-edited row, a float from
    JSON or a string from a form can never make the engine sort on a value the
    UI cannot represent (or raise while sorting a message).
    """
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY
    return max(MIN_PRIORITY, min(MAX_PRIORITY, number))


def clean_flags(raw: Any, mode: str = MODE_LITERAL) -> str:
    """Normalise a flag blob ("i w", "iw", ["i", "w"]) into a canonical string."""
    if isinstance(raw, (list, tuple, set)):
        chars = "".join(str(x) for x in raw)
    else:
        chars = str(raw or "")
    keep = {ch for ch in chars.lower() if ch in FLAG_CHARS}
    if mode != MODE_REGEX:
        keep -= set(_REGEX_ONLY_FLAGS)
    if "c" in keep:
        # "c" and "i" are opposites; an explicit case-sensitive request wins.
        keep.discard("i")
    return "".join(sorted(keep))


def entry_from_value(source: Any, value: Any) -> ReplaceEntry:
    """Coerce one stored pair into a :class:`ReplaceEntry`.

    Accepts the legacy string value, the option dict, and the dict spellings
    written by older dashboards (``{"from": ..., "to": ...}``,
    ``{"replacement": ...}``). Never raises: an unreadable value becomes an
    empty replacement, which is what "delete this word" means anyway.

    A dict may carry its own ``source``: entries written by the replace-rule
    manager are keyed by rule id, so the key is not the text to find.
    """
    src = "" if source is None else str(source)
    if isinstance(value, Mapping):
        mode = str(value.get("mode") or MODE_LITERAL).strip().lower()
        if mode not in MODES:
            mode = MODE_LITERAL
        to = value.get("to")
        if to is None:
            to = value.get("replacement", value.get("with", ""))
        flags = clean_flags(value.get("flags", ""), mode)
        if not flags and _as_bool(value.get("ignore_case")):
            flags = clean_flags("i", mode)
        whole = _as_bool(value.get("whole_word", value.get("word", False))) or ("w" in flags)
        name = str(value.get("name") or "").strip()
        stored_source = value.get("source")
        if stored_source is not None:
            src = str(stored_source)
        return ReplaceEntry(
            source=src, to="" if to is None else str(to), mode=mode,
            flags=flags, whole_word=whole, name=name[:MAX_NAME_LEN],
            enabled=_as_bool(value.get("enabled", True)),
            priority=clamp_priority(value.get("priority", DEFAULT_PRIORITY)),
            rid=str(value.get("id") or value.get("rid") or "").strip()[:64],
        )
    return ReplaceEntry(source=src, to="" if value is None else str(value))


def validate_entry(entry: ReplaceEntry) -> None:
    """Raise :class:`ReplaceError` when the entry cannot be stored/applied."""
    if not entry.source.strip():
        raise ReplaceError("empty_source", "replacement source is empty")
    if len(entry.source) > MAX_PATTERN_LEN:
        raise ReplaceError("pattern_too_long", f"source exceeds {MAX_PATTERN_LEN} chars")
    if len(entry.to) > MAX_REPLACEMENT_LEN:
        raise ReplaceError("replacement_too_long", f"replacement exceeds {MAX_REPLACEMENT_LEN} chars")
    if len(entry.name) > MAX_NAME_LEN:
        raise ReplaceError("name_too_long", f"name exceeds {MAX_NAME_LEN} chars")
    if entry.mode not in MODES:
        raise ReplaceError("unknown_mode", f"mode '{entry.mode}' is not supported")
    if not MIN_PRIORITY <= int(entry.priority) <= MAX_PRIORITY:
        raise ReplaceError("priority_out_of_range", "priority is out of range")
    if entry.is_regex:
        # Compile once here so a bad pattern is refused at the point the user
        # typed it, not silently at delivery time.
        if compile_entry(entry) is None:
            raise ReplaceError("invalid_regex", "the pattern is not a usable regular expression")


def normalize_map(raw: Any, *, strict: bool = False) -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    """Canonicalise a replacements map.

    Returns ``(mapping, problems)`` where ``problems`` is a list of
    ``(source, code)`` pairs. With ``strict`` a problem raises; otherwise the
    offending entry is kept as-is (so an older, hand-written rule is never
    silently rewritten by a read path).
    """
    if not isinstance(raw, Mapping):
        return {}, []
    out: Dict[str, Any] = {}
    problems: List[Tuple[str, str]] = []
    for idx, (source, value) in enumerate(raw.items()):
        entry = entry_from_value(source, value)
        if idx >= MAX_ENTRIES:
            problems.append((entry.source, "too_many_entries"))
            break
        try:
            validate_entry(entry)
        except ReplaceError as exc:
            problems.append((entry.source, exc.code))
            if strict:
                raise
            out[entry.source] = value
            continue
        out[entry.source] = entry.to_value()
    return out, problems


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def _pattern_body(entry: ReplaceEntry) -> str:
    body = entry.source if entry.is_regex else re.escape(entry.source)
    if entry.whole_word:
        # An anchored pattern already states its own boundary; wrapping it in a
        # word lookaround would make ``^promo\d+$`` unmatchable.
        left = "" if body.startswith("^") else r"(?<!\w)"
        right = "" if body.endswith("$") else r"(?!\w)"
        body = f"{left}(?:{body}){right}"
    return body


def compile_entry(entry: ReplaceEntry) -> Optional[re.Pattern]:
    """Compile an entry, or ``None`` when the guarded compiler refuses it."""
    flags = 0
    if entry.ignore_case:
        flags |= re.IGNORECASE
    if entry.is_regex:
        if "m" in entry.flags:
            flags |= re.MULTILINE
        if "s" in entry.flags:
            flags |= re.DOTALL
        if "x" in entry.flags:
            flags |= re.VERBOSE
    return compile_user_pattern(_pattern_body(entry), flags)


def _legacy_literal(entry: ReplaceEntry) -> bool:
    """The original behaviour, preserved bit-for-bit for untouched entries."""
    return (
        entry.mode == MODE_LITERAL
        and not entry.flags
        and not entry.whole_word
    )


def apply_entry(text: str, entry: ReplaceEntry) -> str:
    """Apply one entry. An unusable pattern leaves the text unchanged."""
    if not text or not entry.source.strip():
        return text
    try:
        if _legacy_literal(entry):
            # Exact case first; only when the exact form is absent does the
            # case-insensitive pass run. This is what every existing rule was
            # written against, so it must not change.
            if entry.source in text:
                return text.replace(entry.source, entry.to)
            return re.sub(re.escape(entry.source), _literal_replacement(entry), text,
                          flags=re.IGNORECASE)
        pattern = compile_entry(entry)
        if pattern is None:
            logger.warning(
                "[PIPELINE:REPLACE] refused unusable pattern (mode=%s len=%d) — entry skipped",
                entry.mode, len(entry.source),
            )
            return text
        return pattern.sub(_literal_replacement(entry), text)
    except re.error as exc:
        # A replacement template such as "\9" or an unclosed group: the text is
        # the user's message, not our debugging surface — leave it intact.
        logger.warning("[PIPELINE:REPLACE] entry failed (%s) — text left unchanged", exc)
        return text


def _literal_replacement(entry: ReplaceEntry) -> str:
    """The replacement string handed to ``re.sub``.

    A regex entry may legitimately use ``\\1`` back-references, so its value is
    passed through. A literal entry's replacement is text: a stray ``\\1`` must
    render as ``\\1`` instead of raising or swallowing a group.
    """
    if entry.is_regex:
        return entry.to
    return entry.to.replace("\\", "\\\\")



def apply_replacements(text: str, replacements: Any) -> str:
    """Apply every enabled entry, highest priority first.

    Order matters: ``# -> ##`` followed by ``## -> x`` produces a different
    message than the reverse, so the user gets an explicit priority number
    instead of depending on dict insertion order they cannot see. Entries that
    share a priority keep the order they were written in (a stable sort), which
    is exactly what every rule written before priorities existed relied on.
    """
    if not text or not isinstance(replacements, Mapping) or not replacements:
        return text
    entries: List[ReplaceEntry] = []
    for source, value in replacements.items():
        entry = entry_from_value(source, value)
        if not entry.source.strip() or not entry.enabled:
            continue
        entries.append(entry)
    # Stable: equal priorities keep insertion order.
    entries.sort(key=lambda item: -item.priority)
    out = text
    for entry in entries:
        out = apply_entry(out, entry)
    return out


def stored_entries(replacements: Any, task_id: str = "") -> List[Tuple[str, ReplaceEntry]]:
    """``(rule_id, entry)`` for every stored pair, in map order.

    A rule id is the stable handle the manager edits by. Entries written before
    the manager existed carry none, so one is derived from the task and the
    source text: deterministic (the same row always maps to the same id) and
    local to that task (two tasks that happen to replace the same word stay two
    independent rules until someone edits them through the manager).
    """
    if not isinstance(replacements, Mapping):
        return []
    out: List[Tuple[str, ReplaceEntry]] = []
    for key, value in replacements.items():
        entry = entry_from_value(key, value)
        rid = entry.rid or legacy_rule_id(task_id, str(key))
        out.append((rid, entry))
    return out


def legacy_rule_id(task_id: str, key: str) -> str:
    """A stable, URL-safe id for an entry that predates rule ids."""
    digest = hashlib.sha1(f"{task_id}\x00{key}".encode("utf-8")).hexdigest()
    return "l" + digest[:15]


# --------------------------------------------------------------------------- #
# Lookup helpers (text commands address entries by name or by source text)
# --------------------------------------------------------------------------- #


def find_key(replacements: Any, needle: str) -> Optional[str]:
    """The stored key for ``needle`` — a name first, then the source text."""
    if not isinstance(replacements, Mapping) or not needle:
        return None
    wanted = needle.strip()
    if not wanted:
        return None
    for source, value in replacements.items():
        entry = entry_from_value(source, value)
        if entry.name and entry.name.casefold() == wanted.casefold():
            return str(source)
    for source in replacements:
        if str(source) == wanted:
            return str(source)
    for source in replacements:
        if str(source).casefold() == wanted.casefold():
            return str(source)
    return None


def describe_entry(entry: ReplaceEntry, deleted_label: str = "") -> str:
    """One human line for the panel: ``1. [re/i] `pat` ➔ `out` (name)``."""
    marker = entry.badge()
    target = entry.to if entry.to else deleted_label
    label = f" — {entry.name}" if entry.name else ""
    return f"[{marker}] `{entry.source}` ➔ `{target}`{label}"


# --------------------------------------------------------------------------- #
# Parsing: the panel's free-text form
# --------------------------------------------------------------------------- #


def _split_flags(blob: str) -> Tuple[str, str]:
    """Split a trailing ``| flags`` block from a right-hand side."""
    for sep in (" | ", "|", " #"):
        if sep in blob:
            head, _, tail = blob.rpartition(sep)
            candidate = tail.strip()
            if candidate and all(ch.lower() in FLAG_CHARS for ch in candidate):
                return head.strip(), clean_flags(candidate)
    return blob.strip(), ""


def _split_pair(line: str, mode: str) -> Optional[Tuple[str, str]]:
    delims: Iterable[str] = DELIMITERS
    if mode == MODE_LITERAL:
        delims = tuple(DELIMITERS) + _LITERAL_EXTRA_DELIMITERS
    for delim in delims:
        if delim in line:
            head, _, tail = line.partition(delim)
            return head.strip(), tail.strip()
    parts = line.split(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    if len(parts) == 1:
        return parts[0].strip(), ""
    return None


def parse_freeform(text: str) -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    """Parse the panel's multi-line input into a replacements map.

    Accepted lines (one entry per line)::

        old word -> new word
        discount 10% -> discount 50%
        deleted word ->                     (empty right side deletes the word)
        re:^promo\\s*\\d+$ -> OFFER | i      (regex, ignore case)
        re:word -> WORD | w                 (regex, whole word only)

    Returns ``(mapping, problems)`` with ``problems`` as ``(line, code)``.
    """
    mapping: Dict[str, Any] = {}
    problems: List[Tuple[str, str]] = []
    if not isinstance(text, str):
        return mapping, problems
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        mode = MODE_LITERAL
        if line[:3].lower() == "re:":
            mode = MODE_REGEX
            line = line[3:].strip()
        elif line[:3].lower() == "lit":
            line = line[3:].lstrip(":").strip()
        pair = _split_pair(line, mode)
        if not pair:
            problems.append((raw_line.strip(), "missing_delimiter"))
            continue
        source, right = pair
        right, flags = _split_flags(right)
        if mode == MODE_REGEX and len(source) > 1 and source.startswith("/") and source.endswith("/"):
            source = source[1:-1]
        entry = ReplaceEntry(source=source, to=right, mode=mode, flags=flags,
                             whole_word=("w" in flags))
        entry = dataclass_replace(entry, flags=clean_flags(flags, mode))
        try:
            validate_entry(entry)
        except ReplaceError as exc:
            problems.append((source, exc.code))
            continue
        mapping[entry.source] = entry.to_value()
    return mapping, problems


# --------------------------------------------------------------------------- #
# Parsing: the text command
# --------------------------------------------------------------------------- #

_OPTION_ALIASES = {
    "--regex": "regex",
    "-r": "regex",
    "--literal": "literal",
    "-l": "literal",
    "--ignore-case": "i",
    "-i": "i",
    "--case-sensitive": "c",
    "-c": "c",
    "--whole-word": "w",
    "-w": "w",
}

def tokenize(raw: str) -> List[str]:
    """Split a command tail into tokens, honouring quotes and ``-"text"``.

    The documented form is ``/"old text"`` — a dash in front of the quote is
    accepted (and dropped) because that is how the command was specified:
    ``/replace add name -"old" -"new"``.
    """
    tokens: List[str] = []
    buf: List[str] = []
    have = False
    i, n = 0, len(raw or "")
    while i < n:
        ch = raw[i]
        if ch in " \t\r\n":
            if have:
                tokens.append("".join(buf))
                buf, have = [], False
            i += 1
            continue
        quoted = ch in "\"'"
        if ch == "-" and i + 1 < n and raw[i + 1] in "\"'":
            quoted = True
            i += 1
            ch = raw[i]
        if quoted:
            quote = ch
            i += 1
            have = True
            closed = False
            while i < n:
                c = raw[i]
                if c == "\\" and i + 1 < n and raw[i + 1] in (quote, "\\"):
                    buf.append(raw[i + 1])
                    i += 2
                    continue
                if c == quote:
                    closed = True
                    i += 1
                    break
                buf.append(c)
                i += 1
            if not closed:
                raise ReplaceError("unterminated_quote", "a quote was opened but never closed")
            continue
        buf.append(ch)
        have = True
        i += 1
    if have:
        tokens.append("".join(buf))
    return tokens


def parse_add_arguments(raw: str) -> Tuple[ReplaceEntry, bool]:
    """Parse ``NAME -"OLD" -"NEW" [--regex] [-i] [-w] [-c] [--priority=N]``.

    Returns ``(entry, named)``. The name is optional: with two positionals they
    are OLD and NEW, with one positional it is OLD and the replacement is empty
    (delete the word).
    """
    tokens = tokenize(raw)
    positionals: List[str] = []
    mode = MODE_LITERAL
    flags = ""
    name_opt = ""
    delete_opt = False
    priority_opt: Optional[int] = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        low = token.lower()
        if low in ("-n", "--name"):
            if i + 1 >= len(tokens):
                raise ReplaceError("missing_source", "no name after --name")
            name_opt = tokens[i + 1].strip()[:MAX_NAME_LEN]
            i += 2
            continue
        if low.startswith("--name="):
            name_opt = token.split("=", 1)[1].strip()[:MAX_NAME_LEN]
            i += 1
            continue
        if low in ("-d", "--delete"):
            # Explicit delete: the replacement is empty even if one was typed.
            delete_opt = True
            i += 1
            continue
        if low.startswith("--flags="):
            flags = clean_flags(flags + token.split("=", 1)[1], mode)
            i += 1
            continue
        if low == "--flags":
            raise ReplaceError("missing_flags", "use --flags=imsx")
        if low.startswith("--priority="):
            priority_opt = clamp_priority(token.split("=", 1)[1])
            i += 1
            continue
        if low in ("-p", "--priority"):
            if i + 1 >= len(tokens):
                raise ReplaceError("priority_out_of_range", "no number after --priority")
            priority_opt = clamp_priority(tokens[i + 1])
            i += 2
            continue
        action = _OPTION_ALIASES.get(low)
        if action in MODES:
            mode = action
            i += 1
            continue
        if action:
            flags += action
            i += 1
            continue
        if low.startswith("--"):
            raise ReplaceError("unknown_option", f"unknown option '{token}'")
        positionals.append(token)
        i += 1

    if not positionals:
        raise ReplaceError("missing_source", "no source text given")
    name = name_opt
    if len(positionals) >= 3:
        name = name or positionals[0]
        source, target = positionals[1], positionals[2]
    elif len(positionals) == 2:
        source, target = positionals
    else:
        source, target = positionals[0], ""
    if delete_opt:
        target = ""

    flags = clean_flags(flags, mode)
    entry = ReplaceEntry(
        source=source, to=target, mode=mode,
        flags=flags, whole_word=("w" in flags), name=name.strip()[:MAX_NAME_LEN],
        priority=DEFAULT_PRIORITY if priority_opt is None else priority_opt,
    )
    validate_entry(entry)
    return entry, bool(name.strip())


def count_matches(text: str, entry: ReplaceEntry) -> int:
    """How many places ``entry`` would change in ``text`` (for a test preview).

    Counted with the same rules ``apply_entry`` uses, including the legacy
    literal's smart case (an exact-case occurrence wins, otherwise a
    case-insensitive pass), so the number a preview shows is the number of
    replacements that will actually happen.
    """
    if not text or not entry.source.strip():
        return 0
    try:
        if _legacy_literal(entry):
            if entry.source in text:
                return text.count(entry.source)
            return len(re.findall(re.escape(entry.source), text, flags=re.IGNORECASE))
        pattern = compile_entry(entry)
        if pattern is None:
            return 0
        return sum(1 for _ in pattern.finditer(text))
    except re.error:
        return 0


def entry_with(mapping: Mapping[str, Any], key: str, **changes: Any) -> ReplaceEntry:
    """Read an entry back, apply field changes, validate and return it.

    ``whole_word`` is derived from the ``w`` flag so the two can never disagree
    — the stored dict writes both, and a toggle that changed only one of them
    would render an option as "on" while the engine matched as if it were off.
    """
    entry = entry_from_value(key, mapping.get(key))
    updated = dataclass_replace(entry, **changes)
    updated = dataclass_replace(updated, flags=clean_flags(updated.flags, updated.mode))
    if "whole_word" not in changes:
        updated = dataclass_replace(updated, whole_word=("w" in updated.flags))
    if updated.whole_word and "w" not in updated.flags:
        updated = dataclass_replace(updated, flags=clean_flags(updated.flags + "w", updated.mode))
    validate_entry(updated)
    return updated


def merge_entry(mapping: Any, entry: ReplaceEntry) -> Dict[str, Any]:
    """Return a new map with ``entry`` merged in, enforcing the entry bound.

    A name that already exists replaces that entry's target instead of creating
    a second, identically named row — otherwise ``/replace add promo`` twice
    leaves the user with two "promo" rows and no way to tell them apart.
    """
    current: Dict[str, Any] = dict(mapping) if isinstance(mapping, Mapping) else {}
    validate_entry(entry)
    if entry.name:
        existing = find_key(current, entry.name)
        if existing is not None and existing != entry.source:
            current.pop(existing, None)
    if entry.source not in current and len(current) >= MAX_ENTRIES:
        raise ReplaceError("too_many_entries", f"a task holds at most {MAX_ENTRIES} replacements")
    current[entry.source] = entry.to_value()
    return current
