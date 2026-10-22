"""Configuration management: plan a `.env` change, validate it, apply it safely.

The shape of this use case is the answer to "how do you edit the file the whole
stack depends on without breaking a running deployment":

1. **Plan** — every submission is validated against `domain.envschema` and turned
   into a :class:`Plan` that holds the text *before* and *after*. Nothing is
   written. A plan is what `--dry-run`, `atf config diff` and the console preview
   all show, so a preview cannot describe a different change than the one that
   would be applied.
2. **Apply** — the plan's text goes to the store, which backs up, replaces
   atomically, verifies and rolls back on its own. The use case never touches the
   filesystem, so it is exercised in tests against an in-memory store.
3. **Validate** — the *existing* file is read and checked against the schema, so
   an installation can be audited without proposing a change: placeholder
   credentials, invalid ports, missing required keys, duplicate assignments and
   a world-readable file are findings, not surprises at container start.

Secrets never leave: a plan's JSON carries `[REDACTED]` for any value whose name
says it is a credential, and the human diff renders the same marker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ...domain import envschema
from ...domain.errors import UsageError
from ...domain.models import Finding
from . import _payload

#: How many problems one refusal lists before it says "and N more".
MAX_PROBLEMS = 8


@dataclass(frozen=True)
class Change:
    """One key the operator asked to set, with what it replaces."""

    key: str
    value: str
    before: str | None = None
    secret: bool = False
    unknown: bool = False

    @property
    def changed(self) -> bool:
        return self.before != self.value

    @property
    def shown(self) -> str:
        return "[REDACTED]" if self.secret else self.value

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.shown,
            "before": None if self.before is None else ("[REDACTED]" if self.secret else self.before),
            "changed": self.changed,
            "unknown": self.unknown,
        }


@dataclass(frozen=True)
class Plan:
    """A validated, previewable change set — the unit `apply` consumes."""

    changes: tuple[Change, ...] = ()
    before: str = ""
    after: str = ""
    warnings: tuple[str, ...] = ()
    path: str = ""

    @property
    def empty(self) -> bool:
        return not self.changed_keys

    @property
    def changed_keys(self) -> tuple[str, ...]:
        return tuple(change.key for change in self.changes if change.changed)

    def diff_lines(self, *, limit: int = 60) -> list[str]:
        """Human preview of the effective change, one line per key."""
        rows: list[str] = []
        for change in self.changes:
            if not change.changed:
                rows.append(f"  = {change.key} (unchanged)")
                continue
            before = "(unset)" if change.before is None else ("[REDACTED]" if change.secret else change.before)
            marker = "+" if change.before is None else "~"
            rows.append(f"  {marker} {change.key}: {before} → {change.shown}")
        if len(rows) > limit:
            rows = rows[:limit] + [f"  … and {len(rows) - limit} more"]
        return rows

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "changes": [change.to_json() for change in self.changes],
            "changed": list(self.changed_keys),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Report:
    """The result of auditing the file as it stands."""

    findings: tuple[Finding, ...] = ()
    keys: int = 0
    path: str = ""
    mode: int = 0
    groups: Mapping[str, int] = field(default_factory=dict)

    @property
    def problems(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity in ("error", "warning"))

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "keys": self.keys,
            "mode": oct(self.mode) if self.mode else "",
            "counts": {
                severity: sum(1 for f in self.findings if f.severity == severity)
                for severity in ("error", "warning", "info")
            },
            "findings": [
                {
                    "id": f.id,
                    "severity": f.severity,
                    "title": f.title,
                    "detail": f.detail,
                    "remediation": f.remediation,
                    "subject": f.subject,
                }
                for f in self.findings
            ],
        }


class Environment:
    """Use cases over the deployment's `.env`."""

    def __init__(self, store, *, allow_unknown: bool = True) -> None:
        self.store = store
        self.allow_unknown = allow_unknown

    # ------------------------------------------------------------------ schema
    def schema(self, *, group: str = "") -> list[envschema.EnvVarSpec]:
        return envschema.specs_in(group) if group else list(envschema.SCHEMA)

    def groups(self) -> tuple[str, ...]:
        return envschema.GROUPS

    # ----------------------------------------------------------------- values
    def values(self) -> Mapping[str, str]:
        return envschema.EnvDocument.parse(self.store.text_or_empty()).values()

    def document(self) -> envschema.EnvDocument:
        return envschema.EnvDocument.parse(self.store.text_or_empty())

    # ------------------------------------------------------------------- plan
    def plan(
        self,
        values: Mapping[str, str],
        *,
        allow_unknown: bool | None = None,
    ) -> Plan:
        """Validate `values` and return the change that *would* be written."""
        if not values:
            raise UsageError(
                "nothing to set",
                hint="pass KEY=VALUE pairs, e.g. `atf config set ATF_WEB_PORT=8090`",
            )
        unknown_ok = self.allow_unknown if allow_unknown is None else allow_unknown
        problems: list[str] = []
        warnings: list[str] = []
        for key, raw in values.items():
            problem = envschema.validate_value(key, raw, allow_unknown=unknown_ok)
            if problem:
                problems.append(f"{key}: {problem}")
                continue
            if envschema.spec_for(key) is None:
                warnings.append(f"{key} is not a documented setting — written as given")
        if problems:
            shown = problems[:MAX_PROBLEMS]
            more = len(problems) - len(shown)
            detail = "; ".join(shown) + (f"; and {more} more" if more > 0 else "")
            raise UsageError(
                f"refusing to write: {detail}",
                hint="`atf config schema` lists the documented keys and their accepted values",
            )

        before = self.store.text_or_empty()
        document = envschema.EnvDocument.parse(before)
        current = document.values()
        after = document.with_values(values).render()
        changes = tuple(
            Change(
                key=key,
                value="" if raw is None else str(raw),
                before=current.get(key),
                secret=_masked(key),
                unknown=envschema.spec_for(key) is None,
            )
            for key, raw in values.items()
        )
        return Plan(changes=changes, before=before, after=after, warnings=tuple(warnings), path=str(self.store.path))

    # ------------------------------------------------------------------ apply
    def apply(self, plan: Plan, *, dry_run: bool = False) -> _payload.Outcome:
        """Write a plan through the store. `dry_run` reports, never writes."""
        if plan.empty:
            return _payload.Outcome(
                {"path": plan.path, "changed": [], "written": False},
                "no changes — the file already holds those values",
            )
        if dry_run:
            return _payload.Outcome(
                {"path": plan.path, "changed": list(plan.changed_keys), "written": False, "dry_run": True},
                f"dry run: {len(plan.changed_keys)} key(s) would change in {plan.path}",
            )
        result = self.store.write(plan.after)
        document: dict[str, Any] = {
            "path": str(result.path),
            "changed": list(plan.changed_keys),
            "written": True,
            "backup": result.backup,
            "keys": result.keys,
            "mode": oct(result.mode),
        }
        detail = f"updated {len(plan.changed_keys)} key(s) in {result.path}"
        if result.backup:
            detail += f" (backup: {result.backup})"
        warnings = [_payload.Warning_(message=text) for text in plan.warnings]
        return _payload.Outcome(document, detail, warnings=warnings)

    # --------------------------------------------------------------- validate
    def validate(self) -> Report:
        """Audit the file as it stands: schema, duplicates, placeholders, mode."""
        findings: list[Finding] = []
        path = str(self.store.path)
        if not self.store.exists():
            findings.append(
                Finding(
                    id="env-missing-file",
                    severity="error",
                    title="no `.env` file",
                    detail=f"{path} does not exist",
                    remediation="run `atf setup`, or `atf config set` with the required keys",
                    subject=path,
                )
            )
            return Report(tuple(findings), 0, path, 0, {})

        text = self.store.text_or_empty()
        document = envschema.EnvDocument.parse(text)
        values = document.values()

        for key in envschema.required_keys():
            if key not in values:
                findings.append(
                    Finding(
                        id=f"env-missing-{key}",
                        severity="error",
                        title=f"{key} is not set",
                        detail="a required setting is absent",
                        remediation=f"atf config set {key}=<value>",
                        subject=key,
                    )
                )
        for key in document.duplicates():
            findings.append(
                Finding(
                    id=f"env-duplicate-{key}",
                    severity="warning",
                    title=f"{key} is assigned more than once",
                    detail="only one of the assignments takes effect; the others are ignored",
                    remediation="`atf config set` rewrites every occurrence; or delete the stale line",
                    subject=key,
                )
            )
        for key, value in sorted(values.items()):
            spec = envschema.spec_for(key)
            if spec is None:
                findings.append(
                    Finding(
                        id=f"env-unknown-{key}",
                        severity="info",
                        title=f"{key} is not a documented setting",
                        detail="accepted as-is — the CLI does not refuse keys it does not know",
                        remediation="check the spelling if the stack ignores it",
                        subject=key,
                    )
                )
                continue
            problem = spec.validate(value)
            if problem:
                severity = "error"
                findings.append(
                    Finding(
                        id=f"env-invalid-{key}",
                        severity=severity,
                        title=f"{key}: {problem}",
                        detail=envschema.describe(key),
                        remediation=f"atf config set {key}=<valid value>",
                        subject=key,
                    )
                )
        mode = self.store.mode()
        if mode and mode & 0o077:
            findings.append(
                Finding(
                    id="env-permissions",
                    severity="warning",
                    title=f"`.env` is readable by other users (mode {oct(mode)})",
                    detail="the file carries the bot token and the master key",
                    remediation="chmod 600 .env — the next `atf config set` also tightens it",
                    subject=path,
                )
            )
        groups: dict[str, int] = {}
        for key in values:
            spec = envschema.spec_for(key)
            group = spec.group if spec else "undocumented"
            groups[group] = groups.get(group, 0) + 1
        return Report(tuple(findings), len(values), path, mode, groups)

    # ---------------------------------------------------------------- backups
    def backups(self) -> list[Any]:
        return self.store.backups()

    def restore(self, name: str) -> _payload.Outcome:
        result = self.store.restore(name)
        return _payload.Outcome(
            {
                "path": str(result.path),
                "restored": str(name),
                "backup": result.backup,
                "keys": result.keys,
            },
            f"restored {name} into {result.path}",
        )


def _masked(key: str) -> bool:
    """A value whose name says it is a credential never enters a report."""
    return envschema.is_masked(key)


__all__ = ["Change", "Environment", "MAX_PROBLEMS", "Plan", "Report"]
