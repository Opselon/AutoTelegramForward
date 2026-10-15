"""Database management, migrations, inspection, and snapshots."""

from __future__ import annotations

import os
from pathlib import Path

from ..context import CONTAINERS
from ..errors import AtfError, UsageError
from . import _shared as sh

NAME = "db"
HELP = "Inspect, snapshot, and manage SQLite or PostgreSQL database state"


def _inspect(ctx, args) -> None:
    data = ctx.api.get("/api/diagnostics")
    if args.json:
        ctx.out.json(data)
        return
    ctx.out.title("Database Diagnostics")
    counts = data.get("counts") if isinstance(data, dict) else {}
    if isinstance(counts, dict):
        ctx.out.table(
            ("ENTITY", "COUNT"),
            [[k, str(v)] for k, v in counts.items()],
            empty="no entity counts available",
        )
    findings = data.get("findings") if isinstance(data, dict) else []
    if findings:
        ctx.out.line()
        ctx.out.warn(f"Diagnostic findings: {findings}")


def _backup(ctx, args) -> None:
    dest = Path(args.output or f"data/backup_db_{ctx.repo.version}.db")
    # Check if core container is running SQLite
    db_path = ctx.repo.container_env("core", "ATF_DB_PATH") or "/app/data/atf.db"
    cmd = ["docker", "exec", CONTAINERS["core"], "sqlite3", db_path, f".backup /tmp/backup.db"]
    res = ctx.repo.run(cmd, check=False)
    if res.ok:
        # Copy out of container
        dest.parent.mkdir(parents=True, exist_ok=True)
        ctx.repo.run(["docker", "cp", f"{CONTAINERS['core']}:/tmp/backup.db", str(dest)])
        ctx.repo.run(["docker", "exec", CONTAINERS["core"], "rm", "-f", "/tmp/backup.db"], check=False)
        ctx.out.ok(f"Database backed up to {dest}")
    else:
        # Fallback to API backup
        ctx.out.warn("Container sqlite3 backup unavailable; using API JSON export...")
        data = ctx.api.post("/api/backup/export")
        dest = dest.with_suffix(".json")
        dest.write_text(str(data), encoding="utf-8")
        ctx.out.ok(f"Database JSON exported to {dest}")


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("inspect", aliases=["status"], help="inspect database tables and health", parents=parents)
    p.set_defaults(func=_inspect)

    p = sub.add_parser("backup", aliases=["snapshot"], help="take a database snapshot", parents=parents)
    p.add_argument("-o", "--output", default="", help="destination file path")
    p.set_defaults(func=_backup)
