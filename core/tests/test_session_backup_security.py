"""Regression tests for the Telegram bot's session backup confidentiality."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "infrastructure" / "telegram" / "pro_bot_ui.py").read_text(
    encoding="utf-8"
)


def test_session_backup_requires_a_user_passphrase() -> None:
    start = SOURCE.index('async def _cb_backup(_, cq: CallbackQuery):')
    end = SOURCE.index("# ---------------- migration backup/restore", start)
    handler = SOURCE[start:end]
    assert 'st.step = "session_backup_passphrase"' in handler
    assert 'self._t("ui_migrate_ask_passphrase")' in handler
    assert "urlsafe_b64encode" not in handler


def test_session_backup_payload_uses_authenticated_encryption() -> None:
    start = SOURCE.index("async def _step_session_backup_passphrase(")
    end = SOURCE.index("async def _step_migrate_import_passphrase(", start)
    handler = SOURCE[start:end]
    assert "encrypt_bundle(" in handler
    assert 'json.dumps(bundle, ensure_ascii=False' in handler
    assert "os.remove(path)" in handler
    assert "except OSError:" in handler
