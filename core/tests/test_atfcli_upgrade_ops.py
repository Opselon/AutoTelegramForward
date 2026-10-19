"""Regression tests for the upgrade / port / validator surfaces of the `atf` CLI.

These pin the behaviour the CLI promises a human at the prompt:

* a refusal names the reason and, for a closed vocabulary, what was probably
  meant (`--service webhook` -> "did you mean 'web'?"), and arrives as argparse
  exit code 2 so a wrapper script can tell "you typed it wrong" from "the
  command failed";
* an *optional* filter flag (`--level`, `--service`) still accepts the empty
  string its default uses, while polices every real value — the trap is that
  argparse runs `type=` over a string default, so a validator wired to a `""`
  default refuses the invocation that omits the flag;
* `--dry-run` never changes anything, yet still reads the repository: before
  this, read-only `git` probes were suppressed too and `--dry-run upgrade`
  reported "this install is not a git checkout" — a false statement;
* the port diagnosis separates "our own stack holds the port" from "a foreign
  process does", and picks an alternative that is really bindable.
"""

from __future__ import annotations

import io
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from atfcli import ports as portmap
from atfcli import upgrade as upg
from atfcli import validators
from atfcli.cli import build_parser
from atfcli.commands import doctor
from atfcli.console import Console
from atfcli.context import RepoContext
from atfcli.errors import AtfError

#: The repository root, anchored to this file rather than the CWD: pytest may be
#: invoked from anywhere, and these tests read source files by path.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _ctx(root=None, *, dry_run: bool = False):
    """A context wired to a real console sink, optionally rooted at `root`."""
    sink = io.StringIO()
    console = Console(stream=sink, color=False)
    return SimpleNamespace(repo=RepoContext(console, root=root, dry_run=dry_run), out=console, json=False), sink


def _parse_error(argv: list[str], capsys) -> str:
    """Parse `argv`, require an argparse usage error, and return its message."""
    parser, _registry = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)
    assert excinfo.value.code == 2, f"expected a usage error for {argv}"
    return capsys.readouterr().err


# ------------------------------------------------------------------ validators
def test_every_validator_kind_a_command_references_is_registered():
    """A missing key used to surface as `KeyError: unknown validator 'service'`.

    It is raised at call time, not import time, so only the code path that
    references the kind can reveal it. Every kind the command modules name is
    checked here at once.
    """
    import re

    kinds: set[str] = set()
    pattern = re.compile(r'argparse_type\(\s*"([a-z_]+)"|choice_argparse_type\(\s*"([a-z_]+)"')
    for path in (REPO_ROOT / "atfcli" / "commands").glob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            kinds.add(match.group(1) or match.group(2))
    kinds |= {"service", "level", "port", "free_port", "duration", "domain", "url", "email"}
    assert kinds, "no validator kinds found — the scan is broken, not the registry"
    unknown = sorted(k for k in kinds if k not in validators.VALIDATORS)
    assert unknown == [], f"validators referenced by a command but not registered: {unknown}"


def test_optional_argparse_type_passes_the_empty_default_but_polices_values():
    """`optional=True` exists because argparse runs `type=` over string defaults.

    A flag whose default is `""` (`--level`, `--service`) is refused by its own
    validator when omitted — so the empty default needs an explicit escape that
    real values do not get.
    """
    strict = validators.argparse_type("port")
    optional = validators.argparse_type("port", optional=True)

    assert optional("") == "", "an omitted flag must not be refused by its own type="
    with pytest.raises(Exception):
        strict("")  # the same value is still refused when it is a real answer
    assert optional("8080") == 8080, "a numeric flag hands the command an int"
    assert optional(" 8080 ") == 8080
    with pytest.raises(Exception) as excinfo:
        optional("99999")
    assert "must be <= 65535" in str(excinfo.value)

    # the shipped filter flags keep the escape and still police real values
    assert validators.argparse_type("level", optional=True)("") == ""
    with pytest.raises(Exception) as excinfo:
        validators.argparse_type("level", optional=True)("nonsense")
    assert "log level must be one of" in str(excinfo.value)
    assert validators.argparse_type("service", optional=True)("") == ""
    with pytest.raises(Exception) as excinfo:
        validators.argparse_type("service", optional=True)("webhook")
    assert "did you mean 'web'?" in str(excinfo.value)


def test_service_argparse_type_accepts_and_normalises_a_container_name():
    """`atf docker logs atf-core` and `atf docker logs core` are one vocabulary."""
    parse = validators.service_argparse_type()
    assert parse("atf-core") == "core", "a container alias is normalised, not just tolerated"
    assert parse("CORE") == "core"
    assert parse("web") == "web"
    with pytest.raises(Exception) as excinfo:
        parse("webhook")
    assert "did you mean 'web'?" in str(excinfo.value)

    optional = validators.service_argparse_type(optional=True)
    assert optional("") == ""
    assert optional("atf-api") == "api"


def test_git_ref_validator_refuses_options_ranges_spaces_and_junk():
    assert validators.valid_git_ref("feature/upgrade-1.2") is None
    assert validators.valid_git_ref("") is not None
    assert "cannot start with '-'" in str(validators.valid_git_ref("--force"))
    assert "not a plain branch" in str(validators.valid_git_ref("main dev"))
    assert "not a plain branch" in str(validators.valid_git_ref("main..dev"))
    assert "may contain letters, digits" in str(validators.valid_git_ref("main;v2"))
    assert "not a plain branch" in str(validators.valid_git_ref("main@{"))


def test_refusals_name_the_likely_intent():
    assert "did you mean 'web'?" in str(validators.valid_service_name("webhook"))
    assert "@userinfobot" in str(validators.valid_telegram_user_id("xyz"))
    assert "api.example.com" in str(validators.valid_domain("not_a_domain"))
    assert "number must be >= 1" in str(validators.valid_int("0", minimum=1, label="number"))


# ------------------------------------------------------------- the argparse wire
def test_upgrade_verbs_parse_and_keep_their_defaults(capsys):
    parser, registry = build_parser()
    assert registry["upgrade"] is not None

    for argv in (["upgrade", "check"], ["upgrade", "plan"], ["upgrade", "history"], ["upgrade"]):
        ns = parser.parse_args(argv)
        assert ns.func is not None
        assert ns.branch is None, "a string default would be run through `type=` and refused"
        assert ns.ref == ""

    ns = parser.parse_args(["upgrade", "apply", "--stash", "--no-build", "--no-verify"])
    assert ns.stash and ns.no_build and ns.no_verify and ns.cmd == "apply"
    assert parser.parse_args(["upgrade", "check", "--no-fetch"]).no_fetch is True

    err = _parse_error(["upgrade", "--branch", "bad ref"], capsys)
    assert "not a plain branch, tag or commit" in err
    # `--branch=-x` (not `--branch -x`): argparse claims a bare `-x` as an option
    err = _parse_error(["upgrade", "--branch=-x"], capsys)
    assert "cannot start with '-'" in err
    err = _parse_error(["upgrade", "history", "--limit", "0"], capsys)
    assert "must be >= 1" in err


def test_empty_filter_values_are_accepted_and_bad_ones_refused(capsys):
    parser, _registry = build_parser()

    ns = parser.parse_args(["logs", "query", "--level", "", "--service", ""])
    assert ns.level == "" and ns.service == "" and ns.func is not None

    err = _parse_error(["logs", "query", "--level", "nonsense"], capsys)
    assert "log level must be one of" in err
    err = _parse_error(["logs", "query", "--service", "webhook"], capsys)
    assert "did you mean 'web'?" in err
    err = _parse_error(["logs", "query", "--hours", "0"], capsys)
    assert "must be >= 1" in err

    assert parser.parse_args(["logs", "query", "--hours", "48"]).hours == 48


def test_every_logs_surface_shares_the_same_filters():
    """`export` used to be the one subcommand with bare, unwired filters."""
    parser, _registry = build_parser()

    for argv in (
        ["logs", "query"],
        ["logs", "tail"],
        ["logs", "export"],
    ):
        ns = parser.parse_args([*argv, "--service", "atf-core", "--level", "warn"])
        assert ns.service == "core", f"{argv} does not normalise a container alias"
        assert ns.level == "warn"

    ns = parser.parse_args(["logs", "export", "--hours", "24"])
    assert ns.hours == 24
    ns = parser.parse_args(["logs", "export", "--level", "", "--service", ""])
    assert ns.level == "" and ns.service == ""

    with pytest.raises(SystemExit) as exitinfo:
        parser.parse_args(["logs", "export", "--service", "webhook"])
    assert exitinfo.value.code == 2


def test_numeric_flags_reach_the_command_as_numbers():
    """A validated-but-still-string number breaks the code that counts with it.

    `atf upgrade history --limit 5` died with "bad operand type for unary -:
    'str'" — `entries[-limit:]`. The default path worked because the default is
    an int, which is exactly why this needs pinning: only an explicit flag hit
    it. Numeric kinds are converted in `type=` for every command.
    """
    parser, _registry = build_parser()

    ns = parser.parse_args(["upgrade", "history", "--limit", "5"])
    assert ns.limit == 5 and isinstance(ns.limit, int)
    assert isinstance(parser.parse_args(["upgrade", "history"]).limit, int)

    assert parser.parse_args(["logs", "query", "--hours", "48"]).hours == 48
    assert parser.parse_args(["logs", "export", "--hours", "48"]).hours == 48
    assert parser.parse_args(["logs", "purge", "--older-than-days", "7"]).older_than_days == 7
    assert parser.parse_args(["docker", "logs", "core", "--tail", "50"]).tail == 50
    assert parser.parse_args(["ci", "status", "--limit", "3"]).limit == 3
    assert parser.parse_args(["net", "port", "fix", "web", "--port", "18088"]).port == 18088
    assert parser.parse_args(["setup", "--wait", "120"]).wait == 120

    # text-valued kinds must NOT be coerced: `--admin-id` is stored and
    # compared as a string, and 5094837833 would lose nothing but its type
    admin_id = parser.parse_args(["setup", "--admin-id", "5094837833"]).admin_id
    assert admin_id == "5094837833" and isinstance(admin_id, str)


def test_unknown_service_positionals_are_refused_with_a_suggestion(capsys):
    parser, _registry = build_parser()
    assert parser.parse_args(["docker", "logs", "atf-core"]).service == "core"
    err = _parse_error(["docker", "logs", "webhook"], capsys)
    assert "did you mean 'web'?" in err
    err = _parse_error(["net", "port", "fix", "webhook"], capsys)
    assert "did you mean 'web'?" in err


def test_setup_admin_id_and_wait_are_validated(capsys):
    parser, _registry = build_parser()

    ns = parser.parse_args(["setup", "--admin-id", "5094837833"])
    assert ns.admin_id == "5094837833"
    assert parser.parse_args(["setup"]).admin_id is None, "an empty default would be refused by type="
    assert parser.parse_args(["setup", "--quick"]).quick is True
    assert parser.parse_args(["setup", "--wait", "120"]).wait == 120

    err = _parse_error(["setup", "--admin-id", "xyz"], capsys)
    assert "@userinfobot" in err
    err = _parse_error(["setup", "--wait", "soon"], capsys)
    assert "must be numeric" in err


def test_doctor_only_is_a_derived_vocabulary(capsys):
    """Help text, rejection advice and dispatch come from one tuple."""
    assert doctor.CHECK_NAMES == tuple(c.__name__.removeprefix("check_") for c in doctor.CHECKS)
    assert {"docker", "repo", "ports", "firewall"} <= set(doctor.CHECK_NAMES)

    parser, _registry = build_parser()
    ns = parser.parse_args(["doctor", "--only", "docker", "ports"])
    assert ns.only == ["docker", "ports"]
    assert parser.parse_args(["doctor"]).only is None, "no --only means every check"

    # the help text is generated from the same tuple, so it cannot drift
    with pytest.raises(SystemExit) as exitinfo:
        parser.parse_args(["doctor", "--help"])
    assert exitinfo.value.code == 0
    help_text = capsys.readouterr().out
    for name in doctor.CHECK_NAMES:
        assert name in help_text, f"{name} is missing from `atf doctor --only` help"

    # an unknown name is refused by the command (it can suggest), not silently run
    ctx, _sink = _ctx()
    with pytest.raises(AtfError) as excinfo:
        doctor._doctor(ctx, SimpleNamespace(only=["doctro"], fix=False, json=False))
    assert "unknown check" in excinfo.value.message
    assert "did you mean" in excinfo.value.message
    assert "docker" in excinfo.value.hint


def test_a_suggestion_is_appended_exactly_once():
    """`validators.suggest` returns the whole tail (" — did you mean 'x'?").

    Two call sites wrapped it again, so a single typo printed
    `did you mean " — did you mean 'docker'?"?`. The doubling is invisible to a
    `in message` assertion — it has to be counted.
    """
    from atfcli.commands import doctor as doctor_cmd
    from atfcli.commands import net as net_cmd

    ctx, _sink = _ctx()
    with pytest.raises(AtfError) as excinfo:
        doctor_cmd._doctor(ctx, SimpleNamespace(only=["doctro"], fix=False, json=False))
    message = excinfo.value.message
    assert message == "unknown check 'doctro' — did you mean 'docker'?", message
    assert message.count("did you mean") == 1

    with pytest.raises(AtfError) as excinfo:
        net_cmd._ports_fix(
            ctx,
            SimpleNamespace(service="webhok", port=None, json=False, no_recreate=True, force=False),
        )
    message = excinfo.value.message
    assert message == "unknown service 'webhok' — did you mean 'web'?", message
    assert message.count("did you mean") == 1


def test_bootstrap_forwards_quick_to_the_wizard(monkeypatch):
    """`install.sh --quick` must arrive at `atf setup --quick`.

    bootstrap parses its own flags and *passes through* everything after `--`, so
    a `--quick` it does not recognise would end as `bootstrap.py: error:
    unrecognized arguments: --quick` — before the wizard ever runs.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("atf_bootstrap", REPO_ROOT / "scripts" / "bootstrap.py")
    assert spec is not None and spec.loader is not None
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(bootstrap, "docker_state", lambda: (True, True, "stub"))
    monkeypatch.setattr(bootstrap, "ensure_venv", lambda force: Path(sys.executable))
    monkeypatch.setattr(bootstrap, "run_wizard", lambda py, extra: (seen.update(extra=extra), 0)[1])

    assert bootstrap.main(["--quick", "--no-deps", "--no-link"]) == 0
    assert seen["extra"] == ["--quick"]

    seen.clear()
    assert bootstrap.main(["--no-deps", "--no-link"]) == 0
    assert seen["extra"] == [], "no flag means no forwarded argument"

    seen.clear()
    assert bootstrap.main(["--quick", "--no-deps", "--no-link", "--", "--no-build"]) == 0
    assert sorted(seen["extra"]) == ["--no-build", "--quick"], "pass-through and --quick coexist once each"

    # --check is a report, never a wizard run
    seen.clear()
    monkeypatch.setattr(bootstrap, "check_only", lambda: 0)
    assert bootstrap.main(["--quick", "--check"]) == 0
    assert seen == {}


def test_doctor_only_runs_the_selected_check_only():
    ctx, sink = _ctx()
    code = doctor._doctor(ctx, SimpleNamespace(only=["repo"], fix=False, json=True))
    assert code in (0, 1)
    payload = json.loads(sink.getvalue())
    assert [f["check"] for f in payload["findings"]] == ["repository"]
    assert "ports" not in payload, "an enrichment for a check that did not run"


# ----------------------------------------------------------------------- ports
SS_SAMPLE = (
    'LISTEN 0      128          0.0.0.0:8088       0.0.0.0:*    '
    'users:(("docker-proxy",pid=4067508,fd=7))\n'
    'LISTEN 0      511          0.0.0.0:443        0.0.0.0:*    '
    'users:(("nginx",pid=3972267,fd=8))\n'
)
LSOF_SAMPLE = "p4242\ncnginx\nn*:443\n"


def test_parse_ss_lsof_and_proc_net_identify_the_holder():
    stack = portmap.parse_ss(SS_SAMPLE, 8088)
    assert stack is not None and stack.pid == 4067508 and stack.process == "docker-proxy"
    assert stack.kind == "stack" and stack.source == "ss"
    assert portmap.parse_ss(SS_SAMPLE, 9999) is None

    foreign = portmap.parse_ss(SS_SAMPLE, 443)
    assert foreign is not None and foreign.kind == "foreign"

    via_lsof = portmap.parse_lsof(LSOF_SAMPLE, 443)
    assert via_lsof is not None and via_lsof.pid == 4242 and via_lsof.process == "nginx"
    assert portmap.parse_lsof("", 443) is None

    # 1F90 = 8080, state 0A = LISTEN
    proc_net = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n" \
               "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 98765 1 0\n"
    assert portmap.parse_proc_net(proc_net, 8080) == 98765
    assert portmap.parse_proc_net(proc_net, 8081) is None


def test_classify_separates_our_stack_from_a_foreign_holder():
    assert portmap.classify("docker-proxy", "users:((\"docker-proxy\",pid=1))") == "stack"
    assert portmap.classify("dockerd", "") == "stack"
    assert portmap.classify("nginx", "") == "foreign"
    assert portmap.classify("", "") == "unknown"


def test_free_port_skips_a_port_that_is_really_bound():
    """The bind test is the whole point: a "free" port must be bindable now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]

        assert portmap.free_port(taken, host="127.0.0.1", span=0) is None, "span=0 is only that port"
        candidate = portmap.free_port(taken, host="127.0.0.1", span=64)
        assert candidate is not None and candidate != taken

    # a window entirely below the privilege line yields no candidate, rather
    # than an answer that would need root to bind
    assert portmap.free_port(80, host="127.0.0.1", span=64) is None
    assert portmap.LOWEST_AUTO_PORT > 80


def test_put_env_value_replaces_in_place_and_appends():
    body = "# the operator reads this file\nATF_API_PORT=8080\nATF_LOG_LEVEL=INFO\n"
    replaced = portmap.put_env_value(body, "ATF_API_PORT", "18080")
    assert replaced == "# the operator reads this file\nATF_API_PORT=18080\nATF_LOG_LEVEL=INFO\n"

    appended = portmap.put_env_value(body, "ATF_WEB_PORT", "18088")
    assert appended.splitlines()[-1] == "ATF_WEB_PORT=18088"
    assert "# the operator reads this file" in appended
    assert appended.count("ATF_WEB_PORT") == 1
    # idempotent: writing the same value twice does not duplicate the key
    assert portmap.put_env_value(appended, "ATF_WEB_PORT", "18088").count("ATF_WEB_PORT") == 1


def test_expected_ports_reads_compose_and_lets_dotenv_win(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  api:\n    ports:\n      - \"${ATF_API_PORT:-8080}:8080\"\n"
        "  web:\n    ports:\n      - \"${ATF_WEB_PORT:-8099}:8088\"\n",
        encoding="utf-8",
    )
    ctx, _sink = _ctx(tmp_path)
    ports = portmap.expected_ports(ctx)
    assert ports["api"] == 8080
    assert ports["web"] == 8099, "the compose default is read, not assumed"

    (tmp_path / ".env").write_text("ATF_WEB_PORT=9099\n", encoding="utf-8")
    ctx, _sink = _ctx(tmp_path)
    assert portmap.expected_ports(ctx)["web"] == 9099, ".env overrides the compose default"
    assert portmap.host_port_for(ctx, "web") == 9099


def test_port_diagnosis_json_and_the_failing_filter():
    ok = portmap.Diagnosis(service="api", port=8080, listening=True, verdict="ok", container="atf-api")
    conflicted = portmap.Diagnosis(
        service="web",
        port=8088,
        listening=True,
        verdict="conflict",
        container="atf-web",
        owner=portmap.PortOwner(port=8088, kind="foreign", pid=12, process="nginx", source="ss"),
        fix="atf net port fix web",
        alternative=8089,
    )
    internal = portmap.Diagnosis(service="core", port=50051, listening=True, verdict="closed", expected=True)

    payload = portmap.to_json([ok, conflicted, internal])
    assert payload["open"] == 3
    assert payload["problems"] == ["web"], "only a host-facing service is a failure"
    assert payload["ports"][1]["owner"]["process"] == "nginx"
    assert payload["ports"][1]["alternative"] == 8089
    assert portmap.failing([ok, conflicted, internal]) == [conflicted]
    assert portmap.alternatives([ok, conflicted, internal]) == [conflicted]
    assert conflicted.host_facing and not internal.host_facing


# --------------------------------------------------------------------- upgrade
def test_semver_ordering_is_conservative():
    assert upg.semver_tuple("v2.1.0") == (2, 1, 0)
    assert upg.semver_tuple("2.0.0-rc.1") == (2, 0, 0)
    assert upg.semver_tuple("not-a-version") is None
    assert upg.version_is_newer("2.1.0", "2.0.0") is True
    assert upg.version_is_newer("2.0.0", "2.1.0") is False
    assert upg.version_is_newer("2.0.0", "2.0.0") is False
    # unparseable versions are never ordered: a wrong "yes" promises an upgrade
    # that does not exist
    assert upg.version_is_newer("unknown", "2.0.0") is False
    assert upg.version_is_newer("2.1.0", "") is False


def test_revision_label_and_check_verdicts():
    rev = upg.Revision(sha="a" * 40, short="aaaaaaa", version="2.0.0")
    assert rev.label == "v2.0.0 (aaaaaaa)" and rev.known
    assert upg.Revision().label == "unknown" and not upg.Revision().known

    current = upg.Revision(short="aaaaaaa", version="2.0.0")
    chk = upg.UpgradeCheck(current=current, target=current)
    assert chk.up_to_date and chk.can_apply and not chk.newer_version

    chk.behind = 3
    assert not chk.up_to_date and chk.can_apply
    chk.blockers.append("HEAD is detached")
    assert not chk.can_apply and not chk.up_to_date


def test_upgrade_plan_matches_the_state_it_is_given():
    current = upg.Revision(short="aaaaaaa", version="2.0.0")
    up_to_date = upg.UpgradeCheck(current=current, target=current)
    plan = " | ".join(upg.plan(SimpleNamespace(), up_to_date))
    assert "already at the newest revision" in plan
    assert "verify: doctor checks + port diagnosis" in plan
    assert f"record: previous revision saved to {upg.STATE_DIR}/{upg.STATE_FILE}" in plan

    behind = upg.UpgradeCheck(current=current, target=upg.Revision(short="bbbbbbb", version="2.1.0"), behind=2)
    plan = " | ".join(upg.plan(SimpleNamespace(), behind))
    assert "move: aaaaaaa → v2.1.0 (bbbbbbb) (fast-forward only)" in plan

    behind.blockers.append("working tree has uncommitted changes")
    plan = " | ".join(upg.plan(SimpleNamespace(), behind, build=False))
    assert "blocked — working tree has uncommitted changes" in plan
    assert "build:" not in plan, "--no-build is honoured in the plan too"


def test_upgrade_state_records_the_previous_revision_and_tolerates_corruption(tmp_path):
    ctx, _sink = _ctx(tmp_path)
    assert upg.read_state(ctx) == {"history": []}
    assert upg.previous_revision(ctx) is None

    run = upg.UpgradeRun(
        verb="upgrade",
        from_=upg.Revision(sha="a" * 40, short="aaaaaaa", version="2.0.0", branch="master"),
        to=upg.Revision(sha="b" * 40, short="bbbbbbb", version="2.1.0", branch="master"),
    )
    run.add("move", "ok", "aaaaaaa → bbbbbbb")
    upg.record(ctx, run)

    state = upg.read_state(ctx)
    assert state["previous"]["short"] == "aaaaaaa"
    assert state["current"]["short"] == "bbbbbbb"
    assert state["result"] == "ok" and state["verb"] == "upgrade"
    previous = upg.previous_revision(ctx)
    assert previous is not None and previous.version == "2.0.0"
    entries = upg.history(ctx)
    assert len(entries) == 1 and entries[0]["steps"][0]["name"] == "move"
    assert upg.state_path(ctx).parent.name == upg.STATE_DIR

    # A corrupt state file must not block an upgrade: history is a convenience.
    upg.state_path(ctx).write_text("{not json", encoding="utf-8")
    recovered = upg.read_state(ctx)
    assert recovered["history"] == [] and "warning" in recovered
    assert upg.previous_revision(ctx) is None


def test_dirty_files_ignores_the_upgrade_state_directory(monkeypatch):
    """The state file the CLI writes itself is never a reason to refuse."""
    ctx, _sink = _ctx()
    calls: list[tuple[tuple, dict]] = []

    def fake_git(_ctx, *args, **kwargs):
        calls.append((args, kwargs))
        if "--is-inside-work-tree" in args:
            return SimpleNamespace(ok=True, stdout="true\n", returncode=0)
        if args[:1] == ("status",):
            return SimpleNamespace(
                ok=True,
                stdout=f" M {upg.STATE_DIR}/{upg.STATE_FILE}\n M atfcli/cli.py\n?? atfcli/new.py\n",
                returncode=0,
            )
        return SimpleNamespace(ok=False, stdout="", returncode=1)

    monkeypatch.setattr(upg, "_git", fake_git)
    assert upg.dirty_files(ctx) == ["M atfcli/cli.py", "?? atfcli/new.py"]
    assert calls, "no git probe was made"
    assert all(kwargs.get("read_only") is True for _args, kwargs in calls), "every probe is read-only"


def test_a_dry_run_still_reads_the_repository():
    """`--dry-run` must not change anything, but it must still *look*.

    Read-only git probes therefore bypass `--dry-run` suppression. Without
    that, `--dry-run upgrade` reported "this install is not a git checkout" —
    a false statement about the repository, and an upgrade plan with nothing in
    it.
    """
    ctx, _sink = _ctx(dry_run=True)
    assert upg.is_checkout(ctx) is True
    assert upg.branch(ctx) != ""

    current = upg.current_revision(ctx)
    assert current.version == ctx.repo.version, "read from HEAD, not from the working tree"
    assert len(current.sha) == 40 and current.short

    chk = upg.check(ctx, do_fetch=False)
    assert "not a git checkout" not in " ".join(chk.notes)
    assert chk.target.version == ctx.repo.version
    assert not upg.plan(ctx, chk) == []


def test_upgrade_run_json_is_stable():
    run = upg.UpgradeRun(verb="rollback", from_=upg.Revision(short="bbbbbbb"), to=upg.Revision(short="aaaaaaa"))
    run.add("move", "ok", "back")
    run.add("recreate", "fail", "boom")
    payload = run.to_json()
    assert payload["verb"] == "rollback" and payload["ok"] is False
    assert [s["status"] for s in payload["steps"]] == ["ok", "fail"]
    assert run.failures[0].name == "recreate"
    assert set(payload) == {"verb", "ok", "from", "to", "steps", "findings", "duration_s"}


# ------------------------------------------------------------------ net (ports)
def test_net_port_free_reports_a_candidate_and_the_current_holders():
    from atfcli.commands import net

    ctx, sink = _ctx()
    code = net._ports_free(ctx, SimpleNamespace(port=None, service="web", json=True))
    payload = json.loads(sink.getvalue())
    assert code in (0, 1)
    assert payload["base"] == portmap.DEFAULT_PORTS["web"] or isinstance(payload["base"], int)
    assert set(payload) == {"base", "free", "holders"}
    assert (payload["free"] is None) == (code == 1)
    if payload["free"] is not None:
        assert payload["free"] >= portmap.LOWEST_AUTO_PORT
        # never trades a conflict with a stranger for one with ourselves
        assert payload["free"] not in {
            port for name, port in portmap.expected_ports(ctx).items() if name != "web"
        }
