"""The logger acceptance gate must assert redaction in *both* directions.

The gate that guards the Rust logger engine runs in exactly one place — the
release workflow's `clean-install` job — so an expectation inside it can sit
stale for weeks without a red PR. That is what happened here: the engine
deliberately stopped running the phone heuristic over a serialised JSON detail
(it was rewriting `"chat_id":5094837833` into `"chat_id":5094[PHONE]`, i.e.
unparseable JSON with every ordinary id in the blob mangled), the release gate
still demanded the old behaviour, and the whole release failed on a check whose
expectation was the bug.

Two directions must therefore stay pinned together:

* a long digit run inside a **structured** detail survives, as a number, in a
  document that still parses;
* a phone number in **free text** is still masked (keeping the first four
  characters, `[PHONE]` for the rest) — the free-text pass was never the
  problem, and a fix that disabled it wholesale would leak a real phone number.

The first direction is also pinned by the Rust unit test named below. Keeping
this test aware of that test means whoever reverses the engine decision is told
that the gate asserts the opposite, instead of discovering it on a release tag.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "acceptance" / "logger_acceptance.py"
RUST_REDACTION = REPO / "rust" / "logger" / "src" / "redaction.rs"

# The engine's own pin for the structured-detail direction.
ENGINE_TEST = "detail_redaction_keeps_numeric_ids_and_valid_json"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_gate_checks_a_structured_detail_still_parses() -> None:
    text = _script()
    # The *call* has to be there, not just the helper's definition — asserting on
    # a bare `_is_json_document(` also matches `def _is_json_document(`, so a gate
    # that quietly stopped validating the document would still look covered.
    assert re.search(r"_is_json_document\(stored\.detail", text), (
        "the acceptance gate no longer asserts that a redacted structured detail "
        "is still valid JSON — a redaction pass that breaks the document is a "
        "data-loss bug even when the secrets are gone"
    )


def test_the_gate_checks_an_id_survives_as_a_value_not_a_substring() -> None:
    text = _script()
    assert "_json_field(" in text, (
        "the acceptance gate no longer parses the detail before looking for the "
        "id; a substring assertion also passes on a mangled document"
    )
    # The id must be compared as a value somewhere in the gate.
    assert re.search(r'_json_field\([^)]*,\s*"chat_id"\)', text), (
        "the gate does not read chat_id back out of the stored detail"
    )


def test_the_gate_still_masks_a_phone_number_in_free_text() -> None:
    text = _script()
    assert "[PHONE]" in text, (
        "the acceptance gate no longer asserts free-text phone masking — that "
        "would let a real phone number be logged verbatim"
    )
    assert re.search(r"phone not in text_blob", text), (
        "the free-text check must assert the digits are absent from what was "
        "stored, not merely that a marker string appears"
    )


def test_the_gate_no_longer_demands_a_digit_run_be_stripped_from_a_json_blob() -> None:
    """The exact stale expectation, named so it cannot come back by accident."""
    text = _script()
    assert not re.search(r'"5094837833"\s+not\s+in\s+blob', text), (
        "the gate again requires a numeric id inside a structured detail to be "
        "masked; see rust/logger/src/redaction.rs::" + ENGINE_TEST
    )


def test_the_engine_still_pins_the_same_structured_detail_direction() -> None:
    rust = RUST_REDACTION.read_text(encoding="utf-8")
    assert ENGINE_TEST in rust, (
        f"{ENGINE_TEST} disappeared from rust/logger/src/redaction.rs; if the "
        "engine genuinely changed direction, the acceptance gate above has to "
        "change with it in the same commit"
    )
