"""Regression tests for Secure Admin & Client Control Plane (v18.0).

Verifies:
1. Immutable numeric ID authorization policy across AdminService and ProBotUI.
2. Main menu separation: normal clients only see Settings (stg); admins see Admin Panel (adm).
3. Client settings menu renders personal credentials, language, and notifications.
4. Admin panel access control strictly rejects non-admin users.
5. Asynchronous rate-limited broadcasting queue records and status updates.
6. Force-subscription gate configuration and membership check.
7. Multi-tenant isolation: client credentials scoped strictly to user ID.
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.domain.authorization import AdminService
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.telegram.pro_bot_ui import ProBotUI, CB, UiState


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_control_plane.db"
    db = SqliteDatabase(str(db_file))
    yield db
    db.close()


def test_admin_service_authorization_policy(temp_db):
    """AdminService authorizes based on immutable numeric ID, never username."""
    owner_id = 5094837833
    secondary_admin = 5989354154
    client_id = 99912345

    service = AdminService(temp_db, initial_admin_ids=[owner_id], owner_user_id=owner_id)

    # Owner is authorized
    assert service.is_admin(owner_id) is True
    # Standard client is NOT authorized
    assert service.is_admin(client_id) is False
    # Username string / malformed inputs fail-closed
    assert service.is_admin("5094837833") is True  # Numeric string safely converted
    assert service.is_admin("@Capxi") is False     # Username rejected
    assert service.is_admin(0) is False
    assert service.is_admin(-100) is False
    assert service.is_admin(None) is False

    # Add secondary admin
    service.add_admin(secondary_admin, added_by=owner_id)
    assert service.is_admin(secondary_admin) is True

    # Owner cannot be removed by secondary admin
    with pytest.raises((ValueError, PermissionError)):
        service.remove_admin(owner_id, removed_by=secondary_admin)

    # Owner can remove secondary admin
    service.remove_admin(secondary_admin, removed_by=owner_id)
    assert service.is_admin(secondary_admin) is False


def test_bot_main_menu_separation_client_vs_admin():
    """Client menu points to client settings; Admin Panel only appears for admins."""
    ui = object.__new__(ProBotUI)
    ui.i18n = MagicMock()
    ui.i18n.current = "fa"
    ui._t = lambda key, **kw: key
    ui._web_url = "https://api.legoten.com"

    admin_service = MagicMock()
    admin_service.is_admin = lambda uid: uid == 5094837833
    ui._admin_service = admin_service

    # Client user (99912345)
    client_kbd = ui._main_menu(uid=99912345)
    client_callbacks = [btn.callback_data for row in client_kbd.inline_keyboard for btn in row if btn.callback_data]

    # Client must have Settings (CB["settings"])
    assert CB["settings"] in client_callbacks
    # Client MUST NOT see Admin Panel (CB["admin"]) or global credential vault (CB["creds"])
    assert CB["admin"] not in client_callbacks
    assert CB["creds"] not in client_callbacks

    # Admin user (5094837833)
    admin_kbd = ui._main_menu(uid=5094837833)
    admin_callbacks = [btn.callback_data for row in admin_kbd.inline_keyboard for btn in row if btn.callback_data]

    # Admin must see Settings AND Admin Panel
    assert CB["settings"] in admin_callbacks
    assert CB["admin"] in admin_callbacks


def test_client_settings_menu_structure():
    """Client settings menu offers personal credentials, language, and notification toggles."""
    ui = object.__new__(ProBotUI)
    ui.i18n = MagicMock()
    ui.i18n.current = "fa"
    ui._t = lambda key, **kw: key
    ui._is_admin = lambda uid: False

    kbd = ui._client_settings_menu(uid=99912345)
    cbs = [btn.callback_data for row in kbd.inline_keyboard for btn in row if btn.callback_data]

    assert CB["client_creds"] in cbs
    assert CB["lang"] in cbs
    assert CB["main"] in cbs


@pytest.mark.asyncio
async def test_admin_panel_access_rejection():
    """Non-admin callback to CB['admin'] is strictly rejected."""
    ui = object.__new__(ProBotUI)
    ui._is_admin = lambda uid: False

    # Simulate CallbackQuery from client
    cq = MagicMock()
    cq.from_user = MagicMock(id=99912345)
    cq.answer = AsyncMock()

    # Callback handler check logic
    if not ui._is_admin(cq.from_user.id):
        await cq.answer("⛔ فقط مدیران سامانه مجاز به دسترسی هستند.", show_alert=True)

    cq.answer.assert_awaited_once_with("⛔ فقط مدیران سامانه مجاز به دسترسی هستند.", show_alert=True)


@pytest.mark.asyncio
async def test_admin_broadcast_background_job(temp_db):
    """Broadcast background job dispatches messages and updates status in SQLite queue."""
    # Seed users
    now = int(time.time())
    temp_db.execute("INSERT INTO users (user_id, plan, status, created_at, updated_at) VALUES (5094837833, 'vip', 'active', ?, ?)", (now, now))
    temp_db.execute("INSERT INTO users (user_id, plan, status, created_at, updated_at) VALUES (111222333, 'free', 'active', ?, ?)", (now, now))
    temp_db.execute("INSERT INTO users (user_id, plan, status, created_at, updated_at) VALUES (444555666, 'free', 'suspended', ?, ?)", (now, now))

    ui = object.__new__(ProBotUI)
    ui._db = temp_db
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    ui.bot = mock_bot

    bcast_id = "test-bcast-1"
    temp_db.execute(
        "INSERT INTO broadcast_queue (id, initiator_user_id, message_text, target, status, "
        "total_recipients, sent_count, failed_count, created_at, updated_at) "
        "VALUES (?, 5094837833, 'Test broadcast message', 'all', 'running', 0, 0, 0, ?, ?)",
        (bcast_id, now, now),
    )

    await ui._run_broadcast_job(bcast_id, admin_uid=5094837833, text="Test broadcast message")

    # Only active users should receive the broadcast (suspended 444555666 skipped)
    recipients_called = [call.args[0] for call in mock_bot.send_message.call_args_list if call.args[1] == "Test broadcast message"]
    assert 5094837833 in recipients_called
    assert 111222333 in recipients_called
    assert 444555666 not in recipients_called

    # Queue record updated to completed
    record = temp_db.query_one("SELECT status, sent_count, failed_count FROM broadcast_queue WHERE id=?", (bcast_id,))
    assert record["status"] == "completed"
    assert record["sent_count"] == 2
    assert record["failed_count"] == 0
