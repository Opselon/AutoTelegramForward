"""The guided administration console: the interactive path, actually executed.

The console's whole value is the loop — menu, answer, validated action, back to
the menu — so these tests drive that loop rather than the handlers behind it. Two
levels:

* the session against a scripted `SessionIO` (routing, confirmations, failure
  containment, journalling, end-of-input);
* the *real* adapter over a real `Console` and a piped stdin, because "the prompt
  re-asks after a bad answer" is behaviour the operator feels and a fake that
  returns queued strings would never verify.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

from atfcli.application.console.actions import actions as registry_actions
from atfcli.application.console.contracts import DESTRUCTIVE, Action, Confirmation, Prompt
from atfcli.application.console.session import AdminConsole
from atfcli.application.console.values import coerce
from atfcli.application.use_cases import _payload
from atfcli.application.use_cases.envconfig import Environment
from atfcli.commands.console import ConsoleIO, FileJournal
from atfcli.console import Console
from atfcli.domain.errors import AuthError, NotFoundError, UsageError
from atfcli.domain.models import AdminUser
from atfcli.infrastructure.host.envfile import EnvFileStore


# ------------------------------------------------------------------- fixtures
class ScriptedIO:
    """A `SessionIO` that answers from a queue and records what was said."""

    def __init__(self, answers: Iterable[str] = (), *, confirmations: Iterable[bool] = ()) -> None:
        self.answers = list(answers)
        self.confirmations = list(confirmations)
        self.said: list[str] = []
        self.failures: list[str] = []
        self.prompts: list[str] = []

    # output
    def title(self, text: str) -> None:
        self.said.append(f"# {text}")

    def line(self, text: str = "") -> None:
        self.said.append(text)

    def ok(self, text: str) -> None:
        self.said.append(f"ok: {text}")

    def warn(self, text: str) -> None:
        self.said.append(f"warn: {text}")

    def fail(self, text: str) -> None:
        self.failures.append(text)

    def hint(self, text: str) -> None:
        self.said.append(f"hint: {text}")

    def table(self, headers, rows, *, empty: str = "") -> None:
        self.said.append("table:" + ",".join(headers) + f":{len(list(rows))}")

    def menu(self, groups, *, legend: str = "") -> None:
        self.said.append(f"menu:{sum(len(entries) for _g, entries in groups)}")

    # input
    def read(self, prompt: str = "") -> str | None:
        if not self.answers:
            return None
        return self.answers.pop(0)

    def ask(self, prompt: Prompt) -> str:
        """Like the adapter: a validator problem becomes the failure the action
        reports — but without the re-asking loop (the adapter's loop is covered
        by its own tests)."""
        self.prompts.append(prompt.label)
        answer = self.read(prompt.label) or ""
        if prompt.validate is not None and answer:
            problem = prompt.validate(answer)
            if problem:
                raise UsageError(problem)
        return answer

    def confirm(self, question: str, *, default: bool = False) -> bool:
        return self.confirmations.pop(0) if self.confirmations else default

    def require(self, confirmation: Confirmation) -> bool:
        answer = self.read(confirmation.hint) or ""
        return confirmation.accepts(answer)

    # assertions
    def text(self) -> str:
        return "\n".join(self.said + self.failures)


class FakeAdmin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def overview(self) -> Mapping[str, Any]:
        self.calls.append(("overview", None))
        return {"users": 3, "tasks": 4, "maintenance_mode": False, "admins": {"count": 1}}

    def users(self, *, limit: int = 50, offset: int = 0) -> list[AdminUser]:
        self.calls.append(("users", limit))
        return [
            AdminUser(id="5094837833", username="capxi", role="admin", active=True, tasks=2),
            AdminUser(id="5094837834", username="", role="user", active=False, tasks=0),
        ]

    def set_user_status(self, user_id: str, status: str) -> _payload.Outcome:
        self.calls.append(("set_user_status", (user_id, status)))
        return _payload.Outcome({"success": True}, f"user {user_id} → {status}")

    def broadcast(self, message: str, *, confirm: bool) -> _payload.Outcome:
        self.calls.append(("broadcast", message))
        return _payload.Outcome({"sent": 3}, "broadcast sent to 3 user(s)")

    def settings(self) -> Mapping[str, Any]:
        return {"maintenance_mode": False, "support_username": "capxi", "webhook_secret": "do-not-print-me"}

    def save_settings(self, values: Mapping[str, Any]) -> _payload.Outcome:
        self.calls.append(("save_settings", dict(values)))
        return _payload.Outcome({"success": True}, "saved 1 setting(s)")

    def admins(self) -> list[AdminUser]:
        return [AdminUser(id="5094837833", username="capxi", role="admin")]

    def add_admin(self, user_id: str, *, username: str = "") -> _payload.Outcome:
        self.calls.append(("add_admin", (user_id, username)))
        return _payload.Outcome({"success": True}, f"{user_id} is now an admin")

    def remove_admin(self, user_id: str, *, confirm: bool) -> _payload.Outcome:
        self.calls.append(("remove_admin", (user_id, confirm)))
        return _payload.Outcome({"success": True}, f"{user_id} is no longer an admin")

    def extensions(self) -> list[Mapping[str, Any]]:
        return [{"id": "backup", "enabled": True, "version": "1.2"}, {"id": "metrics", "enabled": False}]

    def toggle_extension(self, ref: str) -> _payload.Outcome:
        self.calls.append(("toggle_extension", ref))
        return _payload.Outcome({"enabled": False}, f"toggled extension {ref} → disabled")


class FakeOps:
    def __init__(self) -> None:
        self.ready = True

    def service_health(self):
        from atfcli.domain.models import ServiceHealth

        return [
            ServiceHealth(name="api", online=True, detail="", latency_ms=4),
            ServiceHealth(name="core", online=False, detail="no gRPC answer", latency_ms=0),
        ]

    def readiness(self):
        from atfcli.domain.models import Readiness

        return Readiness(ready=self.ready, deployment="docker", missing_required=() if self.ready else ("redis",))


class FakeUseCases:
    def __init__(self, env: Environment) -> None:
        self.admin = FakeAdmin()
        self.ops = FakeOps()
        self.env = env


@pytest.fixture
def env_store(tmp_path: Path) -> EnvFileStore:
    path = tmp_path / ".env"
    path.write_text(
        "ATF_BOT_TOKEN=123456:AA" + "x" * 33 + "\n"
        "ATF_API_ID=17349\n"
        "ATF_API_HASH=" + "a" * 32 + "\n"
        "ATF_MASTER_KEY=" + "k" * 40 + "\n"
        "ADMIN_USER_ID=5094837833\n"
        "ATF_WEB_PORT=8088\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return EnvFileStore(path)


# --------------------------------------------------------------------- routing
def test_every_action_is_numbered_once_in_menu_order() -> None:
    io_ = ScriptedIO()
    session = AdminConsole(io_, FakeUseCases(Environment(EnvFileStore(Path("/nonexistent")))),
                           journal=None)
    numbered = session.numbered()
    assert sorted(a.id for a in numbered) == sorted(a.id for a in registry_actions())
    assert len({id(a) for a in numbered}) == len(numbered)
    # The first group is the read-only overview, the last is the destructive one.
    assert numbered[0].id == "overview"
    assert numbered[-1].id == "broadcast"


def test_a_number_runs_the_action_it_points_at(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["1", "q"])
    uc = FakeUseCases(Environment(env_store))
    result = AdminConsole(io_, uc, journal=None).run()

    assert result.actions == 1
    assert ("overview", None) in uc.admin.calls
    assert result.exit_code == 0


def test_an_action_id_runs_the_same_action_as_its_number(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["overview", "q"])
    uc = FakeUseCases(Environment(env_store))
    AdminConsole(io_, uc, journal=None).run()
    assert ("overview", None) in uc.admin.calls


def test_a_unique_prefix_resolves_and_an_ambiguous_one_is_refused(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO()
    session = AdminConsole(io_, FakeUseCases(Environment(env_store)), journal=None)
    assert session.resolve("over").id == "overview"

    twin_a = Action("twina", "a", "Overview", lambda ctx: None)
    twin_b = Action("twinb", "b", "Overview", lambda ctx: None)
    twinned = AdminConsole(io_, FakeUseCases(Environment(env_store)), journal=None, registry=(twin_a, twin_b))
    assert twinned.resolve("twin") is None
    assert any("matches 2 actions" in line for line in io_.said)


def test_unknown_input_warns_and_keeps_the_session_open(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["nonsense", "99", "q"])
    result = AdminConsole(io_, FakeUseCases(Environment(env_store)), journal=None).run()

    assert result.actions == 0
    assert result.quit_reason == "quit"
    # Two warnings, then the menu is shown again: the session never exited.
    assert sum(1 for line in io_.said if line.startswith("warn:")) >= 2


def test_end_of_input_closes_the_session(env_store: EnvFileStore) -> None:
    result = AdminConsole(ScriptedIO([]), FakeUseCases(Environment(env_store)), journal=None).run()
    assert result.quit_reason == "end of input"
    assert result.exit_code == 0


def test_the_step_ceiling_stops_a_stuck_loop(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["1"] * 10)
    result = AdminConsole(io_, FakeUseCases(Environment(env_store)), journal=None, max_actions=3).run()
    assert result.quit_reason == "step ceiling reached"
    assert result.actions == 3


# ---------------------------------------------------------------- containment
def test_a_refusal_ends_the_action_not_the_session(env_store: EnvFileStore) -> None:
    class Grumpy(FakeAdmin):
        def overview(self):
            raise AuthError("401 unauthorized", hint="run `atf auth login`")

    uc = FakeUseCases(Environment(env_store))
    uc.admin = Grumpy()
    io_ = ScriptedIO(["1", "1", "q"])
    result = AdminConsole(io_, uc, journal=None).run()

    assert result.actions == 2
    assert result.failures == 2
    assert any("401 unauthorized" in failure for failure in io_.failures)
    assert any("atf auth login" in line for line in io_.said)
    assert result.exit_code == 1


def test_an_unexpected_exception_is_contained_too(env_store: EnvFileStore) -> None:
    class Exploding(FakeAdmin):
        def overview(self):
            raise ValueError("boom")

    uc = FakeUseCases(Environment(env_store))
    uc.admin = Exploding()
    io_ = ScriptedIO(["1", "q"])
    result = AdminConsole(io_, uc, journal=None).run()

    assert result.failures == 1
    assert any("ValueError: boom" in failure for failure in io_.failures)
    assert "the session is still open" in io_.text()


# ------------------------------------------------------------- confirmations
def test_broadcast_requires_the_typed_word_not_a_yes(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["broadcast", "hello world", "yes", "q"])
    uc = FakeUseCases(Environment(env_store))
    AdminConsole(io_, uc, journal=None).run()

    assert ("broadcast", "hello world") not in uc.admin.calls
    assert any("cancelled" in line for line in io_.said)


def test_broadcast_runs_once_the_word_is_typed(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["broadcast", "hello world", "BROADCAST", "q"])
    uc = FakeUseCases(Environment(env_store))
    AdminConsole(io_, uc, journal=None).run()

    assert ("broadcast", "hello world") in uc.admin.calls


def test_yes_does_not_bypass_a_typed_confirmation(env_store: EnvFileStore) -> None:
    """`--yes` answers y/n prompts; the typed token is a deliberate decision."""
    io_ = ScriptedIO(["admin-revoke", "5094837834", "y", "q"])
    uc = FakeUseCases(Environment(env_store))
    AdminConsole(io_, uc, journal=None, assume_yes=True).run()

    assert not [call for call in uc.admin.calls if call[0] == "remove_admin"]


def test_yes_does_bypass_an_ordinary_confirmation(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["user-suspend", "5094837834", "q"])
    uc = FakeUseCases(Environment(env_store))
    AdminConsole(io_, uc, journal=None, assume_yes=True).run()

    assert ("set_user_status", ("5094837834", "suspended")) in uc.admin.calls


def test_a_declined_ordinary_confirmation_changes_nothing(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["user-suspend", "5094837834", "q"], confirmations=[False])
    uc = FakeUseCases(Environment(env_store))
    AdminConsole(io_, uc, journal=None).run()

    assert not [call for call in uc.admin.calls if call[0] == "set_user_status"]
    assert any("left unchanged" in line for line in io_.said)


# ------------------------------------------------------------------- journal
def test_every_action_is_journalled_with_its_outcome(env_store: EnvFileStore, tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.log"
    console = Console(stream=io.StringIO(), color=False)
    uc = FakeUseCases(Environment(env_store))
    io_ = ScriptedIO(["1", "q"])
    AdminConsole(io_, uc, journal=FileJournal(console, journal_path)).run()

    lines = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["action"] for entry in lines] == ["overview"]
    assert lines[0]["ok"] is True
    assert lines[0]["group"] == "Overview"


def test_the_journal_never_carries_a_registered_secret(env_store: EnvFileStore, tmp_path: Path) -> None:
    canary = "0123456789:canary-token-value"
    console = Console(stream=io.StringIO(), color=False)
    console.register_secret(canary)
    journal_path = tmp_path / "journal.log"
    journal = FileJournal(console, journal_path)

    journal.record({"at": "2026-01-01T00:00:00Z", "action": "overview", "ok": False, "reason": f"failed with {canary}"})

    text = journal_path.read_text(encoding="utf-8")
    assert canary not in text
    assert Console.REDACTION_NOTICE in text


def test_a_broken_journal_does_not_end_the_session(env_store: EnvFileStore) -> None:
    class Broken:
        def record(self, entry):
            raise OSError("disk full")

    io_ = ScriptedIO(["1", "q"])
    result = AdminConsole(io_, FakeUseCases(Environment(env_store)), journal=Broken()).run()

    assert result.actions == 1
    assert any("journal unavailable" in line for line in io_.said)


# --------------------------------------------------------------- config verbs
def test_config_audit_reports_errors_without_asking(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["config-audit", "q"])
    AdminConsole(io_, FakeUseCases(Environment(env_store)), journal=None).run()
    assert "table:FILE,KEYS,ERRORS,WARNINGS:1" in io_.said


def test_config_set_through_the_console_writes_and_backs_up(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["config-set", "ATF_WEB_PORT", "8090", "q"])
    uc = FakeUseCases(Environment(env_store))
    AdminConsole(io_, uc, journal=None).run()

    assert "ATF_WEB_PORT=8090" in env_store.read()
    assert env_store.backups(), "a change made through the console is rollback-able too"


def test_config_set_refuses_an_undocumented_key_in_the_guided_path(env_store: EnvFileStore) -> None:
    io_ = ScriptedIO(["config-set", "ATF_NOT_A_SETTING", "q"])
    result = AdminConsole(io_, FakeUseCases(Environment(env_store)), journal=None).run()

    assert result.failures == 1
    assert any("not a documented setting" in failure for failure in io_.failures)
    assert env_store.read().strip().count("ATF_NOT_A_SETTING") == 0


# ----------------------------------------------------------------- real adapter
@pytest.fixture
def real_io(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def build(lines: Iterable[str]) -> ConsoleIO:
        stream = io.StringIO()
        console = Console(stream=stream, color=False)
        monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + ("\n" if lines else "")))
        return ConsoleIO(console)

    return build


def test_the_real_prompt_re_asks_after_a_bad_answer(real_io) -> None:
    adapter = real_io(["notaport", "99999", "8090"])
    value = adapter.ask(
        Prompt("port", validate=lambda raw: None if raw.isdigit() and int(raw) < 10000 else "must be a small number")
    )
    assert value == "8090"


def test_the_real_prompt_gives_up_after_three_answers(real_io) -> None:
    adapter = real_io(["x", "y", "z"])
    with pytest.raises(UsageError) as caught:
        adapter.ask(Prompt("port", validate=lambda raw: "always wrong"))
    assert "no valid answer" in caught.value.message


def test_the_real_prompt_accepts_the_default_on_an_empty_line(real_io) -> None:
    adapter = real_io([""])
    assert adapter.ask(Prompt("limit", default="20")) == "20"


def test_the_real_prompt_requires_a_value_when_it_says_required(real_io) -> None:
    adapter = real_io(["", "value"])
    assert adapter.ask(Prompt("name", required=True)) == "value"


def test_the_real_typed_confirmation_is_exact(real_io) -> None:
    confirmation = Confirmation("revoke 42?", "42", hint="type 42")
    assert real_io(["42"]).require(confirmation) is True
    assert real_io([" 42 "]).require(confirmation) is True
    assert real_io(["43"]).require(confirmation) is False


def test_the_real_confirm_defaults_to_no_and_accepts_yes(real_io) -> None:
    assert real_io([""]).confirm("continue?") is False
    assert real_io(["y"]).confirm("continue?") is True
    assert real_io(["n"]).confirm("continue?", default=True) is False


def test_end_of_input_is_not_an_empty_answer(real_io) -> None:
    adapter = real_io([])
    assert adapter.read("  › ") is None


# --------------------------------------------------------------- value coercion
@pytest.mark.parametrize(
    "text,expected",
    [
        ("true", True),
        ("False", False),
        ("yes", True),
        ("null", None),
        ("7", 7),
        ("1.5", 1.5),
        ("[1,2]", [1, 2]),
        ('{"a":1}', {"a": 1}),
        ("plain text", "plain text"),
        ("", ""),
    ],
)
def test_a_guided_answer_becomes_the_same_type_a_flag_would(text: str, expected: Any) -> None:
    assert coerce(text) == expected


def test_destructive_actions_are_the_ones_that_ask_for_a_typed_token() -> None:
    destructive = {action.id for action in registry_actions() if action.danger == DESTRUCTIVE}
    assert destructive == {"admin-revoke", "config-restore", "broadcast"}
