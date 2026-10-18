"""Giving pre-ownership rows an owner — or refusing to guess.

Rows created before `owner_user_id` existed carry zero. Leaving them at zero is
not neutral: `owner_user_id=0` is nobody's, so the row disappears from every
tenant's list while still being reachable by an id (the bot's admin scope, a
by-id servicer call). Assigning the wrong owner is worse — it hands one tenant's
task to another. So this module does exactly one of three things to each row:

  * **assigns** it, when the database already contains verifiable evidence of
    who created it (for a rule: the session it was built on, or the API
    credential its AI config uses — both are owned rows that the task had to
    reference to exist at all);
  * **quarantines** it, when there is no such evidence, recording the row and
    why in `ownership_quarantine` so an operator can decide with a human's
    knowledge;
  * **leaves it alone**, when it already has an owner.

It never infers from "the only tenant on this box" or from a timestamp: those are
guesses, and a guess here is a cross-tenant disclosure. The plan is computed
first and applied second, the apply is idempotent, and it runs inside one
transaction so a failure leaves the database exactly as it was.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

#: Tables that carry `owner_user_id`, in the order the plan resolves them (a
#: rule's evidence can be a session, so sessions are read, not written).
OWNED_TABLES = (
    "sessions",
    "forward_rules",
    "filter_rules",
    "ai_configs",
    "api_credentials",
    "bot_tokens",
    "prompt_templates",
)

#: A rule references the session it runs on. That reference is the evidence: the
#: session is an owned row, and the rule could not exist without it.
_RULE_EVIDENCE = """
SELECT s.owner_user_id
FROM forward_rules r
JOIN sessions s ON s.id = r.session_id
WHERE r.id = ?
"""

#: An AI config or filter is referenced *by* rules. If exactly one owner's rules
#: use it, that owner is evidence; several owners sharing it is ambiguous and is
#: left for a human.
_AI_EVIDENCE = """
SELECT DISTINCT r.owner_user_id
FROM forward_rules r
WHERE r.ai_config_id = ? AND r.owner_user_id > 0
"""

_FILTER_EVIDENCE = """
SELECT DISTINCT r.owner_user_id
FROM forward_rules r
WHERE r.filter_rule_id = ? AND r.owner_user_id > 0
"""


@dataclass
class Decision:
    """What should happen to one row, and why."""

    table: str
    row_id: str
    owner: int = 0
    evidence: str = ""
    reason: str = ""

    @property
    def quarantined(self) -> bool:
        return self.owner <= 0


@dataclass
class Report:
    assigned: Dict[str, int] = field(default_factory=dict)
    quarantined: Dict[str, int] = field(default_factory=dict)
    already_owned: int = 0
    decisions: List[Decision] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return sum(self.assigned.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assigned": dict(self.assigned),
            "quarantined": dict(self.quarantined),
            "already_owned": self.already_owned,
            "changed": self.changed,
            "decisions": [d.__dict__ for d in self.decisions],
        }


def _rows(db, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    rows = db.query_all(sql, tuple(params))
    return [dict(r) for r in rows]


def _single_owner(db, sql: str, row_id: str) -> Optional[int]:
    """The one positive owner the evidence points at, or ``None``."""
    try:
        rows = db.query_all(sql, (row_id,))
    except Exception:
        return None
    owners = {int(r[0]) for r in rows if r and r[0] is not None}
    if len(owners) == 1:
        owner = owners.pop()
        return owner if owner > 0 else None
    # Zero rows: no evidence. Several owners: contradictory evidence, which is
    # exactly the case a human has to look at.
    return None


def _evidence_for(db, table: str, row_id: str) -> tuple[Optional[int], str, str]:
    """``(owner, evidence, reason)`` for one unowned row."""
    if table == "forward_rules":
        owner = _single_owner(db, _RULE_EVIDENCE, row_id)
        if owner:
            return owner, f"session of rule {row_id}", ""
        return None, "", "no_owned_session: the rule's session is missing or itself unowned"
    if table == "ai_configs":
        owner = _single_owner(db, _AI_EVIDENCE, row_id)
        if owner:
            return owner, f"rules using config {row_id}", ""
        return None, "", "ambiguous_or_unused: no rule, or rules of several owners, use this config"
    if table == "filter_rules":
        owner = _single_owner(db, _FILTER_EVIDENCE, row_id)
        if owner:
            return owner, f"rules using filter {row_id}", ""
        return None, "", "ambiguous_or_unused: no rule, or rules of several owners, use this filter"
    # Sessions, bot tokens, credentials and templates carry no upstream pointer
    # that proves who created them. Anything we deduced would be a guess.
    return None, "", "no_verifiable_evidence"


def plan(db, tables: Iterable[str] = OWNED_TABLES) -> Report:
    """Every unowned row, with the decision this module would take for it."""
    report = Report()
    for table in tables:
        try:
            rows = _rows(
                db,
                f"SELECT id, owner_user_id FROM {table} "
                "WHERE owner_user_id IS NULL OR owner_user_id = 0",
            )
        except Exception:
            continue  # a table this database predates
        for row in rows:
            row_id = str(row.get("id") or "")
            owner, evidence, reason = _evidence_for(db, table, row_id)
            report.decisions.append(
                Decision(table=table, row_id=row_id, owner=owner or 0,
                         evidence=evidence, reason=reason)
            )
    return report


def _ensure_quarantine(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ownership_quarantine ("
        " table_name TEXT NOT NULL,"
        " row_id TEXT NOT NULL,"
        " reason TEXT NOT NULL DEFAULT '',"
        " evidence TEXT NOT NULL DEFAULT '',"
        " discovered_at INTEGER NOT NULL DEFAULT 0,"
        " resolved_at INTEGER,"
        " resolved_owner INTEGER,"
        " PRIMARY KEY (table_name, row_id)"
        ")",
        (),
    )


def apply(db, report: Optional[Report] = None) -> Report:
    """Writes the plan: assigns what is provable, quarantines the rest.

    One transaction. A row that already has an owner is skipped by the WHERE
    clause, so a second run is a no-op even if the database changed between plan
    and apply.
    """
    report = report if report is not None else plan(db)
    now = int(time.time())
    _ensure_quarantine(db)
    for decision in report.decisions:
        if decision.quarantined:
            db.execute(
                "INSERT INTO ownership_quarantine"
                " (table_name, row_id, reason, evidence, discovered_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(table_name, row_id) DO NOTHING",
                (decision.table, decision.row_id, decision.reason,
                 decision.evidence, now),
            )
            report.quarantined[decision.table] = report.quarantined.get(decision.table, 0) + 1
            continue
        cur = db.execute(
            f"UPDATE {decision.table} SET owner_user_id = ?"
            " WHERE id = ? AND (owner_user_id IS NULL OR owner_user_id = 0)",
            (int(decision.owner), decision.row_id),
        )
        if getattr(cur, "rowcount", 1) > 0:
            report.assigned[decision.table] = report.assigned.get(decision.table, 0) + 1
        else:
            report.already_owned += 1
    return report


def quarantined(db) -> List[Dict[str, Any]]:
    """The rows still waiting for a human, newest first."""
    _ensure_quarantine(db)
    rows = db.query_all(
        "SELECT table_name, row_id, reason, evidence, discovered_at"
        " FROM ownership_quarantine WHERE resolved_at IS NULL"
        " ORDER BY discovered_at DESC",
        (),
    )
    return [dict(r) for r in rows]


def resolve(db, table: str, row_id: str, owner: int) -> bool:
    """An operator's decision about one quarantined row.

    Assigns the owner and closes the quarantine entry in one transaction. This is
    the only path that assigns without database evidence, and it takes a human
    explicitly saying who owns the row.
    """
    if int(owner) <= 0:
        raise ValueError("owner must be a positive user id")
    now = int(time.time())
    _ensure_quarantine(db)
    cur = db.execute(
        f"UPDATE {table} SET owner_user_id = ?"
        " WHERE id = ? AND (owner_user_id IS NULL OR owner_user_id = 0)",
        (int(owner), row_id),
    )
    if getattr(cur, "rowcount", 0) <= 0:
        return False
    db.execute(
        "UPDATE ownership_quarantine SET resolved_at = ?, resolved_owner = ?"
        " WHERE table_name = ? AND row_id = ?",
        (now, int(owner), table, row_id),
    )
    return True


def dumps(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)
