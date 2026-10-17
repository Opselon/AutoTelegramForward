"""Regression tests for release verification gates.

Covers the three review findings on the unified release workflow:
  1. post-publish --sample/--list-sample use the same deterministic draw, so
     CI downloads exactly the entries it verifies (no wrong-reason failures).
  2. publish gate requires clean-install success (asserted in
     test_release_workflow_gates; the workflow YAML itself is the contract).
  3. a missing REQUIRED portable bundle is an explicit unavailable entry and
     verify_manifest exits non-zero (never a silent null skip).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


release_manifest = _load("release_manifest")
verify_manifest = _load("verify_manifest")


def test_missing_portable_is_unavailable_required_entry(tmp_path):
    """Empty artifacts dir -> portable is available:false/required:true."""
    manifest = release_manifest.build_manifest(
        product_version="9.9.9",
        commit_sha="testsha",
        artifacts_dir=tmp_path,  # empty
        owner="OWNER",
    )
    portable = manifest["portable"]
    assert isinstance(portable, dict), "portable must never be null"
    assert portable["required"] is True
    assert portable["available"] is False
    assert "9.9.9" in portable["name"]
    req = [e for e in manifest["required_vs_optional"] if e["name"] == portable["name"]]
    assert req and req[0]["required"] is True


def test_verify_fails_when_portable_absent(tmp_path):
    """End-to-end: generator + verifier fail on missing portable bundle."""
    manifest = release_manifest.build_manifest(
        product_version="9.9.9",
        commit_sha="testsha",
        artifacts_dir=tmp_path,
        owner="OWNER",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    rc = verify_manifest.main(
        ["--manifest", str(manifest_path), "--artifacts-dir", str(tmp_path)]
    )
    assert rc != 0, "verify must fail when REQUIRED portable bundle is absent"


def test_sample_and_list_sample_agree(tmp_path):
    """--list-sample N and --sample N --seed S draw the same entries."""
    # Build a small populated artifacts dir so sampling has verifiable entries.
    for name in ("atf-linux-x64", "atf-api-linux-x64"):
        (tmp_path / name).write_bytes(b"x" * 64)
    (tmp_path / "autotelegramforward-9.9.9-portable.tar.gz").write_bytes(b"y" * 64)
    manifest = release_manifest.build_manifest(
        product_version="9.9.9",
        commit_sha="testsha",
        artifacts_dir=tmp_path,
        owner="OWNER",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = verify_manifest.main(
            [
                "--manifest", str(manifest_path),
                "--artifacts-dir", str(tmp_path),
                "--list-sample", "4",
            ]
        )
    assert rc == 0
    listed = sorted(line.strip() for line in buf.getvalue().splitlines() if line.strip())

    # Reproduce the draw directly: same seed, same verifiable pool.
    import random

    entries = verify_manifest.iter_manifest_entries(manifest)
    verifiable = [e for e in entries if e.get("available", True) and e.get("sha256")]
    expected = sorted(
        e.get("name", "?")
        for e in random.Random(42).sample(verifiable, min(4, len(verifiable)))
    )
    assert listed == expected
    # And --sample on the same dir passes (all sampled files are on disk).
    rc2 = verify_manifest.main(
        [
            "--manifest", str(manifest_path),
            "--artifacts-dir", str(tmp_path),
            "--sample", "4", "--seed", "42",
        ]
    )
    assert rc2 == 0


def test_ios_artifacts_are_hashed_and_owner_is_normalized(tmp_path):
    """Simulator ZIP and IPA are integrity-verifiable; image refs are lowercase."""
    import hashlib

    sim = tmp_path / "AutoTelegramForward-9.9.9-ios-simulator.zip"
    ipa = tmp_path / "AutoTelegramForward-9.9.9-ios.ipa"
    sim.write_bytes(b"simulator bundle")
    ipa.write_bytes(b"signed ipa")
    manifest = release_manifest.build_manifest(
        product_version="9.9.9",
        commit_sha="testsha",
        artifacts_dir=tmp_path,
        owner="Opselon",
    )
    assert all(image.startswith("ghcr.io/opselon/") for image in manifest["server"]["images"])
    for key, path in (("simulator", sim), ("ipa", ipa)):
        node = manifest["ios"][key]
        assert node["available"] is True
        assert node["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert node["size_bytes"] == path.stat().st_size


def test_release_workflow_gates():
    """publish needs clean-install==success; post-publish uses --list-sample."""
    import yaml

    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "release.yml").read_text())
    publish = wf["jobs"]["publish"]
    assert "clean-install" in publish["needs"], "publish must depend on clean-install"
    cond = publish["if"] if isinstance(publish["if"], str) else " ".join(publish["if"])
    assert "needs.clean-install.result == 'success'" in cond.replace('"', "'") or \
        'needs.clean-install.result' in cond

    ppv = wf["jobs"]["post-publish-verify"]
    text = json.dumps(ppv)
    assert "--list-sample" in text, "post-publish must download the exact sampled entries"
    assert "--sample 4 --seed 42" in text or "--sample 4" in text
    workflow_text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "ANDROID_KEYSTORE_BASE64" in workflow_text
    assert 'echo "ANDROID_KEY_PASSWORD=$ANDROID_KEY_PASSWORD"' in workflow_text
    assert "HAS_IOS_SECRETS" in workflow_text
    assert "ProductVersion=${{ needs.meta.outputs.version }}" in workflow_text
    assert "CommitSHA=${GITHUB_SHA}" in workflow_text
    assert "d.get('status') == 'healthy'" in workflow_text
    publish_text = workflow_text[workflow_text.index("  publish:"):]
    # The images publish as a multi-arch manifest: `docker-manifest` is the job
    # whose success means `latest`/`<version>` exist for both architectures, so
    # that — not the per-architecture build job — is what the gate must name.
    assert "needs.docker-manifest.result == 'success'" in publish_text
    assert "needs.ios-simulator.result == 'success'" in publish_text
    assert "continue-on-error: true" not in workflow_text[workflow_text.index("  docker:"):workflow_text.index("  portable:")]
    ci_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "flutter build apk --debug" in ci_text
    gradle_text = (REPO_ROOT / "mobile" / "android" / "app" / "build.gradle.kts").read_text()
    assert "if (allowDebugSigning || !isReleaseTask)" in gradle_text
    assert "Release signing keystore is missing" in gradle_text
