"""The `/replace add` fast form: find, replace and tasks on one line.

The legacy grammar (``/replace add [task] NAME -"OLD" -"NEW"``) keeps working;
this module is the second grammar for people who want the whole rule in one
message instead of the entry-per-task flow::

    /replace add "old text" -> "new text" [--task <id|name>] [--name LABEL]
                              [--priority N] [--regex] [-i] [-w] [--delete]

Everything here is presentation parsing: the values come back raw and
:class:`core.application.replace_rules.ReplaceRuleService` does the one job of
deciding whether the rule may exist (tasks resolved out of the caller's own
owner-scoped list, so an id can never reach another tenant). Deleting is the
empty replacement the rest of the UI uses — `--delete` spells it explicitly
because an empty `to` typed by accident is a common mistake.

Ambiguity is refused, never guessed: missing `--task` with more than one task
answers "which task?" via its own code instead of creating the rule somewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: The arrow is what tells the two grammars apart; the legacy form
#: (`add [task] NAME -"OLD" -"NEW" --regex`) has the same flags but no arrow.
ARROW_RE = re.compile(r"(?:->|=>)")
LEGACY_ENTRY_RE = re.compile(r"""(?:^|\s)-["'`]""")

#: Stable refusal codes; the command maps each to a catalog key.
EMPTY = "empty"
MISSING_ARROW = "missing_arrow"
MISSING_SOURCE = "missing_source"
MISSING_TASK = "missing_task"
BAD_PRIORITY = "bad_priority"
BAD_FLAG = "bad_flag"

#: A flag starts at a token boundary and ends before the next one. Scanning for
#: *all* flags (rather than peeling one tail regex) is what lets an unquoted
#: value stay one word: `--task task-a --name usdt` must not read
#: "task-a --name usdt" as the task.
FLAG_SCAN = re.compile(r"(?<!\S)(--(?:task|name|priority)|--regex|--delete|-[iwc])(?=\s|\Z)")

QUOTED = re.compile(r"""^([\"'`])(?P<q>.+)\1$""", re.DOTALL)


@dataclass(frozen=True)
class FastReplaceAdd:
    """A parsed fast-form body. Raw values; validation is the service's job."""

    source: str
    to: str
    mode: Optional[str] = None
    flags: Tuple[str, ...] = ()
    name: str = ""
    task: str = ""
    priority: Optional[int] = None


class FastReplaceAddError(ValueError):
    """The fast form did not parse; `code` maps to a catalog key."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _unquote(text: str) -> str:
    match = QUOTED.match(text.strip())
    return text.strip() if match is None else match.group("q").strip()


def is_arrow_form(rest: str) -> bool:
    """True when the line is the arrow grammar, not the legacy entry grammar.

    Flags alone cannot decide this: `--regex -i -w` is valid in both forms, so
    only the arrow is a safe discriminator. A legacy entry marker (`-"`) before
    the arrow means the user is writing per-task entries whose text happens to
    contain an arrow.
    """
    text = (rest or "").strip()
    arrow = ARROW_RE.search(text)
    if arrow is None:
        return False
    legacy = LEGACY_ENTRY_RE.search(text)
    return legacy is None or legacy.start() > arrow.start()


def parse_replace_fast_add(raw: str) -> FastReplaceAdd:
    """Parse the line, flags last-to-first so values may contain words."""
    text = (raw or "").strip()
    arrow = ARROW_RE.search(text)
    if arrow is None:
        raise FastReplaceAddError(MISSING_ARROW, text[:40])
    head, rest = text[:arrow.start()], text[arrow.end():]

    mode: Optional[str] = None
    delete: bool = False
    flags: List[str] = []
    name = ""
    task = ""
    priority: Optional[int] = None

    marks = list(FLAG_SCAN.finditer(rest))
    body = rest[:marks[0].start()] if marks else rest

    for index, mark in enumerate(marks):
        stop = marks[index + 1].start() if index + 1 < len(marks) else len(rest)
        flag = mark.group(1)
        value = rest[mark.end():stop].strip().strip("\"'`")
        if flag == "--regex":
            mode = "regex"
        elif flag == "--delete":
            delete = True
        elif flag in ("-i", "-w", "-c"):
            flags = flags + [flag[1]]
        elif flag == "--name":
            if not value:
                raise FastReplaceAddError(EMPTY, "--name")
            name = value[:40]
        elif flag == "--task":
            if not value:
                raise FastReplaceAddError(EMPTY, "--task")
            task = value
        elif flag == "--priority":
            try:
                priority = int(value)
            except ValueError:
                raise FastReplaceAddError(BAD_PRIORITY, value) from None
        else:  # pragma: no cover - FLAG_SCAN only yields known flags
            raise FastReplaceAddError(BAD_FLAG, flag)

    to = _unquote(body.strip()[:2000])
    if delete:
        to = to or ""
    source = _unquote(head.strip())
    if not source:
        raise FastReplaceAddError(MISSING_SOURCE, text[:40])
    return FastReplaceAdd(
        source=source,
        to=to,
        mode=mode,
        flags=tuple(flags),
        name=name,
        task=task,
        priority=priority,
    )
