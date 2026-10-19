"""`atf logs` — the log archive: query, tail, export, purge, and the collection policy.

This module only parses flags and renders. Every rule it needs — which levels
exist, that purge counts in hours, that a query with no filter means "no filter"
— comes from the domain and application layers, so the same answers apply to the
wizard and to the tests.

Verbs
-----
stats, query/search, tail, export, purge, policy (show|set), health, diagnostics
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from .. import validators
from ..console import human_duration
from ..domain.logs import LogQuery, canonical_level
from ..domain.validators import LEVELS, argparse_type
from ..domain.errors import UsageError
from . import _shared as sh

NAME = "logs"
HELP = "Query, tail, export, and manage system and per-task logs"


# ------------------------------------------------------------------- rendering
def _filter_flags(parser, *, levels: bool = True) -> None:
    """The filter flags every read verb shares, so they cannot drift apart."""
    parser.add_argument("--level", default="", type=argparse_type("level", optional=True), help="single severity filter")
    if levels:
        parser.add_argument("--levels", default="", help="comma-separated severities (e.g. WARN,ERROR)")
    parser.add_argument("--service", default="", help="filter by service (core/api/web/logger)")
    parser.add_argument("-s", "--search", default="", help="text search in message/detail")
    parser.add_argument("-r", "--rule", default="", help="filter by task/rule id")
    parser.add_argument("--corr", default="", help="filter by correlation id")
    parser.add_argument("--category", default="", help="filter by log category")
    parser.add_argument("--hours", type=int, default=0, help="only records from the last N hours")
    parser.add_argument("--since", type=int, default=0, help="unix seconds: window start")
    parser.add_argument("--until", type=int, default=0, help="unix seconds: window end")


def _query_from(args) -> LogQuery:
    """Build the one value object every read path uses."""
    levels = tuple(
        canonical_level(part.strip())
        for part in str(getattr(args, "levels", "")).split(",")
        if part.strip()
    )
    return LogQuery(
        level=str(getattr(args, "level", "") or ""),
        levels=levels,
        service=str(getattr(args, "service", "") or ""),
        rule_id=str(getattr(args, "rule", "") or ""),
        search=str(getattr(args, "search", "") or ""),
        correlation_id=str(getattr(args, "corr", "") or ""),
        category=str(getattr(args, "category", "") or ""),
        hours=int(getattr(args, "hours", 0) or 0),
        since=int(getattr(args, "since", 0) or 0),
        until=int(getattr(args, "until", 0) or 0),
        page=int(getattr(args, "page", 0) or 0),
        limit=int(getattr(args, "limit", getattr(args, "lines", 50)) or 50),
        offset=int(getattr(args, "offset", 0) or 0),
        ascending=bool(getattr(args, "asc", False)),
    )


def _applied(ctx, query: LogQuery) -> None:
    """Say which filters were used — an unfiltered read must not look filtered."""
    applied = query.described_by()
    if applied and not ctx.want_json():
        ctx.out.line()
        ctx.out.line("  filters: " + ", ".join(f"{key}={value}" for key, value in applied.items()))


def _level_colour(level: str) -> str:
    if level in ("WARN", "WARNING"):
        return "yellow"
    if level in ("ERROR", "CRITICAL", "FATAL"):
        return "red"
    if level == "INFO":
        return "cyan"
    if level in ("DEBUG", "TRACE"):
        return "grey"
    return "grey"


def _stats(ctx, args) -> int:
    stats = ctx.uc.logs.stats(rule_id=getattr(args, "rule", "") or "", hours=int(getattr(args, "hours", 0) or 0))
    if ctx.want_json():
        ctx.out.json(stats.to_json())
        return 0
    ctx.out.title("Log Archive Statistics")
    now = time.time()
    ctx.out.kv(
        [
            ("Total Records", stats.total),
            ("Oldest Entry", f"{human_duration(now - stats.oldest_ts)} ago" if stats.oldest_ts else "—"),
            ("Newest Entry", f"{human_duration(now - stats.newest_ts)} ago" if stats.newest_ts else "—"),
        ]
    )
    if stats.by_level:
        ctx.out.line()
        ctx.out.title("By Severity")
        ctx.out.kv(list(stats.by_level.items()))
    if stats.by_service:
        ctx.out.line()
        ctx.out.title("By Service")
        ctx.out.kv(list(stats.by_service.items()))
    return 0


def _query(ctx, args) -> int:
    query = _query_from(args)
    page = ctx.uc.logs.query(query)
    if ctx.want_json():
        ctx.out.json(page.to_json())
        return 0
    rows = []
    for entry in page.entries:
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(entry.timestamp)) if entry.timestamp else "—"
        rows.append(
            [
                when,
                entry.level or "INFO",
                entry.service or "—",
                entry.rule_id[:8] or "—",
                sh.truncate(entry.message or "—", 60),
            ]
        )
    ctx.out.table(("TIME", "LEVEL", "SERVICE", "TASK", "MESSAGE"), rows, empty="no matching log entries")
    ctx.out.line()
    ctx.out.line(f"  showing {len(page.entries)} of {page.total} record(s)")
    _applied(ctx, query)
    return 0


def _tail(ctx, args) -> int:
    """Follow the archive. `--follow` polls; without it, one page is printed."""
    query = _query_from(args)
    seen: set[str] = set()
    try:
        while True:
            page = ctx.uc.logs.query(query)
            for entry in reversed(list(page.entries)):
                key = str(entry.raw.get("id") or f"{entry.timestamp}:{entry.message}")
                if key in seen:
                    continue
                seen.add(key)
                when = time.strftime("%H:%M:%S", time.localtime(entry.timestamp)) if entry.timestamp else "—"
                task_tag = f" [{entry.rule_id[:8]}]" if entry.rule_id else ""
                ctx.out.line(
                    f"{ctx.out.paint(when, 'grey')} "
                    f"{ctx.out.paint((entry.level or 'INFO').ljust(5), _level_colour(entry.level))} "
                    f"{ctx.out.paint((entry.service or 'core').ljust(7), 'bold')}{task_tag} {entry.message}"
                )
            if not getattr(args, "follow", False):
                break
            time.sleep(float(getattr(args, "interval", 2.0) or 2.0))
    except KeyboardInterrupt:
        return 0
    return 0


def _export(ctx, args) -> int:
    fmt = args.format or "json"
    query = _query_from(args)
    content = ctx.uc.logs.export(query, fmt=fmt)
    if args.output:
        ctx.files.write_bytes(args.output, content)
        ctx.out.ok(f"exported {len(content)} byte(s) to {args.output}")
        _applied(ctx, query)
        return 0
    if getattr(args, "json", False):
        # `--json` promises a JSON document on stdout: wrap the bytes rather than
        # emitting a CSV body that no parser asked for.
        ctx.out.json({"format": fmt, "bytes": len(content), "content": content.decode("utf-8", "replace")})
        return 0
    sh.write_stdout(content.decode("utf-8", "replace"))
    return 0


def _purge(ctx, args) -> int:
    older = int(getattr(args, "older_than_days", 0) or 0)
    if not older:
        raise UsageError(
            "purge needs --older-than-days",
            hint=(
                "the server would otherwise fall back to its own 24h default; "
                "say the horizon you mean"
            ),
        )
    query = _query_from(args)
    ctx.out.warn(
        f"about to delete log records older than {older} day(s)"
        + (f" matching {', '.join(f'{k}={v}' for k, v in query.described_by().items())}" if query.described_by() else "")
    )
    sh.confirm_or_fail(ctx, "purge these records?")
    outcome = ctx.uc.logs.purge(query, older_than_days=older)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _policy(ctx, args) -> int:
    view = ctx.uc.logs.policy()
    if ctx.want_json():
        ctx.out.json(view.to_json())
        return 0
    ctx.out.title("Log Collection Policy")
    if view.generated_at:
        ctx.out.kv([("generated at", view.generated_at)])
    for scope, entry in sorted(view.scopes.items()):
        ctx.out.line()
        ctx.out.title(f"{scope} scope")
        rows = [(str(key), _render(value)) for key, value in sorted(entry.items())]
        ctx.out.kv(rows)
    if view.notes:
        ctx.out.line()
        for note in view.notes:
            ctx.out.hint(note)
    return 0


def _render(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, dict):
        return ", ".join(f"{key}={val}" for key, val in value.items()) or "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value) if value not in (None, "") else "—"


def _policy_set(ctx, args) -> int:
    levels: Sequence[str] = ()
    if getattr(args, "levels", ""):
        levels = tuple(part.strip() for part in str(args.levels).split(",") if part.strip())
    outcome = ctx.uc.logs.set_policy(
        levels=levels or None,
        verbose_rate=args.verbose_rate if getattr(args, "verbose_rate", None) is not None else None,
        diagnostic_seconds=args.diagnostic_seconds if getattr(args, "diagnostic_seconds", None) else None,
        disable_diagnostics=True if getattr(args, "disable_diagnostics", False) else None,
    )
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _health(ctx, args) -> int:
    payload = ctx.uc.logs.health()
    if ctx.want_json():
        ctx.out.json(payload)
        return 0
    ctx.out.title("Logging Pipeline Health")
    for key in ("generated_at", "status"):
        if payload.get(key):
            ctx.out.kv([(key.replace("_", " "), _render(payload[key]))])
    services = payload.get("services")
    if isinstance(services, Sequence) and services and all(isinstance(item, dict) for item in services):
        rows = [
            [
                str(item.get("service") or item.get("name") or "—"),
                str(item.get("status") or item.get("state") or "—"),
                str(item.get("detail") or "—"),
            ]
            for item in services
        ]
        ctx.out.table(("SERVICE", "STATUS", "DETAIL"), rows, empty="no producer reported")
    if services:
        ctx.out.line()
    for key, value in sorted(payload.items()):
        if key in ("services", "generated_at", "status"):
            continue
        ctx.out.kv([(str(key), _render(value))])
    bad = [
        str(item.get("service") or "?")
        for item in (services if isinstance(services, Sequence) else [])
        if isinstance(item, dict) and str(item.get("status") or "").lower() not in ("healthy", "ok")
    ]
    if bad:
        ctx.out.line()
        ctx.out.fail(f"degraded: {', '.join(bad)}")
        return 1
    ctx.out.line()
    ctx.out.ok("every producer reports healthy")
    return 0


def _diagnostics(action: str):
    def handler(ctx_, args) -> int:
        outcome = ctx_.uc.logs.diagnostics(action, seconds=int(getattr(args, "seconds", 0) or 0))
        ctx_.out.emit(outcome.to_json(), outcome.detail)
        return 0

    return handler


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("stats", help="show log statistics and volume breakdowns", parents=parents)
    p.add_argument("-r", "--rule", default="", help="scope the statistics to one task")
    p.add_argument("--hours", type=int, default=0, help="only count records from the last N hours")
    p.set_defaults(func=_stats)

    p = sub.add_parser("query", aliases=["search"], help="query historical logs with filters", parents=parents)
    p.add_argument("-n", "--limit", type=int, default=50, help="max records to return")
    p.add_argument("--offset", type=int, default=0, help="starting record offset")
    p.add_argument("--page", type=int, default=0, help="1-based page number (alternative to --offset)")
    _filter_flags(p)
    p.add_argument("--asc", action="store_true", help="order ascending (oldest first)")
    p.set_defaults(func=_query)

    p = sub.add_parser("tail", help="tail and follow logs in real time", parents=parents)
    p.add_argument("-f", "--follow", action="store_true", help="follow new lines continuously")
    p.add_argument("-n", "--lines", type=int, default=20, help="number of lines to show")
    _filter_flags(p)
    p.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    p.set_defaults(func=_tail)

    p = sub.add_parser("export", help="export logs to JSON or CSV", parents=parents)
    p.add_argument("--format", choices=["json", "csv"], default="json", help="export format")
    p.add_argument("-o", "--output", default="", help="output file path (defaults to stdout)")
    _filter_flags(p)
    p.set_defaults(func=_export)

    p = sub.add_parser("purge", help="delete old log records", parents=parents)
    p.add_argument("--older-than-days", type=int, default=0, help="delete records older than N days (required)")
    _filter_flags(p, levels=False)
    p.set_defaults(func=_purge)

    p = sub.add_parser("policy", help="show what atf-core collects, and change it", parents=parents)
    p.set_defaults(func=_policy)
    policy_sub = sh.add_group(p)
    p_show = policy_sub.add_parser("show", help="show the enforced collection policy", parents=parents)
    p_show.set_defaults(func=_policy, cmd="show")
    p_set = policy_sub.add_parser("set", help="change the collection policy (admin only)", parents=parents)
    p_set.add_argument("--levels", default="", help=f"comma-separated severities to collect ({', '.join(LEVELS)})")
    p_set.add_argument("--verbose-rate", type=int, default=None, help="per-second ceiling on verbose records (0 = no ceiling)")
    p_set.add_argument("--diagnostic-seconds", type=int, default=0, help="open a bounded diagnostics window")
    p_set.add_argument("--disable-diagnostics", action="store_true", help="close/forbid the diagnostics window")
    p_set.set_defaults(func=_policy_set)

    p = sub.add_parser("health", help="report the logging pipeline's health", parents=parents)
    p.set_defaults(func=_health)

    p = sub.add_parser("diagnostics", help="open or close a bounded verbose-collection window", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    diag_sub = sh.add_group(p)
    p_start = diag_sub.add_parser("start", help="collect verbosely for a bounded window (admin only)", parents=parents)
    p_start.add_argument("--seconds", type=int, default=0, help="how long the window stays open (required)")
    p_start.set_defaults(func=_diagnostics("start"))
    p_stop = diag_sub.add_parser("stop", help="close the verbose window now (admin only)", parents=parents)
    p_stop.set_defaults(func=_diagnostics("stop"))
