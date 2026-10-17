"""One version number, and a CI-built Android artifact for every one of them.

The version is hand-maintained in exactly two places: the `VERSION` file (read by
`core/config.py`, `atf.py` and the release meta job) and `mobile/pubspec.yaml`
(the Flutter build's `versionName`). Everything else derives from them —
`website/scripts/sync-repo-content.mjs` regenerates `repo-info.json` from
`VERSION` on every site build. A third hand-written copy is the classic way a
release ships 2.2.2 binaries under a 2.2.1 label, and nothing in the pipeline
would notice, so these guards fail at PR time instead.

The second half pins the Android half of the same promise: the production host
has no Android SDK, so the APK exists only if CI builds it — which means a *new
version* has to keep producing one. These assertions read the workflows as text
because that is exactly the contract being checked: the version input the release
passes down, the names the build writes, and the names the release copies out.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERSION_FILE = REPO / "VERSION"
PUBSPEC = REPO / "mobile" / "pubspec.yaml"
RELEASE = REPO / ".github" / "workflows" / "release.yml"
ANDROID = REPO / ".github" / "workflows" / "mobile-android.yml"
SITE_SYNC = REPO / "website" / "scripts" / "sync-repo-content.mjs"


def _version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def test_version_file_is_a_plain_semver():
    version = _version()

    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"VERSION must be bare X.Y.Z (no `v`, no suffix): {version!r}"
    )


def test_the_android_version_tracks_the_release_version():
    pubspec = PUBSPEC.read_text(encoding="utf-8")
    (line,) = [ln for ln in pubspec.splitlines() if ln.startswith("version:")]
    declared = line.split(":", 1)[1].strip()

    assert declared.startswith(f"{_version()}+"), (
        "mobile/pubspec.yaml and VERSION disagree — a build would install as one "
        f"version and be published as another: pubspec {declared!r} vs VERSION "
        f"{_version()!r}"
    )


def test_the_android_build_number_moves_forward():
    declared = [ln for ln in PUBSPEC.read_text(encoding="utf-8").splitlines()
                if ln.startswith("version:")][0].split(":", 1)[1].strip()
    build = declared.split("+", 1)[1]

    assert build.isdigit() and int(build) >= 1, (
        "Android needs a positive build number (`version: X.Y.Z+N`); Play rejects "
        f"a re-used one: {declared!r}"
    )


def test_the_release_refuses_a_tag_that_disagrees_with_the_version():
    workflow = RELEASE.read_text(encoding="utf-8")

    assert 'TAG#v' in workflow and '!= "${VERSION}"' in workflow, (
        "the release meta job must compare the pushed tag against VERSION, or a "
        "tag can publish artifacts named for a different version"
    )


def test_every_new_version_gets_a_ci_built_apk():
    release = RELEASE.read_text(encoding="utf-8")
    android = ANDROID.read_text(encoding="utf-8")

    # The release hands its resolved version to the one Android build...
    assert "uses: ./.github/workflows/mobile-android.yml" in release
    assert "version: ${{ needs.meta.outputs.version }}" in release
    # ...which stamps it into the artifact names...
    assert 'VERSION="${{ inputs.version }}"' in android
    for suffix in ("android-arm64.apk", "android-armv7.apk", "android-x86_64.apk"):
        assert f"AutoTelegramForward-${{VERSION}}-{suffix}" in android, (
            f"the Android build must name its artifact for the version: {suffix}"
        )
    assert "AutoTelegramForward-${VERSION}-android-bundle.aab" in android
    # ..., and the release attaches exactly those to the GitHub Release (the
    # stage step copies the pattern-matched files into the release payload).
    assert "name 'AutoTelegramForward-*.apk'" in release
    assert "name 'AutoTelegramForward-*.aab'" in release


def test_a_dispatch_without_a_version_falls_back_to_the_file():
    """`gh workflow run mobile-android.yml` must not need the version twice."""
    android = ANDROID.read_text(encoding="utf-8")

    assert "if [ -z \"${VERSION}\" ]" in android and "< VERSION" in android, (
        "a manual Android build with no version input must fall back to the "
        "VERSION file rather than naming artifacts with an empty version"
    )


def test_the_site_version_is_generated_from_the_version_file():
    sync = SITE_SYNC.read_text(encoding="utf-8")
    generated = json.loads(
        (REPO / "website" / "src" / "data" / "repo-info.json").read_text(encoding="utf-8")
    )

    assert "read('VERSION')" in sync and "releaseTag: `v${version}`" in sync, (
        "website/src/data/repo-info.json must be derived from VERSION, not typed"
    )
    # The committed copy may lag (it is regenerated on every site build), but it
    # must never claim a version the repo is not on.
    assert generated["version"] <= _version(), (
        "the committed website metadata claims a newer version than VERSION"
    )
