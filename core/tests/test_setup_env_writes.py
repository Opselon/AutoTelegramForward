"""The setup wizard writes `.env` the same way `atf config` does.

Before the shared store, the wizard wrote with `write_text` + `os.chmod`: no
backup, no read-back verification, and a real risk of a half-written `.env` from
an interrupted first run — exactly the file the stack then fails to parse. The
wizard and the CLI must not be two writers with two ideas of durability.
"""

from __future__ import annotations

import argparse
import io
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from atfcli.cli import CliContext
from atfcli.commands import setup as setup_mod
from atfcli.console import Console
from atfcli.context import RepoContext

ACCENT = "# written by hand, do not lose this comment\n"

# A file that already carries everything the wizard generates: generation must be
# a no-op, not a rewrite.
SEED = (
    ACCENT
    + "ATF_THING_KEEP_ME=forever\n"
    + "ATF_MASTER_KEY=" + "k" * 40 + "\n"
    + "ATF_API_KEY=" + "q" * 43 + "\n"
    + "ADMIN_USER_ID=5094837833\n"
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".env.example").write_text("# template\nATF_MASTER_KEY=\nATF_BOT_TOKEN=\n", encoding="utf-8")
    return tmp_path


def make_ctx(root: Path, *, dry_run: bool = False) -> SimpleNamespace:
    con = Console(stream=io.StringIO(), color=False)
    repo = RepoContext(con, root=root, dry_run=dry_run, assume_yes=True)
    return SimpleNamespace(repo=repo, out=con, args=make_args())


def make_args(admin_id: str = "", **kw: str) -> argparse.Namespace:
    return argparse.Namespace(admin_id=admin_id, **kw)


def test_apply_answers_preserves_the_file_it_edits(root: Path) -> None:
    (root / ".env").write_text(SEED, encoding="utf-8")
    ctx = make_ctx(root)
    runbook = setup_mod.Runbook()

    written = setup_mod.apply_answers(ctx, {"web_url": "https://a.example"}, runbook)

    assert written == ["ATF_WEB_URL"]
    body = (root / ".env").read_text(encoding="utf-8")
    assert "ATF_WEB_URL=https://a.example" in body
    assert ACCENT in body and "ATF_THING_KEEP_ME=forever" in body
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    assert list(root.glob(".env.bak.*")), "the previous file was backed up"
    assert not list(root.glob(".env*.tmp")), "no temp file is left behind"


def test_apply_answers_dry_run_writes_nothing(root: Path) -> None:
    (root / ".env").write_text(SEED, encoding="utf-8")
    before = (root / ".env").read_text(encoding="utf-8")
    ctx = make_ctx(root, dry_run=True)

    written = setup_mod.apply_answers(ctx, {"web_url": "https://a.example"}, setup_mod.Runbook())

    assert written == ["ATF_WEB_URL"]
    assert (root / ".env").read_text(encoding="utf-8") == before
    assert not list(root.glob(".env.bak.*"))


def test_apply_answers_records_the_admin_id_once(root: Path) -> None:
    (root / ".env").write_text(SEED, encoding="utf-8")
    ctx = make_ctx(root)

    setup_mod.apply_answers(ctx, {"admin_id": "4242"}, setup_mod.Runbook())

    body = (root / ".env").read_text(encoding="utf-8")
    assert "ADMIN_USER_ID=4242" in body
    assert "ADMIN_IDS=4242" in body
    assert "ADMIN_USER_ID=5094837833" not in body


def test_bootstrap_env_generates_the_local_secrets(root: Path) -> None:
    ctx = make_ctx(root)
    runbook = setup_mod.Runbook()

    generated = setup_mod.bootstrap_env(ctx, make_args(), runbook)

    assert set(generated) == set(setup_mod.GENERATED_KEYS)
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    # the generated material is reported by length, never by value
    values = setup_mod.read_env(ctx)
    secrets = [values[key] for key in setup_mod.GENERATED_KEYS]
    for step in runbook.steps:
        assert step.status != "fail"
        # The secret is only ever reported by length, never by value.
        for secret in secrets:
            assert secret not in step.detail
    announced = [step for step in runbook.steps if "generated" in step.detail]
    assert announced, "the generation was announced in the runbook"
    assert " chars)" in announced[0].detail

    # and the values themselves are real random material, not the placeholders
    values = setup_mod.read_env(ctx)
    assert len(values["ATF_MASTER_KEY"]) >= 32
    assert values["ATF_MASTER_KEY"] != "k" * 40


def test_bootstrap_env_from_scratch_writes_a_parseable_file(root: Path) -> None:
    root.joinpath(".env.example").unlink()
    ctx = make_ctx(root)

    setup_mod.bootstrap_env(ctx, make_args(admin_id="5094837833"), setup_mod.Runbook())

    body = (root / ".env").read_text(encoding="utf-8")
    assert "ADMIN_USER_ID=5094837833" in body
    assert "ADMIN_IDS=5094837833" in body
    assert "ATF_MASTER_KEY=" in body and "ATF_API_KEY=" in body
    assert body.endswith("\n")


def test_bootstrap_env_leaves_a_completed_file_alone(root: Path) -> None:
    (root / ".env").write_text(SEED, encoding="utf-8")
    before = (root / ".env").read_text(encoding="utf-8")
    ctx = make_ctx(root)

    setup_mod.bootstrap_env(ctx, make_args(), setup_mod.Runbook())

    assert (root / ".env").read_text(encoding="utf-8") == before
    assert not list(root.glob(".env.bak.*")), "nothing was written, so nothing was backed up"
