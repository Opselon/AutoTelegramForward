"""Authentication and credential management.

Supports:
- Token minting (local HMAC key derivation from core DB/secret)
- Password login against /api/auth/login
- Current identity inspect (/api/auth/me)
- Token store caching (~/.atf/cli.json)
- Saved servers: `atf auth servers` / `atf auth use <url>`
"""

from __future__ import annotations

import getpass

from ..api import TokenStore
from ..console import human_duration
from ..domain import validators
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
        client.store.set_default_url(cred.url)
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
    # Logging in to a host is also a statement that this is the host you mean;
    # persisting it saves every later command from repeating --url.
    ctx.api.store.set_default_url(cred.url)
    ctx.out.ok(f"Logged in successfully as {username} (admin={cred.admin})")
    ctx.out.hint(f"Token saved to {ctx.api.store.path}")


def _logout(ctx, args) -> None:
    url = ctx.api.resolve_url()
    ctx.api.store.clear(url if not args.all else None)
    ctx.out.ok(f"Cleared stored tokens for {'all hosts' if args.all else url}")


def _servers(ctx, args) -> int:
    """List the servers this machine holds a token for — without using one.

    Deliberately offline: it reads the token store, never the API, so it still
    answers when the dashboard is down (which is exactly when an operator is
    trying to work out which server they are pointed at).
    """
    store = TokenStore()
    entries = store.entries()
    default = store.default_url()
    if args.json:
        ctx.out.json(
            {"store": str(store.path), "default_server": default, "servers": entries}
        )
        return 0
    if not entries:
        ctx.out.info(f"no saved servers in {store.path}")
        ctx.out.hint("run `atf auth login --username <user>`")
        return 0
    ctx.out.title("Saved servers")
    ctx.out.table(
        ("URL", "USER", "ADMIN", "EXPIRES", "DEFAULT"),
        [
            [
                row["url"],
                row.get("username") or "—",
                "yes" if row.get("admin") else "no",
                human_duration(row.get("expires_in_s") or 0) if row.get("expires_in_s") else "expired",
                "yes" if row.get("default") else "",
            ]
            for row in entries
        ],
    )
    return 0


def _use(ctx, args) -> int:
    """Make a validated server URL this machine's default (persisted)."""
    store = TokenStore()
    try:
        url = store.set_default_url(args.url)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    saved = store.get(url) is not None
    ctx.out.emit(
        {"ok": True, "default_server": url, "has_token": saved, "store": str(store.path)},
        f"default server is now {url}"
        + ("" if saved else "  (no token saved — run `atf auth login` or pass --token)"),
    )
    return 0


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

    p = sub.add_parser("servers", help="list the servers this machine has a token for", parents=parents)
    p.set_defaults(func=_servers)

    p = sub.add_parser("use", help="set the default server URL for this machine", parents=parents)
    p.add_argument("url", type=validators.argparse_type("url"), help="API base URL, e.g. https://atf.example.com")
    p.set_defaults(func=_use)
