"""Forwarding status: one task's delivery health, in one value object.

"Viewing the forwarding status" used to mean reading the task card and guessing:
the card showed the on/off switch, while the delivery queue, the retry counter
and the dead-letter pile lived on other screens. This use case folds the three
sources of truth into one snapshot:

* the rule — its state (running / paused / paused until / disabled);
* the rule's counters — forwarded, filtered, errors, last forward;
* the durable queue — pending, claimed, retrying, sent, failed for *this* rule.

Ownership is resolved through :class:`ForwardRuleUseCases.get_owned`, so a
status snapshot can never describe another tenant's task.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..domain.entities import (
    STATUS_DISABLED,
    STATUS_PAUSED,
    STATUS_PAUSED_UNTIL,
    STATUS_RUNNING,
)
from .repositories import IDeliveryQueueRepository, IRuleStatsRepository
from .use_cases import ForwardRuleUseCases

logger = logging.getLogger(__name__)

#: Delivery states that mean "the engine still owes the user something".
_BACKLOG_STATES = ("PENDING", "CLAIMED", "RETRY_WAIT")


@dataclass(frozen=True)
class ForwardingStatus:
    """A task's state and counters, ready to render."""

    rule_id: str
    state: str
    paused_until: int
    forwarded: int
    filtered: int
    errors: int
    last_forward_ts: int
    queue: Dict[str, int]

    @property
    def pending(self) -> int:
        return sum(self.queue.get(s, 0) for s in _BACKLOG_STATES)

    @property
    def retrying(self) -> int:
        return self.queue.get("RETRY_WAIT", 0)

    @property
    def sent(self) -> int:
        return self.queue.get("SENT", 0)

    @property
    def failed(self) -> int:
        return self.queue.get("FAILED", 0) + self.queue.get("DEAD_LETTER", 0)

    @property
    def is_running(self) -> bool:
        return self.state == STATUS_RUNNING

    @property
    def state_key(self) -> str:
        """The catalog key suffix for this state; the renderer translates it."""
        return {
            STATUS_RUNNING: "running",
            STATUS_PAUSED: "paused",
            STATUS_PAUSED_UNTIL: "paused_until",
            STATUS_DISABLED: "disabled",
        }.get(self.state, "paused")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "state": self.state,
            "paused_until": self.paused_until,
            "forwarded": self.forwarded,
            "filtered": self.filtered,
            "errors": self.errors,
            "last_forward_ts": self.last_forward_ts,
            "pending": self.pending,
            "retrying": self.retrying,
            "sent": self.sent,
            "failed": self.failed,
        }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class ForwardingStatusUseCase:
    """Build status snapshots from the rule, its counters and the queue."""

    def __init__(
        self,
        rules: ForwardRuleUseCases,
        rule_stats: Optional[IRuleStatsRepository] = None,
        delivery_queue: Optional[IDeliveryQueueRepository] = None,
    ) -> None:
        self._rules = rules
        self._stats = rule_stats
        self._queue = delivery_queue

    async def for_rule(
        self,
        rule_id: str,
        owner_user_id: Optional[int],
        now: Optional[float] = None,
    ) -> Optional[ForwardingStatus]:
        rule = await self._rules.get_owned(rule_id, owner_user_id)
        if rule is None:
            return None
        stats: Dict[str, Any] = {}
        if self._stats is not None:
            try:
                stats = await self._stats.get(rule.id) or {}
            except Exception as exc:
                logger.warning("[PIPELINE:FWD_STATUS] stats failed rule=%s: %s", rule.id[:8], exc)
        counts: Dict[str, int] = {}
        if self._queue is not None:
            try:
                counts = await self._queue.counts_for_rule(rule.id) or {}
            except Exception as exc:
                logger.warning("[PIPELINE:FWD_STATUS] queue counts failed rule=%s: %s", rule.id[:8], exc)
        return self._build(rule, stats, counts, now=now)

    async def for_owner(
        self,
        owner_user_id: Optional[int],
        limit: int = 20,
        now: Optional[float] = None,
    ) -> List[ForwardingStatus]:
        if owner_user_id is None:
            rules = await self._rules.list_all()
        else:
            rules = await self._rules.list_by_owner(int(owner_user_id))
        return [
            self._build(rule, {}, {}, now=now)
            for rule in list(rules)[: max(1, int(limit))]
        ]

    @staticmethod
    def _build(
        rule: Any,
        stats: Dict[str, Any],
        counts: Dict[str, int],
        now: Optional[float] = None,
    ) -> ForwardingStatus:
        state = rule.status_state(now) if hasattr(rule, "status_state") else (
            STATUS_RUNNING if getattr(rule, "is_active", False) else STATUS_DISABLED
        )
        queue = {str(k).upper(): _int(v) for k, v in (counts or {}).items()}
        return ForwardingStatus(
            rule_id=str(getattr(rule, "id", "")),
            state=state,
            paused_until=_int(getattr(rule, "paused_until", 0)),
            forwarded=_int(stats.get("forwarded")),
            filtered=_int(stats.get("filtered")),
            errors=_int(stats.get("errors")),
            last_forward_ts=_int(stats.get("last_forward_ts")),
            queue=queue,
        )


def last_forward_text(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """A stable, locale-independent timestamp for the status screen."""
    if not ts:
        return ""
    try:
        return time.strftime(fmt, time.localtime(int(ts)))
    except (OSError, ValueError, OverflowError):
        return ""
