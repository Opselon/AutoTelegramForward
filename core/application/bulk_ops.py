"""Multi-rule selection and bulk execution, independent of any surface.

Telegram calls it "select a few rules and switch them off together"; the same
idea serves replace rules and forwarding tasks, and both must behave the same
way. This module owns the rules of that operation:

* **Selection** is a small, bounded, serialisable value. It lives in the user's
  UI-state buffer (so it survives a restart and cannot be supplied by a
  callback), it expires, and it is pruned against the owner's *current* rules
  before every render — a rule deleted on another device can never linger as a
  ghost target.
* **Execution** is per item. One failing rule never aborts the batch and never
  hides the outcome of the others: every id comes back with its own result and
  a stable code, and the caller reports them side by side.
* **Idempotency** is the caller's: enabling an already enabled rule is a
  success with ``unchanged``, deleting an already deleted one is ``missing``,
  so a retried batch is safe.

Nothing here knows about Telegram, SQL or translations: ids in, outcomes out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Telegram keyboards and a phone screen both stop being usable far below this;
#: the cap also bounds the work a single callback can trigger.
MAX_SELECTION = 50

#: A selection left open longer than this is stale: the rules may have changed
#: on another device, and the user has moved on.
TTL_SECONDS = 30 * 60

ENABLE = "enable"
DISABLE = "disable"
DELETE = "delete"
OPS: Tuple[str, ...] = (ENABLE, DISABLE, DELETE)

#: Bulk actions that change or remove data need an explicit confirmation.
DESTRUCTIVE = (DELETE,)

#: Stable per-item result codes, so the surface can localise each line.
OK = "ok"
UNCHANGED = "unchanged"
MISSING = "missing"
FAILED = "failed"


@dataclass(frozen=True)
class Selection:
    """An ordered, de-duplicated set of rule ids plus the moment it changed."""

    ids: Tuple[str, ...] = ()
    ts: float = 0.0

    def __len__(self) -> int:
        return len(self.ids)

    def __contains__(self, rule_id: Any) -> bool:
        return str(rule_id) in self.ids

    def toggle(self, rule_id: Any, now: Optional[float] = None) -> "Selection":
        rid = str(rule_id or "").strip()
        if not rid:
            return self
        stamp = time.time() if now is None else float(now)
        if rid in self.ids:
            return Selection(tuple(i for i in self.ids if i != rid), stamp)
        if len(self.ids) >= MAX_SELECTION:
            raise ValueError(f"selection is limited to {MAX_SELECTION} rules")
        return Selection(self.ids + (rid,), stamp)

    def select_all(self, rule_ids: Iterable[Any], now: Optional[float] = None) -> "Selection":
        """Select what fits, in listing order. Never raises on overflow."""
        stamp = time.time() if now is None else float(now)
        kept = list(self.ids)
        for raw in rule_ids:
            rid = str(raw or "").strip()
            if rid and rid not in kept:
                if len(kept) >= MAX_SELECTION:
                    break
                kept.append(rid)
        return Selection(tuple(kept), stamp)

    def clear(self, now: Optional[float] = None) -> "Selection":
        return Selection((), time.time() if now is None else float(now))

    def prune(self, valid_ids: Iterable[Any], now: Optional[float] = None) -> "Selection":
        """Drop ids that no longer belong to the owner's rules."""
        valid = {str(v) for v in valid_ids}
        kept = tuple(i for i in self.ids if i in valid)
        if kept == self.ids:
            return self
        return Selection(kept, time.time() if now is None else float(now))

    def expired(self, now: Optional[float] = None, ttl: int = TTL_SECONDS) -> bool:
        if not self.ids:
            return False
        stamp = time.time() if now is None else float(now)
        return (stamp - float(self.ts or 0)) > ttl

    def to_buffer(self) -> Dict[str, Any]:
        return {"ids": list(self.ids), "ts": float(self.ts)}

    @classmethod
    def from_buffer(cls, raw: Any) -> "Selection":
        """Rebuild from UI state; anything malformed degrades to empty.

        The buffer is persisted JSON, so it is untrusted input: a hand-edited
        file must not be able to inject a non-string id or an unbounded list.
        """
        if not isinstance(raw, Mapping):
            return cls()
        ids = raw.get("ids")
        if not isinstance(ids, (list, tuple)):
            return cls()
        cleaned: List[str] = []
        for item in ids:
            if not isinstance(item, str):
                continue
            rid = item.strip()[:64]
            if rid and rid not in cleaned:
                cleaned.append(rid)
            if len(cleaned) >= MAX_SELECTION:
                break
        try:
            stamp = float(raw.get("ts") or 0.0)
        except (TypeError, ValueError):
            stamp = 0.0
        return cls(tuple(cleaned), stamp)


@dataclass(frozen=True)
class Outcome:
    """What happened to one rule in a batch."""

    rule_id: str
    code: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code in (OK, UNCHANGED)


@dataclass(frozen=True)
class Summary:
    total: int = 0
    ok: int = 0
    unchanged: int = 0
    missing: int = 0
    failed: int = 0

    @property
    def changed(self) -> int:
        return self.ok

    @property
    def all_ok(self) -> bool:
        return self.failed == 0 and self.missing == 0


def summarize(outcomes: Sequence[Outcome]) -> Summary:
    counts = {OK: 0, UNCHANGED: 0, MISSING: 0, FAILED: 0}
    for outcome in outcomes:
        counts[outcome.code if outcome.code in counts else FAILED] += 1
    return Summary(
        total=len(outcomes),
        ok=counts[OK],
        unchanged=counts[UNCHANGED],
        missing=counts[MISSING],
        failed=counts[FAILED],
    )


#: One rule at a time: ``(rule_id) -> code``; raising is reported as ``failed``.
Action = Callable[[str], Awaitable[str]]


async def apply_each(ids: Iterable[Any], action: Action) -> List[Outcome]:
    """Run ``action`` for every id, isolating failures.

    Order is preserved so the report reads in the order the user selected, and
    a failure carries the exception class (never its message: messages can
    contain ids or SQL, and this string reaches the chat).
    """
    results: List[Outcome] = []
    for raw in ids:
        rid = str(raw or "").strip()
        if not rid:
            continue
        try:
            code = await action(rid)
        except Exception as exc:  # noqa: BLE001 - per-item isolation is the point
            results.append(Outcome(rid, FAILED, type(exc).__name__))
            continue
        if code not in (OK, UNCHANGED, MISSING, FAILED):
            code = OK if code else FAILED
        results.append(Outcome(rid, code))
    return results


def empty_summary() -> Summary:
    return Summary()


def selected_ids(selection: Selection) -> Tuple[str, ...]:
    return selection.ids
