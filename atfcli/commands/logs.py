"""Log archive management, tailing, querying, stats, export, and purge."""

from __future__ import annotations

import json
import time
from typing import Any

from .. import validators
from ..console import human_duration
from ..context import SERVICES
from ..errors import UsageError
from . import _shared as sh

NAME = "logs"
HELP = "Query, tail, export, and manage system and per-task logs"


def _stats(ctx, args) -> None:
    data = ctx.api.get("/api/logs/stats")
    if args.json:
        ctx.out.json(data)
        return
    ctx.out.title("Log Archive Statistics")
    total = data.get("total", 0)
    oldest = data.get("oldest_ts")
    newest = data.get("newest_ts")
    now = time.time()
    ctx.out.kv(
        [
            ("Total Records", total),
            ("Oldest Entry", f"{human_duration(now - oldest)} ago" if oldest else "—"),
            ("Newest Entry", f"{human_duration(now - newest)} ago" if newest else "—"),
        ]
    )
    levels = data.get("by_level") or data.get("levels") or {}
    if isinstance(levels, dict) and levels:
        ctx.out.line()
        ctx.out.title("By Severity")
        ctx.out.kv([(k, v) for k, v in levels.items()])
    services = data.get("by_service") or data.get("services") or {}
    if isinstance(services, dict) and services:
        ctx.out.line()
        ctx.out.title("By Service")
        ctx.out.kv([(k, v) for k, v in services.items()])


def _query(ctx, args) -> None:
    params: dict[str, Any] = {
        "limit": args.limit or 50,
        "offset": args.offset or 0,
        "level": args.level or "",
        "service": args.service or "",
        "search": args.search or "",
        "rule_id": args.rule or "",
        "correlation_id": args.corr or "",
        "order": "asc" if args.asc else "desc",
    }
    if args.hours:
        params["hours"] = args.hours

    data = ctx.api.get("/api/logs", params=params)
    entries: list[dict[str, Any]] = sh.list_of(data, "logs")
    total = int(data.get("total", len(entries))) if isinstance(data, dict) else len(entries)

    if args.json:
        ctx.out.json({"total": total, "count": len(entries), "logs": entries})
        return

    rows = []
    for entry in entries:
        ts = entry.get("timestamp") or entry.get("ts") or 0
        t_str = time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts))) if ts else "—"
        svc = str(entry.get("service") or "—")
        lvl = str(entry.get("level") or "INFO").upper()
        msg = str(entry.get("message") or entry.get("msg") or "—")
        rule = str(entry.get("rule_id") or "")[:8]
        rows.append([t_str, lvl, svc, rule or "—", sh.truncate(msg, 60)])

    ctx.out.table(("TIME", "LEVEL", "SERVICE", "TASK", "MESSAGE"), rows, empty="no matching log entries")
    ctx.out.line()
    ctx.out.line(f"  showing {len(entries)} of {total} record(s)")


def _tail(ctx, args) -> int:
    """Follow logs in real-time or show the most recent lines."""
    limit = args.lines or 20
    seen_ids: set[str] = set()
    params: dict[str, Any] = {
        "limit": limit,
        "service": args.service or "",
        "level": args.level or "",
        "search": args.search or "",
        "rule_id": args.rule or "",
        "order": "desc",
    }

    try:
        while True:
            data = ctx.api.get("/api/logs", params=params)
            entries: list[dict[str, Any]] = sh.list_of(data, "logs")
            ordered = list(reversed(entries))
            for e in ordered:
                eid = str(e.get("id") or f"{e.get('timestamp')}:{e.get('message')}")
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                ts = e.get("timestamp") or e.get("ts") or 0
                t_str = time.strftime("%H:%M:%S", time.localtime(float(ts))) if ts else "—"
                lvl = str(e.get("level") or "INFO").upper()
                svc = str(e.get("service") or "core")
                msg = str(e.get("message") or e.get("msg") or "")
                rule = str(e.get("rule_id") or "")[:8]
                task_tag = f" [{rule}]" if rule else ""
                
                color = "grey"
                if lvl in ("WARN", "WARNING"):
                    color = "yellow"
                elif lvl in ("ERROR", "FATAL"):
                    color = "red"
                elif lvl == "INFO":
                    color = "cyan"

                line = f"{ctx.out.paint(t_str, 'grey')} {ctx.out.paint(lvl.ljust(5), color)} {ctx.out.paint(svc.ljust(7), 'bold')}{task_tag} {msg}"
                ctx.out.line(line)

            if not args.follow:
                break
            time.sleep(args.interval or 2.0)
    except KeyboardInterrupt:
        return 0
    return 0


def _export(ctx, args) -> None:
    fmt = args.format or "json"
    params = {
        "format": fmt,
        "service": args.service or "",
        "level": args.level or "",
        "search": args.search or "",
        "rule_id": args.rule or "",
    }
    if args.hours:
        params["hours"] = args.hours

    content = ctx.api.stream("/api/logs/export", params=params, accept=f"text/{fmt}" if fmt == "csv" else "application/json")
    if args.output:
        ctx.repo.path(args.output).write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        ctx.out.ok(f"Exported logs to {args.output} ({len(content)} bytes)")
    else:
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        sh.write_stdout(text)


def _purge(ctx, args) -> None:
    if not args.older_than_days and not args.rule:
        raise UsageError("Provide --older-than-days or --rule to specify what to purge")
    sh.confirm_or_fail(ctx, "Are you sure you want to purge log entries?")
    body: dict[str, Any] = {}
    if args.older_than_days:
        body["older_than_days"] = args.older_than_days
    if args.rule:
        body["rule_id"] = args.rule
    res = ctx.api.post("/api/logs/purge", payload=body)
    ctx.out.emit(res, f"Purge initiated: {res.get('message', 'done') if isinstance(res, dict) else res}")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("stats", help="show log statistics and volume breakdowns", parents=parents)
    p.set_defaults(func=_stats)

    p = sub.add_parser("query", aliases=["search"], help="query historical logs with filters", parents=parents)
    p.add_argument("-n", "--limit", type=int, default=50, help="max records to return")
    p.add_argument("--offset", type=int, default=0, help="starting record offset")
    p.add_argument("--level", type=validators.argparse_type("level", optional=True), default="",
                   help=f"filter by level ({validators.describe_choices(validators.LEVELS)})")
    p.add_argument("--service", type=validators.service_argparse_type(optional=True), default="",
                   help=f"filter by service ({validators.describe_choices(SERVICES)}, container names accepted)")
    p.add_argument("-s", "--search", default="", help="text search in message/detail")
    p.add_argument("-r", "--rule", default="", help="filter by rule/task ID")
    p.add_argument("--corr", default="", help="filter by correlation ID")
    p.add_argument("--hours", type=validators.argparse_type("int", minimum=1, maximum=8760),
                   help="only records in the last N hours")
    p.add_argument("--asc", action="store_true", help="order ascending (oldest first)")
    p.set_defaults(func=_query)

    p = sub.add_parser("tail", help="tail and follow logs in real time", parents=parents)
    p.add_argument("-f", "--follow", action="store_true", help="follow new lines continuously")
    p.add_argument("-n", "--lines", type=int, default=20, help="number of lines to show")
    p.add_argument("--service", type=validators.service_argparse_type(optional=True), default="",
                   help=f"filter by service ({validators.describe_choices(SERVICES)})")
    p.add_argument("--level", type=validators.argparse_type("level", optional=True), default="",
                   help=f"filter by level ({validators.describe_choices(validators.LEVELS)})")
    p.add_argument("-s", "--search", default="", help="search term")
    p.add_argument("-r", "--rule", default="", help="filter by rule ID")
    p.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    p.set_defaults(func=_tail)

    p = sub.add_parser("export", help="export logs to JSON or CSV", parents=parents)
    p.add_argument("--format", choices=["json", "csv"], default="json", help="export format")
    p.add_argument("-o", "--output", default="", help="output file path (defaults to stdout)")
    p.add_argument("--service", type=validators.service_argparse_type(optional=True), default="",
                   help="filter by service")
    p.add_argument("--level", type=validators.argparse_type("level", optional=True), default="",
                   help="filter by level")
    p.add_argument("-s", "--search", default="", help="filter by search query")
    p.add_argument("-r", "--rule", default="", help="filter by rule ID")
    p.add_argument("--hours", type=validators.argparse_type("int", minimum=1, maximum=8760),
                   help="only logs from the last N hours")
    p.set_defaults(func=_export)

    p = sub.add_parser("purge", help="delete old log records", parents=parents)
    p.add_argument("--older-than-days", type=int, help="delete records older than N days")
    p.add_argument("-r", "--rule", default="", help="delete records for a specific rule")
    p.set_defaults(func=_purge)
