"""Regression tests: a deployed image can name the commit it was built from.

Defect being pinned: the API has always declared ``handlers.CommitSHA`` as an
``-ldflags`` target and ``/api/version`` has always promised it (see
``docs/api-compatibility.md``, the field mobile and web use to reconcile a
server against a build), but nothing ever set it. ``Dockerfile.api`` compiled
with a bare ``-ldflags "-s -w"`` and the Publish workflow passed no build
arguments, so every container deployment answered ``"commit_sha": "unknown"``
— the exact answer that field exists to make impossible. On the host there was
no way to tell which commit was live: the image tag said ``sha-abc1234`` while
the process said ``unknown``.

Contract these tests enforce:
  * ``Dockerfile.api`` declares ``ARG ATF_COMMIT_SHA`` and injects it into the
    ``-X`` target that ``version.go`` actually declares, so the path cannot
    drift from the module/package that owns the variable;
  * the same revision is written as an OCI label, so ``docker inspect`` names
    the commit even where the API is unreachable;
  * the Publish workflow passes the commit it checked out (``github.sha``) as
    that build argument — a workflow that forgot it would ship images whose
    tag and payload disagree about provenance again.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile.api"
PUBLISH = REPO_ROOT / ".github" / "workflows" / "publish.yml"
GOMOD = REPO_ROOT / "api" / "go.mod"
VERSION_GO = REPO_ROOT / "api" / "internal" / "handlers" / "version.go"


def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def workflow() -> dict:
    data = yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))
    # YAML 1.1 parses the `on:` key as the boolean True.
    data["on"] = data.get("on", data.get(True))
    return data


def build_job_steps() -> list[dict]:
    job = workflow()["jobs"]["images"]
    return job["steps"]


def build_push_step() -> dict:
    steps = [s for s in build_job_steps() if "build-push-action" in str(s.get("uses", ""))]
    assert steps, "the Publish workflow no longer uses docker/build-push-action"
    return steps[0]


def declared_variable() -> str:
    """The ldflags symbol for CommitSHA, read from the source of truth.

    The symbol is ``<import path>.CommitSHA``, where the import path is the
    module plus the directory inside ``api/`` (``Dockerfile.api`` copies
    ``api/`` to the build root, so the directory — not the package name alone —
    is what addresses the variable).
    """
    module = re.search(r"^module\s+(\S+)", GOMOD.read_text(encoding="utf-8"), re.M)
    assert module, "api/go.mod no longer declares a module path"
    source = VERSION_GO.read_text(encoding="utf-8")
    package = re.search(r"^package\s+(\w+)", source, re.M)
    assert package, "version.go no longer declares a package"
    assert re.search(r"CommitSHA\s+=", source), (
        "version.go no longer declares the CommitSHA ldflags variable; "
        "the Dockerfile injection targets a variable that must exist"
    )
    dir_path = VERSION_GO.parent.relative_to(REPO_ROOT / "api").as_posix()
    # A directory whose name differs from its package still resolves through
    # the directory, so guard only the case that would silently miss the symbol.
    assert dir_path.rsplit("/", 1)[-1] == package.group(1), (
        f"{VERSION_GO.name} lives in {dir_path}/ but declares package "
        f"{package.group(1)}; the ldflags symbol would not address it"
    )
    return f"{module.group(1)}/{dir_path}"


def test_dockerfile_declares_the_build_argument():
    assert re.search(r"^ARG\s+ATF_COMMIT_SHA\b", dockerfile(), re.M), (
        "Dockerfile.api must declare ARG ATF_COMMIT_SHA, or the workflow's "
        "build argument is silently dropped and the API reports 'unknown'"
    )


def test_dockerfile_injects_it_into_the_variable_version_go_declares():
    target = declared_variable()
    expected = f"-X {target}.CommitSHA=${{ATF_COMMIT_SHA}}"
    text = dockerfile()
    assert expected in text, (
        f"Dockerfile.api must build with -ldflags \"{expected}\"; "
        f"anything else leaves /api/version reporting 'unknown'"
    )
    # The build line itself, not a comment, has to carry it.
    build_lines = [
        line for line in text.splitlines()
        if "go build" in line and not line.lstrip().startswith("#")
    ]
    assert build_lines, "Dockerfile.api no longer runs a go build"
    assert any("CommitSHA=${ATF_COMMIT_SHA}" in line for line in build_lines), (
        "the -X injection is missing from the go build command"
    )


def test_dockerfile_labels_the_image_with_the_revision():
    assert re.search(
        r'^LABEL\s+org\.opencontainers\.image\.revision="?\$\{ATF_COMMIT_SHA\}',
        dockerfile(),
        re.M,
    ), (
        "Dockerfile.api must label the image with the revision it was built "
        "from, so `docker inspect` answers the provenance question offline"
    )


def test_publish_passes_the_checked_out_commit():
    step = build_push_step()
    build_args = step.get("with", {}).get("build-args")
    assert build_args, "the Publish build step passes no build-args"
    assert "ATF_COMMIT_SHA=" in build_args, (
        "Publish must pass ATF_COMMIT_SHA, or published images keep "
        "reporting commit_sha 'unknown'"
    )
    assert "github.sha" in build_args, (
        "ATF_COMMIT_SHA must come from the commit being built (github.sha), "
        "not a constant or the branch name"
    )


def test_publish_still_publishes_immutable_and_moving_tags():
    """The sha-<short> tag is what the host pins; provenance must not replace it."""
    step = build_push_step()
    tags = step["with"]["tags"]
    assert "sha-${{ steps.meta.outputs.short }}" in tags
