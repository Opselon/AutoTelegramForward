"""`atf admin console` — the guided, interactive administration session.

Why a console when every verb already exists on the CLI? Two reasons:

* **Discovery without documentation.** An administrator who hasn't read
  `atf --help` can type one number, see the exact question the next step needs
  and give an answer that is validated *before* it matters. The prompts are the
  validators — the same `domain.validators` / `domain.envschema` rules the flags
  use — so a guided answer and a flag produce the same request.
* **Destructive work needs a decision, not a reflex.** Revoking an administrator,
  restoring a `.env` backup and broadcasting to every user all demand a *typed*
  token (the user id, the backup name, the word `BROADCAST`). `--yes` does not —
  cannot — bypass those: its job is to answer the ordinary y/n prompts when
  scripting, and a script past a typed confirmation is exactly what the typed
  confirmation exists to stop.

Everything non-interactive the console does is also a flag (`--action <id>` runs
one action and exits), so a cron job and a human share one implementation. Input
may also be piped (`printf '1\\nq\\n' | atf admin console`), which is how the
end-to-end tests drive the real prompt loop.

The session is journalled to `.atf/admin-console.log` (JSON lines, redacted like
every other output), so an admin can answer "what did I change?" from the host.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from ..application.console.actions import actions as all_actions
from ..application.console.actions import by_id
from ..application.console.contracts import Confirmation, Prompt, SessionIO
from ..application.console.session import AdminConsole, SessionResult
from ..console import Console
from ..errors import UsageError
from . import _shared as sh

NAME = "console"
HELP = "Guided administration console: interactively manage users, settings, config, broadcasts"

#: Where the session journal goes when nobody names a file. Inside the repo, so
#: it stays with the installation instead of disappearing into /tmp.
DEFAULT_JOURNAL = ".atf/admin-console.log"
DEFAULT_MAX_ACTIONS = 500


# --------------------------------------------------------------------- terminal
class ConsoleIO:
    """The `SessionIO` port over a real (or piped) terminal.

    Every write goes through the `Console`, so the journal, the prompt loop and
    the menu all inherit the redaction barrier: a bot token that reaches this
    class by mistake is dropped, not reformatted.
    """

    def __init__(self, console: Console) -> None:
        self.console = console

    def title(self, text: str) -> None:
        self.console.title(text)

    def line(self, text: str = "") -> None:
        self.console.line(text)

    def ok(self, text: str) -> None:
        self.console.ok(text)

    def warn(self, text: str) -> None:
        self.console.warn(text)

    def fail(self, text: str) -> None:
        self.console.fail(text)

    def hint(self, text: str) -> None:
        self.console.hint(text)

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]], *, empty: str = "") -> None:
        self.console.table(headers, [list(row) for row in rows], empty=empty)

    def menu(self, groups: Sequence[tuple[str, Sequence[tuple[str, str]]]], *, legend: str = "") -> None:
        self.console.rule()
        for group, entries in groups:
            self.console.line()
            self.console.line("  " + self.console.paint(group, "bold"))
            for number, label in entries:
                self.console.line(f"    {number:>3}. {label}")
        if legend:
            self.console.hint(f"  {legend}")

    # ---------------------------------------------------------------- input
    def read(self, prompt: str = "") -> str | None:
        """One line; `None` marks end of input rather than an empty answer."""
        self.console.raw(prompt)
        try:
            text = sys.stdin.readline()
        except KeyboardInterrupt:
            print()
            return None
        if text == "":
            return None
        return text.rstrip("\n")

    def ask(self, prompt: Prompt) -> str:
        """Ask until the answer validates, or the input ends."""
        self.console.line()
        label = self.console.paint(prompt.label, "bold")
        marker = prompt.secret_marker if prompt.secret else prompt.default
        suffix = f"  {self.console.paint('[' + marker + ']', 'grey')}" if marker else ""
        self.console.line(f"  {label}{suffix}")
        if prompt.hint:
            self.console.hint(f"  {prompt.hint}")
        for attempt in range(3):
            raw = self._answer(prompt).strip()
            if not raw:
                if prompt.default:
                    return prompt.default
                if not prompt.required:
                    return ""
                self.console.warn("  this one is required")
                continue
            if prompt.validate is not None:
                problem = prompt.validate(raw)
                if problem:
                    self.console.warn(f"  {problem}")
                    if attempt == 2:
                        raise UsageError(
                            f"no valid answer for {prompt.label!r}",
                            hint="the value was refused locally; nothing was changed",
                        )
                    continue
            return raw
        raise UsageError(
            f"no valid answer for {prompt.label!r}",
            hint="the value was refused locally; nothing was changed",
        )

    def _answer(self, prompt: Prompt) -> str:
        if prompt.secret and self.console.is_tty:
            self.console.line()
            try:
                return getpass.getpass("   › ")
            except (EOFError, KeyboardInterrupt):
                print()
                return ""
        raw = self.read("   › ")
        return "" if raw is None else raw

    def confirm(self, question: str, *, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        self.console.line()
        raw = self.read(f"  {question} {suffix} › ")
        if raw is None or raw.strip() == "":
            return default
        return raw.strip().lower() in ("y", "yes", "1", "true")

    def require(self, confirmation: Confirmation) -> bool:
        self.console.line()
        self.console.fail(f"  {confirmation.question}")
        self.console.hint(f"  {confirmation.hint}")
        raw = self.read(f"  confirm ({confirmation.token!r}) › ")
        if raw is None:
            return False
        return confirmation.accepts(raw)


# --------------------------------------------------------------------- journal
class FileJournal:
    """JSON-lines journal of every action, redacted and appended safely."""

    def __init__(self, console: Console, path: Path) -> None:
        self.console = console
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.touch()
            path.chmod(0o600)
        except OSError as exc:
            raise UsageError(
                f"cannot open the journal at {path}: {exc}",
                hint="pass `--journal ''` to run without one",
            ) from exc

    def record(self, entry: Any) -> None:
        payload = {
            key: (self.console.redact(str(value)) if isinstance(value, str) else value)
            for key, value in entry.items()
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


# -------------------------------------------------------------------- laziness
class LazyUseCases:
    """Resolve a use case bundle only when an action touches it.

    The console's first screen is the menu, not an API call: an operator auditing
    configuration on a host whose API is down should still get past the banner.
    `env` is host-only and answered directly; everything else routes through the
    API-bound bundle and fails *inside the containing action* if the server is
    unreachable.
    """

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def __getattr__(self, name: str) -> Any:
        if name == "env":
            return self._ctx.env_uc
        return getattr(self._ctx.uc, name)


# --------------------------------------------------------------------- commands
def _session(ctx, args, *, assume_yes: bool) -> AdminConsole:
    console: Console = ctx.out
    journal_setting = getattr(args, "journal", None)
    if journal_setting == "":
        journal = None
    else:
        journal_path = Path(journal_setting) if journal_setting else ctx.repo.path(DEFAULT_JOURNAL)
        journal = FileJournal(console, journal_path)
    return AdminConsole(
        ConsoleIO(console),
        LazyUseCases(ctx),
        journal=journal,
        assume_yes=assume_yes,
        max_actions=int(getattr(args, "max_actions", DEFAULT_MAX_ACTIONS) or DEFAULT_MAX_ACTIONS),
        version=ctx.repo.version,
    )


def _report(ctx, result: SessionResult, *, closing: bool) -> int:
    if sh.wants_json(ctx, None):
        ctx.out.json(
            {
                "ok": result.failures == 0,
                "actions": result.actions,
                "failures": result.failures,
                "quit_reason": result.quit_reason,
            }
        )
        return result.exit_code
    if closing:
        ctx.out.line()
        if result.failures:
            ctx.out.fail(f"session closed with {result.failures} failed action(s)")
        else:
            ctx.out.ok(f"session closed after {result.actions} action(s)")
    return result.exit_code


def _run(ctx, args) -> int:
    result = _session(ctx, args, assume_yes=bool(ctx.repo.assume_yes)).run()
    return _report(ctx, result, closing=True)


def _run_action(ctx, args, ref: str) -> int:
    action, why = by_id(all_actions(), ref)
    if action is None:
        raise UsageError(why, hint="`atf console list` shows the action ids")
    result = _session(ctx, args, assume_yes=bool(ctx.repo.assume_yes)).run_once(action.id)
    return _report(ctx, result, closing=False)


def _list(ctx, args) -> int:
    rows = [[action.id, action.group, action.legend or "-", action.title] for action in all_actions()]
    if sh.wants_json(ctx, args):
        ctx.out.json(
            {
                "count": len(rows),
                "actions": [dict(zip(("id", "group", "danger", "title"), row)) for row in rows],
            }
        )
        return 0
    ctx.out.table(("ACTION", "GROUP", "GUARD", "WHAT"), rows)
    ctx.out.line()
    ctx.out.hint("`atf console` opens the menu · `atf console --action <id>` runs one action")
    return 0


def _max_actions(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("max-actions must be a whole number") from None
    if not 1 <= value <= 10_000:
        raise argparse.ArgumentTypeError("max-actions must be between 1 and 10000")
    return value


def _add_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--journal",
        default=None,
        help=f"session journal path (default {DEFAULT_JOURNAL}; pass '' to disable)",
    )
    parser.add_argument(
        "--max-actions",
        default=DEFAULT_MAX_ACTIONS,
        type=_max_actions,
        help="guard against a stuck loop: actions before the session stops itself",
    )
    parser.add_argument(
        "--action",
        default="",
        metavar="ID",
        help="run one action id and exit (for scripts and cron)",
    )
    parser.add_argument("--list", action="store_true", help="list every action id and exit")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    # Bare `atf console` opens the session: the whole point is that an
    # administrator does not have to know a verb to get a guided prompt. The
    # action is chosen with `--action`, not a positional, because argparse cannot
    # mix an optional positional with subcommands (`atf console list`).
    _add_flags(parser)
    parser.set_defaults(func=_entry)

    sub = sh.add_group(parser)
    p = sub.add_parser("list", help="list every action id and exit", parents=parents)
    p.set_defaults(func=_list)


def _entry(ctx, args) -> int:
    if getattr(args, "list", False):
        return _list(ctx, args)
    if getattr(args, "action", ""):
        return _run_action(ctx, args, args.action)
    return _run(ctx, args)


#: `atf admin console` reuses the same entry point (registered in `admin.py`).
def open_console(ctx, args) -> int:
    return _entry(ctx, args)
