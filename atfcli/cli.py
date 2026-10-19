"""Command tree and dispatch for the ATF CLI.

Design notes:

* One global parent parser supplies `--json/--url/--token/--yes/--no-color/...`
  and is attached to every subcommand, so flags work in the natural place
  (`atf rules list --json`) as well as before the group (`atf --json rules list`).
* Commands are modules under `atfcli.commands`; each exposes `NAME`, `add_parser`
  and `run`. Adding a capability means adding a module, never editing dispatch.
* Every command returns an exit code (or None for 0) and raises `AtfError` for
  anything the user should see explained rather than as a traceback.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import traceback
from dataclasses import dataclass
from typing import Any

from . import __version__
from .api import ApiClient
from .commands._shared import HelpHandler
from .console import Console
from .context import RepoContext
from .domain import validators
from .errors import AtfError, EXIT_FAILURE, EXIT_OK, EXIT_USAGE

# Order matters only for the help listing: most-used groups first.
COMMAND_MODULES: tuple[str, ...] = (
    "auth",
    "setup",
    "upgrade",
    "platform",
    "doctor",
    "validate",
    "status",
    "rules",
    "ai",
    "prompts",
    "filters",
    "sessions",
    "logs",
    "data",
    "admin",
    "audit",
    "docker",
    "deploy",
    "db",
    "certs",
    "net",
    "test",
    "i18n",
    "proto",
    "release",
    "ci",
    "watch",
    "system",
    "config",
)

#: One-verb shortcuts for the whole stack, so `atf up` == `atf system start`.
#: A shortcut never shadows a real group (see `_expand_aliases`).
ALIASES: dict[str, list[str]] = {
    "up": ["system", "start"],
    "down": ["system", "stop"],
    "restart": ["system", "restart"],
    "update": ["upgrade"],
}


#: Global flags that consume the following token, so the alias scanner must not
#: mistake that value for the command position.
_VALUE_FLAGS = frozenset({"--url", "--token", "--timeout", "--root", "--retries"})


def _retries_type(raw: str) -> int:
    """`--retries N`: how many *extra* attempts a safe request may make.

    Kept small on purpose: this is a CLI, not a queue worker. `0` is the escape
    hatch for a script that would rather fail now and decide itself.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("expected a whole number of retries, e.g. 2") from None
    if not 0 <= value <= 10:
        raise argparse.ArgumentTypeError("retries must be between 0 and 10")
    return value


def _retries_default() -> int:
    """`ATF_CLI_RETRIES`, clamped; a malformed value falls back instead of crashing."""
    try:
        value = int(os.environ.get("ATF_CLI_RETRIES", "") or 2)
    except ValueError:
        return 2
    return max(0, min(value, 10))


def _timeout_type(raw: str) -> float:
    """`--timeout S`: seconds, finite and positive."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("expected a number of seconds, e.g. 20") from None
    if not 0 < value <= 3600:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 3600 seconds")
    return value


def _expand_aliases(argv: list[str]) -> list[str]:
    """Rewrite a shortcut verb into the command path it stands for.

    The shortcut may sit after global flags (`atf --json up`), so the first
    non-flag token is located rather than assuming position 0.
    """
    expect_value = False
    for index, token in enumerate(argv):
        if expect_value:
            expect_value = False
            continue
        if token in _VALUE_FLAGS:
            expect_value = True
            continue
        if token.startswith("-"):
            continue
        if token in ALIASES and token not in COMMAND_MODULES:
            return [*argv[:index], *ALIASES[token], *argv[index + 1 :]]
        return argv  # the first real command token is not a shortcut
    return argv


@dataclass
class CliContext:
    """Everything a command needs, built once per invocation.

    ``repo`` is the host adapter (files, docker, subprocesses); ``uc`` is the
    application layer. A command module should reach for ``ctx.uc`` — the
    use cases — and only touch ``ctx.api``/``ctx.repo`` directly when it is
    doing something the application layer does not model (a raw deploy step,
    for instance).
    """

    out: Console
    repo: RepoContext
    args: argparse.Namespace
    _api: ApiClient | None = None
    _gateways: Any = None
    _uc: Any = None

    @property
    def api(self) -> ApiClient:
        """The API client, credentialed on first use."""
        if self._api is None:
            client = ApiClient(
                self.out,
                url=getattr(self.args, "url", "") or "",
                token=getattr(self.args, "token", "") or "",
                timeout=float(getattr(self.args, "timeout", 20.0) or 20.0),
                retries=int(getattr(self.args, "retries", 2) or 0),
                verify_tls=not getattr(self.args, "insecure", False),
                context=self.repo,
            )
            client.resolve_url()
            client.load_credential(allow_mint=not getattr(self.args, "no_mint", False))
            self._api = client
        return self._api

    @property
    def api_or_none(self) -> ApiClient | None:
        """Best-effort client: returns None instead of raising when unauthenticated."""
        try:
            return self.api
        except AtfError:
            return None

    @property
    def gateways(self) -> Any:
        """The API adapters, built on first use (they require a credential)."""
        if self._gateways is None:
            from .infrastructure.http.gateways import HttpGateways

            self._gateways = HttpGateways(self.api, host=self.repo)
        return self._gateways

    @property
    def uc(self) -> Any:
        """The use cases: what a command actually *does*."""
        if self._uc is None:
            from .application.container import build_use_cases

            self._uc = build_use_cases(self.gateways, files=HostFiles(self.repo, self.out))
        return self._uc

    @property
    def files(self) -> "HostFiles":
        """The file-writing port, so a command can honour `--output`."""
        return HostFiles(self.repo, self.out)

    def want_json(self) -> bool:
        return bool(getattr(self.args, "json", False))


class HostFiles:
    """The :class:`~atfcli.application.gateways.FileWriter` port over the host."""

    def __init__(self, repo: RepoContext, out: Console) -> None:
        self.repo = repo
        self.out = out

    def write_text(self, path: str, text: str) -> None:
        target = self.repo.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def write_bytes(self, path: str, payload: bytes) -> None:
        target = self.repo.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


#: What a global flag means when nobody passed it. Kept beside `_global_flags`
#: so the two cannot drift.
_GLOBAL_DEFAULTS: dict[str, Any] = {
    "json": False,
    "url": "",
    "token": "",
    "timeout": 20.0,
    "retries": 2,
    "insecure": False,
    "no_mint": False,
    "no_color": False,
    "quiet": False,
    "verbose": False,
    "yes": False,
    "dry_run": False,
    "root": "",
}


def _global_flags(*, defaults: bool) -> argparse.ArgumentParser:
    """The global flag set, in two flavours.

    A flag given *before* the group name (`atf --json rules list`) is parsed by
    the top-level parser, and argparse copies every subparser attribute over the
    parent namespace afterwards — so the subparser copies must default to
    SUPPRESS, or they would overwrite it with their own (empty) default and the
    flag would silently do nothing.
    """
    parser = argparse.ArgumentParser(add_help=False)
    default = (lambda value: value) if defaults else (lambda value: argparse.SUPPRESS)

    parser.add_argument("--json", action="store_true", default=default(False), help="emit one JSON document on stdout")
    parser.add_argument(
        "--url",
        type=validators.argparse_type("url", optional=True),
        default=default(os.environ.get("ATF_CLI_URL", "")),
        help="dashboard API base URL (default: $ATF_CLI_URL, the saved server, or a local probe)",
    )
    parser.add_argument("--token", default=default(os.environ.get("ATF_TOKEN", "")), help="bearer token (prefer ATF_TOKEN)")
    parser.add_argument("--timeout", type=_timeout_type, default=default(20.0), help="HTTP timeout in seconds (default: 20)")
    parser.add_argument(
        "--retries",
        type=_retries_type,
        default=default(_retries_default()),
        help="extra attempts for safe requests after a timeout/5xx (default: 2, 0 disables)",
    )
    parser.add_argument("--insecure", action="store_true", default=default(False), help="skip TLS verification")
    parser.add_argument("--no-mint", action="store_true", default=default(False), help="never derive a local admin token")
    parser.add_argument("--no-color", action="store_true", default=default(False), help="disable ANSI colour")
    parser.add_argument("-q", "--quiet", action="store_true", default=default(False), help="suppress human output")
    parser.add_argument("-v", "--verbose", action="store_true", default=default(False), help="show every command executed")
    parser.add_argument("-y", "--yes", action="store_true", default=default(False), help="assume yes for confirmations")
    parser.add_argument("--dry-run", action="store_true", default=default(False), help="describe changes without applying them")
    parser.add_argument("--root", default=default(""), help="repository root (default: auto-detected)")
    return parser


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, Any]]:
    global_flags = _global_flags(defaults=True)
    sub_flags = _global_flags(defaults=False)

    parser = argparse.ArgumentParser(
        prog="atf",
        description="AutoTelegramForward control CLI — run, inspect and repair the whole stack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[global_flags],
        epilog=(
            "Examples:\n"
            "  atf doctor                          full system health check\n"
            "  atf status --watch                  live service board\n"
            "  atf rules list --json               machine-readable task list\n"
            "  atf logs tail --follow -n 50        follow the log archive\n"
            "  atf rules settings set <id> clean_buttons=true\n"
            "  atf deploy --build                  rebuild, recreate, verify\n"
            "  atf auth token --save               mint and store an admin token\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"atf {__version__}")

    sub = parser.add_subparsers(dest="group", metavar="<group>")
    registry: dict[str, Any] = {}
    missing: list[str] = []
    for name in COMMAND_MODULES:
        try:
            module = importlib.import_module(f".commands.{name}", package=__package__)
        except ImportError as exc:  # a broken module must not hide the rest
            missing.append(f"{name} ({exc})")
            continue
        module.add_parser(sub, parents=[sub_flags])
        registry[getattr(module, "NAME", name)] = module
    # A missing group is worth knowing about, but not worth polluting every
    # invocation of `atf --help` with it.
    if missing and ("-v" in sys.argv or "--verbose" in sys.argv):
        print(f"warning: unavailable command groups: {', '.join(missing)}", file=sys.stderr)
    return parser, registry


def make_console(args: argparse.Namespace) -> Console:
    color: bool | None = None
    if getattr(args, "no_color", False):
        color = False
    return Console(
        json_mode=bool(getattr(args, "json", False)),
        color=color,
        quiet=bool(getattr(args, "quiet", False)),
        verbose=bool(getattr(args, "verbose", False)),
    )


def _fill_global_defaults(args: argparse.Namespace) -> None:
    """Guarantee every global flag exists on the namespace.

    Subparser copies default to SUPPRESS (see `_global_flags`), so a command
    handler can otherwise hit an AttributeError on a flag it never received.
    """
    for key, value in _GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)


def _is_help_handler(handler: Any) -> bool:
    """True for the synthetic `func` a command *group* gets (see `help_handler`)."""
    return isinstance(handler, HelpHandler)


def _group_commands(handler: Any) -> list[str]:
    """The verbs a bare group could have run, for the machine-readable refusal."""
    parser = getattr(handler, "parser", None)
    commands: list[str] = []
    if parser is None:
        return commands
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            commands = sorted(action.choices)
    return commands


def _json_usage_error(console: Console, args: argparse.Namespace, handler: Any) -> int:
    """A bare group in `--json` mode: one document, a real usage exit code.

    Printing help text to stdout and exiting 0 made `atf admin --json | jq`
    receive prose with a success status — the worst of both contracts. The verbs
    are listed so the caller can pick one without a second invocation.
    """
    group_parser = getattr(handler, "parser", None)
    prog = getattr(group_parser, "prog", "") or f"atf {getattr(args, 'group', '')}"
    commands = _group_commands(handler)
    console.json(
        {
            "ok": False,
            "error": f"a command is required after `{prog}`",
            "group": getattr(args, "group", ""),
            "commands": commands,
            "hint": f"run `{prog} <command> --help`",
        }
    )
    return EXIT_USAGE


def dispatch(argv: list[str] | None = None) -> int:
    argv = _expand_aliases(list(sys.argv[1:] if argv is None else argv))
    parser, registry = build_parser()
    args = parser.parse_args(argv)
    _fill_global_defaults(args)
    console = make_console(args)

    if not getattr(args, "group", None):
        parser.print_help()
        return EXIT_OK

    handler = getattr(args, "func", None)
    if console.json_mode and (handler is None or _is_help_handler(handler)):
        return _json_usage_error(console, args, handler)

    if handler is None:
        # `atf rules` with no verb: show that group's help.
        parser.parse_args([args.group, "--help"])
        return EXIT_OK

    if _is_help_handler(handler):
        # Nested group (`atf rules settings`): its own help, human mode only.
        handler.parser.print_help()  # type: ignore[attr-defined]
        return EXIT_OK

    repo = RepoContext(
        console,
        root=args.root or None,
        dry_run=bool(getattr(args, "dry_run", False)),
        assume_yes=bool(getattr(args, "yes", False)),
        timeout=float(getattr(args, "timeout", 120.0) or 120.0),
    )
    ctx = CliContext(out=console, repo=repo, args=args)

    try:
        result = handler(ctx, args)
        return EXIT_OK if result is None else int(result)
    except KeyboardInterrupt:
        console.warn("interrupted")
        return 130
    except AtfError as exc:
        if console.json_mode:
            console.json(
                {
                    "ok": False,
                    "error": exc.message,
                    "hint": exc.hint,
                    "error_type": type(exc).__name__,
                    "exit_code": exc.exit_code,
                }
            )
        else:
            console.fail(exc.message)
            console.hint(exc.hint)
        return exc.exit_code
    except BrokenPipeError:
        # `atf logs tail | head` is normal usage, not a failure.
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - last line of defence
        if os.environ.get("ATF_DEBUG"):
            traceback.print_exc()
        if console.json_mode:
            console.json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "exit_code": EXIT_FAILURE,
                }
            )
        else:
            console.fail(f"{type(exc).__name__}: {exc}")
            console.hint("re-run with ATF_DEBUG=1 for a traceback")
        return EXIT_FAILURE


def main(argv: list[str] | None = None) -> None:
    sys.exit(dispatch(argv))
