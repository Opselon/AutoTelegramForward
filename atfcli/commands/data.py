"""Data management, backup & restore, queue & DLQ monitoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import UsageError
from . import _shared as sh

NAME = "data"
HELP = "Manage backups, delivery queues, dead-letter items, and system statistics"


def _stats(ctx, args) -> None:
    data = ctx.api.get("/api/stats")
    if args.json:
        ctx.out.json(data)
        return
    ctx.out.title("System & Delivery Statistics")
    if isinstance(data, dict):
        ctx.out.kv([(k, v) for k, v in data.items() if not isinstance(v, (dict, list))])
        for group in ("queue", "forwarding", "system"):
            if group in data and isinstance(data[group], dict):
                ctx.out.line()
                ctx.out.title(f"{group.capitalize()} Details")
                ctx.out.kv([(k, v) for k, v in data[group].items()])


def _backup_export(ctx, args) -> None:
    data = ctx.api.post("/api/backup/export")
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        ctx.out.ok(f"Exported system backup to {path}")
    else:
        if args.json:
            ctx.out.json(data)
        else:
            sh.write_stdout(json.dumps(data, indent=2, ensure_ascii=False))


def _backup_import(ctx, args) -> None:
    path = Path(args.file)
    if not path.exists():
        raise UsageError(f"Backup file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    sh.confirm_or_fail(ctx, f"Importing backup will overwrite/merge records. Continue?")
    res = ctx.api.post("/api/backup/import", payload=payload)
    ctx.out.emit(res, f"Backup import completed: {res.get('message', 'ok') if isinstance(res, dict) else res}")


def _dlq_list(ctx, args) -> None:
    data = ctx.api.get("/api/dlq")
    items = sh.list_of(data, "dlq")
    if args.json:
        ctx.out.json({"count": len(items), "dlq": items})
        return
    rows = []
    for item in items:
        rows.append(
            [
                str(item.get("id") or "—")[:8],
                str(item.get("rule_id") or "—")[:8],
                sh.truncate(str(item.get("error") or "—"), 40),
                str(item.get("retry_count") or 0),
            ]
        )
    ctx.out.table(("ID", "RULE", "ERROR", "RETRIES"), rows, empty="dead letter queue is empty")
    ctx.out.line()
    ctx.out.line(f"  {len(items)} failed message(s) in DLQ")


def _dlq_retry(ctx, args) -> None:
    mid = args.id
    res = ctx.api.post(f"/api/dlq/{mid}/retry")
    ctx.out.emit(res, f"Replaying message {mid[:8]} from DLQ")


def _dlq_purge(ctx, args) -> None:
    sh.confirm_or_fail(ctx, "Purge all messages in dead letter queue?")
    res = ctx.api.delete("/api/dlq")
    ctx.out.emit(res, "Purged dead letter queue")


def _queue(ctx, args) -> None:
    data = ctx.api.get("/api/queue")
    if args.json:
        ctx.out.json(data)
        return
    ctx.out.title("Queue Status")
    if isinstance(data, dict):
        ctx.out.kv([(k, v) for k, v in data.items() if not isinstance(v, (dict, list))])


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("stats", help="show high-level operational statistics", parents=parents)
    p.set_defaults(func=_stats)

    p = sub.add_parser("queue", help="inspect message queue states", parents=parents)
    p.set_defaults(func=_queue)

    p = sub.add_parser("backup-export", help="export entire system configuration and state", parents=parents)
    p.add_argument("-o", "--output", default="", help="destination file path")
    p.set_defaults(func=_backup_export)

    p = sub.add_parser("backup-import", help="restore system state from JSON file", parents=parents)
    p.add_argument("-f", "--file", required=True, help="source backup file")
    p.set_defaults(func=_backup_import)

    p = sub.add_parser("dlq", help="dead-letter queue actions", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    dlq_sub = sh.add_group(p)

    sp = dlq_sub.add_parser("list", aliases=["ls"], help="list dead letter items", parents=parents)
    sp.set_defaults(func=_dlq_list)

    sp = dlq_sub.add_parser("retry", help="retry dead letter item", parents=parents)
    sp.add_argument("id", help="DLQ message ID")
    sp.set_defaults(func=_dlq_retry)

    sp = dlq_sub.add_parser("purge", help="purge all items from DLQ", parents=parents)
    sp.set_defaults(func=_dlq_purge)
