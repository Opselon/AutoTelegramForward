"""Cross-language contract for the per-user task kinds (API-first phase 2).

The Go API refuses unknown task kinds before a gRPC round trip; the Python
domain owns the authoritative KINDS set. The two lists MUST be identical —
this test parses both sources of truth and fails on the first drift.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _python_kinds() -> set:
    text = (REPO / "core" / "application" / "user_tasks.py").read_text()
    m = re.search(r"KINDS: Set\[str\] = \{(.*?)\}", text, re.S)
    assert m, "python KINDS set not found"
    return {k.strip().strip('"') for k in m.group(1).split(",") if k.strip()}


def _go_kinds() -> set:
    text = (REPO / "api" / "internal" / "handlers" / "tasks.go").read_text()
    m = re.search(r"var taskKinds = map\[string\]bool\{(.*?)\}", text, re.S)
    assert m, "go taskKinds map not found"
    return {k.strip().strip('"') for k in re.findall(r'"([^"]+)"', m.group(1))}


def _proto_kinds() -> set:
    """The kinds documented in the proto comment block must include itself."""
    text = (REPO / "proto" / "autoforward.proto").read_text()
    m = re.search(r"// states.*PENDING RUNNING RETRY_WAIT SUCCEEDED FAILED CANCELLED", text)
    assert m, "proto UserTask must document the state set"


def test_task_kinds_are_1_to_1_across_go_and_python():
    py = _python_kinds()
    go = _go_kinds()
    assert py == go, (
        f"task kind drift: python-only={sorted(py - go)} go-only={sorted(go - py)}")


def test_every_kind_appears_in_the_worker_or_is_executor_gated():
    """A kind that reaches KINDS without any executor intent never works —
    it stays PENDING forever. Keep the drift visible here."""
    worker_text = (REPO / "core" / "application" / "user_task_worker.py").read_text()
    for kind in _python_kinds():
        assert f'"{kind}"' in worker_text, (
            f"kind {kind} is not wired into the worker registry")


if __name__ == "__main__":
    sys.exit(0 if test_task_kinds_are_1_to_1_across_go_and_python() is None else 1)
