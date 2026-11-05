"""SessionManager.is_ready — readiness semantics, including the empty-session probe.

**The production defect this pins.** The Go Worker's /ops connectivity probe
calls ``IsSessionReady`` with an empty session id: the worker delivers for
EVERY session and owns none of them, so "can the runtime serve MTProto work
right now?" is a pool-wide question. The manager used to answer the empty id
with ``(False, "no session id")`` — a validation rejection that read, on the
wire, as "Telegram disconnected" while delivery was working fine. The /ops
Telegram chip was permanently red on every healthy deployment.

The empty id now means "any session": ready when at least one session is
connected and not time-limited. A named session keeps the exact per-session
contract (offline / time-limited / connecting / ready).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_runtime.app.session_manager import SessionManager  # noqa: E402


class _FakeClient:
    def __init__(self, connected: bool) -> None:
        self.is_connected = connected


class _FakePool:
    def __init__(self, sessions: dict) -> None:
        self.sessions = sessions

    def get(self, session_id: str):
        return self.sessions.get(session_id)

    def list_sessions(self):
        return list(self.sessions.keys())

    def is_time_limited(self, session_id: str) -> bool:
        return False


class _LimitedPool(_FakePool):
    def is_time_limited(self, session_id: str) -> bool:
        return session_id == "limited"


def test_named_session_ready():
    pool = _FakePool({"s1": _FakeClient(True)})
    assert SessionManager(pool).is_ready("s1") == (True, "ready")


def test_named_session_offline():
    pool = _FakePool({})
    assert SessionManager(pool).is_ready("gone") == (False, "session_offline")


def test_named_session_connecting():
    pool = _FakePool({"s1": _FakeClient(False)})
    assert SessionManager(pool).is_ready("s1") == (False, "session_connecting")


def test_named_session_time_limited():
    pool = _LimitedPool({"limited": _FakeClient(True)})
    ok, state = SessionManager(pool).is_ready("limited")
    assert ok is False
    assert state.startswith("session_time_limited:")


def test_empty_session_id_means_any_ready():
    """The worker probe's question: can the pool serve MTProto right now?"""
    pool = _FakePool({"s1": _FakeClient(False), "s2": _FakeClient(True)})
    assert SessionManager(pool).is_ready("") == (True, "ready")


def test_empty_session_id_no_sessions():
    assert SessionManager(_FakePool({})).is_ready("") == (False, "no sessions")


def test_empty_session_id_all_connecting():
    pool = _FakePool({"s1": _FakeClient(False)})
    assert SessionManager(pool).is_ready("") == (False, "session_connecting")


def test_empty_session_id_time_limited_not_ready():
    """A FloodWait-suspended session does not make the pool ready."""
    pool = _LimitedPool({"limited": _FakeClient(True)})
    assert SessionManager(pool).is_ready("") == (False, "session_connecting")


def test_pool_without_list_sessions_falls_back_to_sessions_dict():
    class _NoList(_FakePool):
        def list_sessions(self):  # pragma: no cover - never called
            raise AssertionError("must not be called")

        def __getattr__(self, name):
            if name == "session_ids":
                raise AttributeError(name)
            raise AttributeError(name)

    pool = _NoList({"s1": _FakeClient(True)})
    # remove the bound method so getattr falls through to __getattr__
    del _NoList.list_sessions
    assert SessionManager(pool).is_ready("") == (True, "ready")
