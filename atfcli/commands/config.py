"""Configuration and environment (.env) management."""

from __future__ import annotations

from pathlib import Path

from ..context import is_secret_name
from ..errors import UsageError
from . import _shared as sh

NAME = "config"
HELP = "Manage environment variables and configuration files (.env)"


def _list(ctx, args) -> None:
    values = ctx.repo.env_file_values
    pairs = []
    for k, v in sorted(values.items()):
        val_str = "[REDACTED]" if is_secret_name(k) else v
        pairs.append((k, val_str))
    if args.json:
        ctx.out.json({k: ("[REDACTED]" if is_secret_name(k) else v) for k, v in values.items()})
        return
    ctx.out.title("Environment Configuration (.env)")
    ctx.out.kv(pairs)


def _get(ctx, args) -> None:
    key = args.key
    val = ctx.repo.env(key)
    sensitive = is_secret_name(key)
    if sensitive and args.reveal:
        # An explicit, deliberate escape hatch: the console is the redaction
        # barrier, so a deliberate reveal has to bypass it rather than pretend
        # to be possible through it.
        ctx.out.warn(f"{key} is a credential — revealing on stdout")
        sh.write_stdout(val)
        return
    if sensitive:
        ctx.out.emit({"key": key, "value": "[REDACTED]"}, f"{key}=[REDACTED]  (pass --reveal to print it)")
        return
    ctx.out.emit({"key": key, "value": val}, f"{key}={val}")


def _set(ctx, args) -> None:
    settings = sh.settings_from_args(args)
    if not settings:
        raise UsageError("Provide at least one KEY=VALUE pair to update")
    env_file = ctx.repo.path(".env")
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip()
            if k in settings:
                new_lines.append(f"{k}={settings[k]}")
                updated_keys.add(k)
                continue
        new_lines.append(line)

    for k, v in settings.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    ctx.out.ok(f"Updated {len(settings)} key(s) in .env")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("list", aliases=["ls"], help="list configuration variables", parents=parents)
    p.set_defaults(func=_list)

    p = sub.add_parser("get", help="get single configuration variable", parents=parents)
    p.add_argument("key", help="configuration key name")
    p.add_argument("--reveal", action="store_true", help="reveal secret value if authorized")
    p.set_defaults(func=_get)

    p = sub.add_parser("set", help="set configuration variables", parents=parents)
    p.add_argument("set", nargs="+", metavar="KEY=VALUE", help="key=value pairs to set")
    p.set_defaults(func=_set)
