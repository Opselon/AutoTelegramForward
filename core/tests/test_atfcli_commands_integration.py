"""`atf config` and `atf console` end to end: flag → dispatch → the real file.

These run the actual dispatched command (the same argv an operator types) against
a real `.env` in a temporary install, because the unit tests above prove the
parts but not the seam: that the command actually wires the use case to the
store the `--root` names, and exits with the code the contract says.
"""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest

from atfcli.cli import dispatch
from atfcli.console import Console

ENV_SEED = """\
# the good deployment
ATF_BOT_TOKEN=123456:AA-{}{}
ATF_API_ID=17349
ATF_API_HASH={}
ATF_MASTER_KEY={}
ADMIN_USER_ID=5094837833
ATF_WEB_PORT=8088
"""


def seed(tmp_path: Path, *, permissions: int = 0o644, extra: str = "") -> Path:
    path = tmp_path / ".env"
    body = ENV_SEED.format("a" * 33, "1", "b" * 32, "k" * 40) + "\n"
    path.write_text(body + extra, encoding="utf-8")
    path.chmod(permissions)
    return path


def dispatch_ok(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = dispatch(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def install(tmp_path: Path) -> Path:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return tmp_path


def assert_doc(install: Path) -> str:
    return (install / ".env").read_text(encoding="utf-8")


# ------------------------------------------------------------------- config set
def test_config_set_writes_the_value_and_keeps_the_comments(install: Path, capsys) -> None:
    seed(install)

    code, out, err = dispatch_ok(["--root", str(install), "config", "set", "ATF_WEB_PORT=8090"], capsys)

    assert code == 0
    text = assert_doc(install)
    assert "ATF_WEB_PORT=8090" in text
    assert "# the good deployment" in text
    # mode 0600: the file carries the bot token
    assert stat.S_IMODE((install / ".env").stat().st_mode) == 0o600
    backups = list(install.glob(".env.bak.*"))
    assert len(backups) == 1
    assert "8088" in backups[0].read_text(encoding="utf-8") and "8090" not in backups[0].read_text(encoding="utf-8")


def test_config_set_refuses_an_invalid_value_and_writes_nothing(install: Path, capsys) -> None:
    seed(install)
    before = assert_doc(install)

    code, out, err = dispatch_ok(["--root", str(install), "config", "set", "ATF_WEB_PORT=notaport"], capsys)

    assert code == 2
    assert "refusing to write" in out + err
    assert assert_doc(install) == before


def test_config_set_dry_run_describes_and_writes_nothing(install: Path, capsys) -> None:
    seed(install)
    before = assert_doc(install)

    code, out, err = dispatch_ok(["--root", str(install), "config", "set", "ATF_WEB_PORT=8090", "--dry-run"], capsys)

    assert code == 0
    assert "nothing was written" in out + err
    assert assert_doc(install) == before


def test_config_diff_never_writes(install: Path, capsys) -> None:
    seed(install)
    code, out, err = dispatch_ok(["--root", str(install), "config", "diff", "ATF_WEB_PORT=8090"], capsys)
    assert code == 0
    assert "nothing was written" in out + err
    assert "8090" in out + err


def test_config_set_is_idempotent(install: Path, capsys) -> None:
    seed(install)
    dispatch_ok(["--root", str(install), "config", "set", "ATF_WEB_PORT=8090"], capsys)
    before = assert_doc(install)

    code, out, err = dispatch_ok(["--root", str(install), "config", "set", "ATF_WEB_PORT=8090"], capsys)

    assert code == 0
    assert "no changes" in out + err
    assert assert_doc(install) == before


def test_config_json_reports_the_change_without_the_secret(install: Path, capsys) -> None:
    seed(install)
    code, out, err = dispatch_ok(
        ["--root", str(install), "--json", "config", "set", "ATF_BOT_TOKEN=987654:BB" + "z" * 33], capsys
    )
    assert code == 0
    document = json.loads(out)
    assert document["written"] is True
    assert document["changed"] == ["ATF_BOT_TOKEN"]
    assert "987654" not in out


# ------------------------------------------------------- config get / list / schema
def test_config_get_masks_a_secret_and_prints_the_rest(install: Path, capsys) -> None:
    seed(install)
    secret = "AA-a-" * 7

    code, out, err = dispatch_ok(["--root", str(install), "--json", "config", "get", "ATF_BOT_TOKEN"], capsys)
    document = json.loads(out)
    assert code == 0 and document["value"] == "[REDACTED]"

    code, out, err = dispatch_ok(["--root", str(install), "--json", "config", "get", "ATF_API_ID"], capsys)
    document = json.loads(out)
    assert document["value"] == "17349"

    # `--reveal` is the deliberate bypass, and it prints exactly the value.
    code, out, err = dispatch_ok(["--root", str(install), "config", "get", "ATF_MASTER_KEY", "--reveal"], capsys)
    assert code == 0
    assert ("k" * 40) in out


def test_config_get_for_a_missing_key_explains_where_to_set_it(install: Path, capsys) -> None:
    seed(install)
    code, out, err = dispatch_ok(["--root", str(install), "config", "get", "ATF_REDIS_ADDR"], capsys)
    assert code == 4
    assert "is not set" in out + err
    assert "atf config set ATF_REDIS_ADDR=" in out + err


def test_config_list_masks_secrets(install: Path, capsys) -> None:
    seed(install)
    code, out, err = dispatch_ok(["--root", str(install), "--json", "config", "list"], capsys)
    document = json.loads(out)
    assert code == 0
    assert "123456" not in out
    assert document["ATF_API_ID"] == "17349"
    assert document["ATF_BOT_TOKEN"] == "[REDACTED]"


def test_config_schema_documents_every_group_without_secrets(install: Path, capsys) -> None:
    seed(install)
    code, out, err = dispatch_ok(["--root", str(install), "--json", "config", "schema"], capsys)
    document = json.loads(out)
    assert code == 0
    names = {row["name"] for row in document["settings"]}
    assert {"ATF_BOT_TOKEN", "ATF_WEB_PORT", "ATF_RATELIMIT_ALGO"} <= names
    assert document["groups"] == ["telegram", "admin", "security", "web", "redis", "runtime", "logging"]

    code, out, err = dispatch_ok(["--root", str(install), "config", "schema"], capsys)
    assert "TELEGRAM" in out and "SECURITY" in out
    # a plain body carries the same key names
    assert "ATF_BOT_TOKEN" in out


def test_config_schema_group_filter(install: Path, capsys) -> None:
    seed(install)
    code, out, err = dispatch_ok(["--root", str(install), "--json", "config", "schema", "--group", "redis"], capsys)
    document = json.loads(out)
    assert code == 0
    assert {row["name"] for row in document["settings"]} == {
        "ATF_REDIS_ADDR",
        "ATF_REDIS_PASSWORD",
        "ATF_REDIS_MAXMEMORY",
        "ATF_REDIS_MAXMEMORY_POLICY",
        "ATF_RATELIMIT_ALGO",
    }


# ------------------------------------------------------------------ config validate
def test_config_validate_is_clean_on_a_good_file_and_exits_zero(install: Path, capsys) -> None:
    seed(install, permissions=0o600)
    code, out, err = dispatch_ok(["--root", str(install), "config", "validate"], capsys)
    assert code == 0
    assert "configuration is valid" in out + err


def test_config_validate_exits_non_zero_and_names_the_problems(install: Path, capsys) -> None:
    seed(install, extra="ATF_WEB_PORT=99999\n")

    code, out, err = dispatch_ok(["--root", str(install), "config", "validate"], capsys)

    assert code == 1
    text = out + err
    # The file also carries the .env.example bot token placeholder.
    assert "[error]" in text
    assert "readable by other users" in text and "chmod 600" in text


def test_config_validate_reports_the_placeholder_token(tmp_path: Path, capsys) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "ATF_BOT_TOKEN=123456:ABC-your-bot-token\nATF_API_ID=17349\n", encoding="utf-8"
    )
    code, out, err = dispatch_ok(["--root", str(tmp_path), "config", "validate"], capsys)
    assert code == 1
    text = out + err
    assert "placeholder" in text
    assert "ATF_BOT_TOKEN" in text


def test_config_validate_json_counts_findings(install: Path, capsys) -> None:
    seed(install, extra="ATF_WEB_PORT=99999\n")
    code, out, err = dispatch_ok(["--root", str(install), "--json", "config", "validate"], capsys)
    assert code == 1
    document = json.loads(out)
    assert document["ok"] is False
    assert document["counts"]["error"] >= 1
    ids = {finding["id"] for finding in document["findings"]}
    assert "env-permissions" in ids


# ------------------------------------------------------------- backups / restore
def test_backups_and_restore_round_trip(install: Path, capsys) -> None:
    seed(install)
    dispatch_ok(["--root", str(install), "config", "set", "ATF_WEB_PORT=8090"], capsys)

    code, out, err = dispatch_ok(["--root", str(install), "--json", "config", "backups"], capsys)
    document = json.loads(out)
    assert code == 0
    assert document["count"] == 1
    name = document["backups"][0]["name"]

    # restore without --yes is refused (tty-less run answers the default, no)
    code, out, err = dispatch_ok(["--root", str(install), "config", "restore", name], capsys)
    assert code != 0
    assert "9080" not in assert_doc(install) and "8090" in assert_doc(install)

    code, out, err = dispatch_ok(["--root", str(install), "--yes", "config", "restore", name], capsys)
    assert code == 0
    assert "8088" in assert_doc(install) and "8090" not in assert_doc(install)


def test_config_restore_refuses_a_made_up_backup_name(install: Path, capsys) -> None:
    seed(install)
    code, out, err = dispatch_ok(["--root", str(install), "--yes", "config", "restore", "../evil"], capsys)
    assert code == 4
    assert "no backup named" in out + err


# ------------------------------------------------------------------- console CLI
def test_console_list_documents_the_actions(install: Path, capsys) -> None:
    code, out, err = dispatch_ok(["admin", "console", "--list"], capsys)
    assert code == 0
    text = out + err
    for action in ("overview", "admin-revoke", "config-audit", "broadcast"):
        assert action in text


def test_console_list_json(install: Path, capsys) -> None:
    code, out, err = dispatch_ok(["admin", "console", "--list", "--json"], capsys)
    document = json.loads(out)
    assert code == 0
    assert document["count"] >= 18
    assert {row["id"] for row in document["actions"]} >= {"admin-revoke", "broadcast"}


def test_console_runs_one_action_and_reports_the_outcome(install: Path, capsys, monkeypatch) -> None:
    """`--action` skips the menu: same implementation, zero prompt loop."""
    seed(install, permissions=0o600)
    code, out, err = dispatch_ok(["--root", str(install), "console", "--action", "config-audit", "--journal", ""], capsys)

    assert code == 0
    text = out
    assert "config-audit" in text


def test_console_piped_session_audits_then_quits_cleanly(install: Path, capsys, monkeypatch) -> None:
    seed(install, permissions=0o600)
    monkeypatch.setattr("sys.stdin", io.StringIO("13\nq\n"))

    code, out, err = dispatch_ok(["--root", str(install), "console", "--journal", ""], capsys)

    assert code == 0
    assert "session closed after 1 action(s)" in out + err
    assert "Configuration audit" in out


def test_console_piped_session_survives_a_failed_api_action(install: Path, capsys, monkeypatch) -> None:
    seed(install, permissions=0o600)
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n13\nq\n"))  # health (no API), audit, quit

    # A dead address, because on a development host the real API is often up and
    # the point here is that a *failing* action does not end the session.
    code, out, err = dispatch_ok(
        ["--root", str(install), "--url", "http://127.0.0.1:1", "--retries", "0", "console", "--journal", ""],
        capsys,
    )

    text = out + err
    assert code == 1, "the failed action decides the exit code"
    assert "session closed with 1 failed action(s)" in text


def test_console_journal_is_written_under_the_install_and_is_owner_only(
    install: Path, capsys, monkeypatch
) -> None:
    seed(install, permissions=0o600)
    monkeypatch.setattr("sys.stdin", io.StringIO("13\nq\n"))
    dispatch_ok(["--root", str(install), "console"], capsys)

    journal = install / ".atf" / "admin-console.log"
    assert journal.exists()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [row["action"] for row in rows] == ["config-audit"]
    assert rows[0]["ok"] is True


def test_console_reports_its_document_in_json_mode(install: Path, capsys, monkeypatch) -> None:
    seed(install, permissions=0o600)
    monkeypatch.setattr("sys.stdin", io.StringIO("13\nq\n"))

    code, out, err = dispatch_ok(["--root", str(install), "--json", "console", "--journal", ""], capsys)

    assert code == 0
    document = json.loads(out)
    assert document == {"ok": True, "actions": 1, "failures": 0, "quit_reason": "quit"}
