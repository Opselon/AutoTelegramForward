"""One supported interpreter range, agreed by every installer.

Defect being pinned: `install.sh` accepted any Python >= 3.10, and
`scripts/setup.sh` preferred whatever `python3` happened to be on PATH. On a
2026 distro that is Python 3.14 — and the pinned pyrogram (2.0.106) calls
`asyncio.get_event_loop()` *while it is being imported*, which Python 3.14 no
longer answers with a fresh loop, so the virtualenv the installer just built
cannot import the core at all:

    >>> import pyrogram
    RuntimeError: There is no current event loop in thread 'MainThread'.

Reproduced on the host this was fixed on (python 3.14.7), where the same import
succeeds as soon as a loop exists:

    >>> asyncio.set_event_loop(asyncio.new_event_loop()); import pyrogram
    ok

So the ceiling is 3.13 for this dependency set, and it has to be enforced where
the user first meets it — the installer — instead of surfacing later as a stack
trace from `core.main`, or as a silently broken source-mode install.

Contract these tests enforce:

  * the range lives in one place (`atfcli.platforms.PYTHON_MIN/PYTHON_MAX`);
  * `scripts/bootstrap.py`'s stdlib-only fallback literal mirrors it exactly,
    so a partial checkout refuses the same hosts for the same reason;
  * the refusal is a sentence naming the interpreter, the supported range and a
    way forward — never a bare "too new";
  * bootstrap refuses a host it cannot serve (no docker, unusable interpreter)
    and keeps going on one it can (docker mode runs the core in images that pin
    3.11), rather than blocking an install it does not need to block.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from atfcli import platforms

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bootstrap():
    """`scripts/bootstrap.py` loaded by path (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("atf_bootstrap", REPO_ROOT / "scripts" / "bootstrap.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ the range
def test_the_range_is_311_to_313_and_reports_both_directions():
    assert platforms.PYTHON_MIN == (3, 11)
    assert platforms.PYTHON_MAX == (3, 13)

    for version in ((3, 11), (3, 12), (3, 13)):
        assert platforms.python_supported(version) is True, version
        assert platforms.python_problem(version) == "", version

    for version in ((3, 9), (3, 10), (3, 14), (3, 15), (4, 0)):
        assert platforms.python_supported(version) is False, version
        problem = platforms.python_problem(version)
        assert problem, f"no problem reported for {version}"


def test_a_refusal_names_the_interpreter_the_range_and_a_way_forward():
    """A refusal has to be usable (see `atfcli/validators.py` for the same rule)."""
    too_new = platforms.python_problem((3, 14))
    assert "3.14" in too_new
    assert platforms.SUPPORTED_PYTHON in too_new
    assert "event loop" in too_new, "the reason is the actionable part"
    assert "docker" in too_new, "the way forward is not optional"

    too_old = platforms.python_problem((3, 10))
    assert "3.10" in too_old and platforms.SUPPORTED_PYTHON in too_old


def test_the_supported_range_is_what_the_pinned_pyrogram_can_actually_import():
    """The ceiling is a claim about a failure, not a policy.

    Whatever interpreter runs this suite, the range must agree with it:
    on 3.11-3.13 `import pyrogram` gets its loop, on 3.14 it raises. If a
    future pyrogram release fixes that, this test fails and the ceiling can be
    lifted deliberately rather than by assumption.
    """
    importable = False
    try:
        import pyrogram  # noqa: F401,PLC0415 - the point of the test
    except ImportError:  # pragma: no cover - pyrogram is a pinned dependency
        pytest.skip("pyrogram is not installed in this interpreter")
    except RuntimeError as exc:
        assert "event loop" in str(exc), exc
    else:
        importable = True

    assert importable is platforms.python_supported(sys.version_info), (
        f"python {sys.version.split()[0]} importable={importable} but the range says supported="
        f"{platforms.python_supported(sys.version_info)}"
    )


def test_installable_guidance_starts_with_a_command_for_this_host():
    rows = platforms.python_install_guidance(SimpleNamespace(os="linux", distro="ubuntu"))
    assert rows, "no way to install a supported interpreter"
    what, command = rows[0]
    assert "python3." in command, rows
    assert any("docker" in command for _what, command in rows), "docker mode is the no-interpreter path"


# ------------------------------------------------------------------- bootstrap
def test_bootstrap_mirrors_the_range_when_the_checkout_is_incomplete():
    """The stdlib-only fallback must fail closed on exactly the same hosts."""
    bootstrap = _bootstrap()
    assert bootstrap.MIN_PYTHON == platforms.PYTHON_MIN
    assert bootstrap.MAX_PYTHON == platforms.PYTHON_MAX
    assert bootstrap.SUPPORTED_PYTHON == platforms.SUPPORTED_PYTHON


def test_bootstrap_uses_the_shared_range_and_explains_a_too_new_interpreter(monkeypatch):
    bootstrap = _bootstrap()
    monkeypatch.setattr(sys, "version_info", (3, 14, 7, "final", 0))
    assert bootstrap.python_problem() == platforms.python_problem((3, 14, 7))

    monkeypatch.setattr(bootstrap, "_platforms", lambda: None)
    message = bootstrap.python_problem()
    assert "3.11" in message and "3.14" in message and "docker" in message


def _stub_bootstrap(bootstrap, monkeypatch, *, docker: bool):
    """Replace everything that touches the host, keeping `main`'s own logic."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(bootstrap, "docker_state", lambda: (docker, docker, "stub"))
    monkeypatch.setattr(bootstrap, "ensure_venv", lambda force: calls.setdefault("venv", Path(sys.executable)))
    monkeypatch.setattr(bootstrap, "install_deps", lambda py: calls.setdefault("deps", True))
    monkeypatch.setattr(bootstrap, "link_cli", lambda py: calls.setdefault("link", True))
    monkeypatch.setattr(bootstrap, "run_wizard", lambda py, extra: (calls.setdefault("wizard", extra), 0)[1])
    return calls


def test_an_unsupported_interpreter_without_docker_is_refused(monkeypatch, capsys):
    """Source mode on 3.14 cannot work, so the install stops — with the reason."""
    bootstrap = _bootstrap()
    calls = _stub_bootstrap(bootstrap, monkeypatch, docker=False)
    monkeypatch.setattr(sys, "version_info", (3, 14, 7, "final", 0))

    assert bootstrap.main(["--no-deps", "--no-link"]) == 2
    assert calls == {}, "nothing may be built on an interpreter the stack cannot run on"
    captured = capsys.readouterr()
    assert "3.11" in captured.out + captured.err
    assert "docker" in captured.out + captured.err


def test_docker_mode_still_installs_on_a_too_new_interpreter(monkeypatch, capsys):
    """Docker mode runs the core in images that pin 3.11 — do not block it.

    The virtualenv is only the CLI's interpreter there, and `atfcli` does not
    import pyrogram at all, so refusing would break a path that works.
    """
    bootstrap = _bootstrap()
    calls = _stub_bootstrap(bootstrap, monkeypatch, docker=True)
    monkeypatch.setattr(sys, "version_info", (3, 14, 7, "final", 0))

    assert bootstrap.main(["--no-deps", "--no-link", "--", "--no-build"]) == 0
    assert calls.get("wizard") == ["--no-build"], "the wizard still has to run"
    captured = capsys.readouterr()
    warning = captured.out + captured.err
    assert "3.14" in warning and "source-wizard" in warning, warning


def test_the_check_report_is_readable_on_an_unsupported_interpreter(monkeypatch):
    """`--check` is a report: it must survive the state it is reporting on."""
    bootstrap = _bootstrap()
    monkeypatch.setattr(sys, "version_info", (3, 14, 7, "final", 0))
    assert bootstrap.main(["--check"]) == 1


# ---------------------------------------------------------------------- doctor
def _doctor_report(monkeypatch, problem: str, *, docker: bool):
    """Run `doctor`'s interpreter check against a stubbed host."""
    from atfcli.commands import doctor  # noqa: PLC0415 - keeps the import local

    monkeypatch.setattr(platforms, "python_problem", lambda *args, **kwargs: problem)
    ctx = SimpleNamespace(repo=SimpleNamespace(docker_info=lambda refresh=False: SimpleNamespace(available=docker)))
    report = doctor.Report()
    doctor.check_python(ctx, report, fix=False)
    assert len(report.findings) == 1
    return report.findings[0]


def test_doctor_reports_a_supported_interpreter_as_ok(monkeypatch):
    finding = _doctor_report(monkeypatch, "", docker=True)
    assert finding.status == "ok"
    assert platforms.SUPPORTED_PYTHON in finding.detail


def test_doctor_only_warns_when_docker_can_carry_the_stack(monkeypatch):
    """Source mode is what breaks — say so, and do not fail a working install."""
    finding = _doctor_report(monkeypatch, "python 3.14 is newer than this stack supports", docker=True)
    assert finding.status == "warn"
    assert "source mode" in finding.detail
    assert finding.fix, "the finding has to carry the way out"


def test_doctor_fails_when_nothing_can_run_our_code(monkeypatch):
    """No docker *and* an unusable interpreter is a broken host, not a warning."""
    finding = _doctor_report(monkeypatch, "python 3.14 is newer than this stack supports", docker=False)
    assert finding.status == "fail"


def test_the_interpreter_check_is_reachable_from_the_cli():
    """`--only` is a derived vocabulary: the new check must be in it and usable."""
    from atfcli import validators  # noqa: PLC0415
    from atfcli.cli import build_parser  # noqa: PLC0415
    from atfcli.commands import doctor  # noqa: PLC0415

    assert "python" in doctor.CHECK_NAMES
    assert len(doctor.CHECK_NAMES) == len(set(doctor.CHECK_NAMES)), "duplicate check names"

    parser, _registry = build_parser()
    assert parser.parse_args(["doctor", "--only", "python"]).only == ["python"]
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    help_text = subparsers.choices["doctor"].format_help()
    assert "--only" in help_text and "python" in help_text, "the help text is generated from the same tuple"

    # The rejection path an operator hits after a typo: name the closest check.
    assert "python" in validators.suggest("pythn", doctor.CHECK_NAMES)
