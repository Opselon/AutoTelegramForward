"""`atf filters` — message filters applied before forwarding.

Filters decide what never leaves the source channel; getting one wrong either
spams a target or silently drops a wanted post. The collection verbs are the
shared ones, and ``atf filters explain`` turns a saved filter into readable
plain text so a keyword list can be reviewed without reading JSON.
"""

from __future__ import annotations

from ..domain.resources import FILTERS
from . import _resource
from . import _shared as sh

NAME = "filters"
HELP = "Message filters: list, create, and see exactly what a filter drops"


def _list_of(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    return str(value) if value not in (None, "") else "—"


def _explain(ctx, args) -> int:
    """Plain-language reading of one filter: what passes, what is dropped."""
    row = ctx.uc.filters.find(args.ref)
    raw = row.raw
    if ctx.want_json():
        ctx.out.json(raw)
        return 0

    def truthy(name: str) -> bool:
        return bool(raw.get(name))

    ctx.out.title(f"filter {row.id[:8]} · {row.label or 'unnamed'}")
    lines: list[tuple[str, str]] = []

    whitelist = raw.get("whitelistKeywords") or []
    blacklist = raw.get("blacklistKeywords") or []
    patterns = raw.get("regexPatterns") or []
    allowed = raw.get("allowedMediaTypes") or []
    blocked = raw.get("blockedMediaTypes") or []

    if whitelist:
        lines.append(("only messages containing", _list_of(whitelist)))
    if blacklist:
        lines.append(("drops messages containing", _list_of(blacklist)))
    if patterns:
        lines.append(("drops messages matching", _list_of(patterns)))
    if allowed:
        lines.append(("allows media types", _list_of(allowed)))
    if blocked:
        lines.append(("drops media types", _list_of(blocked)))
    if truthy("dropServiceMessages"):
        lines.append(("service messages", "dropped"))
    minimum = raw.get("minMessageLength")
    maximum = raw.get("maxMessageLength")
    if minimum:
        lines.append(("shorter than", f"{minimum} characters — dropped"))
    if maximum:
        lines.append(("longer than", f"{maximum} characters — dropped"))

    if lines:
        ctx.out.kv(lines)
    else:
        ctx.out.line("  no rules set — this filter matches everything")

    # Anything the operator set that the renderer above does not know about is
    # still shown: a field nobody explained must not look like a field nobody set.
    covered = {
        "id",
        "filter_id",
        "name",
        "owner_user_id",
        "created_at",
        "updated_at",
        "version",
        "whitelistKeywords",
        "blacklistKeywords",
        "regexPatterns",
        "allowedMediaTypes",
        "blockedMediaTypes",
        "dropServiceMessages",
        "minMessageLength",
        "maxMessageLength",
    }
    unaccounted = {key: value for key, value in raw.items() if key not in covered and value not in (None, "", [], {})}
    if unaccounted:
        ctx.out.line()
        ctx.out.kv([(key, sh.truncate(str(value), 80)) for key, value in sorted(unaccounted.items())])
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(
        NAME,
        help=HELP,
        description=HELP,
        parents=parents,
        epilog="List fields take comma-separated values: --blacklist-keywords 'spam,ads'.",
    )
    sub = sh.add_group(parser)
    _resource.register(
        sub,
        parents,
        FILTERS,
        aliases={"list": ["ls"], "create": ["add"], "update": ["edit"], "delete": ["rm"]},
    )

    p = sub.add_parser("explain", aliases=["inspect"], help="plain-language reading of one filter", parents=parents)
    p.add_argument("ref", help="filter id, unique prefix or name")
    p.set_defaults(func=_explain)
