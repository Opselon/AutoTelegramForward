"""Writing `.env` safely: backup, atomic replace, verify, roll back, restore.

These are the tests for the part of configuration management that decides whether
a bad minute becomes a bad deployment. Each one fails against the "read the file,
edit the string, write it back" implementation this replaced — that version
passed every happy path and lost the file on a full disk.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from atfcli.application.use_cases.envconfig import Environment
from atfcli.domain.errors import CommandFailed, NotFoundError, UsageError
from atfcli.infrastructure.host.envfile import KEEP, EnvFileStore


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "# a deployment that explains itself\n"
        "ATF_BOT_TOKEN=123456:AA" + "x" * 33 + "\n"
        "ATF_API_ID=17349\n"
        "ATF_API_HASH=" + "a" * 32 + "\n"
        "ATF_MASTER_KEY=" + "k" * 40 + "\n"
        "ADMIN_USER_ID=5094837833\n"
        "ATF_WEB_PORT=8088\n",
        encoding="utf-8",
    )
    path.chmod(0o644)
    return path


@pytest.fixture
def store(env_file: Path) -> EnvFileStore:
    return EnvFileStore(env_file)


# ------------------------------------------------------------------- writing
def test_write_is_atomic_and_leaves_no_temporary_file(store: EnvFileStore, env_file: Path) -> None:
    store.write(env_file.read_text(encoding="utf-8") + "ATF_WEB_PORT=8090\n")

    leftovers = [p.name for p in env_file.parent.iterdir() if ".tmp." in p.name]
    assert leftovers == []
    assert "ATF_WEB_PORT=8090" in env_file.read_text(encoding="utf-8")


def test_write_tightens_permissions_to_owner_only(store: EnvFileStore, env_file: Path) -> None:
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o644
    result = store.write(env_file.read_text(encoding="utf-8").replace("8088", "8090"))

    assert result.mode == 0o600
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_a_backup_is_written_before_the_change_and_holds_the_old_bytes(
    store: EnvFileStore, env_file: Path
) -> None:
    before = env_file.read_text(encoding="utf-8")
    result = store.write(before.replace("8088", "8090"))

    assert result.backup, "a backup name is reported so the operator can roll back"
    backup = env_file.with_name(env_file.name + ".bak." + result.backup)
    assert backup.read_text(encoding="utf-8") == before
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_backups_are_capped_and_trimmed_oldest_first(store: EnvFileStore, env_file: Path) -> None:
    for index in range(KEEP + 3):
        store.write(env_file.read_text(encoding="utf-8") + f"EXTRA_{index}=1\n")

    names = store.backup_names()
    assert len(names) <= KEEP
    # Newest first: the entry the plan reports is the one `restore` defaults to.
    assert names == sorted(names, reverse=True)


def test_write_verifies_the_bytes_and_rolls_back_when_they_do_not_match(
    store: EnvFileStore, env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem that lies must not leave a config the CLI believes in."""
    before = env_file.read_text(encoding="utf-8")
    def lying_read(self: EnvFileStore) -> str:
        # The store wrote the file, then asked the filesystem what is there and
        # got something else back: exactly the case the verification exists for.
        return before + "ATF_WEB_PORT=1\n"

    monkeypatch.setattr(EnvFileStore, "read", lying_read)

    with pytest.raises(CommandFailed) as caught:
        store.write(before + "ATF_WEB_PORT=8090\n")

    assert "did not hold the written bytes" in caught.value.message
    monkeypatch.undo()
    assert env_file.read_text(encoding="utf-8") == before, "the previous file was restored"


def test_a_symlinked_env_is_written_through_not_replaced(store: EnvFileStore, tmp_path: Path) -> None:
    real = tmp_path / "shared.env"
    real.write_text("ATF_WEB_PORT=8088\n", encoding="utf-8")
    link = tmp_path / "linked.env"
    link.symlink_to(real)

    linked_store = EnvFileStore(link)
    linked_store.write("ATF_WEB_PORT=8090\n")

    assert link.is_symlink(), "atomic replace must not turn the symlink into a plain file"
    assert real.read_text(encoding="utf-8") == "ATF_WEB_PORT=8090\n"


def test_restore_refuses_a_path_like_name(store: EnvFileStore) -> None:
    for name in ("", "..", "../.env", "a/b"):
        with pytest.raises(NotFoundError):
            store.restore(name)


def test_restore_round_trip(store: EnvFileStore, env_file: Path) -> None:
    before = env_file.read_text(encoding="utf-8")
    backup = store.write(before.replace("8088", "8090")).backup
    assert "8090" in env_file.read_text(encoding="utf-8")

    store.restore(backup)
    assert env_file.read_text(encoding="utf-8") == before


# ------------------------------------------------------------------ use case
def test_plan_is_pure_and_apply_is_the_only_write(store: EnvFileStore, env_file: Path) -> None:
    before = env_file.read_text(encoding="utf-8")
    environment = Environment(store)

    plan = environment.plan({"ATF_WEB_PORT": "8090", "ATF_REDIS_MAXMEMORY": "512mb"})
    assert env_file.read_text(encoding="utf-8") == before, "planning must not touch the disk"
    assert set(plan.changed_keys) == {"ATF_WEB_PORT", "ATF_REDIS_MAXMEMORY"}

    outcome = environment.apply(plan)
    assert outcome.payload["written"] is True
    assert env_file.read_text(encoding="utf-8") == plan.after
    assert "# a deployment that explains itself" in plan.after


def test_apply_with_dry_run_never_writes(store: EnvFileStore, env_file: Path) -> None:
    before = env_file.read_text(encoding="utf-8")
    environment = Environment(store)
    plan = environment.plan({"ATF_WEB_PORT": "8090"})

    outcome = environment.apply(plan, dry_run=True)
    assert outcome.payload["dry_run"] is True
    assert env_file.read_text(encoding="utf-8") == before


def test_an_unchanged_value_is_reported_as_no_change(store: EnvFileStore) -> None:
    environment = Environment(store)
    plan = environment.plan({"ATF_WEB_PORT": "8088"})

    assert plan.empty
    outcome = environment.apply(plan)
    assert outcome.payload["written"] is False
    assert "no changes" in outcome.detail


def test_one_bad_value_refuses_the_whole_change_set(store: EnvFileStore, env_file: Path) -> None:
    before = env_file.read_text(encoding="utf-8")
    with pytest.raises(UsageError) as caught:
        Environment(store).plan({"ATF_WEB_PORT": "8090", "ATF_RATELIMIT_ALGO": "nope"})

    assert "ATF_RATELIMIT_ALGO" in caught.value.message
    assert "must be one of" in caught.value.message
    assert env_file.read_text(encoding="utf-8") == before


def test_plan_masks_secret_values_in_its_json(store: EnvFileStore) -> None:
    plan = Environment(store).plan({"ATF_BOT_TOKEN": "987654:BB" + "y" * 33})
    document = plan.to_json()

    assert document["changes"][0]["value"] == "[REDACTED]"
    assert document["changes"][0]["before"] == "[REDACTED]"
    assert "987654" not in str(document)


def test_unknown_keys_are_written_but_warned_about(store: EnvFileStore) -> None:
    environment = Environment(store)
    plan = environment.plan({"ATF_QUEUE_SOMETHING_NEW": "7"})
    assert any("not a documented setting" in warning for warning in plan.warnings)

    outcome = environment.apply(plan)
    assert any("not a documented setting" in warning.message for warning in outcome.warnings)


def test_strict_mode_refuses_unknown_keys(store: EnvFileStore) -> None:
    with pytest.raises(UsageError):
        Environment(store).plan({"ATF_QUEUE_SOMETHING_NEW": "7"}, allow_unknown=False)


# ------------------------------------------------------------------ auditing
def test_validate_finds_placeholders_invalids_duplicates_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "ATF_BOT_TOKEN=123456:ABC-your-bot-token\n"
        "ATF_API_ID=17349\n"
        "ATF_API_HASH=" + "a" * 32 + "\n"
        "ATF_MASTER_KEY=" + "k" * 40 + "\n"
        "ADMIN_USER_ID=5094837833\n"
        "ATF_WEB_PORT=8088\n"
        "ATF_WEB_PORT=99999\n",
        encoding="utf-8",
    )
    path.chmod(0o644)

    report = Environment(EnvFileStore(path)).validate()
    ids = {finding.id for finding in report.findings}

    assert "env-placeholder-ATF_BOT_TOKEN" in ids or any(
        "placeholder" in finding.title for finding in report.findings
    )
    assert "env-invalid-ATF_WEB_PORT" in ids
    assert "env-duplicate-ATF_WEB_PORT" in ids
    assert "env-permissions" in ids
    assert report.ok is False
    assert report.mode == 0o644


def test_validate_reports_a_missing_required_key_and_a_missing_file(tmp_path: Path) -> None:
    report = Environment(EnvFileStore(tmp_path / ".env")).validate()
    assert report.ok is False
    assert any(finding.id == "env-missing-file" for finding in report.findings)

    (tmp_path / ".env").write_text("ATF_WEB_PORT=8088\n", encoding="utf-8")
    report = Environment(EnvFileStore(tmp_path / ".env")).validate()
    missing = {finding.subject for finding in report.findings if finding.id.startswith("env-missing-")}
    assert "ATF_BOT_TOKEN" in missing
    assert "ADMIN_USER_ID" in missing


def test_validate_is_clean_on_a_complete_file(env_file: Path, store: EnvFileStore) -> None:
    report = Environment(store).validate()
    errors = [finding for finding in report.findings if finding.severity == "error"]
    assert errors == []
    assert report.ok is True
    assert report.keys == 6


def test_a_tampered_line_is_preserved_rather_than_silently_dropped(store: EnvFileStore, env_file: Path) -> None:
    env_file.write_text(env_file.read_text(encoding="utf-8") + "BROKEN LINE\n", encoding="utf-8")
    Environment(store).apply(Environment(store).plan({"ATF_WEB_PORT": "8090"}))

    text = env_file.read_text(encoding="utf-8")
    assert "BROKEN LINE" in text
