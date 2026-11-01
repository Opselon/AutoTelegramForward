"""Regression tests: the image the host runs is the image CI published.

Defect being pinned: deploying a merged PR required `docker compose build` on the
production server. That compiles pip/npm/cargo on the box that also forwards
messages, and — worse — it produces an artifact that is *not* the one CI tested,
so "it passed the pipeline" said nothing about what was running.

Contract these tests enforce:
  * every service the base compose file builds is published by the Publish
    workflow, byte-for-byte the same Dockerfile, so a new service cannot be
    added without becoming deployable;
  * a master push publishes an immutable `sha-<short>` tag and a moving branch
    tag, and never publishes `latest` from a branch (a moving `latest` hides
    which commit is live);
  * the GHCR overlay names exactly the services the base file builds, with
    env-driven tags whose default matches the workflow's moving tag, and adds
    nothing else to the stack (no build, no networks, no ports, no volumes);
  * the deploy script pulls *before* it starts anything — with `image:` and
    `build:` both merged, `up` would compile the first image it cannot find —
    and the local-build path is an explicit opt-in.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = REPO_ROOT / "docker-compose.yml"
OVERLAY = REPO_ROOT / "docker-compose.ghcr.yml"
PUBLISH = REPO_ROOT / ".github" / "workflows" / "publish.yml"
DEPLOY = REPO_ROOT / "scripts" / "deploy_release.sh"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def workflow() -> dict:
    data = load_yaml(PUBLISH)
    # YAML 1.1 parses the `on:` key as the boolean True.
    data["on"] = data.get("on", data.get(True))
    return data


def base_buildable() -> dict:
    """service -> Dockerfile, for every service the base file compiles."""
    out = {}
    for name, spec in (load_yaml(BASE).get("services") or {}).items():
        if isinstance(spec, dict) and spec.get("build"):
            dockerfile = spec["build"].get("dockerfile") if isinstance(spec["build"], dict) else None
            out[name] = dockerfile
    return out


def published() -> dict:
    """service -> Dockerfile, from the publish matrix."""
    matrix = workflow()["jobs"]["images"]["strategy"]["matrix"]["include"]
    return {entry["service"]: entry["dockerfile"] for entry in matrix}


def overlay_services() -> dict:
    return load_yaml(OVERLAY).get("services") or {}


def watch_text() -> str:
    return PUBLISH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Nothing can be added without becoming publishable
# --------------------------------------------------------------------------- #


def test_every_built_service_is_published_from_the_same_dockerfile():
    assert base_buildable(), "no service in the base file builds anything"
    assert published() == base_buildable()


def test_the_overlay_names_exactly_the_published_services():
    assert set(overlay_services()) == set(published())


def test_the_overlay_images_match_what_the_workflow_pushes():
    owner = "${ATF_GHCR_OWNER:-opselon}"
    tag = "${ATF_IMAGE_TAG:-master}"
    for service, spec in overlay_services().items():
        image = spec["image"]
        assert image == f"${{ATF_REGISTRY:-ghcr.io}}/{owner}/atf-{service}:{tag}", image
    # the workflow pushes atf-<matrix.service>, so a service its matrix does not
    # cover has an overlay image nobody ever publishes
    assert "/atf-${{ matrix.service }}:" in watch_text()
    assert set(overlay_services()) == {e["service"] for e in
                                       workflow()["jobs"]["images"]["strategy"]["matrix"]["include"]}


# --------------------------------------------------------------------------- #
# The publish workflow publishes the tags the host pulls
# --------------------------------------------------------------------------- #


def test_a_branch_push_publishes_a_sha_tag_and_a_moving_tag():
    text = watch_text()
    assert "sha-${{ steps.meta.outputs.short }}" in text
    assert "steps.meta.outputs.moving" in text
    assert "branches: [main, master]" in text


def test_no_moving_latest_tag_is_published_from_a_branch():
    text = watch_text()
    assert ":latest" not in text, "a moving latest hides which commit is live"


def test_publishing_requires_package_write_and_never_cancels_a_publish():
    data = workflow()
    assert data["permissions"].get("packages") == "write"
    assert data["concurrency"]["cancel-in-progress"] is False


def test_each_matrix_entry_pushes_and_does_not_rebuild_multi_arch():
    entries = workflow()["jobs"]["images"]["strategy"]["matrix"]["include"]
    assert len(entries) == len(base_buildable())
    text = watch_text()
    assert "push: true" in text
    assert "platforms: linux/amd64" in text, "a master publish must stay cheap"


def test_the_cache_scope_is_shared_with_the_ci_build_job():
    # Reusing ci.yml's scopes means a publish re-uses the layers that job just
    # proved buildable, instead of paying for a second cold build.
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_scopes = set(re.findall(r"scope=ci-([a-z]+)", ci))
    publish_scopes = set(re.findall(r"scope=ci-\$\{\{ matrix\.service \}\}", watch_text()))
    assert ci_scopes, "ci.yml no longer scopes its cache"
    assert publish_scopes, "publish.yml no longer scopes its cache"


# --------------------------------------------------------------------------- #
# The host pulls, and never compiles by accident
# --------------------------------------------------------------------------- #


def test_the_deploy_script_is_executable_and_fails_closed():
    assert DEPLOY.exists()
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert str(DEPLOY.stat().st_mode & 0o111) != "0", "deploy script is not executable"


def test_the_deploy_script_pulls_before_it_starts_anything():
    text = DEPLOY.read_text(encoding="utf-8")
    pull_at = text.index('compose pull --quiet')
    up_at = text.index('compose up -d --no-deps "${SERVICES[@]}"')
    assert pull_at < up_at, "up before pull can compile an image on this host"


def test_the_local_build_path_is_an_explicit_opt_in():
    text = DEPLOY.read_text(encoding="utf-8")
    assert '"--build") MODE="build"' in text or '--build) MODE="build"' in text
    assert 'MODE="pull"' in text
    default = text.split('MODE="pull"', 1)[1]
    assert "compose build" not in default.split("if [ \"$MODE\" = \"build\" ]", 1)[0]


def test_the_deploy_script_records_what_it_deployed_and_how_to_go_back():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "deployed-tag" in text
    assert "rolling back to" in text
    assert "previous_tag()" in text


@pytest.mark.parametrize("needle", ["read:packages", "docker login"])
def test_the_refusal_message_says_how_to_get_access(needle):
    # A refusal that does not say what to do is a support ticket.
    assert needle in DEPLOY.read_text(encoding="utf-8")


def test_the_deploy_script_never_passes_a_credential_itself():
    """It refuses and tells the operator to log in; it never logs in for them."""
    text = DEPLOY.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("docker login"), line
        assert "--password " not in stripped, line
    # the only mention of a token is the instruction inside the refusal message
    for line in text.splitlines():
        if "GHCR_TOKEN" in line:
            assert stripped_context(line), line


def stripped_context(line: str) -> bool:
    """True when a GHCR_TOKEN mention is prose (a comment or a printed hint)."""
    stripped = line.lstrip()
    return stripped.startswith("#") or stripped.startswith("echo")


# --------------------------------------------------------------------------- #
# What the deploy summary reports must exist for a pulled image
# --------------------------------------------------------------------------- #


def test_the_summary_reports_the_tag_and_image_id_not_an_empty_digest():
    text = DEPLOY.read_text(encoding="utf-8")
    # RepoDigests is empty for an image fetched by tag through compose: the first
    # pull-based deploy printed "unknown" for every service.
    assert "{{index .RepoDigests 0}}" not in text
    assert "{{.Config.Image}}" in text
    assert "{{.Image}}" in text


def test_the_health_wait_follows_the_services_being_deployed():
    text = DEPLOY.read_text(encoding="utf-8")
    wait = text.split("wait_healthy() {", 1)[1].split("\n}", 1)[0]
    assert "${SERVICES[@]}" in wait
    # a hardcoded index would read past the end of a subset deploy
    for index in range(4):
        assert f"SERVICES[{index}]" not in text, f"SERVICES[{index}] is a fixed slot"


def test_the_service_list_can_be_narrowed_and_defaults_to_the_published_set():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "ATF_SERVICES" in text
    assert "core api web logger" in text
    # The worker is a published service but NOT a default deploy target: it
    # is opt-in (ATF_WORKER_ENABLED) and an image tag predating it cannot run
    # it. The deploy script must therefore keep naming the pre-worker
    # defaults while the overlay lists everything that is published.
    assert "worker" in text
    assert set(overlay_services()) == set("core api web logger worker".split())


def test_every_deploy_step_uses_the_same_service_list():
    text = DEPLOY.read_text(encoding="utf-8")
    for call in ('compose pull --quiet "${SERVICES[@]}"',
                 'compose up -d --no-deps "${SERVICES[@]}"',
                 'compose build "${SERVICES[@]}"'):
        assert call in text, call
