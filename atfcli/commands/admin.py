"""Administrator control plane: users, admins, extensions, global settings, broadcasts."""

from __future__ import annotations

from typing import Any

from ..context import is_secret_name
from ..errors import UsageError
from . import _shared as sh

NAME = "admin"
HELP = "Manage system users, administrators, extensions, and global settings"


def _overview(ctx, args) -> None:
    data = ctx.api.get("/api/admin/overview")
    if args.json:
        ctx.out.json(data)
        return
    ctx.out.title("System Administrator Overview")
    if isinstance(data, dict):
        ctx.out.kv([(k, v) for k, v in data.items() if not isinstance(v, (dict, list))])


def _users_list(ctx, args) -> None:
    data = ctx.api.get("/api/admin/users")
    items = sh.list_of(data, "users")
    if args.json:
        ctx.out.json({"count": len(items), "users": items})
        return
    rows = []
    for u in items:
        rows.append(
            [
                str(u.get("id") or u.get("user_id") or "—"),
                u.get("username") or "—",
                u.get("role") or ("admin" if u.get("is_admin") else "user"),
                "active" if u.get("is_active", True) else "suspended",
                str(u.get("rules_count", u.get("task_count", 0))),
            ]
        )
    ctx.out.table(("USER ID", "USERNAME", "ROLE", "STATUS", "TASKS"), rows, empty="no users found")
    ctx.out.line()
    ctx.out.line(f"  {len(items)} registered user(s)")


def _user_status(ctx, args) -> None:
    uid = args.user_id
    status = args.status
    res = ctx.api.post(f"/api/admin/users/{uid}/status", payload={"status": status})
    ctx.out.emit(res, f"Updated user {uid} status to {status}")


def _broadcast(ctx, args) -> None:
    message = args.message
    if not message:
        raise UsageError("Message is required for broadcast")
    sh.confirm_or_fail(ctx, f"Broadcast message to all users?")
    res = ctx.api.post("/api/admin/broadcast", payload={"message": message})
    ctx.out.emit(res, "Broadcast sent")


def _settings_get(ctx, args) -> None:
    data = ctx.api.get("/api/admin/settings")
    if args.json:
        ctx.out.json(data)
        return
    ctx.out.title("Global Administrator Settings")
    if isinstance(data, dict):
        # Values are masked by name: this surface can carry bot tokens and API
        # keys, and the console's own filter only catches values it has already
        # been told about.
        pairs = [
            (k, "[REDACTED]" if is_secret_name(k) else v) for k, v in data.items()
        ]
        ctx.out.kv(pairs)


def _settings_save(ctx, args) -> None:
    settings = sh.settings_from_args(args)
    if not settings:
        raise UsageError("Pass one or more KEY=VALUE pairs to save")
    res = ctx.api.post("/api/admin/settings", payload=settings)
    ctx.out.emit(res, f"Saved {len(settings)} global setting(s)")


def _admins_list(ctx, args) -> None:
    data = ctx.api.get("/api/admin/admins")
    items = sh.list_of(data, "admins")
    if args.json:
        ctx.out.json({"count": len(items), "admins": items})
        return
    rows = []
    for a in items:
        rows.append([str(a.get("id") or a.get("user_id")), a.get("username") or "—"])
    ctx.out.table(("USER ID", "USERNAME"), rows, empty="no administrators registered")


def _admin_add(ctx, args) -> None:
    uid = args.user_id
    res = ctx.api.post("/api/admin/admins", payload={"user_id": uid})
    ctx.out.emit(res, f"Added admin: {uid}")


def _admin_remove(ctx, args) -> None:
    uid = args.user_id
    sh.confirm_or_fail(ctx, f"Remove admin privileges for user {uid}?")
    res = ctx.api.delete(f"/api/admin/admins/{uid}")
    ctx.out.emit(res, f"Removed admin: {uid}")


def _extensions_list(ctx, args) -> None:
    data = ctx.api.get("/api/admin/extensions")
    items = sh.list_of(data, "extensions")
    if args.json:
        ctx.out.json({"count": len(items), "extensions": items})
        return
    rows = []
    for ext in items:
        rows.append(
            [
                ext.get("id") or ext.get("name"),
                "enabled" if ext.get("enabled", True) else "disabled",
                sh.truncate(ext.get("description", "—"), 45),
            ]
        )
    ctx.out.table(("EXTENSION", "STATUS", "DESCRIPTION"), rows, empty="no extensions registered")


def _extension_toggle(ctx, args) -> None:
    eid = args.id
    res = ctx.api.post(f"/api/admin/extensions/{eid}/toggle")
    ctx.out.emit(res, f"Toggled extension {eid}")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("overview", help="show administrative metrics and summary", parents=parents)
    p.set_defaults(func=_overview)

    p = sub.add_parser("broadcast", help="send broadcast announcement to all users", parents=parents)
    p.add_argument("message", help="message content")
    p.set_defaults(func=_broadcast)

    # Users
    p = sub.add_parser("users", help="manage system users", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    usub = sh.add_group(p)
    sp = usub.add_parser("list", aliases=["ls"], help="list users", parents=parents)
    sp.set_defaults(func=_users_list)
    sp = usub.add_parser("status", help="set user status (active/suspended)", parents=parents)
    sp.add_argument("user_id", help="target user ID")
    sp.add_argument("status", choices=["active", "suspended", "blocked"])
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
    sp = asub.add_parser("add", help="promote user to admin", parents=parents)
    sp.add_argument("user_id", help="user ID to grant admin")
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
