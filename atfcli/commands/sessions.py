"""`atf sessions` — the Telegram accounts the stack is logged into.

Verbs: list, chats (new), backup, terminate. Every verb resolves its argument
through the same use case, so an id prefix or a phone suffix works everywhere
instead of only in `list`.
"""

from __future__ import annotations

from ..errors import UsageError
from . import _shared as sh

NAME = "sessions"
HELP = "Manage connected Telegram client sessions"


def _list(ctx, args) -> int:
    sessions = ctx.uc.sessions.list()
    if ctx.want_json():
        ctx.out.json({"count": len(sessions), "sessions": [s.to_json() for s in sessions]})
        return 0
    rows = [
        [
            session.id[:12],
            session.phone or "—",
            session.user_id or "—",
            session.name or "—",
            "active" if session.active else "inactive",
        ]
        for session in sessions
    ]
    ctx.out.table(("SESSION ID", "PHONE", "USER ID", "NAME", "STATUS"), rows, empty="no sessions found")
    ctx.out.line()
    ctx.out.line(f"  {len(sessions)} session(s)")
    return 0


def _chats(ctx, args) -> int:
    chats = ctx.uc.sessions.chats(args.session_id)
    if ctx.want_json():
        ctx.out.json({"count": len(chats), "chats": [chat.to_json() for chat in chats]})
        return 0
    rows = [[chat.id, sh.truncate(chat.title or "—", 40), chat.kind or "—"] for chat in chats]
    ctx.out.table(("CHAT ID", "TITLE", "TYPE"), rows, empty="this session reports no chats")
    ctx.out.line()
    ctx.out.line(f"  {len(chats)} chat(s)")
    return 0


def _backup(ctx, args) -> int:
    if not args.session_id:
        raise UsageError("session_id is required")
    outcome = ctx.uc.sessions.backup(args.session_id)
    if ctx.want_json():
        ctx.out.json(outcome.to_json())
        return 0
    ctx.out.ok(outcome.detail)
    return 0


def _terminate(ctx, args) -> int:
    if not args.session_id:
        raise UsageError("session_id is required")
    outcome = ctx.uc.sessions.terminate(args.session_id, confirm=bool(getattr(args, "yes", False)))
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("list", aliases=["ls"], help="list linked Telegram sessions", parents=parents)
    p.set_defaults(func=_list)

    p = sub.add_parser("chats", help="list the chats a session can see", parents=parents)
    p.add_argument("session_id", help="session id, unique prefix, or phone suffix")
    p.set_defaults(func=_chats)

    p = sub.add_parser("backup", help="export a backup bundle for a session", parents=parents)
    p.add_argument("session_id", help="session id, unique prefix, or phone suffix")
    p.set_defaults(func=_backup)

    p = sub.add_parser("terminate", aliases=["rm"], help="terminate a session (logs the account out)", parents=parents)
    p.add_argument("session_id", help="session id, unique prefix, or phone suffix")
    p.set_defaults(func=_terminate)
