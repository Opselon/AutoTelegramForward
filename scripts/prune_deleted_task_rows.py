#!/usr/bin/env python3
"""Remove rows left behind by tasks that no longer exist.

Deleting a task used to remove only the ``forward_rules`` row, so its
bookkeeping — ``message_map``, ``processed_messages``, ``delivery_jobs``,
``dead_letter_queue``, ``rule_stats``, ``error_log``, ``content_dedup`` — stayed
in the store. The delete path is now transactional, but stores written before
that fix still carry orphans, and a leftover mapping claims a source message was
already delivered to a target that no longer exists: a task re-created with the
same id (restore from backup, re-import) can silently skip that message.

This is the one-time repair for those stores. It only removes rows whose
``rule_id`` is absent from ``forward_rules``: no existing task's data is touched.

    python scripts/prune_deleted_task_rows.py --db /app/data/atf.db            # report
    python scripts/prune_deleted_task_rows.py --db /app/data/atf.db --apply    # repair

Exit codes: 0 = nothing found (or applied), 2 = orphans found in report mode.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

#: Same list the delete path uses (core/infrastructure/persistence/sqlite_repositories.py).
DEPENDENT_TABLES = (
    "message_map",
    "processed_messages",
    "delivery_jobs",
    "dead_letter_queue",
    "rule_stats",
    "error_log",
    "content_dedup",
)


def tables_with_rule_id(conn: sqlite3.Connection) -> list[str]:
    found = []
    for name in (r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        if "rule_id" in cols:
            found.append(name)
    return found


def orphans(conn: sqlite3.Connection, table: str) -> list[tuple]:
    rows = conn.execute(
        f"SELECT DISTINCT CAST(x.rule_id AS TEXT) FROM {table} x"
        " LEFT JOIN forward_rules r ON r.id = x.rule_id WHERE r.id IS NULL"
    ).fetchall()
    return [str(r[0]) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to atf.db")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: report only)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        tables = [t for t in tables_with_rule_id(conn) if t != "forward_rules"]
        total = 0
        for table in tables:
            ids = orphans(conn, table)
            if not ids:
                continue
            counts = conn.execute(
                f"SELECT COUNT(*) FROM {table} x LEFT JOIN forward_rules r ON r.id=x.rule_id"
                " WHERE r.id IS NULL"
            ).fetchone()[0]
            total += counts
            print(f"{table}: {counts} row(s) from {len(ids)} deleted task(s) "
                  f"[{', '.join(i[:8] for i in ids[:4])}]")
            if args.apply:
                for rule_id in ids:
                    conn.execute(f"DELETE FROM {table} WHERE rule_id=?", (rule_id,))
        if args.apply:
            conn.commit()
            print(f"applied: removed {total} orphan row(s)")
            return 0
        if total:
            print(f"found {total} orphan row(s); re-run with --apply to remove them")
            return 2
        print("no orphan rows: every dependent row belongs to a live task")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
