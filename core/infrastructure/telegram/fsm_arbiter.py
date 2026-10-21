"""Command / FSM arbitration: a command always wins over a pending prompt.

Why this module exists
----------------------
The bot has two ways to read a user's next message: a slash command
(``command_router`` / the panel's builtin handlers, all registered in a group
that runs *before* the free-text router) and the per-user FSM step (the
``ui_states`` row: "Send the rule's new **name**").

Those two used to be independent. Every command rendered its own screen and
returned without touching the FSM, so whatever prompt was armed before the
command stayed armed. The user's *next* message was then consumed by the prompt
they had walked away from:

    user: /replace            -> the replace manager list is rendered
    user: Tabligh             -> ...and is stored as the *rule name* of a
                                 wizard the user left two screens ago,
                                 because step was still replace_field_input
    bot:  Send the new **text to find**.

That is the reported "the command runs but the handlers don't guide the user":
the screen in front of the user and the handler that answers the next message
disagree. No single handler is wrong — the two routers were never arbitrated.

The rule this module enforces
-----------------------------
A slash command is a statement about intent ("I am doing this now"), so it
settles any pending prompt *before* it renders:

* ``step`` is cleared, so the next free-text message goes to the normal router
  (menu hint / BotManager) instead of into an abandoned prompt;
* the pending-answer arm of the replace panel (``buffer["replace"]["editing"]``)
  is cleared with it, so a later render cannot resurrect the prompt;
* data the flow had already collected (drafts, the last-used task, the search
  query, the page) is *kept* — settling is not a reset. ``/start`` and
  ``/cancel`` still reset, they own that decision.

One carve-out: a ``login_*`` step belongs to the login flow, which is itself
command-driven (``/login`` arms ``login_phone``, the phone arms ``login_code``).
A command typed during a login must not orphan the half-finished login, so
those steps are preserved and the preservation is logged.

The arbiter is registered for *every* command token the bot knows — including
the ones other modules own — and a test scans the package for
``filters.command(...)`` literals, so a command added later cannot silently skip
arbitration.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

#: Registered ahead of the command router (-2), the free-text router (-1) and
#: BotManager (1), so it always sees a command before anything renders.
ARBITER_GROUP = -3

#: Steps that belong to a flow which is itself command-driven. Settling one
#: would leave that flow half-alive (pending login state, no step to answer).
PROTECTED_STEP_PREFIXES: Tuple[str, ...] = ("login_",)

#: Where a screen arms the answer it is waiting for. ``editing`` under the
#: replace panel's slice: ``{"rule_id": "...", "field": "source"}``.
REPLACE_PANEL_KEY = "replace"
PENDING_ARM_KEY = "editing"


@dataclass(frozen=True)
class SettleResult:
    """What one arbitration pass changed (the handlers log this verbatim)."""

    step_cleared: str = ""
    arms_cleared: Tuple[str, ...] = ()
    preserved: str = ""
    notes: Tuple[str, ...] = field(default=())

    @property
    def changed(self) -> bool:
        """A pending *prompt* was settled (the reason to log and to resume-warn)."""
        return bool(self.step_cleared)

    @property
    def touched(self) -> bool:
        """Anything at all changed, including a stale answer arm."""
        return bool(self.step_cleared or self.arms_cleared)


def is_protected(step: str) -> bool:
    return any(step.startswith(prefix) for prefix in PROTECTED_STEP_PREFIXES)


def settle(state: Any, reason: str = "") -> SettleResult:
    """Clear a pending prompt on ``state`` in place. Returns what was cleared.

    Pure state mutation, no IO: the caller persists. ``reason`` is carried for
    the caller's log line only (it never affects the decision).
    """
    step = str(getattr(state, "step", "") or "")
    buffer: Dict[str, Any] = getattr(state, "buffer", None) or {}
    if not isinstance(buffer, dict):
        buffer = {}
        state.buffer = buffer

    arms: list = []
    panel = buffer.get(REPLACE_PANEL_KEY)
    if isinstance(panel, dict) and panel.get(PENDING_ARM_KEY):
        panel[PENDING_ARM_KEY] = {}
        arms.append(f"{REPLACE_PANEL_KEY}.{PENDING_ARM_KEY}")

    if not step:
        # Nothing was armed: clearing a leftover arm is hygiene, not a settle.
        return SettleResult(arms_cleared=tuple(arms))

    if is_protected(step):
        return SettleResult(
            arms_cleared=tuple(arms),
            preserved=step,
            notes=(f"preserved {step!r} ({reason})",) if reason else (),
        )

    if step:
        state.step = ""
        # The draft fields belong to a flow that is no longer current; they are
        # the same values the rule editor reads, so clear them with the step.
        state.rule_source = ""
        state.rule_target = ""
        return SettleResult(step_cleared=step, arms_cleared=tuple(arms))

    return SettleResult(arms_cleared=tuple(arms))


class UserGate:
    """A re-entrant, per-user async gate.

    One update at a time per user, and *re-entrant* for the task that holds it:
    a handler that already owns the gate (the free-text router, which then calls
    into a step handler that settles the same flow) must not deadlock against
    itself. A plain `asyncio.Lock` would hang the whole handler — and a hung
    handler looks exactly like the bug this module exists to fix: the bot goes
    silent on a message the user sent.
    """

    __slots__ = ("_lock", "_owner", "_depth")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Any = None
        self._depth = 0

    async def __aenter__(self) -> "UserGate":
        task = asyncio.current_task()
        if self._owner is task and self._depth:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._owner = None
            self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


#: BotManager's in-memory waiting flags (`_awaiting_lang` is a dict). They are a
#: second, older FSM: each one consumes the next free text for one prompt.
AWAITING_ATTRS: Tuple[str, ...] = ("_awaiting_rule", "_awaiting_ai")
AWAITING_DICT_ATTR = "_awaiting_lang"


def settle_awaiting(owner: Any, chat_id: Any) -> Tuple[str, ...]:
    """Clear every waiting flag ``owner`` holds for ``chat_id``.

    Returns the flags that were actually cleared (for the caller's log). Pure
    attribute mutation: the manager owns these, so the same rule as `settle`
    applies — a command must not leave one armed for the next message.
    """
    cleared: list = []
    if chat_id is None:
        return ()
    for attr in AWAITING_ATTRS:
        if getattr(owner, attr, None) == chat_id:
            setattr(owner, attr, None)
            cleared.append(attr)
    waiting = getattr(owner, AWAITING_DICT_ATTR, None)
    if isinstance(waiting, dict) and waiting.pop(chat_id, None):
        cleared.append(AWAITING_DICT_ATTR)
    return tuple(cleared)


def register(client: Any, ui: Any) -> Any:
    """Mount the arbiter on ``client``; ``ui`` is the panel it settles for.

    Lives here (not inline in ``ProBotUI._register``) so the handler, its group
    and its command list are one testable unit: a test mounts it on a fake
    client and drives real messages through it.
    """
    from pyrogram import filters

    from ...application.command_registry import ARBITRATED_COMMANDS

    async def _fsm_arbitrate(_, message: Any) -> None:
        uid = int(getattr(getattr(message, "from_user", None), "id", 0) or 0)
        if not uid:
            return
        try:
            command = (getattr(message, "command", None) or [""])[0]
        except Exception:
            command = ""
        try:
            # Same per-user mutex the free-text router takes: a command and the
            # message it refers to must not settle and consume the same step
            # concurrently (the loser would persist a step the winner cleared).
            lock = ui._user_lock(uid) if callable(getattr(ui, "_user_lock", None)) else None
            if lock is None:
                await ui._settle_flow(uid, reason=f"cmd:/{command}")
            else:
                async with lock:
                    await ui._settle_flow(uid, reason=f"cmd:/{command}")
        except Exception as exc:  # arbitration must never block a command
            logger.warning(
                "[PIPELINE:FSM] settle failed for uid=%s cmd=%r: %s", uid, command, exc)

    # The panel's language binding is applied before registration so the
    # registered callable is the bound one (the arbiter renders nothing today,
    # but a settle message would have to speak the caller's language).
    decorate = getattr(ui, "_localized_handler", None)
    handler = decorate(_fsm_arbitrate) if callable(decorate) else _fsm_arbitrate
    return client.on_message(
        filters.private & filters.command(list(ARBITRATED_COMMANDS)),
        group=ARBITER_GROUP,
    )(handler)


__all__ = [
    "ARBITER_GROUP",
    "AWAITING_ATTRS",
    "AWAITING_DICT_ATTR",
    "PENDING_ARM_KEY",
    "PROTECTED_STEP_PREFIXES",
    "REPLACE_PANEL_KEY",
    "SettleResult",
    "UserGate",
    "is_protected",
    "register",
    "settle",
    "settle_awaiting",
]
