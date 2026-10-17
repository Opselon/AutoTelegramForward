"""Regression tests: the release's multi-arch image job must terminate.

Defects being pinned (release run 38972932982, tag v2.1.0):

  * the `docker` job cross-built four images for `linux/amd64,linux/arm64`
    through QEMU emulation inside a single job with no cache and no
    `timeout-minutes`. The arm64 halves crawled, the job stayed "in progress"
    until it was cancelled ~1.5 h later, `manifest`/`publish` never ran, and no
    release was ever published;
  * both architectures pushed the *same* tags, so even a slow success would have
    raced: whichever platform finished last would publish a single-architecture
    image under the multi-arch tag;
  * `manifest` and `publish` gated on `needs.docker.result` — after the job was
    split, that reference silently pointed at a job that no longer existed.

Contract these tests enforce:
  * the Docker job builds one platform per native runner (an arm64 runner, not
    QEMU) and every build step is cached and time-boxed;
  * per-architecture images are pushed under distinct `<version>-<arch>` tags, so
    two runners can never overwrite each other;
  * a separate manifest job combines them into `latest` and `<version>` and
    *verifies* that both architectures are present, and the later jobs depend on
    that manifest job — not on the per-arch build;
  * no job references a `needs.docker.*` key that does not exist;
  * `VERSION` is the semver the tag must match, and the mobile app ships that
    same version (the release workflow fails a tag that disagrees with VERSION).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
SERVICES = ("core", "logger", "api", "web")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: dict) -> list:
    return list(job.get("steps") or [])


def _build_steps(job: dict) -> list:
    return [s for s in _steps(job) if "build-push-action" in str(s.get("uses", ""))]


def _jobs_referencing(workflow: dict, needle: str) -> list:
    """Job ids whose `needs:` mentions `needle`."""
    found = []
    for name, job in workflow["jobs"].items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        if needle in needs:
            found.append(name)
    return found


# --------------------------------------------------------------------------- #
# The image build itself
# --------------------------------------------------------------------------- #
def test_docker_job_builds_natively_per_platform():
    job = _workflow()["jobs"]["docker"]
    runners = {m["platform"]: m["runner"] for m in job["strategy"]["matrix"]["include"]}

    assert runners == {"linux/amd64": "ubuntu-latest", "linux/arm64": "ubuntu-24.04-arm"}, (
        "each architecture must build on its own native runner; cross-building "
        "arm64 under QEMU is what made the release job hang"
    )
    assert not [s for s in _steps(job) if "qemu" in str(s.get("uses", ""))], (
        "QEMU emulation must not come back into the release image path"
    )


def test_every_release_build_step_is_cached_and_time_boxed():
    job = _workflow()["jobs"]["docker"]

    assert job.get("timeout-minutes"), (
        "an unbounded job is a silent hang: the v2.1.0 release sat in progress "
        "for hours instead of failing"
    )
    builds = _build_steps(job)
    assert len(builds) == len(SERVICES), f"expected {len(SERVICES)} image builds, got {len(builds)}"

    for step in builds:
        with_ = step["with"]
        assert with_.get("cache-from") and with_.get("cache-to"), (
            f"{step['name']} has no GHA cache: every release rebuilds "
            "four images from scratch"
        )
        assert "gha" in str(with_["cache-from"]) and "gha" in str(with_["cache-to"])
        # one platform per step — the matrix already fixes the architecture
        assert with_["platforms"] == "${{ matrix.platform }}"
        assert with_.get("push") is True


def test_per_arch_tags_cannot_race():
    job = _workflow()["jobs"]["docker"]
    taglists = [s["with"]["tags"].strip().splitlines() for s in _build_steps(job)]

    for tags in taglists:
        assert len(tags) == 1, f"each runner pushes one tag, got {tags}"
        assert tags[0].strip().endswith("${{ matrix.suffix }}"), (
            "per-architecture pushes must be distinguished by the arch suffix, "
            "or the two runners overwrite each other's tag"
        )
    for service in SERVICES:
        assert any(f"/atf-{service}:" in tags[0] for tags in taglists), f"{service} image not built"


# --------------------------------------------------------------------------- #
# Publishing the multi-arch tag
# --------------------------------------------------------------------------- #
def test_manifest_job_publishes_and_verifies_both_architectures():
    workflow = _workflow()
    job = workflow["jobs"]["docker-manifest"]
    script = "\n".join(str(s.get("run", "")) for s in _steps(job))

    assert "imagetools create" in script, "the per-arch images must be combined into one tag"
    for service in SERVICES:
        assert f"atf-${{IMG}}" in script or f"atf-{service}" in script or "IMG" in script
    assert "-t \"${REPO}:latest\"" in script and "-t \"${REPO}:${VERSION}\"" in script
    assert "${REPO}:${VERSION}-amd64" in script and "${REPO}:${VERSION}-arm64" in script
    assert "linux/amd64" in script and "linux/arm64" in script, (
        "the manifest job must verify both architectures are present"
    )
    assert "::error::" in script, "a missing architecture must fail the job"


def test_publishing_jobs_wait_for_the_manifest_job():
    workflow = _workflow()

    assert "docker-manifest" in _jobs_referencing(workflow, "docker"), (
        "the manifest job depends on the per-arch builds"
    )
    assert "manifest" in _jobs_referencing(workflow, "docker-manifest")
    assert "publish" in _jobs_referencing(workflow, "docker-manifest"), (
        "the release must not publish before the multi-arch tags exist"
    )


def test_no_job_references_a_missing_docker_needs_key():
    """`needs.docker.result` survived the job split and pointed at nothing."""
    workflow = _workflow()
    assert "docker" in workflow["jobs"]
    for name, job in workflow["jobs"].items():
        blob = "\n".join(
            [str(job.get("if", ""))]
            + [str(s.get("if", "")) for s in _steps(job)]
            + [str(s.get("run", "")) for s in _steps(job)]
        )
        for ref in re.findall(r"needs\.([A-Za-z0-9_-]+)\.", blob):
            assert ref in workflow["jobs"] or ref == "meta", (
                f"job {name} references needs.{ref}.* but no such job exists"
            )


# --------------------------------------------------------------------------- #
# Version, the tag, and what the apps advertise
# --------------------------------------------------------------------------- #
def test_version_is_semver_and_the_mobile_app_matches_it():
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"VERSION must be X.Y.Z, got {version!r}"
    pubspec = (REPO_ROOT / "mobile" / "pubspec.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)", pubspec, re.MULTILINE)
    assert match, "mobile/pubspec.yaml has no version line"
    assert match.group(1) == version, (
        f"mobile ships {match.group(1)} while VERSION is {version}: the release "
        "workflow refuses a tag that disagrees with VERSION, so the two must move "
        "together"
    )
