"""Unit tests for atfcli framework, console redaction, context, and command dispatch."""

from __future__ import annotations

import io
from types import SimpleNamespace
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atfcli.api import ApiClient, Credential, TokenStore, jwt_payload
from atfcli.cli import build_parser, dispatch, make_console
from atfcli.console import Console, human_bytes, human_duration, truncate
from atfcli.context import RepoContext, collect_settings, parse_jsonish
from atfcli.errors import (
    AtfError,
    AuthError,
    CommandFailed,
    ConflictError,
    NotFoundError,
    UsageError,
    http_status_error,
)


def test_errors_taxonomy():
    assert http_status_error(401, "unauthorized").exit_code == 3
    assert http_status_error(403, "forbidden").exit_code == 3
    assert http_status_error(404, "missing").exit_code == 4
    assert http_status_error(409, "conflict").exit_code == 7
    assert http_status_error(502, "bad gateway").exit_code == 5
    assert http_status_error(500, "internal error").exit_code == 1


def test_console_redaction():
    """A credential handed to the console must never reach the stream.

    The value is built at runtime and named for what it is in this test — a
    canary — so the assertion is about behaviour, not about a literal that
    happens to look like a token in the source.
    """
    out = io.StringIO()
    con = Console(stream=out, color=False)
    canary = "".join(["very", "_sensitive", "_canary", "_12345678"])
    con.register_secret(canary)

    con.line(f"Starting bot with {canary} on port 8080")
    rendered = out.getvalue()
    assert canary not in rendered
    assert Console.REDACTION_NOTICE in rendered


def test_console_passes_clean_lines_through():
    out = io.StringIO()
    con = Console(stream=out, color=False)
    con.register_secret("very_sensitive_canary_12345678")
    con.line("Starting bot on port 8080")
    assert "Starting bot on port 8080" in out.getvalue()


def test_console_json_mode():
    out = io.StringIO()
    con = Console(stream=out, json_mode=True)
    con.line("this human message should be silenced")
    con.json({"status": "ok", "items": [1, 2, 3]})
    
    val = out.getvalue().strip()
    parsed = json.loads(val)
    assert parsed["status"] == "ok"
    assert parsed["items"] == [1, 2, 3]


def test_console_helpers():
    assert human_bytes(500) == "500 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_duration(45) == "45s"
    assert human_duration(3665) == "1h01m"
    assert truncate("hello world", 5) == "hell…"


def test_context_settings_parsing():
    assert parse_jsonish("true") is True
    assert parse_jsonish("false") is False
    assert parse_jsonish("null") is None
    assert parse_jsonish("123") == 123
    assert parse_jsonish("12.5") == 12.5
    assert parse_jsonish("string_val") == "string_val"
    assert parse_jsonish('{"a": 1}') == {"a": 1}

    settings = collect_settings(["clean_buttons=true", "delay=5", "sub.opt=nested"])
    assert settings["clean_buttons"] is True
    assert settings["delay"] == 5
    assert settings["sub"]["opt"] == "nested"


def test_jwt_payload_decode():
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(b'{"sub":"5094837833","admin":true}').decode().rstrip("=")
    token = f"{header}.{body}.signature"
    
    payload = jwt_payload(token)
    assert payload["sub"] == "5094837833"
    assert payload["admin"] is True


def test_cli_parser_registered_groups():
    parser, registry = build_parser()
    expected_groups = [
        "auth", "setup", "platform", "doctor", "validate", "status", "rules", "sessions",
        "logs", "data", "admin", "docker", "deploy", "db", "certs", "net", "test",
        "i18n", "proto", "release", "ci", "watch", "system", "config"
    ]
    for grp in expected_groups:
        assert grp in registry, f"Missing CLI group {grp}"


def test_cli_dispatch_help(capsys):
    with pytest.raises(SystemExit) as exc:
        dispatch(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "AutoTelegramForward control CLI" in captured.out


def test_cli_dispatch_doctor_json():
    out = io.StringIO()
    # Mock doctor check dependencies
    with patch("sys.stdout", out):
        code = dispatch(["doctor", "--json"])
        # Doctor either passes or reports findings, returning 0 or 1
        assert code in (0, 1)
        res = json.loads(out.getvalue())
        assert "score" in res
        assert "findings" in res


def test_cli_settings_group_flag_keeps_structural_group():
    """`rules settings schema --group` must not clobber the top-level `group` dest.

    argparse copies each subparser's namespace over the parent's, so an inner
    option that reuses the dest of the top-level subparsers action (``group``)
    silently blanks ``args.group`` with its own default and dispatch routes to
    the wrong branch.
    """
    parser, _registry = build_parser()

    ns = parser.parse_args(["rules", "settings", "schema", "--json"])
    assert ns.group == "rules"
    assert ns.cmd == "schema"
    assert ns.json is True
    assert getattr(ns, "settings_group") == ""

    ns2 = parser.parse_args(["rules", "settings", "schema", "--group", "content_cleanup"])
    assert ns2.group == "rules"
    assert ns2.settings_group == "content_cleanup"

    for argv in (["--json", "rules", "settings", "schema"], ["rules", "settings", "schema", "--json"]):
        assert parser.parse_args(argv).group == "rules"
        assert parser.parse_args(argv).json is True


def test_cli_no_verb_option_shadows_a_structural_dest():
    """No command-level option may reuse a structural dest (see test above)."""
    import argparse

    parser, _registry = build_parser()
    # `cmd` is intentionally shared by every nesting level (innermost wins and
    # always names a real subcommand); `group` is owned solely by the top level.
    structural = {"group"}
    offences = []

    def walk(parser_obj, path):
        for action in parser_obj._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, path + [name])
                continue
            if action.dest in structural:
                offences.append(f"atf {' '.join(path)} :: {action.option_strings or action.dest}")

    walk(parser, [])
    assert offences == []


def test_cli_stack_shortcuts_expand_to_system_verbs():
    """`atf up|down|restart` are shortcuts for the system group, not new groups."""
    from atfcli.cli import COMMAND_MODULES, _expand_aliases

    assert _expand_aliases(["up", "--no-build"]) == ["system", "start", "--no-build"]
    assert _expand_aliases(["down"]) == ["system", "stop"]
    assert _expand_aliases(["restart", "core"]) == ["system", "restart", "core"]
    # global flags may precede the shortcut
    assert _expand_aliases(["--json", "up", "--no-build"]) == ["--json", "system", "start", "--no-build"]
    assert _expand_aliases(["--url", "http://x", "up"]) == ["--url", "http://x", "system", "start"]
    # a real group is never shadowed by a shortcut
    assert _expand_aliases(["status", "--json"]) == ["status", "--json"]
    assert "up" not in COMMAND_MODULES and "down" not in COMMAND_MODULES


def test_cli_validate_and_setup_are_wired():
    from atfcli.cli import _expand_aliases

    parser, registry = build_parser()
    assert registry["validate"] is not None and registry["setup"] is not None

    ns = parser.parse_args(["validate", "--json", "--strict", "--runtime", "--deep"])
    assert ns.group == "validate" and ns.strict and ns.runtime and ns.deep and ns.json
    assert ns.func is not None

    ns2 = parser.parse_args(["setup", "--check", "--admin-id", "5094837833"])
    assert ns2.group == "setup" and ns2.check and ns2.admin_id == "5094837833"
    assert ns2.func is not None

    ns3 = parser.parse_args(_expand_aliases(["--json", "up", "--no-build"]))
    assert ns3.group == "system" and ns3.cmd == "start" and ns3.json is True and ns3.no_build


def test_validate_static_checks_survive_a_cold_checkout():
    """The validator must produce findings, never crash, with no stack running."""
    from atfcli.commands import validate
    from atfcli.console import Console
    from atfcli.context import RepoContext

    sink = io.StringIO()
    console = Console(stream=sink, color=False)
    ctx = SimpleNamespace(repo=RepoContext(console), out=console, args=None)

    report = validate.run_checks(ctx, runtime=False)
    checks = {f.check for f in report.findings}
    assert {"layout", "version", "env-file", "catalog", "catalog-placeholders", "catalog-rtl", "secrets-scan"} <= checks
    assert not any("crashed" in f.detail or "check failed" in f.detail for f in report.findings), [
        f.detail for f in report.findings
    ]


# --------------------------------------------------------------- wizard & platform
def test_setup_validators_reject_wrong_answers_and_accept_right_ones():
    """Every wizard question must validate: a wrong answer is refused, not stored."""
    from atfcli import ui

    # BotFather token: <digits>:<35+ chars of A-Za-z0-9_->.
    assert ui.valid_telegram_token("nope") is not None
    assert ui.valid_telegram_token("123456789:short") is not None
    assert ui.valid_telegram_token("123456789:" + "A" * 35) is None

    assert ui.valid_int("abc", label="api_id") is not None
    assert ui.valid_int("0", minimum=1, label="api_id") is not None
    assert ui.valid_int("5094837833", minimum=1, label="admin user id") is None

    assert ui.valid_domain("not a domain") is not None
    assert ui.valid_domain("example.com") is None
    assert ui.valid_domain("") is None  # optional: blank means "no TLS"

    from atfcli.commands.setup import is_api_hash, valid_email, valid_url

    assert is_api_hash("zzzz") is False
    assert is_api_hash("B18441A1FF607E10A989891A5462E627".lower()) is True
    assert valid_url("ftp://elsewhere") is not None
    assert valid_url("http://127.0.0.1:8088") is None
    assert valid_email("nope") is not None
    assert valid_email("ops@example.com") is None


def test_setup_prompt_loop_retries_then_accepts_and_falls_back_to_default():
    from atfcli import ui
    from atfcli.console import Console

    console = Console(stream=io.StringIO(), color=False)
    answers = iter(["wrong", "123456789:" + "A" * 35])
    with patch.object(ui, "_read_line", lambda *a, **k: next(answers)):
        value = ui.ask(console, "token?", validate=ui.valid_telegram_token, required=True, tries=3)
    assert value == "123456789:" + "A" * 35

    # Three bad answers exhaust the attempts and the default wins — the wizard
    # must always terminate, even with a script feeding it garbage.
    with patch.object(ui, "_read_line", lambda *a, **k: "still-wrong"):
        value = ui.ask(console, "token?", default="fallback", validate=ui.valid_telegram_token, tries=3)
    assert value == "fallback"


def test_setup_answers_are_masked_so_json_never_carries_a_secret():
    """`--json` output must describe a secret, never contain it."""
    from atfcli.commands.setup import public_answers

    fake_token = "123456789:" + "B" * 35
    fake_hash = "a" * 32
    payload = public_answers({"bot_token": fake_token, "api_hash": fake_hash, "admin_id": "5094837833"})
    rendered = json.dumps(payload)
    assert fake_token not in rendered and fake_hash not in rendered
    assert payload["bot_token"] == f"set ({len(fake_token)} chars)"
    assert payload["admin_id"] == "5094837833"


def test_setup_answers_file_roundtrip_and_booleans(tmp_path):
    from atfcli.commands.setup import as_bool, load_answers

    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"mode": "docker", "admin_id": "5094837833", "boot_service": False, "_comment": "ignored"}), encoding="utf-8")
    answers = load_answers(SimpleNamespace(answers=str(path)))
    assert answers == {"mode": "docker", "admin_id": "5094837833", "boot_service": False}
    assert as_bool(False) is False and as_bool("no") is False and as_bool("") is True
    with pytest.raises(AtfError):
        load_answers(SimpleNamespace(answers=str(tmp_path / "missing.json")))


def test_console_table_width_ignores_ansi_escapes():
    """A colourised cell must not shift the columns after it.

    Widths are measured on visible text, so painting a cell (which adds escapes)
    cannot widen its column. Before the fix the second column started at a
    different offset on every coloured row.
    """
    from atfcli.console import strip_ansi

    console = Console(stream=io.StringIO(), color=True)
    console.table(
        ("A", "B"),
        [[console.paint("short", "green"), console.paint("ok", "green")], ["much-longer", "plain"]],
    )
    lines = [line for line in console.out.getvalue().splitlines() if line.strip()]
    expected = [
        "  " + "A".ljust(11) + "  " + "B",
        "  " + "short".ljust(11) + "  " + "ok",
        "  " + "much-longer".ljust(11) + "  " + "plain",
    ]
    assert [strip_ansi(line).rstrip() for line in lines] == [want.rstrip() for want in expected]
    assert "\033[32m" in console.out.getvalue()


def test_platform_command_reports_host_docker_and_modes():
    from atfcli.commands import platform as platform_cmd
    from atfcli.context import RepoContext

    sink = io.StringIO()
    console = Console(stream=sink, color=False)
    ctx = SimpleNamespace(repo=RepoContext(console), out=console, args=None)
    code = platform_cmd._platform(ctx, SimpleNamespace(json=True))
    assert code in (0, 1)
    payload = json.loads(sink.getvalue())
    assert {"host", "docker", "modes", "install", "ready"} <= set(payload)
    assert {mode["name"] for mode in payload["modes"]} == {"docker", "source", "portable"}
    assert payload["host"]["python"]


def test_services_probe_row_status_vocabulary():
    from atfcli.services_probe import failing, status_of

    running_ok = {"service": "api", "container": "atf-api", "state": "running", "health": "-", "reachable": True}
    absent = {"service": "api", "container": "atf-api", "state": "absent", "health": "-", "reachable": False}
    unreachable = {"service": "core", "container": "atf-core", "state": "running", "health": "healthy", "reachable": False}
    assert status_of(running_ok) == "ok"
    assert status_of(absent) == "fail" and status_of(unreachable) == "fail"
    assert failing([running_ok, absent]) == [absent]


def test_portable_entry_template_is_valid_python():
    from atfcli.commands.release import PORTABLE_ENTRY, human_size

    compile(PORTABLE_ENTRY, "portable-entry", "exec")
    assert human_size(88000).endswith("KB")
