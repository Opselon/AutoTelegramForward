"""The log vocabulary and the query value object every log surface shares.

`atf logs query`, `tail`, `export`, `purge` and `atf logs policy` all filter the
same archive, so they all build a :class:`LogQuery`. When each subcommand built
its own parameter dict, `export` silently lost the level filter — the kind of
drift one value object prevents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .errors import UsageError
from .models import items_of
from .validators import LEVELS, _LEVEL_ALIASES

#: Canonical severities, most severe last (the order a human reads a filter in).
SEVERITIES: tuple[str, ...] = LEVELS


def canonical_level(text: str) -> str:
    """`warning` -> `WARN`; unknown text is returned upper-cased for the server."""
    raw = (text or "").strip()
    return _LEVEL_ALIASES.get(raw.lower(), raw.upper())


@dataclass(frozen=True)
class LogQuery:
    """One filter over the archive. Empty means "no filter", never "match ''"."""

    level: str = ""
    levels: tuple[str, ...] = ()
    service: str = ""
    rule_id: str = ""
    search: str = ""
    correlation_id: str = ""
    category: str = ""
    hours: int = 0
    since: int = 0
    until: int = 0
    page: int = 0
    limit: int = 50
    offset: int = 0
    ascending: bool = False

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 5000:
            raise UsageError(f"limit must be between 1 and 5000, got {self.limit}")
        if self.offset < 0:
            raise UsageError("offset cannot be negative")
        if self.hours < 0:
            raise UsageError("hours cannot be negative")
        if self.since < 0 or self.until < 0:
            raise UsageError("since/until are unix seconds and cannot be negative")
        if self.since and self.until and self.since > self.until:
            raise UsageError("--since is after --until; the window would be empty")
        if self.page < 0:
            raise UsageError("page numbers start at 1")

    def to_params(self) -> dict[str, Any]:
        """Only the filters that are set — an empty flag must not narrow a query."""
        params: dict[str, Any] = {
            "limit": self.limit,
            "offset": self.offset,
            "order": "asc" if self.ascending else "desc",
        }
        # `level` is the single-select form, `levels` the multi-select one; the
        # API reads both, so passing one never cancels the other.
        for key in ("level", "service", "rule_id", "search", "correlation_id", "category"):
            value = getattr(self, key)
            if value:
                params[key] = value
        if self.levels:
            params["levels"] = ",".join(canonical_level(item) for item in self.levels if item)
        for key in ("hours", "since", "until", "page"):
            value = getattr(self, key)
            if value:
                params[key] = value
        return params

    def purge_body(self, *, older_than_days: int) -> dict[str, Any]:
        """The purge endpoint counts in HOURS (`older_than_hours`).

        Sending a `older_than_days` key is silently ignored by the API, which
        then falls back to its own 24h default — i.e. the flag appeared to work
        and deleted the wrong window.
        """
        body: dict[str, Any] = {"older_than_hours": max(0, older_than_days) * 24}
        for key in ("level", "service", "rule_id", "category"):
            value = getattr(self, key)
            if value:
                body[key] = value
        return body

    def described_by(self) -> dict[str, Any]:
        """The filters that were actually applied, for the human footer."""
        applied: dict[str, Any] = {}
        for key in (
            "level",
            "levels",
            "service",
            "rule_id",
            "search",
            "correlation_id",
            "category",
            "hours",
            "since",
            "until",
            "page",
        ):
            value = getattr(self, key)
            if value:
                applied[key] = value
        return applied


@dataclass(frozen=True)
class LogEntry:
    timestamp: float = 0.0
    level: str = ""
    service: str = ""
    rule_id: str = ""
    message: str = ""
    correlation_id: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LogEntry":
        raw_ts = payload.get("timestamp", payload.get("ts", 0))
        try:
            timestamp = float(raw_ts or 0)
        except (TypeError, ValueError):
            timestamp = 0.0
        return cls(
            timestamp=timestamp,
            level=canonical_level(str(payload.get("level", ""))),
            service=str(payload.get("service", "")),
            rule_id=str(payload.get("rule_id", "") or ""),
            message=str(payload.get("message") or payload.get("msg") or ""),
            correlation_id=str(payload.get("correlation_id", "") or ""),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class LogPage:
    entries: Sequence[LogEntry] = ()
    total: int = 0
    count: int = 0
    has_more: bool = False

    @classmethod
    def from_payload(cls, payload: Any) -> "LogPage":
        rows = items_of(payload, "logs", "entries", "items")
        entries = [LogEntry.from_payload(row) for row in rows]
        if isinstance(payload, Mapping):
            total = payload.get("total", len(entries))
            try:
                total_int = int(total)
            except (TypeError, ValueError):
                total_int = len(entries)
            return cls(
                entries=entries,
                total=total_int,
                count=int(payload.get("count", len(entries)) or len(entries)),
                has_more=bool(payload.get("has_more", False)),
            )
        return cls(entries=entries, total=len(entries), count=len(entries))

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "count": len(self.entries),
            "logs": [entry.to_json() for entry in self.entries],
        }


@dataclass(frozen=True)
class LogStats:
    total: int = 0
    oldest_ts: float = 0.0
    newest_ts: float = 0.0
    by_level: Mapping[str, int] = field(default_factory=dict)
    by_service: Mapping[str, int] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Any) -> "LogStats":
        if not isinstance(payload, Mapping):
            return cls()
        levels = payload.get("by_level") or payload.get("levels") or {}
        services = payload.get("by_service") or payload.get("services") or {}
        return cls(
            total=int(payload.get("total", 0) or 0),
            oldest_ts=float(payload.get("oldest_ts", 0) or 0),
            newest_ts=float(payload.get("newest_ts", 0) or 0),
            by_level={str(k): int(v) for k, v in levels.items()} if isinstance(levels, Mapping) else {},
            by_service={str(k): int(v) for k, v in services.items()} if isinstance(services, Mapping) else {},
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class LogPolicyView:
    """The core's collection policy, as the API reports it per producer scope."""

    generated_at: str = ""
    scopes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    notes: Sequence[str] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Any) -> "LogPolicyView":
        if not isinstance(payload, Mapping):
            return cls(raw={"value": payload})
        scopes = payload.get("scopes") or {}
        return cls(
            generated_at=str(payload.get("generated_at", "")),
            scopes={str(k): v for k, v in scopes.items() if isinstance(v, Mapping)},
            notes=tuple(str(note) for note in payload.get("notes") or []),
            raw=dict(payload),
        )

    def effective_levels(self, scope: str = "core") -> Sequence[str]:
        entry = self.scopes.get(scope) or {}
        levels = entry.get("effective_levels") or entry.get("configured_levels") or ()
        return tuple(str(level) for level in levels)

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)
