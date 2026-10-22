"""Contracts for the guided administration console.

The console is a small interpreter: it renders a menu, reads one line, and runs
one *action*. Everything that makes that dangerous — prompts, confirmations,
rendering — is behind :class:`SessionIO`, so the interpreter is exercised by a
scripted fake in tests and by a real terminal in production, with no branch that
only runs interactively.

Three ideas are load-bearing:

* **An action is data.** `Action` states its id, title, group, how dangerous it
  is and what it runs. Adding a capability is adding a row, not editing a
  dispatch table — the same rule the rest of `atfcli` follows.
* **A typed confirmation for destructive actions.** `y` is a reflex; typing the
  user id you are about to demote (or the word BROADCAST) is a decision. The
  confirmation token is part of the action, so no destructive verb can be added
  without one, and `--yes` deliberately does *not* bypass it.
* **Prompts validate before they return.** A prompt carries its validator, the
  adapter re-asks on a bad answer, and the action itself never has to parse a
  half-checked string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

#: How much damage an action can do, in increasing order.
READ = "read"
WRITE = "write"
DESTRUCTIVE = "destructive"

LEGEND: dict[str, str] = {
    READ: "",
    WRITE: "changes state",
    DESTRUCTIVE: "requires a typed confirmation",
}


@dataclass(frozen=True)
class Prompt:
    """One question, with the rule that makes the answer usable."""

    label: str
    default: str = ""
    hint: str = ""
    required: bool = True
    secret: bool = False
    validate: Callable[[str], str | None] | None = None
    #: Shown instead of the value when `secret` — never the value itself.
    secret_marker: str = "[hidden]"


@dataclass(frozen=True)
class Confirmation:
    """The typed token a destructive action demands before it runs."""

    question: str
    token: str
    hint: str = ""

    def accepts(self, answer: str) -> bool:
        return (answer or "").strip() == self.token


@runtime_checkable
class SessionIO(Protocol):
    """Everything the console needs from a terminal, and nothing else."""

    def title(self, text: str) -> None: ...
    def line(self, text: str = "") -> None: ...
    def ok(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def fail(self, text: str) -> None: ...
    def hint(self, text: str) -> None: ...
    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]], *, empty: str = "") -> None: ...
    def menu(self, groups: Sequence[tuple[str, Sequence[tuple[str, str]]]], *, legend: str = "") -> None: ...
    #: One line of input; `None` means end of input (a closed pipe, Ctrl-D).
    def read(self, prompt: str = "") -> str | None: ...
    def ask(self, prompt: Prompt) -> str: ...
    def confirm(self, question: str, *, default: bool = False) -> bool: ...
    def require(self, confirmation: Confirmation) -> bool: ...


@dataclass(frozen=True)
class ActionContext:
    """What an action is handed: the ports it may use, and the flags it honours."""

    io: SessionIO
    uc: Any
    #: `--yes`: skip ordinary confirmations (never a typed one).
    assume_yes: bool = False

    def confirm(self, question: str, *, default: bool = False) -> bool:
        if self.assume_yes:
            self.io.hint("  (--yes: confirmed without asking)")
            return True
        return self.io.confirm(question, default=default)


@dataclass(frozen=True)
class Action:
    """One thing the console can do."""

    id: str
    title: str
    group: str
    run: Callable[[ActionContext], None]
    danger: str = READ
    confirmation: Confirmation | None = None
    keywords: tuple[str, ...] = ()

    @property
    def legend(self) -> str:
        return LEGEND.get(self.danger, "")

    def matches(self, ref: str) -> bool:
        wanted = (ref or "").strip().lower()
        if not wanted:
            return False
        if wanted == self.id.lower():
            return True
        if wanted in (keyword.lower() for keyword in self.keywords):
            return True
        # A unique prefix is enough: `sus` finds `user-suspend` when it is the
        # only action that starts that way (the session rejects ambiguity).
        return self.id.lower().startswith(wanted)


__all__ = [
    "Action",
    "ActionContext",
    "Confirmation",
    "DESTRUCTIVE",
    "LEGEND",
    "Prompt",
    "READ",
    "SessionIO",
    "WRITE",
]
