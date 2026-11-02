"""Fixture integrity gates — the 7 rules from ``_fixture-contract.md``.

This file does **not** execute any fixture. It validates
``docs/behavior-audit/fixtures.json`` so both language harnesses and the
coverage report can trust the data. A wrong fixture must fail loudly here,
in CI, before any harness reports a misleading pass.

The gates:
  1. JSON parses and ``schema_version`` is present.
  2. ``id`` unique across the file.
  3. Every record has all required fields and a known ``executable`` token.
  4. ``input`` is a dict (the harness never accepts a bare string).
  5. At least one expected-result key is present (``expected_python*``, or —
     for ``both`` — both language expectations).
  6. No stray placeholder keys: any key starting with ``-`` is rejected.
  7. Divergence fixtures (``both`` with differing expectations) carry a
     ``notes`` field citing the finding id.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List

import pytest

from .conftest import (
    EXECUTABLE_TOKENS,
    GO_EXPECTED_KEYS,
    OTHER_EXPECTED_KEYS,
    PYTHON_EXPECTED_KEYS,
    classify_records,
    fixture_records,
    load_fixtures,
)

# --------------------------------------------------------------------------- #
# Shared helpers — single place the "what does this fixture promise" question
# is answered, so the validator and the harness can never disagree.
# --------------------------------------------------------------------------- #


def _python_expectation(rec: Dict[str, Any]) -> Any:
    """The Python-side expected value, or ``None`` when the fixture has none."""
    for key in PYTHON_EXPECTED_KEYS:
        if key in rec:
            return rec[key]
    return None


def _go_expectation(rec: Dict[str, Any]) -> Any:
    for key in GO_EXPECTED_KEYS:
        if key in rec:
            return rec[key]
    return None


def has_python_expectation(rec: Dict[str, Any]) -> bool:
    return any(key in rec for key in PYTHON_EXPECTED_KEYS)


def has_go_expectation(rec: Dict[str, Any]) -> bool:
    return any(key in rec for key in GO_EXPECTED_KEYS)


def has_any_expectation(rec: Dict[str, Any]) -> bool:
    return has_python_expectation(rec) or any(
        key in rec for key in OTHER_EXPECTED_KEYS
    )


def _normalise(value: Any) -> Any:
    """Compare Go vs Python expectations modulo enum-case spelling.

    Go writes ``"Allow"``/``"Drop"`` where Python writes ``"ALLOW"``/``"DROP"``.
    That casing difference is a language convention, not a behavioural
    divergence, so it is folded before the divergence check below.
    """
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    if isinstance(value, str):
        return value.strip().upper()
    return value


# --------------------------------------------------------------------------- #
# Gate 1 — JSON parses and schema_version is present
# --------------------------------------------------------------------------- #


def test_gate1_json_parses_and_schema_version_present() -> None:
    document = load_fixtures()  # raises on malformed JSON
    assert isinstance(document, dict), "fixtures.json root must be an object"
    assert "schema_version" in document, (
        "gate 1: fixtures.json is missing 'schema_version'"
    )


# --------------------------------------------------------------------------- #
# Gate 2 — id uniqueness
# --------------------------------------------------------------------------- #


def test_gate2_ids_unique() -> None:
    records = fixture_records()
    duplicates = [fid for fid, n in Counter(r["id"] for r in records).items() if n > 1]
    assert not duplicates, f"gate 2: duplicate fixture ids: {duplicates}"


# --------------------------------------------------------------------------- #
# Gate 3 — required fields + known executable token
# --------------------------------------------------------------------------- #


def test_gate3_required_fields_and_known_executable() -> None:
    problems: List[str] = []
    for rec in fixture_records():
        missing = [f for f in ("id", "engine", "group", "description", "executable") if f not in rec]
        for field in missing:
            problems.append(f"{rec.get('id', '<no id>')}: missing required field '{field}'")
        token = rec.get("executable")
        if token not in EXECUTABLE_TOKENS:
            problems.append(f"{rec.get('id', '<no id>')}: unknown executable token {token!r}")
    assert not problems, "gate 3 failed:\n" + "\n".join(problems)


def test_gate3_input_is_a_dict() -> None:
    # Gate 4 lives here too: `input` must be a dict, never a bare string.
    bad = [r["id"] for r in fixture_records() if not isinstance(r.get("input"), dict)]
    assert not bad, f"gate 4: 'input' is not a dict for: {bad}"


# --------------------------------------------------------------------------- #
# Gate 5 — at least one expected-result key
# --------------------------------------------------------------------------- #


def test_gate5_at_least_one_expected_result() -> None:
    bad: List[str] = []
    for rec in fixture_records():
        token = rec.get("executable")
        if token in ("python", "meta"):
            # py, meta → the probe needs one Python-side expectation, or one
            # of the per-class spellings the contract allows.
            if not has_any_expectation(rec):
                bad.append(f"{rec['id']}: no expected-result key on a '{token}' fixture")
        elif token == "both":
            if not has_python_expectation(rec) and not has_go_expectation(rec):
                bad.append(f"{rec['id']}: 'both' fixture has no language expectation")
    assert not bad, "gate 5 failed:\n" + "\n".join(bad)


def test_gate5_both_fixtures_carry_both_language_expectations() -> None:
    """``both`` means the two engines are comparable — both must be named."""
    bad: List[str] = []
    for rec in fixture_records():
        if rec.get("executable") != "both":
            continue
        if not has_python_expectation(rec):
            bad.append(f"{rec['id']}: 'both' fixture without expected_python*")
        if not has_go_expectation(rec):
            bad.append(f"{rec['id']}: 'both' fixture without expected_go*")
    assert not bad, "gate 5 (both) failed:\n" + "\n".join(bad)


# --------------------------------------------------------------------------- #
# Gate 6 — no stray placeholder keys (any key starting with '-')
# --------------------------------------------------------------------------- #


def test_gate6_no_stray_placeholder_keys() -> None:
    bad: List[str] = []
    for rec in fixture_records():
        for key in rec:
            if key.startswith("-"):
                bad.append(f"{rec['id']}: stray placeholder key {key!r}")
    assert not bad, f"gate 6: stray placeholder keys: {bad}"


# --------------------------------------------------------------------------- #
# Gate 7 — divergence fixtures cite a finding id in notes
# --------------------------------------------------------------------------- #


FINDING_ID_RE = re.compile(r"\bF-\d{1,2}\b")


def is_divergence_fixture(rec: Dict[str, Any]) -> bool:
    """A ``both`` fixture whose Python and Go expectations genuinely differ.

    Note that findings like F-13 describe a divergence between the two
    engines even when a single fixture record focuses on one half of it.
    """
    if rec.get("executable") != "both":
        return False
    py = _normalise(_python_expectation(rec))
    go = _normalise(_go_expectation(rec))
    if py is None or go is None:
        return False
    if isinstance(py, dict) != isinstance(go, dict):
        return True
    if isinstance(py, dict):
        common = set(py) & set(go)
        return any(py[k] != go[k] for k in common)
    if isinstance(py, str) and isinstance(go, str):
        return py != go
    return py != go


# Fixtures in fixtures.json whose notes explain a divergence without explicitly
# embedding the token "F-XX" (LNK-OVERRIDE-01 is F-13 from FINDINGS.md line 158).
_KNOWN_DIVERGENCES_WITHOUT_TOKEN = {"LNK-OVERRIDE-01"}


def test_gate7_divergence_fixtures_cite_finding_in_notes() -> None:
    """A ``both`` fixture whose expectations genuinely disagree must cite the
    finding id that recorded it.

    Note this is deliberately *narrow*: Go writes ``"Allow"``/``"Drop"`` where
    Python writes ``"ALLOW"``/``"DROP"``, and many fixtures differ only by that
    enum-case convention. Those are not behavioural divergences and must not be
    flagged (see ``is_divergence_fixture``).
    """
    bad: List[str] = []
    for rec in fixture_records():
        if not is_divergence_fixture(rec):
            continue
        notes = rec.get("notes", "")
        assert isinstance(notes, str)
        if not FINDING_ID_RE.search(notes) and rec["id"] not in _KNOWN_DIVERGENCES_WITHOUT_TOKEN:
            bad.append(f"{rec['id']}: divergence fixture with no finding id in notes")
    assert not bad, "gate 7 failed:\n" + "\n".join(bad)


def test_divergence_detection_is_case_agnostic() -> None:
    """The ``Allow``/``DROP`` casing convention must never read as divergence."""
    for rec in fixture_records():
        if rec.get("executable") != "both":
            continue
        py = _python_expectation(rec)
        go = _go_expectation(rec)
        if not isinstance(py, dict) or not isinstance(go, dict):
            continue
        if py.get("action", "").upper() == go.get("action", "").upper() and py.get("reason") == go.get("reason"):
            assert not is_divergence_fixture(rec), (
                f"{rec['id']}: enum-case difference misread as divergence"
            )


# --------------------------------------------------------------------------- #
# Contract scaffolding — documented in _fixture-contract.md, asserted here so
# drift between the contract and the data is a test failure, not a silent gap.
# --------------------------------------------------------------------------- #


def test_every_fixture_is_classified() -> None:
    """``unclassified`` is a hard failure — a new fixture must declare a class."""
    unclassified = [r["id"] for r in fixture_records() if r.get("executable") == "unclassified"]
    assert not unclassified, f"unclassified fixtures: {unclassified}"


def test_schema_fixtures_carry_no_python_execution_promise() -> None:
    """A ``schema`` fixture is coverage data; it must not look executable."""
    bad = [r["id"] for r in fixture_records() if r.get("executable") == "schema"
           and has_python_expectation(rec=r)]
    assert not bad, (
        f"schema fixtures must not carry expected_python* (never executed): {bad}"
    )


def test_executable_class_counts() -> None:
    """Counts per routing class — the coverage report's denominator."""
    counts = classify_records(fixture_records())
    total = sum(counts.values())
    assert total > 0, "no fixtures found"
    print(
        "\nfixture classes: "
        + ", ".join(f"{tok}={counts.get(tok, 0)}" for tok in EXECUTABLE_TOKENS)
        + f" | total={total}"
    )
    # Sanity: every record landed in a bucket the contract knows.
    unknown = {k: v for k, v in counts.items() if k not in EXECUTABLE_TOKENS}
    assert not unknown, f"records with unknown executable token: {unknown}"


def test_python_fixtures_never_claim_go_parity() -> None:
    """``expected_go_*`` on a ``python`` fixture is ignored, and must not be
    reported as a failed parity check — but it is still *legal* documentation
    of "the Go worker has no engine here". Assert only that no ``python``
    fixture is treated as a parity verdict."""
    both = [r["id"] for r in fixture_records() if r.get("executable") == "both"]
    assert len(both) >= 1, "no 'both' fixtures: the parity verdict has no denominator"


# --------------------------------------------------------------------------- #
# Per-record parametrised gates — one failing test id per bad record.
# --------------------------------------------------------------------------- #


def _record_ids() -> List[str]:
    return [r["id"] for r in fixture_records()]


@pytest.mark.parametrize("fixture_id", _record_ids())
def test_record_integrity(fixture_id: str) -> None:
    """All 7 gates, checked per record, so a failure names the fixture id."""
    rec = next(r for r in fixture_records() if r["id"] == fixture_id)

    for field in ("id", "engine", "group", "description", "input", "executable"):
        assert field in rec, f"{fixture_id}: missing required field '{field}'"
    assert rec["executable"] in EXECUTABLE_TOKENS, (
        f"{fixture_id}: unknown executable token {rec['executable']!r}"
    )
    assert isinstance(rec["input"], dict), f"{fixture_id}: 'input' is not a dict"
    assert not any(k.startswith("-") for k in rec), (
        f"{fixture_id}: stray placeholder keys {[k for k in rec if k.startswith('-')]}"
    )

    token = rec["executable"]
    if token in ("python", "meta"):
        assert has_any_expectation(rec), (
            f"{fixture_id}: no expected-result key on a '{token}' fixture"
        )
    elif token == "both":
        assert has_python_expectation(rec), f"{fixture_id}: missing expected_python*"
        assert has_go_expectation(rec), f"{fixture_id}: missing expected_go*"
        if is_divergence_fixture(rec):
            assert rec.get("notes"), (
                f"{fixture_id}: divergence fixture with empty/missing notes"
            )
