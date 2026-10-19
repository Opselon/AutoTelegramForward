"""The log archive: query, tail, export, purge, and the collection policy.

Two decisions worth stating, because both were bugs before:

* every surface builds the *same* :class:`LogQuery`, so `export` cannot quietly
  lose the level or service filter that `query` honours;
* purging without an explicit horizon is refused. The API defaults to "older than
  24h", which is a reasonable default for a UI and a terrible one for a script —
  the CLI says what it is about to delete.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ...domain.errors import UsageError
from ...domain.logs import LogPage, LogPolicyView, LogQuery, LogStats, canonical_level
from ...domain.validators import LEVELS
from . import _payload


class Logs:
    """Use cases over the central log archive."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway

    # ------------------------------------------------------------------ queries
    def query(self, query: LogQuery) -> LogPage:
        return self.gateway.query(query)

    def stats(self, *, rule_id: str = "", hours: int = 0) -> LogStats:
        return self.gateway.stats(rule_id=rule_id, hours=hours)

    def export(self, query: LogQuery, *, fmt: str = "json") -> bytes:
        if fmt not in ("json", "csv"):
            raise UsageError(
                f"unknown export format {fmt!r}",
                hint="formats: json, csv",
            )
        return self.gateway.export(query, fmt=fmt)

    def purge(self, query: LogQuery, *, older_than_days: int) -> _payload.Outcome:
        if older_than_days <= 0:
            raise UsageError(
                "purge needs a horizon",
                hint="pass --older-than-days <n>; 0 would delete everything including today's evidence",
            )
        document = _payload.mapping(self.gateway.purge(query, older_than_days=older_than_days))
        purged = document.get("purged", "?")
        remaining = document.get("remaining", "?")
        return _payload.Outcome(
            payload=document,
            detail=f"purged {purged} record(s), {remaining} remaining (older than {older_than_days}d)",
        )

    # ------------------------------------------------------------------- policy
    def policy(self) -> LogPolicyView:
        return self.gateway.policy()

    def set_policy(
        self,
        *,
        levels: Sequence[str] | None = None,
        verbose_rate: int | None = None,
        diagnostic_seconds: int | None = None,
        disable_diagnostics: bool | None = None,
    ) -> _payload.Outcome:
        """Change what atf-core collects. Admin-only server-side, and audited.

        The request body is deliberately exact: the handler decodes with
        ``DisallowUnknownFields`` and refuses a body with no field set, so the
        mapping from friendly flag to wire name lives here and nowhere else.
        """
        changes: dict[str, Any] = {}
        if levels is not None:
            wanted = [canonical_level(item) for item in levels if item]
            unknown = [item for item in wanted if item not in LEVELS]
            if unknown:
                raise UsageError(
                    f"unknown log level(s): {', '.join(unknown)}",
                    hint=f"levels: {', '.join(LEVELS)}",
                )
            if not wanted:
                raise UsageError("--levels needs at least one level", hint=f"e.g. --levels {LEVELS[2]},{LEVELS[5]}")
            changes["levels"] = wanted
        if verbose_rate is not None:
            if verbose_rate < 0:
                raise UsageError("--verbose-rate cannot be negative", hint="0 means no ceiling")
            changes["verbose_rate_per_second"] = verbose_rate
        if diagnostic_seconds is not None:
            if diagnostic_seconds <= 0:
                raise UsageError(
                    "--diagnostic-seconds must be positive",
                    hint="the window closes itself; an unbounded one is how a box fills up",
                )
            changes["diagnostic_seconds"] = diagnostic_seconds
        if disable_diagnostics is not None:
            changes["disable_diagnostics"] = bool(disable_diagnostics)
        if not changes:
            raise UsageError(
                "no policy field supplied",
                hint="pass --levels, --verbose-rate, --diagnostic-seconds or --disable-diagnostics",
            )
        if changes.get("diagnostic_seconds") and changes.get("disable_diagnostics"):
            # The API refuses this pair too; catching it here names the conflict
            # without spending a round trip on it.
            raise UsageError(
                "--diagnostic-seconds and --disable-diagnostics contradict each other",
                hint="ask for a window, or for diagnostics off — not both",
            )
        document = _payload.mapping(self.gateway.update_policy(changes))
        applied = ", ".join(f"{key}={value}" for key, value in changes.items())
        return _payload.Outcome(document, f"collection policy updated: {applied}")

    def health(self) -> Mapping[str, Any]:
        return self.gateway.health()

    def diagnostics(self, action: str, *, seconds: int = 0) -> _payload.Outcome:
        if action not in ("start", "stop"):
            raise UsageError(f"unknown diagnostics action {action!r}", hint="actions: start, stop")
        if action == "start" and seconds <= 0:
            raise UsageError(
                "diagnostics need a bounded window",
                hint="pass --seconds <n> (the window closes itself)",
            )
        document = _payload.mapping(self.gateway.diagnostics(action, seconds=seconds))
        if action == "start":
            expires = document.get("expires_at") or document.get("expires")
            detail = f"verbose collection on for {seconds}s"
            if expires:
                detail += f" (until {expires})"
        else:
            detail = "verbose collection off"
        return _payload.Outcome(document, detail)

    # -------------------------------------------------------------------- values
    @staticmethod
    def level_names() -> Sequence[str]:
        return LEVELS

    @staticmethod
    def canonical_level(text: str) -> str:
        return canonical_level(text)
