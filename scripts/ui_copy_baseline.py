#!/usr/bin/env python3
"""Content-keyed baselines shared by the UI-copy gates.

Why content and not line numbers
--------------------------------
The web and mobile UI-copy gates (``audit_web_ui_copy.js`` and
``audit_mobile_ui_copy.py``) both had to start green on a tree that still holds
hundreds of unlocalized literals, without pretending the debt is gone. A
``file:line`` allowlist does not survive a single inserted import: every entry
below the edit shifts by one and CI turns red for a change that introduced no
copy at all. A baseline of ``path|normalized text`` survives reformatting, line
insertion and file reordering; it fails only when the *text* is new.

Format
------
    # comment lines are ignored (and preserved by nothing: --update rewrites)
    web/frontend/src/App.tsx|Save changes
    mobile/lib/widgets/list_state_view.dart|Retry

* one entry per *occurrence* (a second ``Text('Retry')`` in the same file is
  new debt and must fail, so the baseline is a multiset, not a set);
* the text is whitespace-normalized (``" ".join(text.split())``) so a JSX node
  re-indented by prettier is the same entry;
* paths are repository-relative POSIX paths.

Staleness is reported, never enforced: when fewer offenders occur than the
baseline pins, the gate prints a "backlog shrank" notice so the baseline gets
regenerated, but stays green — a cleanup PR must not have to regenerate a
baseline in the same commit.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

BASELINE_STEM = "ui_copy_baseline"
HEADER = (
    "# Content-keyed baseline for the UI-copy gate: one 'path|normalized text'\n"
    "# entry per currently tolerated offender. Regenerate with --update-baseline.\n"
    "# Never hand-edit an entry to silence a new string: localize it instead.\n"
)


def normalize(text: str) -> str:
    """Collapse whitespace so re-indentation/reflow never changes an entry."""
    return " ".join(text.split())


@dataclass(frozen=True)
class Offender:
    """One unlocalized UI string found by a gate."""

    path: str
    line: int
    text: str
    column: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, normalize(self.text))

    def entry(self) -> str:
        return f"{self.path}|{normalize(self.text)}"

    def render(self) -> str:
        location = f"{self.path}:{self.line}"
        if self.column:
            location += f":{self.column}"
        return f"{location}: {normalize(self.text)}"


@dataclass
class BaselineResult:
    """Outcome of matching the current offenders against the pinned baseline."""

    pinned: int = 0
    matched: int = 0
    unmatched: list[Offender] = field(default_factory=list)
    unused: list[str] = field(default_factory=list)

    @property
    def shrank(self) -> bool:
        return bool(self.unused)

    @property
    def ok(self) -> bool:
        return not self.unmatched


def parse_baseline(source: str) -> list[str]:
    """Return the entry lines of a baseline file, ignoring comments/blanks."""
    entries: list[str] = []
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            # A malformed line would silently allow nothing; treat it as an
            # entry anyway so the count stays honest and the gate reports the
            # offender instead of trusting a broken pin.
            entries.append(line)
            continue
        path, _, text = line.partition("|")
        entries.append(f"{path.strip()}|{normalize(text)}")
    return entries


def read_baseline(path: Path) -> list[str]:
    if not path.exists():
        return []
    return parse_baseline(path.read_text(encoding="utf-8"))


def write_baseline(path: Path, entries: list[str]) -> int:
    """Rewrite the baseline (sorted, deduplicated per occurrence) and return its size."""
    ordered = sorted(entries)
    body = "\n".join(ordered)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{HEADER}{body}\n" if body else HEADER, encoding="utf-8")
    return len(ordered)


def evaluate(offenders: list[Offender], entries: list[str]) -> BaselineResult:
    """Match offenders against baseline entries as a multiset of content keys."""
    pinned = Counter(entries)
    result = BaselineResult(pinned=len(entries))
    remaining = Counter(pinned)
    for offender in offenders:
        key = f"{offender.path}|{normalize(offender.text)}"
        if remaining[key] > 0:
            remaining[key] -= 1
            result.matched += 1
        else:
            result.unmatched.append(offender)
    for entry, count in sorted(remaining.items()):
        result.unused.extend([entry] * count)
    return result


def shrank_notice(result: BaselineResult, baseline_label: str) -> str:
    return (
        f"note: backlog shrank — regenerate the baseline: {len(result.unused)} of "
        f"{result.pinned} pinned offender(s) in {baseline_label} no longer occur "
        f"({result.matched} still present). Regenerate with --update-baseline "
        "in a follow-up commit."
    )


def run_gate(
    offenders: list[Offender],
    baseline_path: Path,
    baseline_label: str,
    update: bool,
    report: bool = False,
) -> int:
    """Print the report and return the process exit code (fail-closed)."""
    if update:
        size = write_baseline(baseline_path, [offender.entry() for offender in offenders])
        print(f"baseline updated: {size} offender(s) pinned in {baseline_label}")
        return 0

    entries = read_baseline(baseline_path)
    result = evaluate(offenders, entries)
    if report:
        for offender in offenders:
            print(f"  {offender.render()}")
    if result.unmatched:
        for offender in result.unmatched:
            print(f"::error file={offender.path},line={offender.line}::{normalize(offender.text)}")
            print(f"  {offender.render()}")
        print(
            f"FAIL: {len(result.unmatched)} unlocalized UI string(s) not in "
            f"{baseline_label}. Route each one through the translation catalog; "
            "do not add it to the baseline."
        )
        return 1
    print(
        f"PASS: {len(offenders)} UI string(s) checked, {result.matched} baselined, "
        f"0 new unlocalized copy ({baseline_label})"
    )
    if result.shrank:
        print(shrank_notice(result, baseline_label))
    return 0


def default_baseline(root: Path, surface: str) -> Path:
    """Baseline path for a surface ("web" / "mobile"), kept beside the scripts."""
    return root / "scripts" / f".{surface}_{BASELINE_STEM}.txt"
