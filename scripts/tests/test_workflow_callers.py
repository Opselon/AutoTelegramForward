"""A workflow file that GitHub cannot even parse runs nothing — and looks quiet.

The failure this guards against is the worst kind: `release.yml` carried a
`timeout-minutes:` on the job that *calls* the reusable Android workflow. A
caller job may carry only `name`, `uses`, `with`, `secrets`, `needs`, `if` and
`permissions`, so GitHub rejected the entire file. Every release run after that
landed as "workflow file issue" — no jobs, no logs, and a green-looking PR,
because the workflow only runs on a tag. The first tag since then (v2.2.2)
produced no APK at all, which is exactly what this test is for.

These checks parse the workflow files as YAML and read the caller jobs directly,
so they need no network and no GitHub API — they fail in the same second a
`timeout-minutes:` is added back.

The same shape is checked in every workflow, not just `release.yml`: the next
delegation will be written by copy-paste.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"

# From GitHub's reusable-workflow docs. Anything else in a caller job is a
# workflow-file rejection, not a hint.
CALLER_ALLOWED_KEYS = {
    "name", "uses", "with", "secrets", "needs", "if", "permissions",
    "strategy", "concurrency",
}


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def _caller_jobs() -> list[tuple[str, str, dict]]:
    """(file, job name, job body) for every job that calls a local workflow."""
    found: list[tuple[str, str, dict]] = []
    for path in _workflow_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, job in (doc.get("jobs") or {}).items():
            uses = (job or {}).get("uses", "")
            if isinstance(uses, str) and uses.startswith("./"):
                found.append((path.name, name, job))
    return found


def test_there_is_at_least_one_delegation_to_check():
    """If this ever returns nothing, the other tests here silently stop testing."""
    callers = _caller_jobs()

    assert callers, "no reusable-workflow caller found — update this file"
    assert any(name == "mobile-android" for _, name, _ in callers), (
        "release.yml must still delegate the Android build"
    )


@pytest.mark.parametrize("workflow,job,body", _caller_jobs(),
                         ids=lambda value: value if isinstance(value, str) else "")
def test_a_caller_job_carries_only_keys_github_allows(workflow, job, body):
    unexpected = sorted(set(body) - CALLER_ALLOWED_KEYS)

    assert not unexpected, (
        f"{workflow}: job '{job}' calls a reusable workflow but carries "
        f"{unexpected} — GitHub rejects the whole workflow file (no jobs run at "
        f"all), it is not ignored. Allowed: {sorted(CALLER_ALLOWED_KEYS)}"
    )


def test_the_android_build_keeps_its_own_time_bound():
    """Dropping the caller's bound is only safe because the callee has one."""
    doc = yaml.safe_load((WORKFLOWS / "mobile-android.yml").read_text(encoding="utf-8"))
    (job,) = (doc.get("jobs") or {}).values()

    assert job.get("timeout-minutes"), (
        "mobile-android.yml must keep a timeout-minutes on its own job: a caller "
        "cannot carry one, so this is the only bound the Android build has"
    )


def test_the_release_caller_passes_the_resolved_version():
    doc = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    caller = doc["jobs"]["mobile-android"]

    assert caller["uses"] == "./.github/workflows/mobile-android.yml"
    assert caller["with"]["version"] == "${{ needs.meta.outputs.version }}", (
        "the Android artifacts are named from this input; a literal here would "
        "publish a version that disagrees with VERSION"
    )
    assert caller["secrets"] == "inherit", (
        "the signing secrets are required by the callee — without `inherit` the "
        "build fails rather than shipping a debug-signed APK, which is correct "
        "but not what a release wants"
    )
