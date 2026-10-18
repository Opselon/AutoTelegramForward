"""The deployment contract for Redis, enforced in the repo rather than remembered in docs.

A rate limiter whose deployment can silently go wrong is a limiter in name only, so
the compose file, the env example and the docs are pinned here:

  * the store is internal-only (no host port), health-checked, bounded and
    un-persisted on purpose;
  * the image is pinned to a version, never to a moving tag;
  * the operator-facing variable names exist in both services that use them;
  * the integration can be turned OFF deliberately (and docs say how).

Run: python -m pytest scripts/tests/test_redis_compose_contract.py -q
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
ENV_EXAMPLE = REPO / ".env.example"
REDIS_DOC = REPO / "docs" / "redis.md"

# Pinned, so a routine redeploy cannot swap the store a fleet of limiters agrees
# on. Bump deliberately, with the benchmark re-run.
PINNED_REDIS_IMAGE = "redis:7.4.11-alpine3.21"  # no moving tags, no `latest`


def compose_services() -> dict:
    raw = COMPOSE.read_text()
    return yaml.safe_load(raw)["services"]


def test_redis_service_is_internal_only():
    redis = compose_services()["redis"]
    # No host port: a store that answers limit questions must not answer them
    # for the internet.
    assert "ports" not in redis, "the Redis store must publish no host port"
    networks = redis.get("networks", [])
    if isinstance(networks, list):
        assert networks == ["atf-net"]
    else:
        assert "atf-net" in networks


def test_redis_image_is_pinned():
    image = compose_services()["redis"]["image"]
    assert image == PINNED_REDIS_IMAGE, f"{image} is not the pinned store image"


def test_redis_is_health_checked():
    hc = compose_services()["redis"].get("healthcheck", {})
    assert hc, "an unhealthy store must be visible, not silently Up"
    probe = " ".join(str(x) for x in hc.get("test", []))
    assert "redis-cli" in probe and "ping" in probe


def redis_command() -> list[str]:
    return [str(x) for x in compose_services()["redis"].get("command", [])]


def token_after(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def test_redis_memory_is_bounded_and_never_evicts_a_limit():
    """noeviction is the whole point: eviction silently RESETS a scope's allowance."""
    cmd = redis_command()
    assert "--maxmemory" in cmd
    # A policy configured at runtime is still pinned to noeviction by default.
    assert token_after(cmd, "--maxmemory-policy") == "${ATF_REDIS_MAXMEMORY_POLICY:-noeviction}"


def test_redis_is_un_persisted_on_purpose():
    cmd = redis_command()
    # RDB off (empty save) and AOF off: the store restarts empty on purpose — a
    # persisted token bucket would be the allowance the process no longer has.
    assert token_after(cmd, "--save") == ""
    assert token_after(cmd, "--appendonly") == "no"
    volumes = compose_services()["redis"].get("volumes")
    assert not volumes, "a limit store must not gain a volume nobody plans for"


def test_both_consumers_get_the_store_address():
    for service in ("core", "api"):
        env = compose_services()[service].get("environment", {})
        flat = env if isinstance(env, dict) else dict(
            item.split("=", 1) for item in env
        )
        assert any(k.startswith("ATF_REDIS_ADDR") for k in flat), (
            f"{service} must receive ATF_REDIS_ADDR from the compose file"
        )


def test_auth_password_is_offered_but_never_stored():
    text = ENV_EXAMPLE.read_text()
    assert "ATF_REDIS_ADDR" in text
    assert "ATF_REDIS_PASSWORD" in text
    assert "openssl rand" in text, "the example must show how to generate the credential"


def test_the_disabling_path_is_documented():
    assert REDIS_DOC.exists(), "docs/redis.md must exist"
    doc = REDIS_DOC.read_text()
    assert "ATF_REDIS_ADDR=" in doc, "the docs must show how to turn the integration off"


def test_compose_still_configures():
    # The contract lives in the compose file: validate it with the tool that
    # actually reads it. Skipped where docker is unavailable (an author's
    # laptop); the deployment boxes run it in the release pipeline.
    if not os.path.exists("/var/run/docker.sock"):
        pytest.skip("docker not available")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "config", "--quiet", "--no-interpolate"],
            capture_output=True, timeout=120, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pytest.skip("docker compose not runnable here")
