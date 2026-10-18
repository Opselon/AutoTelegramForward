"""A refusal has to be visible on the wire, and every client has to read it.

Protobuf marks every proto3 scalar `omitempty` in the generated Go struct, so a
handler that writes `*pb.RuleActionResponse` straight to the client serialises a
refusal as `{"message":"not_found"}` — `success` is not `false`, it is *absent*.
Both plausible client checks then fail in opposite directions:

* `resp["success"] == false` never fires (the key is missing), so the client
  treats a refusal as a success; the dashboard and the Android app removed the
  dead-letter row they had just failed to re-queue, and the CLI printed
  "Replaying message …".
* A truthiness test sees a non-empty object and also reads success.

This was live: a replay of an unknown id answered `{"message":"not_found",
"error_code":"not_found"}` on the production API with no result flag at all.

These checks are static and cross-language on purpose: the defect is a *contract*
shared by one Go projection and four clients, so it cannot be caught inside any
one of them.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

HANDLERS = ROOT / "api" / "internal" / "handlers" / "handlers.go"
CLIENTS = {
    "web": ROOT / "web" / "frontend" / "src" / "App.tsx",
    "mobile": ROOT / "mobile" / "lib" / "features" / "delivery" / "delivery_screen.dart",
    "cli": ROOT / "atfcli" / "commands" / "data.py",
}

#: The web decision was extracted into a module the CI job compiles and runs
#: (`web/frontend/tests/actionResult.test.mjs`), so the check lives there.
WEB_MODULE = ROOT / "web" / "frontend" / "src" / "actionResult.ts"

#: Handlers that answer a control-plane action with `*pb.RuleActionResponse`.
ACTION_HANDLERS = ("PauseRule", "ResumeRule", "ReplayDeadLetter", "PurgeDeadLetter")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """Drop comments; a comment explaining the defect is not the defect.

    The dashboard's comment literally says "testing `success === false` would
    treat a response that omits the field as a replay", so a plain text scan
    flags the documentation of the fix.
    """
    lines = []
    in_block = False
    for line in source.splitlines():
        if in_block:
            if "*/" in line:
                in_block = False
                line = line.split("*/", 1)[1]
            else:
                continue
        if "/*" in line and "*/" not in line:
            in_block = True
            line = line.split("/*", 1)[0]
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _function_body(source: str, name: str) -> str:
    """The body of `func (h *Handlers) <name>(...)`, brace-matched."""
    match = re.search(r"func \(h \*Handlers\) %s\(" % re.escape(name), source)
    assert match, f"handler {name} not found — renamed or removed?"
    start = source.index("{", match.end())
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_the_go_projection_states_success_without_omitempty() -> None:
    source = _read(HANDLERS)
    assert "type actionResponse struct" in source, (
        "the explicit action projection is gone; a raw pb.RuleActionResponse "
        "reintroduces a refusal with no `success` field"
    )
    block = source.split("type actionResponse struct", 1)[1].split("}", 1)[0]
    success = [ln for ln in block.splitlines() if "Success" in ln]
    assert success, "actionResponse has no Success field"
    field = success[0]
    assert re.search(r'json:"success"', field), (
        f"the success field must be mandatory on the wire, got: {field.strip()}"
    )
    assert "omitempty" not in field, (
        f"`omitempty` drops exactly the false value clients must see: {field.strip()}"
    )


def test_every_action_handler_uses_the_projection() -> None:
    source = _read(HANDLERS)
    offenders = []
    for name in ACTION_HANDLERS:
        body = _function_body(source, name)
        if "writeAction(w, resp)" not in body:
            offenders.append(name)
    assert not offenders, (
        "these handlers answer an action without stating success explicitly: "
        + ", ".join(offenders)
    )


def test_go_structs_are_never_written_raw_for_actions() -> None:
    """`writeJSON(w, …, resp)` on an action response is the original defect."""
    source = _read(HANDLERS)
    for name in ACTION_HANDLERS:
        body = _function_body(source, name)
        raw = re.search(r"writeJSON\(w,\s*http\.StatusOK,\s*resp\)", body)
        assert not raw, (
            f"{name} writes the protobuf response directly; proto3 omitempty "
            "will drop `success: false`"
        )


def test_no_client_tests_success_against_false() -> None:
    """The blindness pattern, in every client language.

    A response that omits the field must not be mistaken for a success, so the
    comparison has to be against an explicit `true`.
    """
    patterns = {
        "web": re.compile(r"success\s*===?\s*false"),
        "mobile": re.compile(r"\['success'\]\s*==\s*false"),
        "cli": re.compile(r"""get\(\s*["']success["']\s*\)\s*is\s+False"""),
    }
    for name, path in CLIENTS.items():
        source = _code_only(_read(path))
        hits = [ln.strip() for ln in source.splitlines() if patterns[name].search(ln)]
        assert not hits, (
            f"{name} still treats an absent `success` as success:\n  "
            + "\n  ".join(hits)
        )


def test_every_client_gates_on_an_explicit_true() -> None:
    expectations = {
        # App.tsx delegates to the compiled module, so the gate is `isAccepted`.
        "web": re.compile(r"isAccepted\("),
        "mobile": re.compile(r"\['success'\]\s*!=\s*true"),
        "cli": re.compile(r"""get\(\s*["']success["']\s*\)\s*is\s+not\s+True"""),
    }
    for name, path in CLIENTS.items():
        source = _code_only(_read(path))
        assert expectations[name].search(source), (
            f"{name} does not require an explicit `true` before reporting an "
            "action as done"
        )


def test_the_web_gate_lives_in_the_compiled_module() -> None:
    """The dashboard's decision must be the one CI actually runs."""
    module = _code_only(_read(WEB_MODULE))
    assert re.search(r"export function isAccepted", module), (
        "actionResult.ts must export the acceptance rule by name"
    )
    assert re.search(r"success\s*===\s*true", module), (
        "the rule must require an explicit `true`; anything else is a refusal"
    )
    test_file = ROOT / "web" / "frontend" / "tests" / "actionResult.test.mjs"
    assert test_file.is_file(), "the module has no test the CI job can run"
    workflow = _read(ROOT / ".github" / "workflows" / "ci.yml")
    assert "tests/actionResult.test.mjs" in workflow, (
        "CI never runs the action-result test"
    )
