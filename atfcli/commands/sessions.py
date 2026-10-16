"""Telegram user sessions management."""

from __future__ import annotations

from typing import Any

from ..errors import NotFoundError, UsageError
from . import _shared as sh

NAME = "sessions"
HELP = "Manage connected Telegram client sessions"


def _list(ctx, args) -> None:
    data = ctx.api.get("/api/sessions")
    items = sh.list_of(data, "sessions")
    if args.json:
        ctx.out.json({"count": len(items), "sessions": items})
        return
    rows = []
    for s in items:
        rows.append(
            [
                str(s.get("session_id") or s.get("id") or "—")[:12],
                s.get("phone") or "—",
                s.get("user_id") or "—",
                s.get("first_name") or s.get("username") or "—",
                "active" if s.get("is_active", True) else "inactive",
            ]
        )
    ctx.out.table(("SESSION ID", "PHONE", "USER ID", "NAME", "STATUS"), rows, empty="no sessions found")
    ctx.out.line()
    ctx.out.line(f"  {len(items)} session(s)")


def _backup(ctx, args) -> None:
    session_id = args.session_id
    if not session_id:
        raise UsageError("session_id is required")
    data = ctx.api.post(f"/api/sessions/{session_id}/backup")
    if args.json:
        ctx.out.json(data)
        return
    ctx.out.ok(f"Backup generated for session {session_id[:12]}")
    if isinstance(data, dict) and "bundle" in data:
        ctx.out.kv([("Bundle", str(data["bundle"])[:40] + "...")])


def _terminate(ctx, args) -> None:
    session_id = args.session_id
    if not session_id:
        raise UsageError("session_id is required")
    sh.confirm_or_fail(ctx, f"Terminate session {session_id}?")
    res = ctx.api.delete(f"/api/sessions/{session_id}")
    ctx.out.emit(res, f"Session {session_id} terminated")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("list", aliases=["ls"], help="list linked Telegram sessions", parents=parents)
    p.set_defaults(func=_list)

    p = sub.add_parser("backup", help="export backup bundle for session", parents=parents)
    p.add_argument("session_id", help="target session ID")
    p.set_defaults(func=_backup)

    p = sub.add_parser("terminate", aliases=["rm"], help="terminate a session", parents=parents)
    p.add_argument("session_id", help="target session ID")
    p.set_defaults(func=_terminate)
