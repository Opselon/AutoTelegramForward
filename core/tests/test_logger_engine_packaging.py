"""Packaging contract: every release artifact carries a runnable Rust engine.

The Rust logging engine replaced the in-process Python logger (`logger/main.py`,
deleted), so a deployment artifact that cannot *find* the engine has no logging
path at all. Two things have to hold, and both broke at the cutover:

1. `find_logger()` must look inside a frozen single-file bundle
   (`sys._MEIPASS`) and next to a source checkout — not only at cargo build
   output, which a release artifact never has.
2. The release workflow must build the engine and bundle it into every
   PyInstaller freeze (POSIX + Windows, one-file + one-dir).

These are regression guards, not documentation: they fail on the pre-fix tree.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import atf

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


# --------------------------------------------------------------------------- #
# 1. Engine resolution
# --------------------------------------------------------------------------- #
def test_frozen_release_looks_in_meipass(tmp_path):
    """In a frozen CLI the engine is unpacked to `sys._MEIPASS`, not cargo output."""
    code = (
        "import sys\n"
        "sys.frozen = True\n"
        f"sys._MEIPASS = {str(tmp_path)!r}\n"
        "import atf\n"
        "print(atf.FROZEN, atf.LOGGER_CANDIDATES[0])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    frozen, first = proc.stdout.split()
    assert frozen == "True"
    assert first == str(tmp_path / atf.LOGGER_BIN_NAME)


def test_source_checkout_bin_is_searched_before_cargo_output():
    """`bin/atf-logger` (what the release job stages) beats cargo build output."""
    paths = [str(c) for c in atf.LOGGER_CANDIDATES]
    bin_idx = next(i for i, p in enumerate(paths) if p.endswith(f"/bin/{atf.LOGGER_BIN_NAME}"))
    cargo_idx = next(i for i, p in enumerate(paths) if "/rust/logger/target/release/" in p)
    assert bin_idx < cargo_idx


def test_find_logger_returns_the_first_existing_candidate(tmp_path, monkeypatch):
    engine = tmp_path / atf.LOGGER_BIN_NAME
    engine.write_bytes(b"\x7fELF fake engine")
    monkeypatch.delenv("ATF_LOGGER_BIN", raising=False)
    monkeypatch.setattr(atf, "LOGGER_CANDIDATES", [tmp_path / "absent", engine])
    found = atf.find_logger()
    assert found is not None
    assert found == engine
    # The engine is executed directly, so it has to end up executable.
    assert found.stat().st_mode & 0o111


def test_find_logger_env_override_wins(tmp_path, monkeypatch):
    engine = tmp_path / "custom-engine"
    engine.write_bytes(b"\x7fELF fake engine")
    monkeypatch.setenv("ATF_LOGGER_BIN", str(engine))
    monkeypatch.setattr(atf, "LOGGER_CANDIDATES", [])
    assert atf.find_logger() == engine


def test_find_logger_is_none_when_no_engine_exists(tmp_path, monkeypatch):
    """None (not a stale path) so callers raise the actionable build hint."""
    monkeypatch.delenv("ATF_LOGGER_BIN", raising=False)
    monkeypatch.setattr(atf, "LOGGER_CANDIDATES", [tmp_path / "absent"])
    assert atf.find_logger() is None


def test_require_logger_tells_the_operator_how_to_build(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ATF_LOGGER_BIN", raising=False)
    monkeypatch.setattr(atf, "LOGGER_CANDIDATES", [tmp_path / "absent"])
    with pytest.raises(SystemExit):
        atf.require_logger()
    captured = capsys.readouterr()
    assert "atf-logger" in captured.err or "atf-logger" in captured.out
    assert "cargo build" in captured.err or "cargo build" in captured.out


# --------------------------------------------------------------------------- #
# 2. Release workflow
# --------------------------------------------------------------------------- #
def _workflow_text() -> str:
    if not RELEASE_WORKFLOW.is_file():
        pytest.skip("release workflow not present in this checkout")
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_builds_the_engine():
    text = _workflow_text()
    assert "cargo build --release --manifest-path rust/logger/Cargo.toml" in text, (
        "the release job must build the Rust logging engine"
    )
    assert "cp rust/logger/target/release/atf-logger" in text, (
        "the built engine must be staged into bin/ for the freeze"
    )


def test_every_pyinstaller_freeze_bundles_the_engine():
    text = _workflow_text()
    freezes = len(re.findall(r"pyinstaller --noconfirm", text))
    bundled = len(re.findall(r"--add-binary \"bin/atf-logger", text))
    assert freezes >= 2, f"expected at least one-file + one-dir freezes, found {freezes}"
    assert bundled == freezes, (
        f"{freezes} PyInstaller freezes but only {bundled} bundle the Rust engine — "
        "a release artifact without it cannot log"
    )


def test_release_workflow_smoke_tests_the_bundled_engine():
    """Publishing must fail if the artifact cannot run its own engine."""
    text = _workflow_text()
    assert "9108/healthz" in text, "the frozen artifact is not health-checked for its engine"
    assert "dist/data/atf_logs_rust.db" in text, (
        "the smoke test must prove the bundled engine created its archive"
    )

LEAKED_FOR_THE_PROOF = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
