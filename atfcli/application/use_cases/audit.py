"""Audit trail queries.

The audit surface is read-only and paged server-side. What this module adds is a
typed request: the API takes a loose list of query parameters, and building them
by hand is how `--outcome denied` used to be sent under the wrong key and quietly
ignored. The vocabulary is validated locally against the same values the proto
declares.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...domain.errors import UsageError
from ...domain.models import AuditEvent
from ...domain.validators import OUTCOMES, valid_telegram_user_id


class Audit:
    """Use cases over the audit event stream."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway

    def query(
        self,
        *,
        user_id: str = "",
        event_type: str = "",
        service: str = "",
        outcome: str = "",
        severity: str = "",
        since: str = "",
        until: str = "",
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditEvent]:
        params: dict[str, Any] = {"limit": max(1, limit), "offset": max(0, offset)}
        if user_id:
            problem = valid_telegram_user_id(user_id)
            if problem:
                raise UsageError(f"user id: {problem}")
            params["user_id"] = user_id
        if outcome:
            wanted = outcome.strip().upper()
            if wanted not in OUTCOMES:
                raise UsageError(
                    f"unknown outcome {outcome!r}",
                    hint=f"outcomes: {', '.join(OUTCOMES)}",
                )
            params["outcome"] = wanted
        for key, value in (
            ("event_type", event_type),
            ("service", service),
            ("severity", severity),
            ("since", since),
            ("until", until),
            ("search", search),
        ):
            if value:
                params[key] = value.strip()
        return self.gateway.query(params)


def summarise(events: list[AuditEvent]) -> Mapping[str, Any]:
    """Counts by outcome, for the one-line footer the command prints."""
    counts: dict[str, int] = {}
    for event in events:
        key = (event.outcome or "UNKNOWN").upper()
        counts[key] = counts.get(key, 0) + 1
    return {"events": len(events), "by_outcome": counts}
