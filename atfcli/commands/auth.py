"""Authentication and credential management.

Supports:
- Token minting (local HMAC key derivation from core DB/secret)
- Password login against /api/auth/login
- Current identity inspect (/api/auth/me)
- Token store caching (~/.atf/cli.json)
"""

from __future__ import annotations

import getpass

from ..errors import AtfError, UsageError
from . import _shared as sh

NAME = "auth"
HELP = "Manage credentials, tokens, and authenticated sessions"


def _whoami(ctx, args) -> None:
    client = ctx.api
    user = client.whoami()
    if args.json:
        ctx.out.json(user)
        return
    ctx.out.title("Authenticated Identity")
    ctx.out.kv(
        [
            ("User ID", user.get("user_id") or user.get("id") or "—"),
            ("Username", user.get("username") or "—"),
            ("Admin", "yes" if user.get("is_admin") or user.get("admin") else "no"),
            ("Role", user.get("role") or ("admin" if user.get("is_admin") else "user")),
            ("Source", user.get("source") or "API"),
        ]
    )


def _token(ctx, args) -> None:
    client = ctx.api
    cred = client.load_credential(allow_mint=not getattr(args, "no_mint", False))
    if args.save:
        client.store.set(cred)
        ctx.out.ok(f"Saved token for {cred.url} into {client.store.path}")
    if args.json:
        ctx.out.json(cred.safe())
        return
    if args.show:
        sh.write_stdout(cred.token)
        return
    ctx.out.kv(
        [
            ("URL", cred.url),
            ("Source", cred.source),
            ("User ID", cred.user_id),
            ("Username", cred.username),
            ("Admin", "yes" if cred.admin else "no"),
            ("Expires in", f"{cred.seconds_left}s"),
        ]
    )
    if not args.save:
        ctx.out.line()
        ctx.out.hint("pass --show to output raw token, or --save to persist to ~/.atf/cli.json")


def _login(ctx, args) -> None:
    username = args.username or input("Username: ").strip()
    if not username:
        raise UsageError("Username is required")
    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
    if not password:
        raise UsageError("Password is required")

    cred = ctx.api.login(username, password)
    ctx.api.store.set(cred)
    ctx.out.ok(f"Logged in successfully as {username} (admin={cred.admin})")
    ctx.out.hint(f"Token saved to {ctx.api.store.path}")


def _logout(ctx, args) -> None:
    url = ctx.api.resolve_url()
    ctx.api.store.clear(url if not args.all else None)
    ctx.out.ok(f"Cleared stored tokens for {'all hosts' if args.all else url}")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("whoami", help="show active authenticated identity", parents=parents)
    p.set_defaults(func=_whoami)

    p = sub.add_parser("token", help="show or derive admin bearer token", parents=parents)
    p.add_argument("--save", action="store_true", help="save credential into ~/.atf/cli.json")
    p.add_argument("--show", action="store_true", help="output raw token string on stdout")
    p.set_defaults(func=_token)

    p = sub.add_parser("login", help="log in with username and password", parents=parents)
    p.add_argument("-u", "--username", default="", help="account username")
    p.add_argument("-p", "--password", default="", help="account password")
    p.set_defaults(func=_login)

    p = sub.add_parser("logout", help="clear stored auth tokens", parents=parents)
    p.add_argument("--all", action="store_true", help="clear tokens for all hosts")
    p.set_defaults(func=_logout)
