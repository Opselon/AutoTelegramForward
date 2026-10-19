"""`atf data` — backups, delivery queue, dead letters, and operational statistics.

Two contract details this module gets right by going through the use cases:

* ``backup-import`` sends ``{"bundle": <the file's text>}``. Posting the parsed
  JSON object instead is refused by the API ("bundle is required"), which is what
  the old code did — it read the file, parsed it, and sent the wrong shape.
* a dead-letter retry counts as done only when the server says ``success: true``;
  the core reports ``not_found`` / ``already_sent`` / ``replay_failed`` in the
  same 200, so anything else is surfaced as a refusal.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..domain.errors import UsageError
from . import _shared as sh

NAME = "data"
HELP = "Manage backups, delivery queues, dead-letter items, and system statistics"


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items()) or "—"
    return str(value) if value not in (None, "") else "—"


def _stats(ctx, args) -> int:
    data = ctx.uc.ops.stats()
    if ctx.want_json():
        ctx.out.json(data)
        return 0
    ctx.out.title("System & Delivery Statistics")
    ctx.out.kv([(key.replace("_", " "), _render(value)) for key, value in sorted(data.items()) if not isinstance(value, (dict, list))])
    for group in ("queue", "forwarding", "system"):
        if isinstance(data.get(group), dict):
            ctx.out.line()
            ctx.out.title(f"{group.capitalize()} Details")
            ctx.out.kv([(key, _render(value)) for key, value in data[group].items()])
    return 0


def _backup_export(ctx, args) -> int:
    outcome = ctx.uc.backup.export(
        passphrase=str(getattr(args, "passphrase", "") or ""),
        output=str(getattr(args, "output", "") or ""),
    )
    for warning in outcome.warnings:
        ctx.out.warn(warning.message)
        ctx.out.hint(warning.hint)
    if ctx.want_json():
        document = outcome.to_json()
        if args.output:
            # A script asked for one JSON document: report where the bundle went
            # rather than replaying every session and key it contains.
            document = {**document, "bundle": "<written to file>"}
        counts = ctx.uc.backup.summarise(outcome.payload)
        if counts:
            document = {**document, "counts": counts}
        ctx.out.json(document)
        return 0
    if args.output:
        counts = ctx.uc.backup.summarise(outcome.payload)
        ctx.out.ok(outcome.detail)
        if counts:
            ctx.out.kv([(key, _render(value)) for key, value in sorted(counts.items())])
        return 0
    sh.write_stdout(str(outcome.payload.get("bundle") or ""))
    return 0


def _backup_inspect(ctx, args) -> int:
    outcome = ctx.uc.backup.inspect(args.file)
    if ctx.want_json():
        ctx.out.json(outcome.to_json())
        return 0
    ctx.out.title("Backup Bundle")
    ctx.out.kv([("file", outcome.detail)])
    counts = outcome.payload.get("counts") or {}
    if isinstance(counts, dict) and counts:
        ctx.out.line()
        ctx.out.kv([(key, _render(value)) for key, value in sorted(counts.items())])
    return 0


def _backup_import(ctx, args) -> int:
    if not args.file:
        raise UsageError("a bundle file is required", hint="pass -f <path>")
    # Read the bundle first: the operator sees what is about to be written (and
    # a wrong path is named) before being asked to confirm anything.
    inspection = ctx.uc.backup.inspect(args.file)
    counts = inspection.payload.get("counts") or {}
    if not ctx.want_json() and isinstance(counts, dict) and counts:
        ctx.out.kv([(key, _render(value)) for key, value in sorted(counts.items())])
    sh.confirm_or_fail(ctx, "restoring replaces the tenant's current state — continue?")
    passphrase = str(getattr(args, "passphrase", "") or "") or _passphrase_from_env()
    outcome = ctx.uc.backup.restore(
        args.file,
        passphrase=passphrase,
        overwrite=bool(getattr(args, "overwrite", False)),
        confirm=True,
    )
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _passphrase_from_env() -> str:
    import os

    return os.environ.get("ATF_BACKUP_PASSPHRASE", "")


def _queue(ctx, args) -> int:
    snapshot = ctx.uc.delivery.queue()
    if ctx.want_json():
        ctx.out.json(snapshot.to_json())
        return 0
    ctx.out.title("Queue Status")
    counters = {key: value for key, value in snapshot.raw.items() if not isinstance(value, (dict, list))}
    ctx.out.kv([(key.replace("_", " "), _render(value)) for key, value in sorted(counters.items())])
    nested = {key: value for key, value in snapshot.raw.items() if isinstance(value, dict)}
    for key, value in sorted(nested.items()):
        ctx.out.line()
        ctx.out.title(key.replace("_", " "))
        ctx.out.kv([(str(k), _render(v)) for k, v in value.items()])
    return 0


def _refused(body: Any) -> bool:
    """True unless the server explicitly accepted.

    Proto3 `omitempty` drops `success: false`, so a refusal arrives as
    `{"message": "not_found"}` with no result flag at all. Only an explicit
    `true` may be read as an accepted replay; everything else is a refusal.
    """
    return not isinstance(body, Mapping) or body.get("success") is not True


def _dlq_list(ctx, args) -> int:
    items = ctx.uc.delivery.dead_letters()
    if sh.wants_json(ctx, args):
        # The rows are passed through untouched: a script reading last_error
        # must get the server's field, not a renamed projection of it.
        ctx.out.json({"count": len(items), "dlq": [item.to_json() for item in items]})
        return 0
    rows = [
        [
            item.id[:8],
            (item.rule_id[:8] or "—"),
            sh.truncate(item.error or "—", 40),
            str(item.retry_count),
        ]
        for item in items
    ]
    ctx.out.table(("ID", "RULE", "ERROR", "RETRIES"), rows, empty="dead letter queue is empty")
    ctx.out.line()
    ctx.out.line(f"  {len(items)} failed message(s) in DLQ")
    return 0


def _dlq_retry(ctx, args) -> int:
    if not args.id:
        raise UsageError("a dead-letter id or unique prefix is required")
    outcome = ctx.uc.delivery.retry(args.id)
    body = dict(outcome.payload)
    if _refused(body):
        code = body.get("error_code") or body.get("message") or "refused"
        ctx.out.emit(body, f"retry refused ({code}); the message stays in the DLQ")
        return 1
    ctx.out.emit(body, f"Replaying message {args.id[:8]}")
    return 0


def _dlq_retry_all(ctx, args) -> int:
    items = ctx.uc.delivery.dead_letters()
    if not items:
        ctx.out.emit({"count": 0}, "dead letter queue is empty")
        return 0
    sh.confirm_or_fail(ctx, f"requeue {len(items)} dead letter(s)?")
    outcome = ctx.uc.delivery.retry_all(confirm=True)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 1 if outcome.failed else 0


def _dlq_purge(ctx, args) -> int:
    items = ctx.uc.delivery.dead_letters()
    sh.confirm_or_fail(ctx, f"discard {len(items)} dead letter(s) permanently?")
    outcome = ctx.uc.delivery.purge(confirm=True)
    body = dict(outcome.payload)
    if _refused(body):
        ctx.out.emit(body, f"purge failed ({body.get('error_code') or body.get('message') or 'refused'})")
        return 1
    removed = body.get("purged", body.get("removed", body.get("message", len(items))))
    ctx.out.emit(body, f"Purged dead letter queue: {removed}")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("stats", help="show high-level operational statistics", parents=parents)
    p.set_defaults(func=_stats)

    p = sub.add_parser("queue", help="inspect message queue states", parents=parents)
    p.set_defaults(func=_queue)

    p = sub.add_parser("backup-export", aliases=["export"], help="export the tenant's configuration and state", parents=parents)
    p.add_argument("-o", "--output", default="", help="destination file path (defaults to stdout)")
    p.add_argument("--passphrase", default="", help="encrypt the bundle with this passphrase")
    p.set_defaults(func=_backup_export)

    p = sub.add_parser("backup-import", aliases=["import"], help="restore state from a bundle file", parents=parents)
    p.add_argument("-f", "--file", default="", help="source bundle written by backup-export")
    p.add_argument("--passphrase", default="", help="passphrase the bundle was encrypted with (or ATF_BACKUP_PASSPHRASE)")
    p.add_argument("--overwrite", action="store_true", help="replace colliding tasks instead of failing")
    p.set_defaults(func=_backup_import)

    p = sub.add_parser("backup-inspect", aliases=["inspect"], help="describe a bundle before restoring it", parents=parents)
    p.add_argument("-f", "--file", default="", help="bundle written by backup-export")
    p.set_defaults(func=_backup_inspect)

    p = sub.add_parser("dlq", help="dead-letter queue actions", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    dlq_sub = sh.add_group(p)

    sp = dlq_sub.add_parser("list", aliases=["ls"], help="list dead letter items", parents=parents)
    sp.set_defaults(func=_dlq_list)

    sp = dlq_sub.add_parser("retry", help="retry one dead letter item", parents=parents)
    sp.add_argument("id", help="dead-letter id or a unique prefix")
    sp.set_defaults(func=_dlq_retry)

    sp = dlq_sub.add_parser("retry-all", help="requeue every dead letter item", parents=parents)
    sp.set_defaults(func=_dlq_retry_all)

    sp = dlq_sub.add_parser("purge", help="discard all items in the DLQ", parents=parents)
    sp.set_defaults(func=_dlq_purge)
