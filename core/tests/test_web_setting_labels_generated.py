"""The dashboard's Persian setting labels are generated, and stay generated.

`web/frontend/src/settingLabels.generated.ts` is a copy of the bot catalog, not a
second source of truth: `scripts/gen_web_setting_labels.py` writes it from
`SETTING_SPECS` + `translations.json`. A hand edit would drift the moment
somebody adds a setting, and the panel would silently fall back to English for
exactly the rows nobody re-checked.

These tests pin the file to the generator: it is current, it covers every
setting, and no label is left as English-only Persian copy.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_web_setting_labels as gen  # noqa: E402

from core.domain.rule_settings import SETTING_SPECS  # noqa: E402

GENERATED = ROOT / "web" / "frontend" / "src" / "settingLabels.generated.ts"
PERSIAN = "[\u0600-\u06ff]"


def test_the_generated_module_is_checked_in():
    assert GENERATED.exists(), "run: python3 scripts/gen_web_setting_labels.py"


def test_the_generated_module_is_current():
    """Regenerating must be a no-op; a diff means somebody edited one side only."""
    assert GENERATED.read_text(encoding="utf-8") == gen.build(), (
        "settingLabels.generated.ts is stale - run: python3 scripts/gen_web_setting_labels.py"
    )


def test_every_setting_has_a_label_in_both_languages():
    text = GENERATED.read_text(encoding="utf-8")
    catalog = json.loads(gen.CATALOG.read_text(encoding="utf-8"))
    missing = [key for key in SETTING_SPECS if f'"{key}": {{ fa:' not in text]
    assert missing == [], f"settings with no generated label: {missing}"
    # And the Persian half is really Persian, not a copy of the English one.
    untranslated = [
        key for key in SETTING_SPECS
        if catalog["fa"][f"ui_reg_set_{key}"] == catalog["en"][f"ui_reg_set_{key}"]
    ]
    assert untranslated == [], f"labels still in English: {untranslated}"


@pytest.mark.parametrize("key", ["header", "block_text", "bot_token_id"])
def test_the_panel_reads_the_generated_labels(key: str):
    """The panel must not keep its own English map next to the generated one."""
    source = (ROOT / "web" / "frontend" / "src" / "components" / "TaskControlPanel.tsx").read_text(
        encoding="utf-8"
    )
    assert "SETTING_LABELS" in source
    assert "SETTING_HELP" in source
    assert f"'{key}':" not in source, f"{key} is hard-coded in the panel instead of looked up"
