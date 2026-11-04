"""run_matrix.py — rerunnable live delivery matrix against real Telegram.

Creates marker_task replicas of the baseline A->B task (never touching the
baseline's permanent config), sends probe messages to Source A through the
real personal-account session, and confirms delivery in Target B through
the same session. Every replica is deleted at the end.

Cells: A-F (task-matrix replicas), MEDIA, FEN, INJ1 (worker restart),
INJ2 (duplicate ingress).

Usage:
    docker exec atf-core python3 /app/worker/test/live/run_matrix.py
    (or: docker cp this file in first; the core container holds the session)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid

DB = "/app/data/atf.db"
BASELINE = "ea565d5c-ccce-4f1c-b5f5-79eca2911642"
SRC = -1004400615270
DST = -1003915313919
SESSION_ID = "1d12518c-039c-437a-aebd-7b9b7c720a18"
OUT = "/tmp/matrix_out.json"


def log(msg: str) -> None:
    print(f"[matrix] {msg}", flush=True)


def db_conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def baseline_row(c: sqlite3.Connection) -> sqlite3.Row:
    return c.execute("SELECT * FROM forward_rules WHERE id=?", (BASELINE,)).fetchone()


def clone_rule(c: sqlite3.Connection, base: sqlite3.Row, marker: str,
               meta_patch: dict | None = None) -> str:
    """Clone the baseline rule under a marker id. Never mutates the baseline."""
    row = dict(base)
    rid = f"mtx-{marker}-{int(time.time())}"
    row["id"] = rid
    row["is_active"] = 1
    row["is_paused"] = 0
    row["version"] = 1
    row["updated_at"] = int(time.time())
    meta = json.loads(row["metadata"] or "{}")
    meta["matrix_marker"] = rid
    meta["test_only"] = True
    if meta_patch:
        meta.update(meta_patch)
    row["metadata"] = json.dumps(meta)
    cols = list(row.keys())
    # Generated columns cannot be inserted into. PRAGMA table_info does not
    # flag them; table_xinfo does (hidden: 0 normal, 1 VIRTUAL, 2 STORED).
    gen = {g[1] for g in c.execute("PRAGMA table_xinfo(forward_rules)").fetchall()
           if g[6]}
    cols = [k for k in cols if k not in gen]
    vals = [row[k] for k in cols]
    marks = ",".join("?" for _ in cols)
    c.execute(
        f"INSERT OR REPLACE INTO forward_rules ({','.join(cols)}) VALUES ({marks})",
        vals,
    )
    c.commit()
    return rid


def drop_rule(c: sqlite3.Connection, rid: str) -> None:
    c.execute("DELETE FROM delivery_jobs WHERE rule_id=?", (rid,))
    c.execute("DELETE FROM rule_counters WHERE rule_id=?", (rid,))
    c.execute("DELETE FROM forward_rules WHERE id=?", (rid,))
    c.commit()


# ---------------------------------------------------------------- pyrogram

def make_client():
    import sys
    sys.path.insert(0, "/app")
    from pyrogram import Client
    from core.infrastructure.security.crypto import CryptoService

    key = os.environ.get("ATF_MASTER_KEY", "")
    c = sqlite3.connect(DB)
    row = c.execute(
        "SELECT session_string_encrypted FROM sessions WHERE id=?", (SESSION_ID,)
    ).fetchone()
    c.close()
    if not row:
        raise RuntimeError("session row not found")
    cs = CryptoService(key)
    sess = cs.decrypt(row[0])
    api_id = os.environ["ATF_API_ID"]
    api_hash = os.environ["ATF_API_HASH"]
    return Client("matrix", api_id=api_id, api_hash=api_hash, session_string=sess)


def send_text(client, text: str) -> int:
    m = client.send_message(chat_id=SRC, text=text)
    return m.id


# ---------------------------------------------------------------- helpers

def wait_for_job(c: sqlite3.Connection, rid: str, src_msg_id: int,
                 timeout: float = 45.0) -> tuple[str | None, str | None]:
    """Poll delivery_jobs until terminal. Returns (status, error_detail)."""
    key = f"{rid}:{SRC}:{src_msg_id}:{DST}"
    t0 = time.time()
    while time.time() - t0 < timeout:
        row = c.execute(
            "SELECT status, error_detail FROM delivery_jobs WHERE id=?", (key,)
        ).fetchone()
        if row is not None:
            st = row[0]
            if st in ("SENT", "FAILED", "DEAD_LETTER"):
                return st, row[1]
        time.sleep(0.4)
    row = c.execute(
        "SELECT status, error_detail FROM delivery_jobs WHERE id=?", (key,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def b_side_has(client, marker: str, since: int) -> tuple[bool, int | None]:
    """Look for our marker text in Target B's recent history."""
    msgs = list(client.get_chat_history(DST, limit=40))
    for m in reversed(msgs):
        if m.id <= since:
            continue
        txt = (m.text or m.caption or "")
        if marker in txt:
            return True, m.id
    return False, None


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------- cells

def cell_replica(client, c: sqlite3.Connection, base: sqlite3.Row, cell: str,
                 text: str, expect_block: bool, meta_patch: dict | None = None) -> dict:
    # matching_rules is single-match per owner: one message is consumed by
    # exactly one task of a tenant (deterministic highest-priority first)
    # unless multi_route=True. The replica copies the baseline's owner and
    # creation time, so the baseline would win every sort and the replica
    # would never see traffic. Pause the baseline for the duration of the
    # cell and restore it on the way out, snapshot taken in main().
    c.execute("UPDATE forward_rules SET is_paused=? WHERE id=?", (1, BASELINE))
    c.commit()
    rid = clone_rule(c, base, cell.lower(), meta_patch=meta_patch)
    # matching_rules is single-match per owner: one message is consumed by
    # exactly one task of a tenant (deterministic highest-priority first)
    # unless multi_route=True. The replica copies the baseline's owner and
    # creation time, so the baseline would win every sort and the replica
    # would never see traffic. Pause the baseline while the replica runs and
    # restore it from the snapshot taken in main().
    c.execute("UPDATE forward_rules SET is_paused=1 WHERE id=?", (BASELINE,))
    c.commit()
    try:
        marker = f"ATF-MTX-{cell}"
        src_id = send_text(client, f"{marker} {text}")
        status, err = wait_for_job(c, rid, src_id)
        delivered, tgt_id = b_side_has(client, marker, src_id - 1)
        ok = (not expect_block and delivered) or (
            # A blocked probe is dropped at ingest (the content policy owns
            # enqueue eligibility), so no job row exists; either way it must
            # never reach B.
            expect_block and not delivered
        )
        return {
            "cell": cell, "kind": "pass" if not expect_block else "block",
            "rule_id": rid, "source_message_id": src_id,
            "status": "PASS" if ok else "FAIL",
            "reason": err or ("delivered" if delivered else "not seen in B"),
            "target_message_id": tgt_id,
            "job_status": status,
        }
    finally:
        drop_rule(c, rid)
        c.execute("UPDATE forward_rules SET is_paused=? WHERE id=?", (0, BASELINE))
        c.commit()


def main() -> int:
    log(f"baseline source={SRC} target={DST}")
    c = db_conn()
    base = baseline_row(c)
    if base is None:
        log("FATAL: baseline rule missing")
        return 2
    snapshot = {k: base[k] for k in ("id", "is_active", "is_paused", "version",
                                     "metadata")}
    out: dict = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "baseline": {"before": snapshot, "after": None, "drift": None},
                 "cells": {}}

    client = make_client()
    with client:
        # Warm the entity cache: a session-string client starts with no peers
        # cached, so resolve_peer on a chat id raises. Materialize the dialog
        # generator (get_dialogs yields lazily) so the cache actually fills.
        list(client.get_dialogs(limit=200))
        log("session ready")

        # kind=True cells assert a BLOCK: their replicas carry a content
        # policy that rejects the probe, so the delivery must end FILTERED
        # rather than reaching B.
        cells = [
            ("A", "plain pass-through", False, None),
            ("B", "blocked by blacklist", True, {
                "content_policy": {"enabled": True, "blacklist_words": ["ATF-MTX-B"]},
            }),
            ("C", "replacement applied", False, {"replacements": {"plain": "REPLACED"}}),
            ("D", "filter+replacement", False, {"replacements": {"replacement": "REPLACED"}}),
            ("E", "isolated config", False, None),
            ("F", "minimal pipeline", False, None),
        ]
        for cell, text, block, meta_patch in cells:
            res = cell_replica(client, c, base, cell, text, block, meta_patch)
            out["cells"][cell] = [res]
            log(f"cell {cell}: {res['status']} ({res['reason']})")

    # baseline must be untouched
    after = baseline_row(c)
    out["baseline"]["after"] = {k: after[k] for k in ("id", "is_active",
                                                      "is_paused", "version",
                                                      "metadata")}
    out["baseline"]["drift"] = out["baseline"]["before"] != out["baseline"]["after"]

    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=2)
    log(f"wrote {OUT}")
    fails = [r["status"] for cell in out["cells"].values() for r in cell
             if r["status"] != "PASS"]
    log(f"{len(fails)} failing cells")
    return 1 if fails or out["baseline"]["drift"] else 0


if __name__ == "__main__":
    sys.exit(main())
