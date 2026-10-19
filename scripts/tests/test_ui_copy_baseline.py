"""Regression tests for the shared content-keyed UI-copy baseline.

These pin the property that makes the two gates usable on a repository with
existing debt: a baseline entry is keyed by ``path|text``, never by line, so
inserting a line above an offender (or re-indenting a JSX node) cannot redden
CI. A *new* text in the same file must still fail.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ui_copy_baseline.py"
SPEC = importlib.util.spec_from_file_location("ui_copy_baseline", SCRIPT)
assert SPEC and SPEC.loader
BASE = importlib.util.module_from_spec(SPEC)
# Register before executing: dataclasses (3.12+) resolve the module by name.
sys.modules.setdefault("ui_copy_baseline", BASE)
SPEC.loader.exec_module(BASE)


def offender(path: str = "web/frontend/src/App.tsx", line: int = 10, text: str = "Save changes"):
    return BASE.Offender(path=path, line=line, text=text)


# --------------------------------------------------------------------------- #
# format
# --------------------------------------------------------------------------- #
def test_entries_are_content_keyed_and_whitespace_normalized() -> None:
    first = offender(line=10, text="Deploy\n   the   widget")
    shifted = offender(line=99, text="Deploy the widget")
    assert first.entry() == shifted.entry() == "web/frontend/src/App.tsx|Deploy the widget"


def test_comments_and_blank_lines_are_ignored() -> None:
    entries = BASE.parse_baseline(
        "# header comment\n\n  web/frontend/src/App.tsx|Save changes  \n# trailing\n"
    )
    assert entries == ["web/frontend/src/App.tsx|Save changes"]


def test_missing_baseline_file_reads_as_empty(tmp_path: Path) -> None:
    assert BASE.read_baseline(tmp_path / "absent.txt") == []


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def test_line_shift_keeps_a_baselined_offender_tolerated() -> None:
    entries = ["web/frontend/src/App.tsx|Save changes"]
    original = BASE.evaluate([offender(line=10)], entries)
    shifted = BASE.evaluate([offender(line=410)], entries)
    assert original.ok and shifted.ok
    assert shifted.matched == 1 and not shifted.unused


def test_new_text_in_the_same_file_fails() -> None:
    entries = ["web/frontend/src/App.tsx|Save changes"]
    result = BASE.evaluate([offender(line=10), offender(line=11, text="Delete everything")], entries)
    assert not result.ok
    assert [item.text for item in result.unmatched] == ["Delete everything"]


def test_baseline_is_per_path_not_repository_wide() -> None:
    entries = ["web/frontend/src/App.tsx|Save changes"]
    result = BASE.evaluate([offender(path="web/frontend/src/Auth.tsx")], entries)
    assert not result.ok
    assert result.unused == ["web/frontend/src/App.tsx|Save changes"]


def test_duplicate_offender_needs_its_own_entry() -> None:
    entries = ["mobile/lib/widgets/list_state_view.dart|Retry"]
    result = BASE.evaluate(
        [
            offender(path="mobile/lib/widgets/list_state_view.dart", line=80, text="Retry"),
            offender(path="mobile/lib/widgets/list_state_view.dart", line=120, text="Retry"),
        ],
        entries,
    )
    assert len(result.unmatched) == 1


def test_shrink_is_reported_without_failing() -> None:
    entries = [
        "mobile/lib/widgets/list_state_view.dart|Retry",
        "mobile/lib/widgets/list_state_view.dart|Could not load data",
    ]
    result = BASE.evaluate([offender(path="mobile/lib/widgets/list_state_view.dart", text="Retry")], entries)
    assert result.ok
    assert result.shrank
    assert len(result.unused) == 1
    notice = BASE.shrank_notice(result, "scripts/.mobile_ui_copy_baseline.txt")
    assert "backlog shrank — regenerate" in notice and "1 of 2" in notice


# --------------------------------------------------------------------------- #
# writer / CLI round-trip
# --------------------------------------------------------------------------- #
def test_write_then_read_round_trips(tmp_path: Path) -> None:
    baseline = tmp_path / "scripts" / ".probe_baseline.txt"
    size = BASE.write_baseline(baseline, [o.entry() for o in (offender(), offender(text="Retry"))])
    assert size == 2
    assert BASE.read_baseline(baseline) == [
        "web/frontend/src/App.tsx|Retry",
        "web/frontend/src/App.tsx|Save changes",
    ]
    assert baseline.read_text(encoding="utf-8").startswith("#")


def test_run_gate_update_round_trip_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline = tmp_path / ".probe_baseline.txt"
    fixtures = [offender(line=3), offender(line=9, text="Retry")]

    assert BASE.run_gate(fixtures, baseline, ".probe_baseline.txt", update=True) == 0
    assert "2 offender(s) pinned" in capsys.readouterr().out

    assert BASE.run_gate(fixtures, baseline, ".probe_baseline.txt", update=False) == 0
    out = capsys.readouterr().out
    assert out.startswith("PASS: 2 UI string(s) checked, 2 baselined")
    assert "backlog shrank" not in out


def test_run_gate_fails_closed_and_reports_github_annotations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / ".probe_baseline.txt"
    BASE.write_baseline(baseline, [])
    assert BASE.run_gate([offender(line=7)], baseline, ".probe_baseline.txt", update=False) == 1
    out = capsys.readouterr().out
    assert "FAIL: 1 unlocalized UI string(s)" in out
    assert "::error file=web/frontend/src/App.tsx,line=7::Save changes" in out


def test_run_gate_prints_shrink_notice_once_backlog_drops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / ".probe_baseline.txt"
    BASE.write_baseline(baseline, [offender().entry(), offender(text="Retry").entry()])
    capsys.readouterr()

    assert BASE.run_gate([offender()], baseline, ".probe_baseline.txt", update=False) == 0
    out = capsys.readouterr().out
    assert "backlog shrank" in out
    assert "1 of 2 pinned offender(s)" in out


def test_default_baseline_paths_are_per_surface() -> None:
    assert BASE.default_baseline(REPO, "web").name == ".web_ui_copy_baseline.txt"
    assert BASE.default_baseline(REPO, "mobile").name == ".mobile_ui_copy_baseline.txt"
