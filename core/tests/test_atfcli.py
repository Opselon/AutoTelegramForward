"""Unit tests for atfcli framework, console redaction, context, and command dispatch."""

from __future__ import annotations

import io
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
    out = io.StringIO()
    con = Console(stream=out, color=False)
    secret_val = "very_secret_bot_token_12345"
    con.register_secret(secret_val)

    con.line(f"Starting bot with {secret_val} on port 8080")
    rendered = out.getvalue()
    assert secret_val not in rendered
    assert "[REDACTED]" in rendered


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
        "auth", "doctor", "status", "rules", "sessions", "logs", "data", "admin",
        "docker", "deploy", "db", "certs", "net", "test", "i18n", "proto",
        "release", "ci", "watch", "system", "config"
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
