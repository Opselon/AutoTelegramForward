#!/usr/bin/env python3
"""Assign owners to pre-ownership rows — or hand them to a human.

A live database upgraded from a build that predates `owner_user_id` holds rows
whose owner is zero. Those rows are invisible to every tenant (so the feature
looks broken) and still addressable by id (so they are a leak waiting for a
request). This script is the operator's handle on that state:

    # what would happen, in one JSON object (default: changes nothing)
    python3 scripts/ownership_backfill.py --db /app/data/atf.db --json

    # do it: assign the provable rows, quarantine the rest
    python3 scripts/ownership_backfill.py --db /app/data/atf.db --apply

    # what is waiting on a human
    python3 scripts/ownership_backfill.py --db /app/data/atf.db --list-quarantine

    # and the human's answer, recorded
    python3 scripts/ownership_backfill.py --db /app/data/atf.db \
        --resolve forward_rules:8f1c-uuid:5094837833

Nothing here reads or prints a credential: the rows it moves carry ids, and the
quarantine table stores a reason, not data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.application import ownership_backfill as ob  # noqa: E402
from core.infrastructure.persistence.database import SqliteDatabase  # noqa: E402


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        # exactly one JSON object on stdout
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, sort_keys=True)}")
        else:
            print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assign owners to pre-ownership rows, or quarantine them for a human.")
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    parser.add_argument("--apply", action="store_true",
                        help="write the plan (default: report only)")
    parser.add_argument("--list-quarantine", action="store_true",
                        help="list rows waiting for an operator decision")
    parser.add_argument("--resolve", metavar="TABLE:ROW_ID:OWNER",
                        help="assign a quarantined row to an owner explicitly")
    parser.add_argument("--json", action="store_true", help="one JSON object on stdout")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: no database at {args.db}", file=sys.stderr)
        return 2

    db = SqliteDatabase(args.db)

    if args.list_quarantine:
        rows = ob.quarantined(db)
        _emit({"waiting": len(rows), "quarantine": rows}, args.json)
        return 0

    if args.resolve:
        parts = args.resolve.split(":")
        if len(parts) != 3:
            print("error: --resolve wants TABLE:ROW_ID:OWNER", file=sys.stderr)
            return 2
        table, row_id, owner = parts[0], parts[1], parts[2]
        try:
            assigned = ob.resolve(db, table, row_id, int(owner))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _emit({"table": table, "row_id": row_id, "assigned": assigned}, args.json)
        return 0 if assigned else 1

    report = ob.plan(db)
    if args.apply:
        ob.apply(db, report)
    _emit({"applied": bool(args.apply), **report.to_dict()}, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
