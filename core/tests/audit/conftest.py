"""Shared loader for the behavior-audit harnesses.

Finds the repo root by walking up from this file and loads
``docs/behavior-audit/fixtures.json``. Never a hard-coded absolute path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

FIXTURES_REL = Path("docs") / "behavior-audit" / "fixtures.json"

#: Routing classes, verbatim from ``_fixture-contract.md``.
EXECUTABLE_TOKENS = ("python", "both", "meta", "schema", "unclassified")

#: Fields every fixture record must carry.
REQUIRED_FIELDS = ("id", "engine", "group", "description", "input", "executable")

#: Python-side expected-result keys. At least one must be present on every
#: ``python`` / ``both`` / ``meta`` fixture (gate 5 of the contract).
PYTHON_EXPECTED_KEYS = (
    "expected_python",
    "expected_python_output",
    "expected_python_durable_output",
    "expected_python_legacy_output",
    "expected_python_preview_output",
    "expected_b_python_output",
)

#: Expected-result keys that are recognised but not "the Python result". Gate 5
#: accepts the *per-class* spellings the contract lists under "per class".
OTHER_EXPECTED_KEYS = (
    "expected",
    "expected_filter_engine",
    "expected_link_block_reason",
    "expected_effective_text",
    "expected_registry_clamped",
    "expected_engine_value",
    "expected_python_output_reversed_insertion",
    # `meta` fixtures state numeric claims (length units), not results.
    "python_len_codepoints",
    "go_len_bytes",
    "go_runes",
)

GO_EXPECTED_KEYS = ("expected_go", "expected_go_output")

#: ``meta`` fixtures carry a numeric claim (length units) rather than a result;
#: the probe reads these directly from the record.
META_EXPECTED_KEYS = (
    "python_len_codepoints",
    "go_len_bytes",
    "go_runes",
)


def repo_root() -> Path:
    """Walk up from this file until ``docs/behavior-audit/fixtures.json`` exists."""
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / FIXTURES_REL).is_file():
            return candidate
    raise FileNotFoundError(f"could not locate {FIXTURES_REL} above {here}")


def fixtures_path() -> Path:
    return repo_root() / FIXTURES_REL


def load_fixtures() -> Dict[str, Any]:
    """Parse ``fixtures.json`` and return the whole document."""
    path = fixtures_path()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root is not a JSON object")
    return data


def fixture_records() -> List[Dict[str, Any]]:
    """Return the ``fixtures`` list, validating the wrapper shape minimally."""
    data = load_fixtures()
    records = data.get("fixtures")
    if not isinstance(records, list):
        raise ValueError(f"{fixtures_path()}: missing or non-list 'fixtures' key")
    return records


def executable_fixture_records() -> List[Dict[str, Any]]:
    """Every fixture the Python harness must run: ``python``, ``both``, ``meta``."""
    return [f for f in fixture_records() if f.get("executable") in ("python", "both", "meta")]


def schema_fixture_records() -> List[Dict[str, Any]]:
    """Characterization data only — never executed, never a passing test."""
    return [f for f in fixture_records() if f.get("executable") == "schema"]


def classify_records(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Counts per routing class, so the coverage denominator is visible."""
    counts: Dict[str, int] = {tok: 0 for tok in EXECUTABLE_TOKENS}
    for rec in records:
        token = rec.get("executable", "<missing>")
        counts[token] = counts.get(token, 0) + 1
    return counts
