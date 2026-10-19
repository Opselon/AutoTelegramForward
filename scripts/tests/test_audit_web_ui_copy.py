"""Regression tests for the JSX-aware web UI-copy gate.

The gate needs the `typescript` package, which only the web-frontend CI job
installs (`npm ci` in `web/frontend`). When node or that package is absent this
module **skips** instead of failing: a python job with no node is not a web
regression, and a test that fails there would be switched off.

Every test drives the real script as a subprocess against a throwaway tree, so
what is pinned is the CLI contract CI depends on: exit 1 with a GitHub
annotation for a new literal, exit 0 for a catalog lookup / technical token /
baselined offender whose line moved.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNER = REPO / "scripts/audit_web_ui_copy.js"
TYPESCRIPT = REPO / "web/frontend/node_modules/typescript"
NODE = shutil.which("node")
NODE_BIN = NODE or "node"

pytestmark = pytest.mark.skipif(
    NODE is None or not TYPESCRIPT.exists(),
    reason="node/typescript unavailable — the web-frontend CI job installs them",
)

OFFENDER = """\
export function Probe({ isRtl }: { isRtl: boolean }) {
  const statusLabel = isRtl ? 'وضعیت سرویس' : 'Service status'
  return (
    <div title="Service health">
      <span>Deploy the widget now</span>
      <input placeholder="Search by owner" />
      <em>{statusLabel}</em>
    </div>
  )
}
"""

TOLERATED = """\
import { Trans } from './i18n'
const t = (key: string) => key

export function Tolerated({ isRtl }: { isRtl: boolean }) {
  return (
    <div className="flex items-center gap-2" data-testid="probe">
      <span>{t('ui_probe_heading')}</span>
      <Trans i18nKey="ui_probe_body">Already translated body</Trans>
      <b>{isRtl ? t('ui_on') : t('ui_off')}</b>
      <a href="https://atf.example.com/api/v1/health">{t('ui_health')}</a>
      <code>AutoTelegramForward</code>
      <code>filter_rule_id</code>
      <code>{'{source_title}'}</code>
      <code>application/json</code>
    </div>
  )
}
"""


def make_tree(tmp_path: Path, body: str = OFFENDER, name: str = "Probe.tsx") -> Path:
    src = tmp_path / "web/frontend/src"
    src.mkdir(parents=True)
    (src / name).write_text(body, encoding="utf-8")
    return src


def run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [NODE_BIN, str(SCANNER), "--root", str(tmp_path), "--typescript", str(TYPESCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


# --------------------------------------------------------------------------- #
# what is copy
# --------------------------------------------------------------------------- #
def test_planted_jsx_text_attribute_and_ternary_are_flagged(tmp_path: Path) -> None:
    make_tree(tmp_path)
    result = run(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    for expected in (
        "Deploy the widget now",  # JSX text node
        "Service health",  # title attribute
        "Search by owner",  # placeholder attribute
        "Service status",  # English branch of a ternary bound to a copy variable
        "وضعیت سرویس",  # Persian branch of the same ternary
    ):
        assert expected in result.stdout
    assert "::error file=web/frontend/src/Probe.tsx,line=" in result.stdout


def test_mixed_ternary_flags_only_the_literal_branch(tmp_path: Path) -> None:
    body = "<span>{isRtl ? 'وضعیت' : t('ui_status')}</span>\n"
    make_tree(tmp_path, body)
    result = run(tmp_path)
    assert result.returncode == 1
    assert "وضعیت" in result.stdout
    assert "ui_status" not in result.stdout  # a catalog lookup is not copy


def test_ampersand_string_expression_is_flagged(tmp_path: Path) -> None:
    body = "<div>{loading && 'Loading your tasks…'}</div>\n"
    make_tree(tmp_path, body)
    result = run(tmp_path)
    assert result.returncode == 1
    assert "Loading your tasks…" in result.stdout


def test_copy_object_property_is_flagged(tmp_path: Path) -> None:
    body = "export const OPTIONS = [{ value: 'on', label: 'Always on' }]\n"
    make_tree(tmp_path, body, name="options.ts")
    result = run(tmp_path)
    assert result.returncode == 1
    assert "Always on" in result.stdout
    assert "value" not in result.stdout


# --------------------------------------------------------------------------- #
# what is not copy
# --------------------------------------------------------------------------- #
def test_catalog_lookups_trans_and_tokens_are_tolerated(tmp_path: Path) -> None:
    make_tree(tmp_path, TOLERATED)
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: 0 UI string(s) checked" in result.stdout


def test_catalog_lookup_with_params_and_locale_is_tolerated(tmp_path: Path) -> None:
    """`t('ui_key', {name}, 'fa')` is a lookup: params and locale are not copy."""
    body = (
        "export const a = t('ui_rules_title', { name: 'Admin' }, 'fa')\n"
        "export const b = t('ui_rules_title', params, lang)\n"
        "export const c = t('rules.header')\n"
    )
    make_tree(tmp_path, body, name="label.ts")
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: 0 UI string(s) checked" in result.stdout


def test_positional_language_helper_is_still_flagged(tmp_path: Path) -> None:
    """`t(fa, en)` is `isRtl ? fa : en` in disguise — the exact defect class."""
    body = "export const label = t('فارسی', 'English')\n"
    make_tree(tmp_path, body, name="label.ts")
    result = run(tmp_path)
    assert result.returncode == 1
    assert "English" in result.stdout and "فارسی" in result.stdout


def test_catalog_module_and_generated_tables_are_not_scanned(tmp_path: Path) -> None:
    src = tmp_path / "web/frontend/src"
    locales = src / "locales"
    locales.mkdir(parents=True)
    (locales / "catalog.ts").write_text("export const CATALOG = { en: { ui_a: 'Save changes' } }\n")
    (src / "settingLabels.generated.ts").write_text("export const LABELS = { label: 'Save changes' }\n")
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 UI string(s) checked" in result.stdout


def test_typescript_fallback_is_used_when_the_flag_is_wrong(tmp_path: Path) -> None:
    """A wrong --typescript must not turn the gate green: it resolves or refuses."""
    make_tree(tmp_path)
    result = subprocess.run(
        [NODE_BIN, str(SCANNER), "--root", str(tmp_path), "--typescript", str(tmp_path / "nope")],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    # The repository copy in scripts/../web/frontend/node_modules is found, so
    # the planted literal is still reported (never a silent pass).
    assert result.returncode == 1
    assert "Deploy the widget now" in result.stdout


def test_missing_typescript_fails_closed(tmp_path: Path) -> None:
    """With no typescript anywhere, the gate must refuse to run (exit 2)."""
    isolated = tmp_path / "tools/audit_web_ui_copy.js"
    isolated.parent.mkdir(parents=True)
    isolated.write_text(SCANNER.read_text(encoding="utf-8"), encoding="utf-8")
    make_tree(tmp_path)  # a tree with no node_modules, next to a copy of the scanner
    result = subprocess.run(
        [NODE_BIN, str(isolated), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 2
    assert "typescript is not installed" in result.stderr


# --------------------------------------------------------------------------- #
# baseline behaviour through the CLI
# --------------------------------------------------------------------------- #
def test_update_baseline_round_trip(tmp_path: Path) -> None:
    make_tree(tmp_path)
    baseline = tmp_path / "scripts/.web_ui_copy_baseline.txt"

    updated = run(tmp_path, "--update-baseline")
    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert "5 offender(s) pinned" in updated.stdout
    text = baseline.read_text(encoding="utf-8")
    assert text.startswith("#")
    assert "web/frontend/src/Probe.tsx|Deploy the widget now" in text

    green = run(tmp_path)
    assert green.returncode == 0, green.stdout + green.stderr
    assert "PASS: 5 UI string(s) checked" in green.stdout


def test_baselined_offender_survives_an_inserted_line(tmp_path: Path) -> None:
    src = make_tree(tmp_path)
    assert run(tmp_path, "--update-baseline").returncode == 0

    file = src / "Probe.tsx"
    file.write_text(
        "// a new import or comment shifts every line below it\n" + file.read_text(encoding="utf-8")
    )
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS:" in result.stdout


def test_baseline_written_by_the_python_module_is_honoured(tmp_path: Path) -> None:
    """One baseline format, two languages: the JS gate must read the shared writer."""
    src = make_tree(tmp_path)
    baseline = tmp_path / "scripts/.web_ui_copy_baseline.txt"
    baseline.parent.mkdir(parents=True)

    spec = importlib.util.spec_from_file_location("ui_copy_baseline", REPO / "scripts/ui_copy_baseline.py")
    assert spec and spec.loader
    shared = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("ui_copy_baseline", shared)
    spec.loader.exec_module(shared)
    shared.write_baseline(
        baseline,
        [shared.Offender(path="web/frontend/src/Probe.tsx", line=1, text="Deploy the widget now").entry()],
    )
    assert (src / "Probe.tsx").exists()

    result = run(tmp_path)
    assert result.returncode == 1
    # The pinned string is tolerated; exactly the four unpinned ones remain.
    assert "FAIL: 4 unlocalized UI string(s)" in result.stdout
    assert result.stdout.count("Deploy the widget now") == 0


def test_new_literal_after_a_clean_baseline_fails(tmp_path: Path) -> None:
    src = make_tree(tmp_path, "<div className=\"a\">#{'{n}'}</div>\n")
    assert run(tmp_path, "--update-baseline").returncode == 0
    (src / "Extra.tsx").write_text("<span>Delete everything</span>\n", encoding="utf-8")
    result = run(tmp_path)
    assert result.returncode == 1
    assert "Delete everything" in result.stdout


def test_shrunk_backlog_prints_the_notice_but_stays_green(tmp_path: Path) -> None:
    src = make_tree(tmp_path)
    assert run(tmp_path, "--update-baseline").returncode == 0
    file = src / "Probe.tsx"
    file.write_text(
        file.read_text(encoding="utf-8").replace("Deploy the widget now", "{t('ui_deploy')}"),
        encoding="utf-8",
    )
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "backlog shrank — regenerate" in result.stdout


def test_no_sources_is_a_tool_failure(tmp_path: Path) -> None:
    (tmp_path / "web/frontend/src").mkdir(parents=True)
    result = run(tmp_path)
    assert result.returncode == 2
    assert "no web sources found" in result.stderr


# --------------------------------------------------------------------------- #
# the real repository
# --------------------------------------------------------------------------- #
def test_repository_baseline_is_current() -> None:
    result = run(REPO)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS:" in result.stdout
