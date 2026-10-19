"""Regression tests for the mobile (Dart) UI-copy gate.

The gate exists because 40-odd widget labels never reached the 10-locale catalog
in ``mobile/lib/l10n/``. Each test pins one behaviour the gate must keep: flag
copy, tolerate catalog lookups and technical tokens, survive a line shift through
the content-keyed baseline, and round-trip ``--update-baseline``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/audit_mobile_ui_copy.py"
SPEC = importlib.util.spec_from_file_location("audit_mobile_ui_copy", SCRIPT)
assert SPEC and SPEC.loader
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
# The gate puts scripts/ on sys.path and imports the shared baseline module.
BASE = importlib.import_module("ui_copy_baseline")

PLANTED = """\
import 'package:flutter/material.dart';

Widget probe(AppMessages t) {
  return Column(children: [
    Text('Deploy the widget now'),
    const Text('Retry'),
    Text(t.get('ui_retry')),
    Text(getLabel()),
  ]);
}
"""


def scan(source: str, path: str = "mobile/lib/probe.dart") -> list[str]:
    return [offender.text for offender in GATE.scan_source(path, source)]


# --------------------------------------------------------------------------- #
# what is copy
# --------------------------------------------------------------------------- #
def test_planted_text_literal_is_flagged() -> None:
    assert "Deploy the widget now" in scan(PLANTED)


def test_const_text_literal_is_flagged() -> None:
    assert "Retry" in scan("const Text('Retry')")


def test_label_and_hint_arguments_are_flagged() -> None:
    source = (
        "TextField(decoration: InputDecoration("
        "labelText: 'Task Name', hintText: 'word1, word2'))"
    )
    assert scan(source) == ["Task Name", "word1, word2"]


def test_semantic_label_and_tooltip_are_flagged() -> None:
    source = (
        "Semantics(label: 'Duplicate Task', child: Tooltip(message: 'Copy it', child: x));\n"
        "IconButton(tooltip: 'Duplicate Task', onPressed: f);"
    )
    found = scan(source)
    assert found == ["Duplicate Task", "Copy it", "Duplicate Task"]


def test_interpolated_copy_keeps_the_static_part() -> None:
    assert scan("Text('Sender: ${r.senderType}')") == ["Sender: ${r.senderType}"]
    assert scan("Text('Debug: ${r.debugMode ? 'on' : 'off'}')") == [
        "Debug: ${r.debugMode ? 'on' : 'off'}"
    ]


def test_ternary_inside_text_is_scanned() -> None:
    source = "Text(cond ? 'Administrator' : 'Standard User')"
    assert scan(source) == ["Administrator", "Standard User"]


def test_empty_list_state_copy_is_flagged() -> None:
    source = "ListStateView(emptyTitle: 'No audit events yet', emptySubtitle: 'Nothing here')"
    assert scan(source) == ["No audit events yet", "Nothing here"]


# --------------------------------------------------------------------------- #
# what is not copy
# --------------------------------------------------------------------------- #
def test_catalog_lookup_is_tolerated() -> None:
    source = (
        "Text(t.get('ui_retry'))\n"
        "TextField(decoration: InputDecoration(labelText: t.get('ui_task_name')))\n"
        "Text('${t.get('ui_x')}')\n"
        "Tooltip(message: t.get('ui_copy'), child: x);"
    )
    assert scan(source) == []


def test_technical_tokens_are_tolerated() -> None:
    source = "\n".join(
        [
            "Text('https://atf.example.com')",
            "Text('AutoTelegramForward')",
            "Text('filter_rule_id')",
            "Text('{source_title}')",
            "Text('12:30')",
            "Text('ERROR')",
            "Text('core')",
            "Text('application/json')",
            "Text('/api/v1/health')",
            "Text('BOT ${s.id}')",
        ]
    )
    assert scan(source) == []


def test_style_strings_in_nested_calls_are_not_data() -> None:
    source = "Text(t.get('ui_x'), style: const TextStyle(fontFamily: 'Vazirmatn', fontSize: 12))"
    assert scan(source) == []


def test_escaped_apostrophe_does_not_desynchronize_the_reader() -> None:
    source = (
        "Text('Doesn\\'t work')\n"
        "Text(t.get('ui_next'))\n"
    )
    assert scan(source) == ["Doesn\\'t work"]


def test_apostrophe_inside_interpolation_does_not_desynchronize() -> None:
    source = "Text('${t.get('lbl')} …')\nText(t.get('ui_next'))"
    assert scan(source) == []


# --------------------------------------------------------------------------- #
# baseline behaviour through the real CLI
# --------------------------------------------------------------------------- #
def make_tree(tmp_path: Path, body: str = PLANTED) -> Path:
    lib = tmp_path / "mobile/lib"
    lib.mkdir(parents=True)
    (lib / "probe.dart").write_text(body, encoding="utf-8")
    return lib


def run(tmp_path: Path, *args: str) -> int:
    return GATE.main(["--root", str(tmp_path), "--lib", str(tmp_path / "mobile/lib"), *args])


def test_update_baseline_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_tree(tmp_path)
    assert run(tmp_path, "--update-baseline") == 0
    baseline = tmp_path / "scripts/.mobile_ui_copy_baseline.txt"
    entries = GATE.default_baseline(tmp_path, "mobile")
    assert baseline == entries
    assert "mobile/lib/probe.dart|Deploy the widget now" in baseline.read_text(encoding="utf-8")

    capsys.readouterr()
    assert run(tmp_path) == 0
    assert "PASS:" in capsys.readouterr().out


def test_baselined_offender_survives_an_inserted_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path)
    assert run(tmp_path, "--update-baseline") == 0

    # Insert lines above the offender and re-indent it: same content, new line.
    file = tmp_path / "mobile/lib/probe.dart"
    shifted = "// a new import or comment that shifts every line below it\n// and another\n" + file.read_text(
        encoding="utf-8"
    )
    shifted = shifted.replace("    Text('Deploy the widget now')", "        Text('Deploy  the widget now')")
    file.write_text(shifted, encoding="utf-8")

    capsys.readouterr()
    assert run(tmp_path) == 0
    assert "PASS:" in capsys.readouterr().out


def test_new_literal_next_to_a_baselined_one_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path)
    assert run(tmp_path, "--update-baseline") == 0
    file = tmp_path / "mobile/lib/probe.dart"
    file.write_text(
        file.read_text(encoding="utf-8").replace(
            "    Text(t.get('ui_retry')),",
            "    Text(t.get('ui_retry')),\n    Text('Delete everything'),",
        ),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert run(tmp_path) == 1
    out = capsys.readouterr().out
    assert "Delete everything" in out
    assert "FAIL: 1 unlocalized UI string(s)" in out


def test_shrunk_backlog_prints_the_notice_but_stays_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path)
    assert run(tmp_path, "--update-baseline") == 0
    # Localizing 'Retry' removes one pinned offender: cleanup must not red CI.
    file = tmp_path / "mobile/lib/probe.dart"
    file.write_text(
        file.read_text(encoding="utf-8").replace("const Text('Retry')", "Text(t.get('ui_retry'))"),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "backlog shrank — regenerate" in out
    assert "--update-baseline" in out


def test_l10n_catalog_directory_is_not_scanned(tmp_path: Path) -> None:
    lib = make_tree(tmp_path)
    catalog = lib / "l10n"
    catalog.mkdir()
    (catalog / "app_messages.dart").write_text("const Map<String, String> en = {'k': 'Retry'};\n")
    offenders, files = GATE.scan_lib(lib, tmp_path)
    assert files == 1  # probe.dart only
    assert {offender.path for offender in offenders} == {"mobile/lib/probe.dart"}


def test_missing_library_directory_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert GATE.main(["--root", str(tmp_path), "--lib", str(tmp_path / "nope")]) == 2
    assert "not found" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the real repository
# --------------------------------------------------------------------------- #
def test_repository_baseline_is_current() -> None:
    """The committed baseline must cover the tree, or CI is red on a clean checkout."""
    offenders, files = GATE.scan_lib(REPO / "mobile/lib", REPO)
    assert files > 10
    baseline = GATE.default_baseline(REPO, "mobile")
    assert baseline.exists(), "scripts/.mobile_ui_copy_baseline.txt is missing"
    result = BASE.evaluate(offenders, BASE.read_baseline(baseline))
    assert result.unmatched == []
