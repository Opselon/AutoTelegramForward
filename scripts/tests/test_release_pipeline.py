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


# --------------------------------------------------------------------------- #
# Build time itself: these four images must not be able to hang a run again
# --------------------------------------------------------------------------- #
WORKFLOWS = [REPO_ROOT / ".github" / "workflows" / name for name in ("release.yml", "ci.yml")]
DOCKERFILES = {
    "core": REPO_ROOT / "Dockerfile.core",
    "logger": REPO_ROOT / "Dockerfile.logger-rust",
    "api": REPO_ROOT / "Dockerfile.api",
    "web": REPO_ROOT / "Dockerfile.web",
}
# manifest COPY (dependency layer) must come before the source COPY, per image.
DEPENDENCY_BEFORE_SOURCE = {
    "core": ("requirements.txt", "core/"),
    "logger": ("rust/logger/Cargo.toml", "rust/logger/src/"),
    "api": ("api/go.sum", "COPY api/ ."),
    "web": ("web/backend/Cargo.toml", "web/backend/src"),
}


def test_no_job_in_any_workflow_is_unbounded():
    """A job with no `timeout-minutes` can only end when GitHub cancels the run."""
    for path in WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        missing = [
            name
            for name, job in workflow["jobs"].items()
            if not job.get("timeout-minutes") and not _calls_a_reusable_workflow(job)
        ]
        assert not missing, (
            f"{path.name}: jobs {missing} have no timeout-minutes. The v2.1.0 release "
            "burned the platform's 6-hour limit because the hanging job had no bound."
        )


def _calls_a_reusable_workflow(job: dict) -> bool:
    uses = job.get("uses", "")
    return isinstance(uses, str) and uses.startswith("./")


def test_a_job_without_a_timeout_is_only_ever_a_reusable_workflow_caller():
    """The one exemption above, and the reason it is not a loophole.

    GitHub allows a *caller* job no `timeout-minutes` — adding one is not a
    harmless hint, it makes the whole workflow file invalid, so a run is created
    with zero jobs and no logs. That is how v2.2.2 published no APK: release.yml
    carried `timeout-minutes: 60` on the job calling the Android workflow, put
    there to satisfy the rule above.

    So a caller is exempt only because its bound lives on the callee. This proves
    the callee has one, for every delegation in the repository.
    """
    seen = []
    for path in WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            if not _calls_a_reusable_workflow(job):
                continue
            seen.append(name)
            assert "timeout-minutes" not in job, (
                f"{path.name}: '{name}' calls a reusable workflow and must not "
                "carry timeout-minutes — GitHub rejects the entire workflow file"
            )
            target = REPO_ROOT / job["uses"].removeprefix("./")
            assert target.exists(), f"{path.name}: '{name}' calls a missing {job['uses']}"
            callee = yaml.safe_load(target.read_text(encoding="utf-8"))
            unbounded = [
                callee_name
                for callee_name, callee_job in callee["jobs"].items()
                if not callee_job.get("timeout-minutes")
            ]
            assert not unbounded, (
                f"{path.name}: '{name}' delegates to {job['uses']}, but its jobs "
                f"{unbounded} are unbounded — the caller is exempt from the rule "
                "above only because the callee carries the bound"
            )
    assert seen, "no reusable-workflow caller found; this test would silently pass"


def test_dockerignore_keeps_the_build_context_lean():
    """308 MB of context (133 MB of a developer's node_modules) went to every build."""
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    for pattern in (".git", "**/node_modules", "**/dist", "**/target"):
        assert pattern in ignored, (
            f".dockerignore must exclude {pattern}: it is either not needed by any "
            "Dockerfile COPY or rebuilt inside the image"
        )
    # and it must not exclude anything a Dockerfile does COPY
    for needed in ("core/", "api/", "web/", "rust/", "proto/", "VERSION"):
        assert needed not in ignored, f".dockerignore excludes {needed}, which a build needs"


def test_dependency_layers_precede_sources_and_are_cache_mounted():
    for service, (manifest, source) in DEPENDENCY_BEFORE_SOURCE.items():
        text = DOCKERFILES[service].read_text(encoding="utf-8")
        assert manifest in text and source in text, f"{service}: missing layer markers"
        assert text.index(manifest) < text.index(source), (
            f"{service}: {source} is copied before {manifest}, so every source edit "
            "re-runs the dependency install"
        )
        assert "--mount=type=cache" in text, (
            f"{service}: no BuildKit cache mount, so dependency downloads and "
            "compiled artifacts are thrown away between builds"
        )


def test_web_backend_compiles_dependencies_against_a_stub_main():
    """Every UI change used to recompile the whole Rust dependency graph."""
    text = DOCKERFILES["web"].read_text(encoding="utf-8")
    stub = [line for line in text.splitlines() if "fn main()" in line and "printf" in line]

    assert stub, "Dockerfile.web has no dependency layer (stub main.rs) for the Rust backend"
    assert text.index(stub[0]) < text.index("COPY web/backend/src"), (
        "the stub dependency build must come before the real sources are copied"
    )
    assert "cp target/release/atf-web-backend /out-atf-web-backend" in text, (
        "the binary must be copied out of the cache-mounted target dir into the image"
    )


def test_no_dockerfile_contains_an_unbounded_retry_loop():
    for service, path in DOCKERFILES.items():
        text = path.read_text(encoding="utf-8")
        assert "while true" not in text and "while :" not in text, (
            f"{service}: an unbounded retry loop can hang the build forever"
        )
        for line in text.splitlines():
            if line.strip().startswith("for ") and " in " in line and "seq" not in line:
                # a bounded word list ("for i in 1 2 3 4 5") is fine; a shell glob is not
                assert "for i in 1 2" in line, f"{service}: retry loop {line.strip()!r} is not bounded"


def test_ci_docker_job_uses_caches_and_proves_a_hit():
    """CI rebuilt all four images from scratch on every PR and never checked that
    a cache existed at all."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["docker-build"]
    builds = [s for s in job["steps"] if "build-push-action" in str(s.get("uses", ""))]

    assert len(builds) == len(SERVICES), f"expected {len(SERVICES)} cached image builds, got {len(builds)}"
    for step in builds:
        with_ = step["with"]
        assert with_.get("cache-from") and with_.get("cache-to"), (
            f"ci.yml {step['name']} has no cache: every PR rebuilds it from scratch"
        )
        assert step.get("timeout-minutes"), f"ci.yml {step['name']} can hang unbounded"

    proof = [s for s in job["steps"] if "run" in s and "cache" in str(s.get("name", "")).lower()]
    assert proof, "the CI docker job must prove a cache hit, not merely set cache-from"
    script = proof[0]["run"]
    assert "type=local,src=" in script and "CACHED" in script, (
        "the proof must rebuild from a cache and fail when no layer came from it"
    )
    assert "::error::" in script, "a cache miss must fail the job"


# --------------------------------------------------------------------------- #
# The compose smoke test must start deterministically (release v2.2.0 regression)
# --------------------------------------------------------------------------- #
def test_smoke_job_starts_the_core_without_live_telegram_credentials():
    """.env.example credentials cannot log in: the core exited on the failed bot
    login, docker restarted it, and the acceptance `docker compose exec` died at
    137 mid-check — so the whole release run failed its clean-install gate."""
    workflow = _workflow()
    job = workflow["jobs"]["clean-install"]
    script = "\n".join(str(s.get("run", "")) for s in _steps(job))

    assert "ATF_DISABLE_BOT=1" in script, (
        "the smoke test must run the core in its supported gRPC-only mode; with "
        "placeholder credentials it restart-loops and kills exec'd checks"
    )
    assert "Diagnostics on failure" in [s.get("name") for s in _steps(job)], (
        "a failed smoke test must dump ps/logs/disk instead of leaving a bare exit code"
    )

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    core_env = compose["services"]["core"]["environment"]
    assert "ATF_DISABLE_BOT" in core_env, (
        "docker-compose.yml must pass ATF_DISABLE_BOT through to the core container, "
        "or setting it in .env changes nothing"
    )
    assert core_env["ATF_DISABLE_BOT"].endswith(":-}") or "${ATF_DISABLE_BOT" in str(core_env["ATF_DISABLE_BOT"]), (
        "the variable must default to empty so normal deployments are unaffected"
    )
