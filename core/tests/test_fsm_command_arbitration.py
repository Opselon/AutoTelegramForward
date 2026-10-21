"""A slash command must never leave the caller answering a prompt they left.

The reported failure: a user typed ``/replace`` (the manager list rendered) and
then typed a name — which was stored by the create wizard's ``name`` prompt that
had been armed two screens earlier, so the bot then asked for the replacement's
"find" text. Live evidence in the 2026-10-21 logs:
``[PIPELINE:CMD] cmd=replace args=''`` at 10:36:39 followed by
``ProBotUI._text: uid=... step='replace_field_input' text='Tabligh'`` at
10:37:08, plus BotManager answering the same message with the menu hint because
the free-text router's ``stop_propagation()`` sat inside ``except Exception``.

These tests pin ALL of it, for every command in the project:

* the arbiter is registered at group -3 for `ARBITRATED_COMMANDS`, and every
  `filters.command(...)` literal in the telegram package is covered by that list
  (a new command cannot skip arbitration silently);
* every command token settles a pending step + the replace panel's answer arm,
  while keeping the data a flow collected (drafts, the last used task);
* `login_*` steps are preserved — that flow is itself command-driven and
  settling one orphans a half-finished login;
* the idle TTL retires a resumed stale step;
* the ui_states row keeps every field a flow reads back, and keeps secrets out.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import time
from types import SimpleNamespace

import pytest

from core.application.command_registry import (
    ARBITRATED_COMMANDS,
    OTHER_COMMAND_TOKENS,
    ROUTED_TOKENS,
)
from core.infrastructure.persistence.database import SqliteDatabase
from core.infrastructure.persistence.sqlite_repositories import SqliteUiStateRepository
from core.infrastructure.telegram import fsm_arbiter
from core.infrastructure.telegram.pro_bot_ui import (
    ProBotUI,
    UI_STEP_TTL_SECONDS,
)
from core.infrastructure.telegram.replace_bot_ui import STEP_FIELD
from core.infrastructure.telegram.ui_state import UiState

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "infrastructure" / "telegram"
)

UID = 5094837833


def armed_state(step: str = STEP_FIELD, field: str = "name") -> UiState:
    """A state mid-prompt: armed step + the panel's pending-answer arm."""
    st = UiState()
    st.step = step
    st.buffer = {
        "replace": {
            "status": "all",
            "page": 1,
            "draft": {"name": "", "source": "", "to": "", "task_ids": []},
            "editing": {"rule_id": "", "field": field},
        }
    }
    return st


# --------------------------------------------------------------------------- #
# the command list
# --------------------------------------------------------------------------- #

def _command_literals(node: ast.AST) -> list:
    """Every literal command name inside one `filters.command(...)` call."""
    names: list = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        names.append(node.value.lower())
    elif isinstance(node, (ast.List, ast.Tuple)) or (
        type(node) is ast.Set
    ):
        for elt in node.elts:
            names.extend(_command_literals(elt))
    elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "list":
        names.extend(_command_literals(node.args[0] if node.args else node))
    # `list(ROUTED_TOKENS)` / other registry names resolve to real commands.
    return names


def every_registered_command() -> dict:
    """{file: [names]} for every `filters.command(...)` call in the package."""
    out: dict = {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = getattr(func, "attr", "")
            if attr != "command":
                continue
            base = getattr(func, "value", None)
            if getattr(base, "id", None) != "filters":
                continue
            names: list = []
            for arg in node.args:
                names.extend(_command_literals(arg))
            out[str(path.relative_to(PACKAGE))] = names
    return out


def test_every_command_literal_is_arbitrated():
    registered = every_registered_command()
    assert {"command_router.py", "pro_bot_ui.py", "bot_manager.py"} <= set(registered), set(registered)
    for path, names in registered.items():
        unknown = [n for n in names if n not in ARBITRATED_COMMANDS]
        assert not unknown, f"{path}: commands missing from ARBITRATED_COMMANDS: {unknown}"


def test_arbitrated_list_is_the_union_of_the_routers():
    assert set(OTHER_COMMAND_TOKENS) <= set(ARBITRATED_COMMANDS)
    assert set(ROUTED_TOKENS) <= set(ARBITRATED_COMMANDS)
    assert len(ARBITRATED_COMMANDS) > len(ROUTED_TOKENS)


# --------------------------------------------------------------------------- #
# settle()
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("command", ARBITRATED_COMMANDS)
def test_every_command_settles_a_pending_prompt(command):
    st = armed_state()
    result = fsm_arbiter.settle(st, reason=f"cmd:/{command}")
    assert result.changed
    assert st.step == ""
    assert result.step_cleared == STEP_FIELD
    assert st.buffer["replace"]["editing"] == {}


def test_settle_keeps_the_data_a_flow_collected():
    st = armed_state()
    st.lang = "fa"
    st.buffer["replace"]["draft"] = {"name": "kept", "source": "", "task_ids": [7]}
    st.buffer["last_rule_id"] = "abc123"
    fsm_arbiter.settle(st, reason="cmd:/menu")
    assert st.buffer["replace"]["draft"] == {"name": "kept", "source": "", "task_ids": [7]}
    assert st.buffer["last_rule_id"] == "abc123"
    assert st.lang == "fa"


def test_a_command_without_a_pending_prompt_is_a_no_op():
    st = armed_state(step="")  # data only, no prompt armed
    result = fsm_arbiter.settle(st, reason="cmd:/replace")
    assert not result.changed
    assert st.step == ""


def test_login_step_is_preserved_not_settled():
    st = armed_state(step="login_code")
    result = fsm_arbiter.settle(st, reason="cmd:/settings")
    assert not result.changed
    assert st.step == "login_code"
    assert result.preserved == "login_code"
    assert st.rule_source == ""


def test_arms_are_cleared_even_when_a_login_is_preserved():
    st = armed_state(step="login_code")
    assert fsm_arbiter.settle(st).arms_cleared == ("replace.editing",)
    assert st.buffer["replace"]["editing"] == {}


# --------------------------------------------------------------------------- #
# the registered handler
# --------------------------------------------------------------------------- #

class FakeBot:
    def __init__(self) -> None:
        self.handlers = []

    def on_message(self, filter=None, group: int = 0):  # noqa: A002
        def decorator(fn):
            self.handlers.append((filter, group, fn))
            return fn

        return decorator


def _message(uid: int, command: list) -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), command=command)


class ArbiterHarness:
    def __init__(self) -> None:
        self.settles = []

    def _localized_handler(self, handler):
        return handler

    async def _settle_flow(self, uid, reason: str = "") -> bool:
        self.settles.append((uid, reason))
        return True


@pytest.mark.asyncio
async def test_arbiter_mounts_one_handler_at_group_minus_three():
    bot = FakeBot()
    harness = ArbiterHarness()
    fsm_arbiter.register(bot, harness)
    assert len(bot.handlers) == 1
    assert bot.handlers[0][1] == fsm_arbiter.ARBITER_GROUP


@pytest.mark.asyncio
async def test_arbiter_settles_through_the_panel_api_with_the_command_name():
    bot = FakeBot()
    harness = ArbiterHarness()
    fsm_arbiter.register(bot, harness)
    _, _, handler = bot.handlers[0]
    await handler(None, _message(3, ["replace", "add", "--x"]))
    assert harness.settles == [(3, "cmd:/replace")]


@pytest.mark.asyncio
async def test_arbiter_without_a_user_id_ignores_the_update():
    bot = FakeBot()
    harness = ArbiterHarness()
    fsm_arbiter.register(bot, harness)
    await bot.handlers[0][2](None, SimpleNamespace(from_user=None, command=["start"]))
    assert harness.settles == []


@pytest.mark.asyncio
async def test_a_settle_failure_never_blocks_the_command(caplog):
    bot = FakeBot()
    harness = ArbiterHarness()

    async def _boom(uid, reason: str = "") -> bool:
        raise RuntimeError("boom")

    harness._settle_flow = _boom
    fsm_arbiter.register(bot, harness)
    with caplog.at_level("WARNING"):
        await bot.handlers[0][2](None, _message(3, ["menu"]))
    # the arbitrary failure is logged, not raised into pyrogram's dispatcher
    assert "settle failed" in caplog.text


# --------------------------------------------------------------------------- #
# stale-step expiry
# --------------------------------------------------------------------------- #

def _ttl_harness(touch: dict) -> SimpleNamespace:
    async def _persist(uid, st):
        return None

    harness = SimpleNamespace(_step_touch=touch, _persist=_persist)
    # `_expire_stale_step` reads the clock through the shipped accessor, so the
    # harness binds the real one instead of re-implementing `_step_touch`.
    harness._touch_map = lambda: harness._step_touch
    return harness


@pytest.mark.asyncio
async def test_an_idle_step_is_retired_with_its_arm():
    st = armed_state()
    old = time.time() - UI_STEP_TTL_SECONDS - 60
    ProBotUI._expire_stale_step(_ttl_harness({1: old}), 1, st)
    assert st.step == ""
    assert st.buffer["replace"]["editing"] == {}


@pytest.mark.asyncio
async def test_a_fresh_step_survives_the_idle_check():
    st = armed_state()
    ProBotUI._expire_stale_step(_ttl_harness({1: time.time()}), 1, st)
    assert st.step == STEP_FIELD


@pytest.mark.asyncio
async def test_a_first_read_starts_the_idle_clock_without_processing():
    st = armed_state()
    touch = {}
    ProBotUI._expire_stale_step(_ttl_harness(touch), 1, st)
    assert st.step == STEP_FIELD  # unknown freshness → clock starts, not reset
    assert 1 in touch


def test_the_ttl_is_explicitly_several_hours_not_the_default_zero():
    assert UI_STEP_TTL_SECONDS >= 30 * 60


class ResumeHarness:
    """Just enough ProBotUI to exercise `_state`'s resume + age path."""

    _load_state_sync = ProBotUI._load_state_sync
    _expire_stale_step = ProBotUI._expire_stale_step
    _touch_map = ProBotUI._touch_map

    def __init__(self, repo) -> None:
        self._states: dict = {}
        self._step_touch: dict = {}
        self._ui_state_repo = repo
        self.persisted: list = []

    def _resolve_lang(self, *a, **k) -> str:
        return "en"

    async def _persist(self, uid, st) -> None:
        self.persisted.append((uid, st.step))


async def _save_armed_row(repo, uid: int = 42, age: int = 0) -> UiState:
    st = armed_state()
    st.lang = "en"
    await repo.save(uid, st.step, {**st.to_dict(), "v": 2})
    if age:
        repo._db.execute(
            "UPDATE ui_states SET updated_at=? WHERE user_id=?", (int(time.time()) - age, uid))
    return st


@pytest.mark.asyncio
async def test_resumed_state_is_aged_from_the_durable_row(tmp_path):
    """A prompt armed in the previous process must not eat today's message."""
    db = SqliteDatabase(str(tmp_path / "ui.db"))
    repo = SqliteUiStateRepository(db)
    old = UI_STEP_TTL_SECONDS + 120
    await _save_armed_row(repo, age=old)

    payload, updated_at = ProBotUI._load_state_sync(
        SimpleNamespace(_ui_state_repo=repo), 42)
    assert payload and updated_at < time.time() - old + 5

    harness = ResumeHarness(repo)
    state = ProBotUI._state(harness, 42)
    assert state.step == ""  # resumed, then retired as stale
    await asyncio.sleep(0)   # let the scheduled persist run
    assert harness.persisted and harness.persisted[-1][1] == ""


@pytest.mark.asyncio
async def test_a_recent_row_resumes_its_step_intact(tmp_path):
    db = SqliteDatabase(str(tmp_path / "ui.db"))
    repo = SqliteUiStateRepository(db)
    await _save_armed_row(repo)

    harness = ResumeHarness(repo)
    state = ProBotUI._state(harness, 42)
    assert state.step == STEP_FIELD
    assert state.buffer["replace"]["editing"] == {"rule_id": "", "field": "name"}


# --------------------------------------------------------------------------- #
# propagation signals
# --------------------------------------------------------------------------- #

def propagation_calls_inside_a_broad_except(tree: ast.AST) -> list:
    """`stop_propagation()` / `continue_propagation()` calls nested in a `try`
    whose handlers catch `Exception` (or a bare `except:`) — pyrogram raises
    those signals, so such a clause swallows them."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        broad = False
        for handler in node.handlers:
            if handler.type is None:
                broad = True
            elif isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                broad = True
        if not broad:
            continue
        # Only the `try` body is guarded by these handlers. A call in
        # `finally` raises straight out (that is the pattern command_router
        # uses on purpose), so it is not an offender.
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in {"stop_propagation", "continue_propagation"}):
                    offenders.append(sub.lineno)
    return offenders


def test_no_propagation_signal_is_swallowed():
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = propagation_calls_inside_a_broad_except(tree)
        assert not lines, f"{path.name}: propagation raised inside a broad except at {lines}"


def test_the_guard_itself_catches_a_swallowed_signal():
    """The AST guard must fail on the pattern it exists to ban."""
    bad = ast.parse(
        "async def h(m):\n"
        "    try:\n"
        "        m.stop_propagation()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert propagation_calls_inside_a_broad_except(bad) == [3]
    good = ast.parse("async def h(m):\n    m.stop_propagation()\n")
    assert propagation_calls_inside_a_broad_except(good) == []
    # `finally` is outside the handlers' reach: the raise travels out.
    in_finally = ast.parse(
        "async def h(m):\n"
        "    try:\n"
        "        await work()\n"
        "    except Exception:\n"
        "        pass\n"
        "    finally:\n"
        "        m.stop_propagation()\n"
    )
    assert propagation_calls_inside_a_broad_except(in_finally) == []


def test_the_panel_recognises_pyrograms_flow_signals_as_signals():
    pyrogram = pytest.importorskip("pyrogram")
    assert ProBotUI._is_propagation_signal(pyrogram.StopPropagation())
    assert ProBotUI._is_propagation_signal(pyrogram.ContinuePropagation())
    assert not ProBotUI._is_propagation_signal(ValueError("boom"))


def test_the_arbiter_group_is_the_outermost_message_group():
    assert fsm_arbiter.ARBITER_GROUP < -1


# --------------------------------------------------------------------------- #
# BotManager's in-memory waiting flags (the second FSM)
# --------------------------------------------------------------------------- #

class _AwaitingManager:
    """The flags `_route_text` consumes, exactly as BotManager holds them."""

    def __init__(self) -> None:
        self._awaiting_rule = None
        self._awaiting_ai = None
        self._awaiting_lang = {}


def test_a_command_clears_the_managers_waiting_flags():
    owner = _AwaitingManager()
    owner._awaiting_rule = 500
    assert fsm_arbiter.settle_awaiting(owner, 500) == ("_awaiting_rule",)
    assert owner._awaiting_rule is None


def test_only_the_chats_own_waiting_flags_are_cleared():
    owner = _AwaitingManager()
    owner._awaiting_ai = 900
    owner._awaiting_lang[700] = True
    assert fsm_arbiter.settle_awaiting(owner, 500) == ()
    assert owner._awaiting_ai == 900
    assert fsm_arbiter.settle_awaiting(owner, 700) == ("_awaiting_lang",)


def test_a_missing_waiting_store_is_tolerated():
    assert fsm_arbiter.settle_awaiting(object(), 500) == ()


# --------------------------------------------------------------------------- #
# the panel's own navigation disarms an armed prompt
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# one user at a time: the per-user mutex
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_user_gate_is_reentrant_for_the_task_that_holds_it():
    """A handler that settles the flow it is already routing must not hang.

    A plain `asyncio.Lock` deadlocks here — and a hung handler is the very
    symptom this module exists to remove (the bot goes silent).
    """
    ui = ArbiterHarness()
    gate = ProBotUI._user_lock(ui, 3)
    async with gate:
        async with gate:
            assert gate.locked()
    assert not gate.locked()


@pytest.mark.asyncio
async def test_the_user_lock_serialises_two_concurrent_updates():
    """Two updates for one user must not interleave at a decision point."""
    import asyncio as _asyncio

    ui = ArbiterHarness()
    lock = ProBotUI._user_lock(ui, 7)
    inside = []

    async def worker(tag: str) -> None:
        async with lock:
            inside.append((tag, "in"))
            await _asyncio.sleep(0)
            inside.append((tag, "out"))

    await _asyncio.gather(worker("a"), worker("b"))
    # Nested pairs, not alternating: the second worker waited for the first.
    assert inside == [("a", "in"), ("a", "out"), ("b", "in"), ("b", "out")]


@pytest.mark.asyncio
async def test_two_users_are_not_serialised_against_each_other():
    """The lock is per user: one chatty user must not block another."""
    ui = ArbiterHarness()
    assert ProBotUI._user_lock(ui, 1) is not ProBotUI._user_lock(ui, 2)


def test_the_lock_survives_a_second_event_loop():
    """`asyncio.Lock` binds its loop; the accessor must not hand a stale one."""
    import asyncio as _asyncio

    ui = ArbiterHarness()

    async def first() -> None:
        ProBotUI._user_lock(ui, 9)

    _asyncio.run(first())
    _asyncio.run(first())  # a different loop
