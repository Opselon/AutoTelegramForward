"""The installers police the same interpreter range as the CLI.

Defect being pinned (see `core/tests/test_python_support_range.py` for the
runtime half): every shell entry point accepted *any* recent Python —
`install.sh` said ">= 3.10", `scripts/setup.sh` preferred whatever `python3`
was — while the pinned pyrogram cannot be imported on 3.14. A user on a distro
whose `python3` is 3.14 therefore got a virtualenv that could not start the
core, with nothing in the installer output saying why.

These tests run the real selection logic: a fake `python3.13`/`python3` pair on
PATH for `scripts/setup.sh`, and `install.sh`'s own `py_ok` sourced out of the
file for the range itself. The PowerShell installer is pinned by text, since CI
has no PowerShell.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from atfcli import platforms

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"

#: Absolute: the tests below run with a PATH of their own, so `bash` has to be
#: resolved before that PATH replaces this process's.
BASH = shutil.which("bash") or "/bin/bash"

RANGE = f"{platforms.PYTHON_MIN[0]}.{platforms.PYTHON_MIN[1]}-{platforms.PYTHON_MAX[0]}.{platforms.PYTHON_MAX[1]}"


def _fake_python(directory: Path, name: str, version: str, *, supported: bool, marker: str) -> Path:
    """A stand-in interpreter: answers `-c` per `supported` and reports its version."""
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "-c" ]; then\n'
        f"    exit {0 if supported else 1}\n"
        "  fi\n"
        "done\n"
        'if [ "$1" = "-V" ] || [ "$1" = "--version" ]; then\n'
        f'  echo "Python {version}"\n'
        "  exit 0\n"
        "fi\n"
        f'echo "{marker}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _toolbox(tmp_path: Path) -> Path:
    """A PATH with the fake interpreters and nothing else but `dirname`.

    The interpreter search walks PATH, so the only way to test "this host has
    only a 3.14" is a PATH that has only a 3.14. `dirname` is the one external
    command setup.sh needs (it resolves its own location).
    """
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    dirname = shutil.which("dirname")
    assert dirname, "dirname is required by scripts/setup.sh"
    (tools / "dirname").symlink_to(dirname)
    return tools


def _run(argv: list[str], *, path: Path | None = None, tmp_path: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path is not None:
        # Replace PATH outright: inheriting it would let the CI image's own
        # python3.11 answer the search and hide the case under test.
        assert tmp_path is not None
        env["PATH"] = f"{path}:{_toolbox(tmp_path)}"
    return subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, timeout=120)


# ------------------------------------------------------------------ setup.sh
def test_setup_sh_prefers_a_supported_interpreter_over_a_newer_python3(tmp_path):
    """`python3` being 3.14 must not win over the python3.13 sitting next to it."""
    _fake_python(tmp_path, "python3", "3.14.7", supported=False, marker="RAN-ON-314")
    _fake_python(tmp_path, "python3.13", "3.13.7", supported=True, marker="RAN-ON-313")

    result = _run([BASH, str(SETUP_SH), "--check"], path=tmp_path, tmp_path=tmp_path)

    assert "RAN-ON-313" in result.stdout, result.stdout + result.stderr
    assert "RAN-ON-314" not in result.stdout
    assert "3.14" not in result.stdout, "the newer interpreter was chosen"


def test_setup_sh_refuses_when_only_an_unsupported_interpreter_exists(tmp_path):
    _fake_python(tmp_path, "python3", "3.14.7", supported=False, marker="RAN-ON-314")

    result = _run([BASH, str(SETUP_SH), "--check"], path=tmp_path, tmp_path=tmp_path)

    assert result.returncode != 0
    message = result.stdout + result.stderr
    assert RANGE in message, message
    assert "docker" in message, "a refusal has to point somewhere"
    assert "RAN-ON-314" not in message, "bootstrap must not run on the refused interpreter"


# ----------------------------------------------------------------- install.sh
def _py_ok_source() -> str:
    """The real `py_ok` function, lifted out of install.sh."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(r"^py_ok\(\) \{.*?^\}", text, re.S | re.M)
    assert match, "py_ok() not found in install.sh — the test is looking at the wrong file"
    return match.group(0)


@pytest.mark.parametrize("version,verdict", [("3.11.9", 0), ("3.12.10", 0), ("3.13.7", 0), ("3.10.16", 1), ("3.14.7", 1)])
def test_install_sh_py_ok_polices_the_same_range(tmp_path, version, verdict):
    interpreter = tmp_path / f"py{version.replace('.', '')}"
    # py_ok asks for "%d.%d" — major.minor, the same shape a real interpreter prints.
    minor_version = ".".join(version.split(".")[:2])
    interpreter.write_text(f"#!/bin/sh\necho {minor_version}\n", encoding="utf-8")
    interpreter.chmod(0o755)

    script = f"{_py_ok_source()}\npy_ok {interpreter} && exit 0 || exit 1\n"
    completed = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert completed.returncode == verdict, (
        f"py_ok said {completed.returncode} for python {version}; expected {verdict} "
        f"(stdout={completed.stdout!r} stderr={completed.stderr!r})"
    )


def test_install_sh_resolves_supported_versions_before_bare_python3():
    """Order is the fix: a 2026 host has both, and only one of them works."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(r"for c in ([^;]+); do", text)
    assert match, "resolve_python's candidate list moved"
    order = [item.strip() for item in match.group(1).split()]
    assert order[:3] == ["python3.13", "python3.12", "python3.11"], order
    assert order.index("python3") > order.index("python3.11"), order


def test_the_windows_installer_polices_the_same_range():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert f"-ge {platforms.PYTHON_MIN[1]}" in text and f"-le {platforms.PYTHON_MAX[1]}" in text, (
        "install.ps1 must accept exactly the supported minors"
    )
    assert "3.10" not in text, "the old floor is still advertised"
    assert RANGE in text, "the failure message must name the supported range"


def test_no_installer_advertises_the_old_floor():
    """3.10 never worked (a dependency floor) — the docs must not promise it."""
    for path in (INSTALL_SH, INSTALL_PS1, SETUP_SH, REPO_ROOT / "scripts" / "bootstrap.py"):
        text = path.read_text(encoding="utf-8")
        assert "3.10" not in text, f"{path.name} still mentions 3.10"


def test_the_shell_ranges_agree_with_the_cli():
    """One range, four consumers: a drift here is the defect coming back."""
    setup = SETUP_SH.read_text(encoding="utf-8")
    match = re.search(r"\((\d+), (\d+)\) <= sys\.version_info\[:2\] <= \((\d+), (\d+)\)", setup)
    assert match, "scripts/setup.sh no longer states the range in one place"
    assert (int(match.group(1)), int(match.group(2))) == platforms.PYTHON_MIN
    assert (int(match.group(3)), int(match.group(4))) == platforms.PYTHON_MAX

    py_ok = _py_ok_source()
    bounds = re.search(r"-ge (\d+) && \"\$minor\" -le (\d+)", py_ok)
    assert bounds, "install.sh's py_ok no longer has a lower and upper bound"
    assert (3, int(bounds.group(1))) == platforms.PYTHON_MIN
    assert (3, int(bounds.group(2))) == platforms.PYTHON_MAX
