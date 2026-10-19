"""`atf audit` — the access and administration trail.

Every privileged action (login, status change, credential write, broadcast) is
recorded server-side and scoped to the caller's tenant. Nothing in the CLI could
read it, which left the one question an incident raises — "who did this, and
when?" — answerable only by querying the database.

The query is built as a typed request, so an unknown ``--outcome`` is refused
locally instead of being sent under a key the server ignores.
"""

from __future__ import annotations

import time

from ..application.use_cases.audit import summarise
from ..domain.validators import OUTCOMES
from . import _shared as sh

NAME = "audit"
HELP = "Query the audit trail: who did what, scoped to this tenant"


def _query(ctx, args) -> int:
    events = ctx.uc.audit.query(
        user_id=getattr(args, "user", "") or "",
        event_type=getattr(args, "event_type", "") or "",
        service=getattr(args, "service", "") or "",
        outcome=getattr(args, "outcome", "") or "",
        severity=getattr(args, "severity", "") or "",
        since=getattr(args, "since", "") or "",
        until=getattr(args, "until", "") or "",
        search=getattr(args, "search", "") or "",
        limit=int(getattr(args, "limit", 50) or 50),
        offset=int(getattr(args, "offset", 0) or 0),
    )
    if ctx.want_json():
        payload = dict(summarise(events))
        payload["events_detail"] = [event.to_json() for event in events]
        ctx.out.json(payload)
        return 0
    rows = [
        [
            time.strftime("%m-%d %H:%M:%S", time.localtime(event.timestamp)) if event.timestamp else "—",
            event.outcome or "—",
            event.event_type or "—",
            event.user_id or "—",
            event.service or "—",
            sh.truncate(event.message or event.detail or "—", 56),
        ]
        for event in events
    ]
    ctx.out.table(("TIME", "OUTCOME", "EVENT", "USER", "SERVICE", "DETAIL"), rows, empty="no audit events")
    counts = summarise(events)["by_outcome"]
    ctx.out.line()
    ctx.out.line(
        f"  {len(events)} event(s)"
        + ("  ·  " + ", ".join(f"{key.lower()}: {value}" for key, value in sorted(counts.items())) if counts else "")
    )
    return 0


def _outcomes(ctx, args) -> int:
    if ctx.want_json():
        ctx.out.json({"outcomes": list(OUTCOMES)})
        return 0
    ctx.out.line("  outcomes: " + ", ".join(item.lower() for item in OUTCOMES))
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(
        NAME,
        help=HELP,
        description=HELP,
        parents=parents,
        epilog="Non-admins only ever see their own tenant's events; the scope is applied server-side.",
    )
    sub = sh.add_group(parser)

    p = sub.add_parser("events", aliases=["list", "ls", "query"], help="query the trail", parents=parents)
    p.add_argument("-n", "--limit", type=int, default=50, help="max events")
    p.add_argument("--offset", type=int, default=0, help="skip this many events")
    p.add_argument("--user", default="", help="only this telegram user id (admin scope)")
    p.add_argument("--event-type", default="", help="only this event type")
    p.add_argument("--service", default="", help="only this service (api, core, ...)")
    p.add_argument("--outcome", default="", help="allow or deny")
    p.add_argument("--severity", default="", help="only this severity")
    p.add_argument("--since", default="", help="start time (RFC3339 or unix seconds)")
    p.add_argument("--until", default="", help="end time (RFC3339 or unix seconds)")
    p.add_argument("-s", "--search", default="", help="search the message text")
    p.set_defaults(func=_query)

    p = sub.add_parser("outcomes", help="the outcome values the server records", parents=parents)
    p.set_defaults(func=_outcomes)
