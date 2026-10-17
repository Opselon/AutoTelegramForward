"""The Android release artifact is built in CI, in exactly one place.

Building the APK needs a Flutter + Android SDK toolchain. The production host
does not have one (its `/opt/android-sdk` has no platforms and no sdkmanager),
so the build belongs on GitHub's runners — and it must not exist twice, or the
two copies drift and a release ships whatever the stale one last produced.

These guards keep three things true:

  * the Android build is dispatchable on its own, so a release APK can be
    produced without cutting a tag or running the whole release matrix;
  * it refuses to build without the four signing secrets instead of quietly
    falling back to the debug key (an APK signed with the debug key installs
    fine and then cannot be upgraded over — a silent trap);
  * the Release workflow delegates to it rather than carrying its own copy of
    the steps.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
MOBILE = WORKFLOWS / "mobile-android.yml"
RELEASE = WORKFLOWS / "release.yml"

SIGNING_SECRETS = (
    "ANDROID_KEYSTORE_BASE64",
    "ANDROID_KEYSTORE_PASSWORD",
    "ANDROID_KEY_ALIAS",
    "ANDROID_KEY_PASSWORD",
)


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _android_steps() -> list[dict]:
    jobs = _doc(MOBILE)["jobs"]
    (job,) = jobs.values()
    return job["steps"]


def _run_text() -> str:
    return "\n".join(
        step.get("run", "") for step in _android_steps() if isinstance(step.get("run"), str)
    )


# --------------------------------------------------------------------------- #
# It exists, and it can be started on purpose
# --------------------------------------------------------------------------- #
def test_android_build_is_dispatchable():
    """`gh workflow run mobile-android.yml` must be enough to get an APK."""
    triggers = _doc(MOBILE).get("on") or _doc(MOBILE).get(True)
    assert "workflow_dispatch" in triggers, "the Android build must be runnable on demand"


def test_android_build_is_reusable_so_the_release_can_call_it():
    triggers = _doc(MOBILE).get("on") or _doc(MOBILE).get(True)
    call = triggers.get("workflow_call")
    assert call, "the Release workflow needs to call this, not copy it"
    assert "version" in (call.get("inputs") or {}), "the release passes the version in"


def test_dispatch_can_take_a_version_but_does_not_require_one():
    triggers = _doc(MOBILE).get("on") or _doc(MOBILE).get(True)
    version = triggers["workflow_dispatch"]["inputs"]["version"]
    assert version.get("required") in (False, None)
    # ...and an empty input falls back to the VERSION file, not to a blank name.
    assert "VERSION" in _run_text()


# --------------------------------------------------------------------------- #
# It builds a signed release artifact, or it fails
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("secret", SIGNING_SECRETS)
def test_signing_secret_is_required(secret: str):
    assert f"secrets.{secret}" in MOBILE.read_text(encoding="utf-8"), (
        f"{secret} is not wired into the Android build"
    )
    assert secret in _run_text(), f"{secret} is not checked before the build"


def test_missing_signing_config_fails_the_job():
    """A debug-signed APK presented as a release is worse than no APK."""
    text = MOBILE.read_text(encoding="utf-8")
    assert "exit 1" in text
    check = _run_text()
    assert "missing" in check.lower(), "no explicit message when a signing secret is absent"
    # The keystore must be materialised from the secret into a file the Gradle
    # build reads, and that path is handed over through the environment.
    assert "base64 --decode > " in check
    assert "ANDROID_KEYSTORE_PATH=" in check


def test_release_apk_is_verified_against_the_release_certificate():
    """The artifact is checked, not assumed: apksigner must accept it."""
    run = _run_text()
    assert "verify --print-certs" in run, "the APKs are never inspected"
    assert "Debug" in run, "the debug-key case must be detected, not tolerated"


def test_all_three_abis_and_the_app_bundle_are_published():
    run = _run_text()
    for abi in ("arm64-v8a", "armeabi-v7a", "x86_64"):
        assert abi in run, f"{abi} is no longer built"
    assert "appbundle" in run, "the Play Store bundle disappeared"


def test_artifacts_land_in_the_name_the_release_contract_uses():
    """release.yml's manifest and publish jobs download `mobile-android`."""
    uploads = [
        step for step in _android_steps()
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads, "nothing is uploaded"
    assert uploads[-1]["with"]["name"] == "mobile-android"
    assert uploads[-1]["with"].get("if-no-files-found") == "error"


# --------------------------------------------------------------------------- #
# One definition, not two
# --------------------------------------------------------------------------- #
def test_release_delegates_to_the_reusable_workflow():
    jobs = _doc(RELEASE)["jobs"]
    assert "mobile-android" in jobs, "the release stopped producing Android artifacts"
    job = jobs["mobile-android"]
    assert job.get("uses") == "./.github/workflows/mobile-android.yml"
    assert job.get("secrets") == "inherit", "the signing secrets would not reach it"
    assert "version" in str(job.get("with", {}))


def test_release_apk_build_steps_exist_in_one_workflow_only():
    """A second copy of these steps is how the two get out of step.

    `ci.yml` is allowed to compile the debug target as a Gradle check on pull
    requests, and it is: that is a compile gate, not a release artifact. What
    must appear exactly once is the *release* build.
    """
    builders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"flutter build apk --release", text):
            builders.append(path.name)
    assert builders == ["mobile-android.yml"], (
        f"release APK build steps appear in {builders}; keep them in the reusable workflow"
    )


def test_the_ci_compile_gate_stays_a_compile_gate():
    """`ci.yml` may compile the debug target; it must not sign a release."""
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "flutter build apk --debug" in text
    assert "ANDROID_KEYSTORE" not in text, "the pull-request gate must not handle signing keys"


def test_the_production_host_is_never_the_builder():
    """No workflow may imply the APK is built on the deployment host."""
    text = MOBILE.read_text(encoding="utf-8")
    assert "runs-on: ubuntu-latest" in text
    assert not re.search(r"runs-on:\s*self-hosted", text)
