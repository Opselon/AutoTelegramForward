"""`bootstrap.link_cli` — how `atf` becomes the admin's PATH command.

The fail modes this pins down are the quiet ones from a non-root install: the
first target is unwritable or already owned by someone else, and the installer
either replaced another tool's file or gave up with nothing to show for it.
"Available to the server admin by default" only means anything if the fallback
directory is actually used and a foreign file is actually left alone.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.py"


@pytest.fixture
def bootstrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The real module, with `ROOT` pointing at a throwaway checkout."""
    spec = importlib.util.spec_from_file_location("atf_bootstrap_under_test", BOOTSTRAP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # importing runs nothing: it is __main__-guarded

    root = tmp_path / "checkout"
    root.mkdir()
    wrapper = root / "atf"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o644)  # link_cli is what makes it executable

    monkeypatch.setattr(module, "ROOT", root)
    return module


def targets(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "system-bin" / "atf", tmp_path / "user-bin" / "atf"


def test_links_into_the_first_writable_target(
    bootstrap, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, user = targets(tmp_path)
    monkeypatch.setattr(bootstrap, "LINK_TARGETS", (str(system), str(user)))

    bootstrap.link_cli(Path("python3"))

    assert system.is_symlink()
    assert Path(os.path.realpath(system)) == (bootstrap.ROOT / "atf")
    assert not user.exists(), "the fallback is not used when the first target works"
    assert os.access(bootstrap.ROOT / "atf", os.X_OK), "the wrapper is made executable"


def test_falls_back_to_the_user_bin_when_the_system_dir_is_unwritable(
    bootstrap, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, user = targets(tmp_path)
    # Simulate the EACCES a non-root install gets from /usr/local/bin (the tests
    # may run as root, where a mode check would not fail — the syscall outcome is
    # what the fallback must survive, not the mode bits).
    real_symlink = os.symlink

    def deny(target: object, link: object) -> None:
        if "system-bin" in str(link):
            raise PermissionError(13, "Permission denied")
        return real_symlink(target, link)  # type: ignore[arg-type]

    monkeypatch.setattr(bootstrap.os, "symlink", deny)
    monkeypatch.setattr(bootstrap, "LINK_TARGETS", (str(system), str(user)))

    bootstrap.link_cli(Path("python3"))

    assert not system.exists()
    assert user.is_symlink(), "an install that cannot write /usr/local/bin still gets `atf`"
    assert Path(os.path.realpath(user)) == (bootstrap.ROOT / "atf")


def test_never_replaces_another_tool_that_owns_the_name(
    bootstrap, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, user = targets(tmp_path)
    system.parent.mkdir(parents=True)
    system.write_text("#!/bin/sh\necho not ours\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "LINK_TARGETS", (str(system), str(user)))

    bootstrap.link_cli(Path("python3"))

    assert system.read_text(encoding="utf-8") == "#!/bin/sh\necho not ours\n"
    assert user.is_symlink(), "a foreign file in the first location does not stop the install"


def test_an_existing_link_to_this_wrapper_is_left_alone(
    bootstrap, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, user = targets(tmp_path)
    system.parent.mkdir(parents=True)
    os.symlink(bootstrap.ROOT / "atf", system)
    monkeypatch.setattr(bootstrap, "LINK_TARGETS", (str(system), str(user)))

    bootstrap.link_cli(Path("python3"))

    assert system.is_symlink()
    assert not user.exists(), "a second link is not created once the command is already on PATH"


def test_reports_the_command_when_every_target_fails(
    bootstrap, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked_a = tmp_path / "a"
    blocked_b = tmp_path / "b"
    for directory in (blocked_a, blocked_b):
        directory.mkdir()
        (directory / "atf").write_text("someone else\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "LINK_TARGETS", (str(blocked_a / "atf"), str(blocked_b / "atf")))

    bootstrap.link_cli(Path("python3"))

    text = capsys.readouterr().out
    assert "could not link atf" in text
    # The operator is told how to run it, rather than being left with nothing.
    assert "run the CLI with" in text
    assert "atf" in text


def test_the_link_targets_include_a_user_writable_location(bootstrap) -> None:
    """The tuple is the whole promise, so it is asserted, not assumed."""
    assert bootstrap.LINK_TARGETS[0] == "/usr/local/bin/atf"
    assert bootstrap.LINK_TARGETS[1].endswith(".local/bin/atf")
