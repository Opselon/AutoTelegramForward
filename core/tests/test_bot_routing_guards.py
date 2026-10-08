"""Guard tests for the bot's text routing.

These exist because of a real incident: BotManager and the button dashboard
(ProBotUI) both register a text handler in pyrogram group 0, and pyrogram runs
every handler in a group. A phone number sent during login produced BOTH the
dashboard's prompt and BotManager's "unknown command" — and a login failure
cleared the FSM step so the bot appeared to hang.

The guards here are the regression net for that class of bug: whoever owns the
flow answers, the other stays silent, and a failure keeps the user on the same
step instead of wiping state.
"""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.infrastructure.persistence.database import SCHEMA_VERSION
from core.infrastructure.telegram.bot_manager import BotManager
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.login_flow import LoginFlowManager

ADMIN = 5094837833


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _i18n() -> I18n:
    i18n = I18n()
    i18n.set_language("fa")
    return i18n


def _fake_message(text: str, uid: int = ADMIN) -> SimpleNamespace:
    """A pyrogram Message stand-in: only what the router touches."""
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=uid),
        from_user=SimpleNamespace(id=uid),
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
        edit_message_text=AsyncMock(),
        document=None,
    )


def _make_manager(ui_step_checker=None, ui_state_repo=None) -> BotManager:
    """Build a BotManager without touching the Telegram network."""
    pool = MagicMock()
    pool.api_id, pool.api_hash = 1, "hash"
    sessions = MagicMock()
    sessions.list_all = AsyncMock(return_value=[])
    rules = MagicMock()
    rules.list_all = AsyncMock(return_value=[])
    filters = MagicMock()
    ai = MagicMock()
    ai.list_all = AsyncMock(return_value=[])
    pipeline = MagicMock()
    pipeline.stats = SimpleNamespace(
        processed=0, forwarded=0, filtered=0, rewritten=0, errors=0, started_at=0,
    )

    mgr = BotManager.__new__(BotManager)  # skip Client() — no network
    mgr.bot = MagicMock()
    mgr._admin_ids = {ADMIN}
    mgr._pool = pool
    mgr._sessions = sessions
    mgr._rules = rules
    mgr._filters = filters
    mgr._ai = ai
    mgr._pipeline = pipeline
    mgr.i18n = _i18n()
    mgr._crypto = MagicMock()
    mgr._credentials = None
    mgr._bot_tokens = None
    mgr._ui_state_repo = ui_state_repo
    mgr._dispatcher = None
    mgr._login = LoginFlowManager(pool, sessions)
    mgr.ui_step_checker = ui_step_checker
    mgr._awaiting_lang = {}
    mgr._awaiting_rule = None
    mgr._awaiting_ai = None
    return mgr


@pytest.fixture
def temp_db(tmp_path):
    path = tmp_path / "atf_test.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ui_states (
            user_id INTEGER PRIMARY KEY, step TEXT NOT NULL DEFAULT '',
            buffer TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE schema_version (version INTEGER);
        """
    )
    conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    yield conn
    conn.close()


# --------------------------------------------------------------------------- #
# 1. The mutual guard — no "unknown command" while the dashboard owns the flow
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_unknown_command_when_ui_step_active():
    """A phone number during an active dashboard flow must not be rejected.

    Regression: BotManager's router fired alongside the dashboard and replied
    "unknown command" to the very phone the dashboard had just asked for.
    """
    mgr = _make_manager(ui_step_checker=lambda uid: True)
    msg = _fake_message("+989016807808")

    await mgr._route_text(msg)

    msg.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_no_unknown_command_when_login_pending():
    """An in-flight login (code step) belongs to the login manager."""
    from core.infrastructure.telegram.login_flow import LoginState

    mgr = _make_manager(ui_step_checker=lambda uid: False)
    # Plant a real pending login state — the router inspects .expired/.step.
    state = LoginState(phone_number="+989016807808", credential_id="default")
    state.step = "code"
    state.client = object()
    mgr._login._pending[ADMIN] = state
    msg = _fake_message("12345")

    await mgr._route_text(msg)

    msg.reply_text.assert_not_called()


# --------------------------------------------------------------------------- #
# 2. A login failure keeps the user on the phone step
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_login_failure_stays_on_phone_step(temp_db):
    """send_code failure must clear nothing — the user just retries.

    Regression: the step was advanced to login_code before start() was called
    and wiped on failure, so the bot went dead silent and the log showed why
    only after this fix landed.
    """
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState

    ui = ProBotUI.__new__(ProBotUI)
    ui.bot = MagicMock()
    ui.i18n = _i18n()
    ui._login = MagicMock()
    ui._login.normalize_phone = lambda t: t.strip()
    ui._login.valid_phone = lambda t: t.startswith("+98")
    ui._login.start = AsyncMock(return_value="cooldown:30")
    ui._log = MagicMock()
    ui._states = {}
    ui._ui_state_repo = None
    ui._error_log = None
    ui._metrics = None
    ui._admin_ids = {ADMIN}

    st = UiState()
    st.step = "login_phone"
    ui._states[ADMIN] = st
    ui._persist = AsyncMock()

    msg = _fake_message("+989016807808")
    await ui._step_login_phone(msg, st, "+989016807808")

    # The step is UNCHANGED — the user can simply send the number again.
    assert st.step == "login_phone", "a failed send_code must not wipe the step"
    msg.reply_text.assert_called_once()
    sent = msg.reply_text.call_args.args[0]
    assert "30" in sent, "the real cooldown value must reach the user"


# --------------------------------------------------------------------------- #
# 3. The persisted-step fallback covers a restart
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_persisted_step_blocks_bot_manager_after_restart(temp_db):
    """ui_states row alone is enough for BotManager to yield to the dashboard.

    After a restart the in-memory checker is empty, so the persisted row is
    the only signal that the dashboard owns the conversation.
    """
    temp_db.execute(
        "INSERT INTO ui_states (user_id, step, buffer, updated_at) VALUES (?,?,?,?)",
        (ADMIN, "login_code", "{}", 1),
    )
    temp_db.commit()

    repo = MagicMock()
    repo._db = SimpleNamespace(
        query_one=lambda q, p: temp_db.execute(q, p).fetchone()
    )
    mgr = _make_manager(ui_step_checker=None, ui_state_repo=repo)

    assert mgr._ui_owns(ADMIN) is True
    msg = _fake_message("12345")
    await mgr._route_text(msg)
    msg.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_clean_user_still_gets_a_reply(temp_db):
    """The guard is not a blanket 'never answer' switch."""
    repo = MagicMock()
    repo._db = SimpleNamespace(query_one=lambda q, p: None)
    mgr = _make_manager(ui_step_checker=None, ui_state_repo=repo)

    assert mgr._ui_owns(ADMIN) is False
    msg = _fake_message("plain text with no flow")
    await mgr._route_text(msg)

    msg.reply_text.assert_called_once()


# --------------------------------------------------------------------------- #
# 4. The log client surfaces failures instead of swallowing them
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_log_client_reports_delivery_failures(monkeypatch):
    """A dead logger must not vanish silently.

    Regression: _pump did `except Exception: pass`, so a broken gRPC pipe made
    every log disappear and the service looked healthy.
    """
    from logger.log_client import LogClient

    client = LogClient("localhost:1")  # nothing listening
    errors = []
    monkeypatch.setattr(
        "logger.log_client.logger",
        SimpleNamespace(error=lambda *a, **k: errors.append(a)),
    )

    # Force every RPC to fail fast.
    stub = MagicMock()
    stub.Log = AsyncMock(side_effect=RuntimeError("connection refused"))
    client._stub = stub
    client._channel = MagicMock()
    client._channel.close = AsyncMock()

    # Feed one entry and let the pump fail on it a few times.
    client.log("ERROR", "bot", "login", "probe")
    task = asyncio.create_task(client._pump())
    try:
        for _ in range(60):
            await asyncio.sleep(0.01)
            if errors:
                break
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert errors, "a persistently failing log client must log the failure"
    # The exact exception class and address reach the log — no more silent pass.
    record = str(errors[0])
    assert "localhost:1" in record
    assert "RuntimeError" in record
