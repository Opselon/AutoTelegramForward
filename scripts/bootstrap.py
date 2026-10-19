#!/usr/bin/env python3
"""Cross-platform bootstrap: from a bare checkout to a running stack.

Run it with any supported Python (3.11-3.13 — see atfcli/platforms.py for why
the ceiling is real) on Linux, macOS or Windows:

    python3 scripts/bootstrap.py            # venv + deps + `atf setup`
    python3 scripts/bootstrap.py --check     # report only, change nothing
    python3 scripts/bootstrap.py --no-run    # install the CLI, stop before setup
    python3 scripts/bootstrap.py -- --no-build        # extra args go to `atf setup`

Why this exists next to `atf setup`: the wizard needs the CLI, and the CLI needs
a virtualenv with its dependencies. This file is the layer below that — it works
with nothing but a system Python, tells the operator exactly how to install
docker for *their* OS, and then hands off to the wizard. It is deliberately
dependency-free (stdlib only) so it can run before anything is installed.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
REQUIREMENT_FILES = ("requirements.txt", "core/requirements.txt")


def _platforms():
    """`atfcli.platforms`, or None when the checkout is incomplete.

    The interpreter range lives there so the wizard, the installers and this
    script cannot disagree; this file stays stdlib-only, so the import is
    optional and the literal below is the fallback — a test pins the two equal.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from atfcli import platforms  # noqa: PLC0415 - optional dependency

        return platforms
    except Exception:  # pragma: no cover - partial checkout
        return None


#: Mirror of `atfcli.platforms.PYTHON_MAX`, used only when that module cannot be
#: imported (a checkout without `atfcli/`). Keep the two in step: there is a
#: regression test that reads both.
MAX_PYTHON = (3, 13)
SUPPORTED_PYTHON = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}\u2013{MAX_PYTHON[0]}.{MAX_PYTHON[1]}"

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
CYAN, GREEN, YELLOW, RED, GREY = "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[90m"


def _color() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def say(text: str, color: str = CYAN) -> None:
    prefix = f"{BOLD}{color}▶{RESET} " if _color() else "> "
    print(f"{prefix}{text}", flush=True)


def ok(text: str) -> None:
    print(f"{GREEN}✓{RESET} {text}" if _color() else f"[ok] {text}", flush=True)


def warn(text: str) -> None:
    print(f"{YELLOW}!{RESET} {text}" if _color() else f"[warn] {text}", flush=True)


def fail(text: str) -> None:
    print(f"{RED}✗{RESET} {text}" if _color() else f"[fail] {text}", file=sys.stderr, flush=True)


def host_label() -> str:
    parts = [platform.platform()]
    if platform.system().lower() == "linux" and "microsoft" in platform.uname().release.lower():
        parts.append("WSL")
    return " · ".join(parts)


def docker_state() -> tuple[bool, bool, str]:
    """(client present, daemon reachable, description)."""
    exe = shutil.which("docker")
    if not exe:
        return False, False, "docker is not on PATH"
    try:
        client = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        client_text = (client.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return False, False, "docker is not runnable"
    try:
        info = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return True, False, f"{client_text} (daemon check failed)"
    if info.returncode == 0 and (info.stdout or "").strip():
        return True, True, f"{client_text} · daemon {(info.stdout or '').strip()}"
    return True, False, f"{client_text} · daemon not reachable"


def install_hints() -> list[tuple[str, str]]:
    """Per-OS docker instructions, loaded from the CLI when it is importable."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from atfcli import platforms  # noqa: PLC0415 - optional, repo may be partially checked out

        return platforms.install_guidance(platforms.detect_host())
    except Exception:  # pragma: no cover - fallback when atfcli is unavailable
        system = platform.system().lower()
        if system == "darwin":
            return [("Docker Desktop", "brew install --cask docker"), ("or Colima", "brew install colima docker && colima start")]
        if system == "windows":
            return [("Docker Desktop + WSL2", "winget install --id Docker.DockerDesktop -e")]
        return [("install script", "curl -fsSL https://get.docker.com | sh"), ("docs", "https://docs.docker.com/engine/install/")]


def python_problem() -> str:
    """Why this interpreter cannot run the stack, or "" when it can.

    The range comes from `atfcli.platforms` when it is importable, so the
    installer, the wizard and the doctor all refuse the same hosts for the same
    reason.
    """
    platforms = _platforms()
    if platforms is not None:
        return platforms.python_problem(sys.version_info)
    current = tuple(sys.version_info[:2])
    if current < MIN_PYTHON:
        return f"python {platform.python_version()} is too old — this stack needs {SUPPORTED_PYTHON}"
    if current > MAX_PYTHON:
        return (
            f"python {platform.python_version()} is newer than this stack supports ({SUPPORTED_PYTHON}) — "
            "pyrogram's sync layer needs an event loop to exist while it is imported, and Python 3.14 "
            "stopped creating one implicitly. Install a supported interpreter, or use docker mode, "
            "whose images pin 3.11"
        )
    return ""


def python_ok() -> bool:
    return not python_problem()


def python_rows() -> list[tuple[str, str]]:
    """How to get a supported interpreter on this machine."""
    platforms = _platforms()
    if platforms is not None:
        return platforms.python_install_guidance(platforms.detect_host())
    return [("python.org", "https://www.python.org/downloads/")]


def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if platform.system().lower() == "windows" else "bin/python")


def check_only() -> int:
    problems = 0
    problem = python_problem()
    if not problem:
        ok(f"python {platform.python_version()} at {sys.executable} (supported: {SUPPORTED_PYTHON})")
    else:
        fail(problem)
        for what, command in python_rows():
            print(f"    {GREY if _color() else ''}{what}: {command}{RESET}")
        problems += 1
    if venv_python().exists():
        ok(f"virtualenv present: {VENV_DIR}")
    else:
        warn(f"no virtualenv yet ({VENV_DIR}) — this script creates it")
    present, daemon, description = docker_state()
    if present and daemon:
        ok(f"docker usable: {description}")
    elif present:
        warn(f"docker installed but not running: {description}")
        for what, command in install_hints():
            print(f"    {GREY if _color() else ''}{what}: {command}{RESET}")
    else:
        warn(description)
        for what, command in install_hints():
            print(f"    {GREY if _color() else ''}{what}: {command}{RESET}")
    if (ROOT / ".env").exists():
        ok(".env present")
    else:
        warn(".env missing — `atf setup` creates it from .env.example")
    print()
    print(f"  host   {host_label()}")
    print(f"  root   {ROOT}")
    return 1 if problems else 0


def ensure_venv(force: bool) -> Path:
    py = venv_python()
    if force and VENV_DIR.exists():
        say("removing the existing virtualenv (--force)")
        shutil.rmtree(VENV_DIR, ignore_errors=True)
    if not py.exists():
        say(f"creating virtualenv at {VENV_DIR}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    else:
        ok(f"reusing virtualenv at {VENV_DIR}")
    if not py.exists():  # pragma: no cover - venv failed to create an interpreter
        fail("virtualenv creation did not produce an interpreter")
        raise SystemExit(1)
    return py


def install_deps(py: Path) -> None:
    found = [ROOT / name for name in REQUIREMENT_FILES if (ROOT / name).exists()]
    if not found:
        warn("no requirements file found — skipping dependency install")
        return
    say("installing dependencies (pip)")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
    failed = 0
    for req in found:
        result = subprocess.run([str(py), "-m", "pip", "install", "--quiet", "-r", str(req)], check=False)
        if result.returncode == 0:
            ok(f"installed {req.name}")
        else:
            failed += 1
            warn(f"{req.name} install reported errors")
    if not failed:
        ok("all dependencies installed")


def link_cli(py: Path) -> None:
    """Put `atf` on PATH when we can; otherwise print the command to use."""
    wrapper = ROOT / "atf"
    target = "/usr/local/bin/atf"
    if platform.system().lower() == "windows":
        print(f"  {DIM if _color() else ''}run the CLI with: {py} {wrapper}{RESET}")
        return
    try:
        if Path(target).is_symlink() or Path(target).exists():
            ok(f"atf already on PATH ({target})")
            return
        os.symlink(wrapper, target)
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
        ok(f"linked {target} -> {wrapper}")
    except OSError:
        print(f"  {DIM if _color() else ''}add to PATH: export PATH=\"{ROOT}:$PATH\"{RESET}")


def run_wizard(py: Path, extra: list[str]) -> int:
    say("handing off to the setup wizard")
    print()
    command = [str(py), str(ROOT / "atf"), "setup", *extra]
    return subprocess.run(command, cwd=str(ROOT)).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Bootstrap AutoTelegramForward: virtualenv, dependencies, then the setup wizard.",
    )
    parser.add_argument("--check", action="store_true", help="report readiness and change nothing")
    parser.add_argument("--force", action="store_true", help="recreate the virtualenv from scratch")
    parser.add_argument("--no-deps", action="store_true", help="skip pip install")
    parser.add_argument("--no-run", action="store_true", help="install the CLI, do not start the wizard")
    parser.add_argument("--no-link", action="store_true", help="do not symlink `atf` into /usr/local/bin")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="ask only for the bot token, keep every other default (forwarded to `atf setup`)",
    )
    parser.add_argument("setup_args", nargs="*", help="extra arguments passed to `atf setup`")
    args = parser.parse_args(argv)

    print()
    if _color():
        print(f"{BOLD}{CYAN}AutoTelegramForward · bootstrap{RESET}")
    else:
        print("AutoTelegramForward · bootstrap")
    print(f"  host  {host_label()}")
    print(f"  root  {ROOT}")
    print()

    if args.check:
        return check_only()

    problem = python_problem()
    if problem:
        # Docker mode runs our code in containers that pin 3.11, so a too-new
        # local interpreter only blocks the paths that execute on *this* host
        # (source mode). Refuse those paths, not the whole install.
        present, daemon, _description = docker_state()
        if present and daemon:
            warn(problem)
            print("  docker mode is unaffected — the images pin python 3.11")
            print("  do not use `atf setup --source-wizard` on this interpreter")
            print()
        else:
            fail(problem)
            print("  install a supported interpreter for this machine:")
            for what, command in python_rows():
                print(f"    • {what}")
                print(f"      {GREY if _color() else ''}{command}{RESET}")
            print("  (or run in docker mode: it pins python 3.11 inside the images)")
            return 2

    present, daemon, description = docker_state()
    if present and daemon:
        ok(f"docker usable: {description}")
    else:
        warn(description)
        print("  install it for this machine:")
        for what, command in install_guidance_rows():
            print(f"    • {what}")
            print(f"      {GREY if _color() else ''}{command}{RESET}")
        print("  (the wizard can also run without docker: `atf setup --source-wizard`)")
        print()

    py = ensure_venv(args.force)
    if not args.no_deps:
        install_deps(py)
    if not args.no_link:
        link_cli(py)
    if args.no_run:
        print()
        say(f"CLI ready — start it with: {py} {ROOT / 'atf'} setup")
        return 0
    extra = list(args.setup_args)
    if args.quick and "--quick" not in extra:
        # Named here as well as in `atf setup` so `install.sh --quick` can pass it
        # through without the `--` separator bootstrap uses for pass-through args.
        extra.append("--quick")
    return run_wizard(py, extra)


def install_guidance_rows() -> list[tuple[str, str]]:
    return install_hints()


if __name__ == "__main__":
    raise SystemExit(main())
