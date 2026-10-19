"""Typed views over the dashboard API's payloads.

This is the CLI's anti-corruption layer. The API returns JSON documents whose
shape is owned by the Go handler; a command that reads ``rule["is_paused"]``
directly spreads that knowledge across thirty files and breaks silently when a
field is renamed. Each entity here parses the payload **once**, states the
fields a human acts on, and keeps the original document as ``raw`` so ``--json``
output stays byte-for-byte what the API said.

Entities are immutable views, not a second database: a use case never mutates a
``Task`` and writes it back, it asks the gateway to change the server and parses
the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .errors import UsageError
from .validators import valid_chat_ref


def items_of(payload: Any, key: str, *fallbacks: str) -> list[dict[str, Any]]:
    """The list inside an API envelope, without inventing a row when it is absent.

    A ``list_of``-style helper that falls back to the whole payload turns a
    wrong key into one bogus row (placeholder id, "—", 0) instead of an error.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for candidate in (key, *fallbacks):
            value = payload.get(candidate)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if payload and "success" not in payload and not any(
            isinstance(value, (dict, list)) for value in payload.values()
        ):
            return [payload]
    return []


def _flag(payload: Mapping[str, Any], *keys: str) -> bool:
    """First present boolean among `keys`, so an alias list can span versions."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"):
            return True
        if isinstance(value, int):
            return bool(value)
    return False


def _text(payload: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _int(payload: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
    return default


# ------------------------------------------------------------------------ tasks
class TaskState(str, Enum):
    ENABLED = "on"
    DISABLED = "off"
    PAUSED = "paused"

    @property
    def label(self) -> str:
        return self.value


@dataclass(frozen=True)
class Task:
    """One forwarding task (``ForwardRule``): sources -> targets."""

    id: str
    name: str = ""
    enabled: bool = False
    paused: bool = False
    source_chat_ids: tuple[str, ...] = ()
    source_chat_id: str = ""
    target_chat_id: str = ""
    source_title: str = ""
    target_title: str = ""
    session_id: str = ""
    session_name: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Task":
        sources = payload.get("source_chat_ids")
        ids = tuple(str(item) for item in sources if item) if isinstance(sources, list) else ()
        primary = _text(payload, "source_chat_id") or (ids[0] if ids else "")
        if not ids and primary:
            ids = (primary,)
        return cls(
            id=_text(payload, "id", "rule_id"),
            name=_text(payload, "name"),
            # The rule DTO spells this `is_active` (proto field 9). Reading
            # `is_enabled` — which the API never sends — made every task render as
            # disabled, and made `atf rules enable` toggle an enabled task off.
            enabled=_flag(payload, "is_active", "is_enabled", "enabled"),
            paused=_flag(payload, "is_paused", "paused"),
            source_chat_ids=ids,
            source_chat_id=primary,
            target_chat_id=_text(payload, "target_chat_id"),
            source_title=_text(payload, "source_chat_name", "source_chat_title"),
            target_title=_text(payload, "target_chat_name", "target_chat_title"),
            session_id=_text(payload, "session_id"),
            session_name=_text(payload, "session_name"),
            raw=dict(payload),
        )

    @property
    def state(self) -> TaskState:
        if self.paused:
            return TaskState.PAUSED
        return TaskState.ENABLED if self.enabled else TaskState.DISABLED

    @property
    def label(self) -> str:
        return self.name or self.source_title or self.id[:8]

    @property
    def source_count(self) -> int:
        return len([chat for chat in self.source_chat_ids if chat])

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw) if self.raw else _task_payload(self)


def _task_payload(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "name": task.name,
        "is_active": task.enabled,
        "is_paused": task.paused,
        "source_chat_ids": list(task.source_chat_ids),
        "source_chat_id": task.source_chat_id,
        "source_chat_name": task.source_title,
        "target_chat_id": task.target_chat_id,
        "target_chat_name": task.target_title,
    }


@dataclass(frozen=True)
class TaskDraft:
    """What it takes to create a task, validated before the request is sent."""

    source: str
    target: str
    name: str = ""
    session_id: str = ""
    source_title: str = ""
    target_title: str = ""
    enabled: bool = True
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (("source", self.source), ("target", self.target)):
            problem = valid_chat_ref(value)
            if problem:
                raise UsageError(f"{label}: {problem}")
        if self.source.strip().lstrip("-") == self.target.strip().lstrip("-"):
            raise UsageError(
                "source and target are the same chat",
                hint="a task that forwards into itself is a loop; pick a different target",
            )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_chat_id": self.source,
            "target_chat_id": self.target,
            # `is_active` is sent explicitly: the proto default is false, so an
            # omitted flag creates a task that never fires.
            "is_active": bool(self.enabled),
        }
        if self.name:
            payload["name"] = self.name
        if self.session_id:
            payload["session_id"] = self.session_id
        if self.source_title:
            payload["source_chat_name"] = self.source_title
        if self.target_title:
            payload["target_chat_name"] = self.target_title
        payload.update(self.extra)
        return payload


@dataclass(frozen=True)
class SimulationResult:
    """What the rule engine says it would do with one message."""

    matched: bool = False
    action: str = ""
    would_forward: bool = False
    transformed_text: str = ""
    notes: Sequence[str] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Any) -> "SimulationResult":
        if not isinstance(payload, Mapping):
            return cls(raw={"value": payload})
        notes = payload.get("notes") or ()
        if isinstance(notes, str):
            notes = (notes,)
        reason = _text(payload, "match_reason", "reason")
        return cls(
            matched=bool(payload.get("matched", False)),
            action=_text(payload, "action", "forward_mode"),
            # The rule engine answers with `matched` plus a reason and the text it
            # would render; there is no `would_forward` field to read.
            would_forward=bool(payload.get("would_forward", payload.get("matched", False))),
            transformed_text=_text(payload, "transformed_text", "rendered_text"),
            notes=tuple(str(note) for note in notes) or ((reason,) if reason else ()),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class RoutePath:
    """A -> B direct, or the branding hop A -> C -> B."""

    kind: str = "direct"
    intermediate: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("direct", "intermediate"):
            raise UsageError(f"route path must be 'direct' or 'intermediate', got {self.kind!r}")
        if self.kind == "intermediate" and not self.intermediate:
            raise UsageError(
                "the intermediate route needs the relay chat",
                hint="pass --intermediate <chat id> (the C in A -> C -> B)",
            )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"route_path": self.kind}
        if self.intermediate:
            payload["intermediate_chat_id"] = self.intermediate
        return payload


# --------------------------------------------------------------------- sessions
@dataclass(frozen=True)
class Session:
    id: str
    phone: str = ""
    user_id: str = ""
    name: str = ""
    active: bool = True
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Session":
        return cls(
            id=_text(payload, "session_id", "id"),
            phone=_text(payload, "phone"),
            user_id=_text(payload, "user_id"),
            name=_text(payload, "first_name", "username", "name"),
            active=bool(payload.get("is_active", True)),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class SessionChat:
    id: str
    title: str = ""
    kind: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SessionChat":
        return cls(
            id=_text(payload, "id", "chat_id"),
            title=_text(payload, "title", "name"),
            kind=_text(payload, "type", "kind"),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


# ----------------------------------------------------------------------- queue
@dataclass(frozen=True)
class DeadLetterItem:
    id: str
    rule_id: str = ""
    error: str = ""
    retry_count: int = 0
    created_at: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeadLetterItem":
        return cls(
            id=_text(payload, "id", "message_id"),
            rule_id=_text(payload, "rule_id"),
            error=_text(payload, "error", "last_error"),
            retry_count=_int(payload, "retry_count", "attempts"),
            created_at=_text(payload, "created_at", "failed_at"),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class QueueSnapshot:
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Any) -> "QueueSnapshot":
        if isinstance(payload, Mapping):
            return cls(raw=dict(payload))
        return cls(raw={"value": payload})

    def counter(self, *keys: str) -> int:
        return _int(self.raw, *keys)

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


# ------------------------------------------------------------------- operations
@dataclass(frozen=True)
class ServiceHealth:
    name: str
    online: bool
    detail: str = ""
    latency_ms: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ServiceHealth":
        return cls(
            name=_text(payload, "name", "service"),
            online=bool(payload.get("online", payload.get("ok", False))),
            detail=_text(payload, "detail", "error"),
            latency_ms=_int(payload, "latency_ms", "latency"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "online": self.online,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class Finding:
    """One problem the preflight diagnostics found, with how to fix it."""

    id: str
    severity: str
    title: str
    detail: str = ""
    remediation: str = ""
    subject: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, index: int = 0) -> "Finding":
        return cls(
            id=_text(payload, "id") or f"finding-{index}",
            severity=(_text(payload, "severity", default="info") or "info").lower(),
            title=_text(payload, "title", "error_name", "category"),
            detail=_text(payload, "detail", "message"),
            remediation=_text(payload, "remediation", "fix", "hint"),
            subject=_text(payload, "rule_id", "subject", "service"),
        )

    @property
    def is_failure(self) -> bool:
        return self.severity in ("error", "critical")

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "remediation": self.remediation,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class Readiness:
    """`/api/system/setup`: what this deployment is configured with."""

    ready: bool = False
    deployment: str = ""
    product_version: str = ""
    api_version: str = ""
    services: Sequence[ServiceHealth] = ()
    features: Sequence[str] = ()
    checks: Sequence[Mapping[str, Any]] = ()
    missing_required: Sequence[str] = ()
    next_steps: Sequence[str] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Any) -> "Readiness":
        if not isinstance(payload, Mapping):
            return cls(raw={"value": payload})
        services = [
            ServiceHealth.from_payload(item)
            for item in payload.get("services") or []
            if isinstance(item, Mapping)
        ]
        checks = [item for item in payload.get("checks") or [] if isinstance(item, Mapping)]
        missing = payload.get("missing_required") or [
            str(item.get("key", "")) for item in checks if not item.get("set") and item.get("level") == "required"
        ]
        return cls(
            ready=bool(payload.get("ready", False)),
            deployment=_text(payload, "deployment"),
            product_version=_text(payload, "product_version", "version"),
            api_version=_text(payload, "api_version"),
            services=services,
            features=tuple(str(item) for item in payload.get("features") or []),
            checks=checks,
            missing_required=tuple(str(item) for item in missing if item),
            next_steps=tuple(str(item) for item in payload.get("next_steps") or []),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw) if self.raw else {}

    @property
    def online_services(self) -> int:
        return sum(1 for service in self.services if service.online)


# ----------------------------------------------------------------------- admin
@dataclass(frozen=True)
class AdminUser:
    id: str
    username: str = ""
    role: str = ""
    active: bool = True
    tasks: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AdminUser":
        role = _text(payload, "role") or ("admin" if payload.get("is_admin") else "user")
        return cls(
            id=_text(payload, "id", "user_id"),
            username=_text(payload, "username"),
            role=role,
            active=bool(payload.get("is_active", True)),
            tasks=_int(payload, "rules_count", "task_count"),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class AuditEvent:
    id: str = ""
    event_type: str = ""
    service: str = ""
    outcome: str = ""
    severity: str = ""
    created_at: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuditEvent":
        return cls(
            id=_text(payload, "id", "event_id"),
            event_type=_text(payload, "event_type", "category"),
            service=_text(payload, "service"),
            outcome=_text(payload, "outcome"),
            severity=_text(payload, "severity"),
            created_at=_text(payload, "created_at", "ts", "timestamp"),
            raw=dict(payload),
        )

    def to_json(self) -> dict[str, Any]:
        return dict(self.raw)


# --------------------------------------------------------------------- patterns
def names(items: Iterable[Mapping[str, Any]], *keys: str) -> list[str]:
    """Collect a display name from each row, skipping rows without one."""
    out: list[str] = []
    for item in items:
        value = _text(item, *keys)
        if value:
            out.append(value)
    return out


def payload_list(payload: Any, key: str, *fallbacks: str) -> list[dict[str, Any]]:
    return items_of(payload, key, *fallbacks)
