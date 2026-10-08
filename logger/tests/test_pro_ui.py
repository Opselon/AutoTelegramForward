"""Invariants for the Pro button UI: Telegram callback limits + i18n coverage."""

import json
from pathlib import Path

from core.infrastructure.telegram.pro_bot_ui import CB


def test_callback_data_within_telegram_limit():
    """Telegram caps callback_data at 64 bytes — every value must fit,
    including the dynamic prefixes (sess:pick:, rule:del: etc.)."""
    worst_case_suffix = ":9999999999"
    for key, val in CB.items():
        assert isinstance(val, str) and val, key
        assert len((val + worst_case_suffix).encode()) <= 64, key


def test_callback_keys_unique():
    vals = list(CB.values())
    assert len(vals) == len(set(vals))


def _catalog():
    here = Path(__file__).resolve()
    p = here.parents[2] / "core" / "infrastructure" / "telegram" / "locales" / "translations.json"
    if not p.exists():
        p = Path("core/infrastructure/telegram/locales/translations.json")
    return json.loads(p.read_text(encoding="utf-8"))


def test_ui_keys_present_in_all_languages():
    catalog = _catalog()
    assert set(catalog) >= {"en", "fa", "ru", "zh"}
    required = [k for k in catalog["en"] if k.startswith("ui_")]
    assert len(required) >= 15
    for lang in ("fa", "ru", "zh"):
        missing = [k for k in required if k not in catalog[lang]]
        assert not missing, f"{lang} missing: {missing}"
