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


# --------------------------------------------------------------------------- #
# 5. Login code anti-phishing keypad and input sanitization
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_login_code_spaced_and_dashed_cleaning():
    """Telegram blocks raw consecutive digits shared in bot chats.
    Users can enter codes with spaces or dashes, and the bot cleans them.
    """
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState

    ui = object.__new__(ProBotUI)
    ui._login = MagicMock()
    ui._login.submit_code = AsyncMock(return_value="login_success:sess1")
    ui._submit_code_flow = ProBotUI._submit_code_flow.__get__(ui, ProBotUI)
    ui._finish_login = AsyncMock()
    ui._step_login_code = ProBotUI._step_login_code.__get__(ui, ProBotUI)

    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN),
        chat=SimpleNamespace(id=ADMIN),
        reply_text=AsyncMock(),
    )
    st = UiState(step="login_code", buffer={})

    # Spaced input: "9 2 2 1 1"
    await ui._step_login_code(msg, st, "9 2 2 1 1")  # type: ignore[arg-type]
    ui._login.submit_code.assert_called_with(ADMIN, "92211")

    # Dashed input: "9-2-2-1-1"
    ui._login.submit_code.reset_mock()
    await ui._step_login_code(msg, st, "9-2-2-1-1")  # type: ignore[arg-type]
    ui._login.submit_code.assert_called_with(ADMIN, "92211")

    # Attached mycode prefix: "mycode73737"
    ui._login.submit_code.reset_mock()
    await ui._step_login_code(msg, st, "mycode73737")  # type: ignore[arg-type]
    ui._login.submit_code.assert_called_with(ADMIN, "73737")

    # 6-digit mycode prefix: "mycode737373"
    ui._login.submit_code.reset_mock()
    await ui._step_login_code(msg, st, "mycode737373")  # type: ignore[arg-type]
    ui._login.submit_code.assert_called_with(ADMIN, "737373")

    # Persian digits with mycode prefix: "mycode۷۳۷۳۷"
    ui._login.submit_code.reset_mock()
    await ui._step_login_code(msg, st, "mycode۷۳۷۳۷")  # type: ignore[arg-type]
    ui._login.submit_code.assert_called_with(ADMIN, "73737")


@pytest.mark.asyncio
async def test_login_code_numeric_keypad_submission():
    """The inline keypad collects 5 digits one by one and submits on the 5th."""
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState

    ui = object.__new__(ProBotUI)
    st = UiState(step="login_code", buffer={})
    ui._state = MagicMock(return_value=st)
    ui._persist = AsyncMock()
    ui._login_code_text = MagicMock(return_value="text")
    ui._code_keypad = MagicMock(return_value="kbd")
    ui._submit_code_flow = AsyncMock()
    ui._cb_keypad = ProBotUI._cb_keypad.__get__(ui, ProBotUI)

    for digit in ["5", "4", "3", "2"]:
        cq = SimpleNamespace(
            from_user=SimpleNamespace(id=ADMIN),
            data=f"k:{digit}",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(),
        )
        await ui._cb_keypad(cq)  # type: ignore[arg-type]

    assert st.buffer["code_digits"] == "5432"
    assert not ui._submit_code_flow.called

    # 5th digit triggers submission
    cq5 = SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN),
        data="k:1",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(),
    )
    await ui._cb_keypad(cq5)  # type: ignore[arg-type]
    ui._submit_code_flow.assert_called_once_with(cq5.message, st, "54321", uid=ADMIN)


@pytest.mark.asyncio
async def test_render_chat_picker_pagination():
    """Chat picker properly partitions dialogs into pages with inline buttons."""
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState

    ui = object.__new__(ProBotUI)
    ui._t = lambda key, **kw: key
    ui._kbd = lambda rows: rows

    st = UiState(
        step="rule_source",
        buffer={
            "chats": [
                {"id": f"-100{i}", "title": f"Chan {i}", "emoji": "📢", "kind": "channel", "username": f"chan{i}"}
                for i in range(1, 13)
            ],
            "page": 0,
        },
    )

    text_p0, kbd_p0 = ui._render_chat_picker(st, role="source", page=0)
    assert "**1. Chan 1**" in text_p0
    assert "**5. Chan 5**" in text_p0
    assert "**6. Chan 6**" not in text_p0
    # 5 items + 1 nav row + 1 action row = 7 rows
    assert len(kbd_p0) == 7  # type: ignore[arg-type]

    text_p1, kbd_p1 = ui._render_chat_picker(st, role="source", page=1)
    assert "**6. Chan 6**" in text_p1
    assert "**10. Chan 10**" in text_p1
    assert "**1. Chan 1**" not in text_p1


@pytest.mark.asyncio
async def test_step_rule_source_matched_from_cached_dialogs():
    """Entering username or ID resolves title from cached dialogs."""
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState

    ui = object.__new__(ProBotUI)
    ui._t = lambda key, **kw: key
    ui._kbd = lambda rows: rows
    ui._persist = AsyncMock()
    ui._pool = MagicMock()
    ui._sessions = MagicMock()

    st = UiState(
        step="rule_source",
        buffer={
            "chats": [
                {"id": "-100888", "title": "Forex VIP", "emoji": "📢", "kind": "channel", "username": "forex_vip"},
            ],
            "page": 0,
        },
    )

    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN),
        reply_text=AsyncMock(),
    )

    await ui._step_rule_source(msg, st, "@forex_vip")  # type: ignore[arg-type]
    assert st.rule_source == "-100888"
    assert "Forex VIP" in st.buffer["source_name"]
    assert st.step == "rule_target"


@pytest.mark.asyncio
async def test_finish_rule_creation_flow():
    """Finishing rule creation persists ForwardRule and clears buffer."""
    from core.infrastructure.telegram.pro_bot_ui import ProBotUI, UiState

    ui = object.__new__(ProBotUI)
    ui._t = lambda key, **kw: key
    ui._kbd = lambda rows: rows
    ui._persist = AsyncMock()
    ui._sessions = MagicMock()
    ui._sessions.list_all = AsyncMock(return_value=[SimpleNamespace(id="sess-active")])
    ui._rules = MagicMock()
    ui._rules.create = AsyncMock()
    ui._log = MagicMock()

    st = UiState(
        step="rule_target",
        rule_source="-100111",
        rule_target="-100222",
        buffer={"source_name": "📢 Source Chan", "chats": []},
    )

    cq = SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN),
        edit_message_text=AsyncMock(),
    )

    await ui._finish_rule_creation(cq, st, "📢 Source Chan", "📢 Target Chan")
    assert st.step == ""
    assert len(st.buffer) == 0
    ui._rules.create.assert_called_once()
    created = ui._rules.create.call_args[0][0]
    assert created.source_chat_id == "-100111"
    assert created.target_chat_id == "-100222"
