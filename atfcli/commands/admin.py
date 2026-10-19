"""`atf admin` — the administrator control plane.

Users, administrators, extensions, platform settings and broadcasts. The
vocabulary each verb accepts comes from the application layer, so `--help`, the
wizard and the request agree — and a status the core will refuse (`blocked`) is
refused here, before the round trip, with the two values it does accept.
"""

from __future__ import annotations

from typing import Any

from ..application.use_cases.admin import USER_STATUSES
from ..domain.secrets import is_secret_name
from ..errors import UsageError
from . import _shared as sh

NAME = "admin"
HELP = "Manage system users, administrators, extensions, and global settings"


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items()) or "—"
    return str(value) if value not in (None, "") else "—"


def _overview(ctx, args) -> int:
    data = ctx.uc.admin.overview()
    if ctx.want_json():
        ctx.out.json(data)
        return 0
    ctx.out.title("System Administrator Overview")
    for key, value in sorted(data.items()):
        if isinstance(value, (dict, list)):
            continue
        ctx.out.kv([(key.replace("_", " "), _render(value))])
    for key, value in sorted(data.items()):
        if not isinstance(value, (dict, list)) or not value:
            continue
        ctx.out.line()
        ctx.out.title(key.replace("_", " "))
        if isinstance(value, dict):
            ctx.out.kv([(str(k), _render(v)) for k, v in value.items()])
        else:
            ctx.out.line("  " + _render(value))
    return 0


def _users_list(ctx, args) -> int:
    users = ctx.uc.admin.users(limit=int(args.limit or 50), offset=int(args.offset or 0))
    if ctx.want_json():
        ctx.out.json({"count": len(users), "users": [user.to_json() for user in users]})
        return 0
    rows = [
        [user.id, user.username or "—", user.role or "user", "active" if user.active else "suspended", str(user.tasks)]
        for user in users
    ]
    ctx.out.table(("USER ID", "USERNAME", "ROLE", "STATUS", "TASKS"), rows, empty="no users found")
    ctx.out.line()
    ctx.out.line(f"  {len(users)} registered user(s)")
    return 0


def _user_status(ctx, args) -> int:
    outcome = ctx.uc.admin.set_user_status(args.user_id, args.status)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _broadcast(ctx, args) -> int:
    if not args.message:
        raise UsageError("message is required", hint="pass the text to announce")
    sh.confirm_or_fail(ctx, "broadcast this message to every user?")
    outcome = ctx.uc.admin.broadcast(args.message, confirm=True)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _settings_get(ctx, args) -> int:
    data = ctx.uc.admin.settings()
    if ctx.want_json():
        # The document a script parses keeps the keys, but a value whose name
        # says "token" is masked: `--json` must not be the way a secret leaks.
        ctx.out.json({key: ("[REDACTED]" if is_secret_name(key) else value) for key, value in data.items()})
        return 0
    ctx.out.title("Global Administrator Settings")
    ctx.out.kv([(key, "[REDACTED]" if is_secret_name(key) else _render(value)) for key, value in sorted(data.items())])
    return 0


def _settings_save(ctx, args) -> int:
    settings = sh.settings_from_args(args)
    if not settings:
        raise UsageError("pass one or more KEY=VALUE pairs to save", hint="e.g. `atf admin settings set maintenance_mode=false`")
    outcome = ctx.uc.admin.save_settings(settings)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _admins_list(ctx, args) -> int:
    admins = ctx.uc.admin.admins()
    if ctx.want_json():
        ctx.out.json({"count": len(admins), "admins": [admin.to_json() for admin in admins]})
        return 0
    rows = [[admin.id, admin.username or "—", admin.role or "admin"] for admin in admins]
    ctx.out.table(("USER ID", "USERNAME", "ROLE"), rows, empty="no administrators registered")
    return 0


def _admin_add(ctx, args) -> int:
    outcome = ctx.uc.admin.add_admin(args.user_id, username=getattr(args, "username", "") or "")
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _admin_remove(ctx, args) -> int:
    sh.confirm_or_fail(ctx, f"remove admin privileges for user {args.user_id}?")
    outcome = ctx.uc.admin.remove_admin(args.user_id, confirm=True)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _extensions_list(ctx, args) -> int:
    extensions = ctx.uc.admin.extensions()
    if ctx.want_json():
        ctx.out.json({"count": len(extensions), "extensions": extensions})
        return 0
    rows = [
        [
            str(ext.get("id") or ext.get("name") or "—"),
            str(ext.get("status") or ("enabled" if ext.get("enabled", True) else "disabled")),
            sh.truncate(str(ext.get("description") or "—"), 45),
        ]
        for ext in extensions
    ]
    ctx.out.table(("EXTENSION", "STATUS", "DESCRIPTION"), rows, empty="no extensions registered")
    return 0


def _extension_toggle(ctx, args) -> int:
    outcome = ctx.uc.admin.toggle_extension(args.id)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("overview", help="show administrative metrics and summary", parents=parents)
    p.set_defaults(func=_overview)

    p = sub.add_parser("broadcast", help="send a broadcast announcement to all users", parents=parents)
    p.add_argument("message", help="message content")
    p.set_defaults(func=_broadcast)

    # Users
    p = sub.add_parser("users", help="manage system users", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    usub = sh.add_group(p)
    sp = usub.add_parser("list", aliases=["ls"], help="list users", parents=parents)
    sp.add_argument("-n", "--limit", type=int, default=50, help="max users to return")
    sp.add_argument("--offset", type=int, default=0, help="skip this many users")
    sp.set_defaults(func=_users_list)
    sp = usub.add_parser(
        "status",
        help=f"set user status ({'/'.join(USER_STATUSES)})",
        parents=parents,
    )
    sp.add_argument("user_id", help="target user ID")
    sp.add_argument("status", help=f"one of: {', '.join(USER_STATUSES)}")
    sp.set_defaults(func=_user_status)

    # Settings
    p = sub.add_parser("settings", help="global admin settings", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    ssub = sh.add_group(p)
    sp = ssub.add_parser("get", help="view admin settings", parents=parents)
    sp.set_defaults(func=_settings_get)
    sp = ssub.add_parser("set", help="update admin settings", parents=parents)
    sp.add_argument("set", nargs="+", metavar="KEY=VALUE")
    sp.set_defaults(func=_settings_save)

    # Admins
    p = sub.add_parser("admins", help="manage administrator users", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    asub = sh.add_group(p)
    sp = asub.add_parser("list", aliases=["ls"], help="list admins", parents=parents)
    sp.set_defaults(func=_admins_list)
    sp = asub.add_parser("add", help="promote a user to admin", parents=parents)
    sp.add_argument("user_id", help="user ID to grant admin")
    sp.add_argument("--username", default="", help="username to record alongside the id")
    sp.set_defaults(func=_admin_add)
    sp = asub.add_parser("remove", aliases=["rm"], help="revoke admin privileges", parents=parents)
    sp.add_argument("user_id", help="user ID to revoke admin")
    sp.set_defaults(func=_admin_remove)

    # Extensions
    p = sub.add_parser("extensions", help="manage platform extensions", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    xsub = sh.add_group(p)
    sp = xsub.add_parser("list", aliases=["ls"], help="list extensions", parents=parents)
    sp.set_defaults(func=_extensions_list)
    sp = xsub.add_parser("toggle", help="toggle extension status", parents=parents)
    sp.add_argument("id", help="extension name or ID")
    sp.set_defaults(func=_extension_toggle)
