"""Regressions for recurring [UI_STATE:BUFFER_TRUNCATED] warnings.

The durable UI-state buffer is bounded by design (``MAX_BUFFER_VALUE``) while
the live render state is not, so a chat-picker buffer bigger than the cap was
truncated on *every* persist — once per screen press. That turned one design
fact into a recurring WARNING (27 identical rows for one ``key=chats`` shape
in the production logger store).

The contract these tests pin:

* the *first* truncation of a given shape is still a WARNING (it is real,
  operator-visible data bounding — silencing it entirely would hide it), and
* an identical repeat (same key, same note, same size) is DEBUG, and
* a *changed* shape (more keys kept, a different note) warns again.
"""

from __future__ import annotations

import logging

import pytest

from core.infrastructure.telegram.ui_state_manager import (
    MAX_BUFFER_VALUE,
    _report_buffer_truncation,
    _safe_buffer,
)

#: The module's own logger — caplog has to target it explicitly, otherwise the
#: root logger's default level filters the DEBUG repeats out before they reach
#: the record list and the test measures a fixture artifact, not the code.
LOGGER_NAME = "core.infrastructure.telegram.ui_state_manager"


def _reset_dedup() -> None:
    from core.infrastructure.telegram import ui_state_manager as m

    m._TRUNCATION_SHAPES.clear()


def test_first_truncation_of_a_shape_warns(caplog):
    _reset_dedup()
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    caplog.clear()

    _report_buffer_truncation("chats", "list 13/56 items", 1969)

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, "the first truncation of a shape must warn"
    assert "BUFFER_TRUNCATED" in warns[0].message


def test_identical_repeat_is_debug_not_warning(caplog):
    _reset_dedup()
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    _report_buffer_truncation("chats", "list 13/56 items", 1969)
    caplog.clear()
    _report_buffer_truncation("chats", "list 13/56 items", 1969)
    _report_buffer_truncation("chats", "list 13/56 items", 1969)

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert not warns, (
        "an identical repeat adds no information and must not warn again: "
        + str([r.message for r in warns])
    )
    assert debugs, "the repeat must still be traceable at DEBUG"


def test_a_changed_shape_warns_again(caplog):
    _reset_dedup()
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    _report_buffer_truncation("chats", "list 13/56 items", 1969)
    caplog.clear()
    # The user scrolled: more chats fit into the cap, so the shape changed.
    _report_buffer_truncation("chats", "list 14/56 items", 2001)

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, "a changed truncation shape is new information"


def test_safe_buffer_still_truncates_and_keeps_bounds(caplog):
    """The de-duplication must not weaken the bounding itself."""
    _reset_dedup()
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    big = {"chats": [{"id": i, "title": f"chat number {i}"} for i in range(56)]}
    first = _safe_buffer(big)
    caplog.clear()
    second = _safe_buffer(big)

    assert len(first["chats"]) == len(second["chats"]), (
        "repeated projection of the same buffer must be stable"
    )
    import json

    assert len(json.dumps(first["chats"])) <= MAX_BUFFER_VALUE
    repeats = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "BUFFER_TRUNCATED" in r.message
    ]
    assert not repeats, (
        "the identical re-projection must not warn again: "
        + str([r.message for r in repeats])
    )


def test_distinct_keys_each_warn_once(caplog):
    _reset_dedup()
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    _report_buffer_truncation("chats", "list 13/56 items", 1969)
    _report_buffer_truncation("sel_chats", "list 13/56 items", 1969)
    caplog.clear()
    # Repeats of both, still identical: neither may warn.
    _report_buffer_truncation("chats", "list 13/56 items", 1969)
    _report_buffer_truncation("sel_chats", "list 13/56 items", 1969)

    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warns, "deduplication is per (key, note), not global"
