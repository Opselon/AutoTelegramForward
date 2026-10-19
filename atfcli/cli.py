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
from .console import Console
from .context import RepoContext
from .errors import AtfError, EXIT_FAILURE, EXIT_OK

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
    "sessions",
    "logs",
    "data",
    "admin",
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
_VALUE_FLAGS = frozenset({"--url", "--token", "--timeout", "--root"})


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
    """Everything a command needs, built once per invocation."""

    out: Console
    repo: RepoContext
    args: argparse.Namespace
    _api: ApiClient | None = None

    @property
    def api(self) -> ApiClient:
        """The API client, credentialed on first use."""
        if self._api is None:
            client = ApiClient(
                self.out,
                url=getattr(self.args, "url", "") or "",
                token=getattr(self.args, "token", "") or "",
                timeout=float(getattr(self.args, "timeout", 20.0) or 20.0),
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

    def want_json(self) -> bool:
        return bool(getattr(self.args, "json", False))


#: What a global flag means when nobody passed it. Kept beside `_global_flags`
#: so the two cannot drift.
_GLOBAL_DEFAULTS: dict[str, Any] = {
    "json": False,
    "url": "",
    "token": "",
    "timeout": 20.0,
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
    parser.add_argument("--url", default=default(os.environ.get("ATF_CLI_URL", "")), help="dashboard API base URL")
    parser.add_argument("--token", default=default(os.environ.get("ATF_TOKEN", "")), help="bearer token (prefer ATF_TOKEN)")
    parser.add_argument("--timeout", type=float, default=default(20.0), help="HTTP timeout in seconds")
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
    if handler is None:
        # `atf rules` with no verb: show that group's help.
        parser.parse_args([args.group, "--help"])
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
            console.json({"ok": False, "error": exc.message, "hint": exc.hint})
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
            console.json({"ok": False, "error": str(exc), "type": type(exc).__name__})
        else:
            console.fail(f"{type(exc).__name__}: {exc}")
            console.hint("re-run with ATF_DEBUG=1 for a traceback")
        return EXIT_FAILURE


def main(argv: list[str] | None = None) -> None:
    sys.exit(dispatch(argv))
