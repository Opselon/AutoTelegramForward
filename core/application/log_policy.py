"""Producer-side log policy: which lifecycle events are allowed out, and how often.

The contract in `docs/logging-contract.md` says CRITICAL/ERROR/WARN are
collected by default, while INFO/SUCCESS/DEBUG/TRACE are not unless an
administrator enables them. The Python Logger has no server-side filter — it
stores whatever arrives — so until the Rust engine owns that filter, the policy
has to be enforced where the events are *produced*. Without this, every
successfully forwarded message writes an INFO row: exactly the high-volume happy
path the contract says must not be logged by default.

This module also implements the operational requirements around that policy:

* a **time-limited diagnostic mode** that turns on the verbose levels and expires
  on its own, so "enable debugging" cannot silently become permanent,
* a **rate limit for verbose events**, so switching diagnostics on under load
  cannot itself become the incident,
* **counters** that make suppression visible — "collection is off" must never be
  indistinguishable from "nothing happened", and
* a **security exemption**: authorization failures and cross-tenant attempts are
  emitted regardless of threshold, diagnostic state or rate limit. A severity
  threshold is a noise control, not a way to disarm auditing.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Iterable, Mapping, Optional, Sequence

# Levels the contract collects by default. Everything else is verbose.
DEFAULT_LEVELS = ("CRITICAL", "ERROR", "WARN")
VERBOSE_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS")
ALL_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARN", "ERROR", "CRITICAL")

# The legacy alias the contract promises to accept, normalized to WARN.
_LEVEL_ALIASES = {"WARNING": "WARN", "ERR": "ERROR", "FATAL": "CRITICAL", "NOTICE": "INFO"}

# Event types and reason codes that are never suppressed. Kept as prefixes/codes
# rather than free text so a typo cannot silently widen or narrow the exemption.
_SECURITY_EVENT_PREFIXES = ("auth.", "security.", "tenant.", "admin.", "permission.")
_SECURITY_REASON_CODES = frozenset({
    "UNAUTHORIZED", "FORBIDDEN", "PERMISSION_DENIED", "CROSS_TENANT",
    "TENANT_MISMATCH", "RULE_OWNER_MISMATCH", "ADMIN_REQUIRED", "TOKEN_INVALID",
})


def normalize_level(level: Optional[str]) -> str:
    """`warning` -> `WARN`; unknown names are treated as INFO, never upgraded."""
    name = str(level or "").strip().upper()
    if not name:
        return "INFO"
    name = _LEVEL_ALIASES.get(name, name)
    return name if name in ALL_LEVELS else "INFO"


def is_verbose(level: str) -> bool:
    return normalize_level(level) in VERBOSE_LEVELS


def is_security_relevant(event_type: str = "", reason_code: str = "") -> bool:
    """Does this event carry security/authorization meaning worth keeping?"""
    lowered = (event_type or "").strip().lower()
    if any(lowered.startswith(prefix) for prefix in _SECURITY_EVENT_PREFIXES):
        return True
    return (reason_code or "").strip().upper() in _SECURITY_REASON_CODES


def _parse_levels(raw: str) -> Sequence[str]:
    levels = []
    for piece in str(raw or "").replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        name = _LEVEL_ALIASES.get(piece.upper(), piece.upper())
        if name in ALL_LEVELS and name not in levels:
            levels.append(name)
    return tuple(levels)


class LogPolicy:
    """Decides whether one lifecycle event may be emitted, and counts the answer.

    Every decision is counted, including the negative ones: an operator asking
    "why is this quiet?" needs `suppressed_level` to say "the policy dropped 900
    INFO events", not silence.
    """

    def __init__(
        self,
        levels: Optional[Iterable[str]] = None,
        verbose_rate_per_second: float = 0,
        clock=time.monotonic,
    ) -> None:
        self._clock = clock
        self._levels = {normalize_level(l) for l in (levels or DEFAULT_LEVELS)}
        # 0 means "no limit". Applied to verbose levels only: a WARN/ERROR storm
        # is the signal, not the noise.
        self._verbose_rate = max(0.0, float(verbose_rate_per_second or 0))
        # The countdown uses the injected (monotonic) clock so tests and clock
        # jumps cannot lie about it; the wall-clock expiry is kept separately
        # purely because that is the value an administrator is shown.
        self._diagnostic_until = 0.0
        self._diagnostic_until_wall = 0.0
        self._diagnostic_levels: set = set()
        self._window_start = self._clock()
        self._window_count = 0
        # Security events bypass the rate limit too; they are never the thing you
        # want trimmed. Tracked separately so the counters stay honest.
        self._counters: Dict[str, int] = {
            "emitted": 0,
            "emitted_security": 0,
            "suppressed_level": 0,
            "suppressed_rate": 0,
            "diagnostic_activations": 0,
        }

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LogPolicy":
        """ATF_LOG_LEVELS, ATF_LOG_VERBOSE_RATE, ATF_LOG_DIAGNOSTIC_SECONDS."""
        env = os.environ if env is None else env

        levels = _parse_levels(env.get("ATF_LOG_LEVELS", "")) or DEFAULT_LEVELS

        try:
            rate = float(env.get("ATF_LOG_VERBOSE_RATE", "0") or 0)
        except (TypeError, ValueError):
            rate = 0.0

        policy = cls(levels=levels, verbose_rate_per_second=rate)
        try:
            seconds = int(env.get("ATF_LOG_DIAGNOSTIC_SECONDS", "0") or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            # Bounded by construction: the window is set from process start.
            policy.enable_diagnostics(seconds)
        return policy

    def enable_diagnostics(self, seconds: int, levels: Iterable[str] = VERBOSE_LEVELS) -> int:
        """Turn on verbose collection for `seconds`; returns the expiry epoch.

        Takes effect immediately and expires by itself, which is the whole point:
        nobody has to remember to turn it back off.
        """
        window = max(1, int(seconds))
        self._diagnostic_until = self._clock() + window
        self._diagnostic_until_wall = time.time() + window
        self._diagnostic_levels = {normalize_level(l) for l in levels}
        self._counters["diagnostic_activations"] += 1
        return int(self._diagnostic_until_wall)

    def disable_diagnostics(self) -> None:
        self._diagnostic_until = 0.0
        self._diagnostic_until_wall = 0.0
        self._diagnostic_levels = set()

    def diagnostic_active(self) -> bool:
        if self._diagnostic_until and self._clock() >= self._diagnostic_until:
            # Expired: clear it so the decision path is a plain set membership.
            self.disable_diagnostics()
        return bool(self._diagnostic_until)

    def effective_levels(self) -> Sequence[str]:
        levels = set(self._levels)
        if self.diagnostic_active():
            levels |= self._diagnostic_levels
        return tuple(l for l in ALL_LEVELS if l in levels)

    # ------------------------------------------------------------------ #
    # the decision
    # ------------------------------------------------------------------ #
    def allow(self, level: str, event_type: str = "", reason_code: str = "") -> bool:
        """May this event be emitted? Counts the answer either way."""
        name = normalize_level(level)
        security = is_security_relevant(event_type, reason_code)

        if not security and name not in self.effective_levels():
            self._counters["suppressed_level"] += 1
            return False

        if security:
            # Exempt from the rate limit as well: security evidence is low volume
            # by nature and is exactly what you must not lose to a burst.
            self._counters["emitted"] += 1
            self._counters["emitted_security"] += 1
            return True

        if self._verbose_rate > 0 and is_verbose(name):
            now = self._clock()
            if now - self._window_start >= 1.0:
                self._window_start = now
                self._window_count = 0
            if self._window_count >= self._verbose_rate:
                self._counters["suppressed_rate"] += 1
                return False
            self._window_count += 1

        self._counters["emitted"] += 1
        return True

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, object]:
        active = self.diagnostic_active()
        return {
            **self._counters,
            "levels": list(self.effective_levels()),
            "configured_levels": [l for l in ALL_LEVELS if l in self._levels],
            "diagnostic_active": active,
            "diagnostic_until": int(self._diagnostic_until_wall) if active else 0,
            "verbose_rate_per_second": self._verbose_rate,
        }
