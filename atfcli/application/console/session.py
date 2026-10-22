"""The session loop: menu, one line of input, one action, repeat.

The loop is deliberately boring. Everything interesting is behind a port:

* a failure is contained — an `AtfError` (the operator did something the server
  refuses) or an unexpected exception ends *that action*, prints the reason and
  returns to the menu. A console that exits on the first refusal is a console an
  operator stops using, and then does the same work by hand with no validation;
* every action is journalled with its outcome, so "what did I change at 03:00"
  has an answer that does not depend on shell history;
* end of input (a closed pipe, Ctrl-D) exits cleanly with the status of the
  session: 0 when every action succeeded, 1 when one failed.

No I/O, no `argparse`, no knowledge of the API: the console is driven by a fake
`SessionIO` and a fake use-case bundle in the tests, which is what makes the
*interactive* path covered rather than assumed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ...domain.errors import AtfError
from .actions import GROUP_ORDER, actions as default_actions, by_id
from .contracts import Action, ActionContext, SessionIO


@runtime_checkable
class Journal(Protocol):
    """Where an action's outcome is recorded (a JSON-lines file, in production)."""

    def record(self, entry: Mapping[str, Any]) -> None: ...


@dataclass
class SessionResult:
    """What the session did, for the command's exit status and report."""

    actions: int = 0
    failures: int = 0
    quit_reason: str = "quit"

    @property
    def exit_code(self) -> int:
        return 0 if self.failures == 0 else 1


class AdminConsole:
    """A guided administration session over the same use cases the CLI verbs use."""

    def __init__(
        self,
        io: SessionIO,
        uc: Any,
        *,
        journal: Journal | None = None,
        registry: Sequence[Action] | None = None,
        assume_yes: bool = False,
        max_actions: int = 500,
        version: str = "",
    ) -> None:
        self.io = io
        self.uc = uc
        self.journal = journal
        self.registry = tuple(registry) if registry is not None else default_actions()
        self.assume_yes = assume_yes
        self.max_actions = max_actions
        self.version = version
        self.result = SessionResult()

    # ------------------------------------------------------------------ menu
    def groups(self) -> list[tuple[str, list[Action]]]:
        """Actions grouped in `GROUP_ORDER`, groups with no actions dropped."""
        rendered: list[tuple[str, list[Action]]] = []
        for group in GROUP_ORDER:
            members = [action for action in self.registry if action.group == group]
            if members:
                rendered.append((group, members))
        # An action in a group the order does not name still gets rendered, at
        # the end: a registry entry must never be invisible.
        leftover = [action for action in self.registry if action.group not in GROUP_ORDER]
        if leftover:
            rendered.append(("Other", leftover))
        return rendered

    def numbered(self) -> list[Action]:
        """The registry in menu order — the numbers the operator types."""
        return [action for _group, members in self.groups() for action in members]

    def banner(self) -> None:
        self.io.title("ATF administration console")
        if self.version:
            self.io.hint(f"  version {self.version} · every change is journalled")
        self.io.hint("  type a number or an action id · `help` for the list · `q` to quit")

    def render_menu(self) -> None:
        numbered = self.numbered()
        index = {id(action): position for position, action in enumerate(numbered, start=1)}
        rows: list[tuple[str, list[tuple[str, str]]]] = []
        for group, members in self.groups():
            rows.append(
                (
                    group,
                    [(str(index[id(action)]), f"{action.id:<18} {action.title}") for action in members],
                )
            )
        self.io.menu(rows, legend="* destructive: requires a typed confirmation")

    # ------------------------------------------------------------------- run
    def run(self) -> SessionResult:
        """Read → dispatch → repeat until `q`, end of input, or the step ceiling."""
        self.banner()
        steps = 0
        while steps < self.max_actions:
            self.render_menu()
            try:
                raw = self.io.read("\n  console › ")
            except KeyboardInterrupt:
                self.result.quit_reason = "interrupted"
                self.io.line()
                self.io.warn("interrupted")
                break
            if raw is None:
                self.result.quit_reason = "end of input"
                self.io.line()
                break
            command = raw.strip()
            if not command:
                continue
            if command.lower() in ("q", "quit", "exit"):
                self.result.quit_reason = "quit"
                break
            if command.lower() in ("help", "?", "h"):
                self.render_menu()
                continue
            action = self.resolve(command)
            if action is None:
                continue
            steps += 1
            self.dispatch(action)
        else:
            self.result.quit_reason = "step ceiling reached"
            self.io.warn(f"stopped after {self.max_actions} actions — start a new session to continue")
        return self.result

    def run_once(self, ref: str) -> SessionResult:
        """Run one action (for `atf admin console --action <id>` and cron)."""
        action = self.resolve(ref)
        if action is not None:
            self.dispatch(action)
        return self.result

    def resolve(self, command: str) -> Action | None:
        """A menu number or an action id, with an explanation when neither fits."""
        numbered = self.numbered()
        if command.isdigit():
            position = int(command)
            if 1 <= position <= len(numbered):
                return numbered[position - 1]
            self.io.warn(f"there is no action {position} — 1..{len(numbered)}")
            return None
        action, why = by_id(numbered, command)
        if action is None:
            self.io.warn(why)
            self.io.hint("`help` lists every action and its id")
        return action

    # -------------------------------------------------------------- dispatch
    def dispatch(self, action: Action) -> bool:
        """Run one action, containing any failure to this action."""
        started = time.monotonic()
        self.result.actions += 1
        self.io.line()
        self.io.title(f"{action.id} — {action.title}")
        if action.confirmation is not None and not self.io.require(action.confirmation):
            self.io.warn("cancelled — nothing was changed")
            self._record(action, ok=False, reason="cancelled", seconds=time.monotonic() - started)
            return False
        try:
            action.run(ActionContext(io=self.io, uc=self.uc, assume_yes=self.assume_yes))
        except AtfError as exc:
            self.result.failures += 1
            self.io.fail(exc.message)
            if exc.hint:
                self.io.hint(f"  hint: {exc.hint}")
            self._record(action, ok=False, reason=exc.message, seconds=time.monotonic() - started)
            return False
        except KeyboardInterrupt:
            self.result.failures += 1
            self.result.quit_reason = "interrupted"
            self.io.warn("interrupted")
            self._record(action, ok=False, reason="interrupted", seconds=time.monotonic() - started)
            raise
        except Exception as exc:  # noqa: BLE001 - one bad action must not end the session
            self.result.failures += 1
            self.io.fail(f"{type(exc).__name__}: {exc}")
            self.io.hint("  the session is still open — `help` lists the other actions")
            self._record(action, ok=False, reason=f"{type(exc).__name__}: {exc}", seconds=time.monotonic() - started)
            return False
        self.io.ok("done")
        self._record(action, ok=True, reason="", seconds=time.monotonic() - started)
        return True

    # --------------------------------------------------------------- journal
    def _record(self, action: Action, *, ok: bool, reason: str, seconds: float) -> None:
        if self.journal is None:
            return
        entry = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "action": action.id,
            "group": action.group,
            "ok": ok,
            "reason": reason,
            "seconds": round(seconds, 3),
        }
        try:
            self.journal.record(entry)
        except Exception as exc:  # a broken journal must not break the session
            self.io.hint(f"  (journal unavailable: {type(exc).__name__}: {exc})")


def build_session(io: SessionIO, uc: Any, **kwargs: Any) -> AdminConsole:
    """Composition helper so a caller does not import the registry twice."""
    return AdminConsole(io, uc, registry=default_actions(), **kwargs)


__all__ = ["AdminConsole", "Journal", "SessionResult", "build_session"]
