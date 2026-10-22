"""The composition root: ports in, use cases out.

The CLI builds this once per invocation (``CliContext.uc``). Keeping the wiring
in one function is what makes the layering real rather than decorative: a test
can build the same bundle over in-memory fakes, and no command module ever has
to know which adapter it got.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.errors import NotSupportedError

from .gateways import FileWriter, Gateways
from .use_cases import admin as admin_uc
from .use_cases import audit as audit_uc
from .use_cases import backup as backup_uc
from .use_cases import delivery as delivery_uc
from .use_cases import envconfig as envconfig_uc
from .use_cases import logs as logs_uc
from .use_cases import ops as ops_uc
from .use_cases import resources as resources_uc
from .use_cases import sessions as sessions_uc
from .use_cases import tasks as tasks_uc


@dataclass(frozen=True)
class UseCases:
    """Every use case the CLI can reach — the application layer's public surface."""

    tasks: tasks_uc.Tasks
    sessions: sessions_uc.Sessions
    logs: logs_uc.Logs
    delivery: delivery_uc.Delivery
    ai: resources_uc.Resources
    prompts: resources_uc.Resources
    filters: resources_uc.Resources
    backup: backup_uc.Backups
    admin: admin_uc.Administration
    audit: audit_uc.Audit
    ops: ops_uc.Operations
    env: envconfig_uc.Environment

    def resources_for(self, spec: Any) -> resources_uc.Resources:
        """The use case set for a :class:`ResourceSpec` (menu-driven callers)."""
        for candidate in (self.ai, self.prompts, self.filters):
            if candidate.spec is spec or candidate.spec == spec:
                return candidate
        raise KeyError(f"no use case bundle for resource {getattr(spec, 'plural', spec)!r}")


class _UnwiredEnvStore:
    """A `.env` port for callers that built the use cases without a host.

    The API-only contract tests never touch the filesystem; they should not have
    to know a `.env` exists. Reading answers "empty", writing explains itself.
    """

    path = ".env"

    def exists(self) -> bool:
        return False

    def mode(self) -> int:
        return 0

    def read(self) -> str:
        return ""

    def text_or_empty(self) -> str:
        return ""

    def write(self, text: str) -> Any:  # pragma: no cover - not reachable from the API path
        raise NotSupportedError(
            "this CLI was built without a host — there is no `.env` to write",
            hint="run the command against an installation (`--root <dir>` or the repo checkout)",
        )

    def backups(self) -> list[Any]:
        return []

    def restore(self, name: str) -> Any:  # pragma: no cover - see write()
        raise NotSupportedError(
            "this CLI was built without a host — there are no backups to restore",
            hint="run the command against an installation (`--root <dir>` or the repo checkout)",
        )


def build_use_cases(
    gateways: Gateways,
    *,
    files: FileWriter | None = None,
    env_store: Any = None,
) -> UseCases:
    """Wire the ports into use cases. The only place that knows both sides."""
    return UseCases(
        tasks=tasks_uc.Tasks(gateways.tasks),
        sessions=sessions_uc.Sessions(gateways.sessions),
        logs=logs_uc.Logs(gateways.logs),
        delivery=delivery_uc.Delivery(gateways.delivery),
        ai=resources_uc.Resources(gateways.ai),
        prompts=resources_uc.Resources(gateways.prompts),
        filters=resources_uc.Resources(gateways.filters),
        backup=backup_uc.Backups(gateways.backup, files=files),
        admin=admin_uc.Administration(gateways.admin),
        audit=audit_uc.Audit(gateways.audit),
        ops=ops_uc.Operations(gateways.ops),
        env=envconfig_uc.Environment(env_store if env_store is not None else _UnwiredEnvStore()),
    )


__all__ = ["UseCases", "build_use_cases"]
