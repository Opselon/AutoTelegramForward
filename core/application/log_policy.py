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

import math
import os
import threading
import time
from typing import Dict, Iterable, Mapping, Optional, Sequence

# Levels the contract collects by default. Everything else is verbose.
DEFAULT_LEVELS = ("CRITICAL", "ERROR", "WARN")
VERBOSE_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS")
ALL_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARN", "ERROR", "CRITICAL")

# Bounds for a mutation. A policy endpoint is the one place where a single typo
# can turn noise into an outage, so the limits live here — in the process that
# has to live with the value — and not only in the caller. The diagnostic window
# shares the control plane's ceiling: an unbounded "debug mode" is just
# "log everything, forever" with a friendlier name.
MAX_VERBOSE_RATE = 100000.0
MIN_DIAGNOSTIC_SECONDS = 1
MAX_DIAGNOSTIC_SECONDS = 3600

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


class LogPolicyError(ValueError):
    """A rejected policy change. The message is safe to show an operator."""


def validate_levels(raw) -> Sequence[str]:
    """Parse a level set strictly: an unknown name is an error, not INFO.

    `normalize_level` maps anything it does not recognise to INFO, which is the
    right answer for an event that already exists. It is the wrong answer for a
    change request: `WARNN` would silently start collecting every INFO event —
    the exact high-volume path the default policy exists to keep quiet. So the
    mutation path refuses what it cannot understand instead of guessing.
    """
    if isinstance(raw, str):
        pieces: Iterable[object] = raw.replace(";", ",").split(",")
    else:
        pieces = list(raw or [])
    levels = []
    for piece in pieces:
        name = str(piece or "").strip()
        if not name:
            continue
        canonical = _LEVEL_ALIASES.get(name.upper(), name.upper())
        if canonical not in ALL_LEVELS:
            raise LogPolicyError(
                f"unknown level {name!r}; expected one of {', '.join(ALL_LEVELS)}"
            )
        if canonical not in levels:
            levels.append(canonical)
    if not levels:
        raise LogPolicyError(
            "at least one level must be collected; to silence a level, leave it "
            f"out of the set (default is {', '.join(DEFAULT_LEVELS)})"
        )
    # Canonical order, so the reported policy does not depend on request order.
    return tuple(l for l in ALL_LEVELS if l in levels)


def validate_verbose_rate(raw) -> float:
    """A finite, non-negative events-per-second ceiling; 0 means no limit."""
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        raise LogPolicyError(f"verbose_rate_per_second must be a number, got {raw!r}") from None
    if math.isnan(rate) or math.isinf(rate) or rate < 0:
        raise LogPolicyError(
            f"verbose_rate_per_second must be a finite number >= 0, got {raw!r}"
        )
    if rate > MAX_VERBOSE_RATE:
        raise LogPolicyError(
            f"verbose_rate_per_second must be <= {MAX_VERBOSE_RATE:g}, got {rate:g}"
        )
    return rate


def validate_diagnostic_seconds(raw) -> int:
    """A bounded window length. 0 is not a window; callers use it as "unchanged"."""
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        raise LogPolicyError(f"diagnostic_seconds must be an integer, got {raw!r}") from None
    if seconds < MIN_DIAGNOSTIC_SECONDS or seconds > MAX_DIAGNOSTIC_SECONDS:
        raise LogPolicyError(
            "diagnostic_seconds must be between "
            f"{MIN_DIAGNOSTIC_SECONDS} and {MAX_DIAGNOSTIC_SECONDS}, got {seconds}"
        )
    return seconds


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
        # Mutations take this lock; readers (`allow`, `snapshot`) do not. Every
        # field a mutation writes is rebound to an immutable value, so a
        # concurrent decision sees either the whole old policy or the whole new
        # one, never a half-applied one — which is why the hot path stays
        # lock-free. The lock is held only so two concurrent changes cannot
        # interleave and so a response cannot describe a state that a second
        # writer has already replaced.
        self._mutex = threading.RLock()
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
    # mutation
    # ------------------------------------------------------------------ #
    def reconfigure(
        self,
        levels: Optional[Iterable[str]] = None,
        verbose_rate_per_second: Optional[float] = None,
        diagnostic_seconds: Optional[int] = None,
        disable_diagnostics: bool = False,
    ) -> Dict[str, object]:
        """Apply a validated policy change in one step; report what now holds.

        Raises `LogPolicyError` *before* touching anything when a field is out of
        bounds, so a rejected request is a no-op rather than a partial change.
        The caller gets the policy that is in force afterwards, read back from
        this same instance, so what is reported is what will be enforced.
        """
        new_levels = None if levels is None else frozenset(validate_levels(levels))
        new_rate = (
            None if verbose_rate_per_second is None else validate_verbose_rate(verbose_rate_per_second)
        )
        window = (
            None
            if not diagnostic_seconds
            else validate_diagnostic_seconds(diagnostic_seconds)
        )
        if window and disable_diagnostics:
            raise LogPolicyError(
                "diagnostic_seconds and disable_diagnostics are mutually exclusive: "
                "one request cannot both open and close a diagnostic window"
            )

        with self._mutex:
            if new_levels is not None:
                self._levels = new_levels
            if new_rate is not None:
                self._verbose_rate = new_rate
            if window:
                self.enable_diagnostics(window)
            elif disable_diagnostics:
                self.disable_diagnostics()
            # Read the answer inside the same critical section: outside it, a
            # concurrent change could make the response describe a policy this
            # process is no longer enforcing.
            return self.snapshot()

    def set_levels(self, levels: Iterable[str]) -> Dict[str, object]:
        """Change only which levels are collected."""
        return self.reconfigure(levels=levels)

    def set_verbose_rate(self, verbose_rate_per_second: float) -> Dict[str, object]:
        """Change only the verbose-events rate ceiling (0 = no limit)."""
        return self.reconfigure(verbose_rate_per_second=verbose_rate_per_second)

    # ------------------------------------------------------------------ #
    # durability
    # ------------------------------------------------------------------ #
    def export_state(self) -> Dict[str, object]:
        """The part of the policy a restart must not forget.

        Counters are deliberately absent: they describe this process's runtime,
        not the policy, and restoring them would invent history. The diagnostic
        window is exported as its wall-clock expiry, because that is the value
        that has a meaning outside this process's uptime.
        """
        with self._mutex:
            active = self.diagnostic_active()
            return {
                "configured_levels": [l for l in ALL_LEVELS if l in self._levels],
                "verbose_rate_per_second": self._verbose_rate,
                "diagnostic_until": int(self._diagnostic_until_wall) if active else 0,
            }

    def import_state(self, state: Optional[Mapping[str, object]]) -> bool:
        """Restore a previously exported policy. Returns whether anything applied.

        A diagnostic window is restored with the *remaining* time, never the
        original length: a restart must not extend a bounded window, and one that
        has already expired must not come back at all. An unreadable state is
        ignored rather than fatal — the environment defaults are the conservative
        answer, and refusing to start over a settings row is not.
        """
        if not isinstance(state, Mapping):
            return False
        applied = False
        with self._mutex:
            try:
                levels = state.get("configured_levels")
                if levels:
                    self._levels = frozenset(validate_levels(levels))
                    applied = True
                if state.get("verbose_rate_per_second") is not None:
                    self._verbose_rate = validate_verbose_rate(
                        state.get("verbose_rate_per_second")
                    )
                    applied = True
                raw_until = state.get("diagnostic_until") or 0
                try:
                    until = int(float(str(raw_until)))
                except (TypeError, ValueError):
                    until = 0
                if until > 0:
                    remaining = until - int(time.time())
                    if remaining > 0:
                        self.enable_diagnostics(min(remaining, MAX_DIAGNOSTIC_SECONDS))
                        applied = True
            except LogPolicyError:
                # A stored value that no longer validates (an older build wrote
                # it, or someone edited the row) is ignored, not fatal: whatever
                # did apply stays, and the rest keeps the environment default.
                return applied
        return applied

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
