"""Circuit Breaker for AI Providers.

Implements the standard Closed -> Open -> Half-Open state machine to protect
system resources, prevent cascading failures, and support controlled failover
to secondary providers.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

from ...domain.value_objects import CircuitState

logger = logging.getLogger(__name__)


class ProviderCircuit:
    """Tracks state and failure metrics for a single AI provider/model configuration."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        half_open_max_probes: int = 1,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_probes = half_open_max_probes

        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._last_failure_time: float = 0.0
        self._last_state_change: float = time.time()
        self._total_requests: int = 0
        self._total_failures: int = 0
        self._tripped_count: int = 0

    @property
    def state(self) -> CircuitState:
        """Returns the current effective circuit state (accounting for recovery timeout)."""
        if self._state == CircuitState.OPEN:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                logger.info(
                    "Circuit %s: Recovery timeout (%.1fs) elapsed. Transitioning OPEN -> HALF_OPEN.",
                    self.name,
                    elapsed,
                )
                self._state = CircuitState.HALF_OPEN
                self._consecutive_successes = 0
                self._last_state_change = time.time()
        return self._state

    def can_execute(self) -> bool:
        """Determines if a request is allowed to proceed."""
        current_state = self.state
        if current_state == CircuitState.CLOSED:
            return True
        if current_state == CircuitState.HALF_OPEN:
            return True
        return False

    def record_success(self) -> None:
        """Records a successful execution."""
        self._total_requests += 1
        self._consecutive_failures = 0
        if self._state == CircuitState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.half_open_max_probes:
                logger.info(
                    "Circuit %s: Probe succeeded. Transitioning HALF_OPEN -> CLOSED.",
                    self.name,
                )
                self._state = CircuitState.CLOSED
                self._consecutive_successes = 0
                self._last_state_change = time.time()

    def record_failure(self, is_transient: bool = True) -> None:
        """Records a failed execution."""
        self._total_requests += 1
        self._total_failures += 1
        self._last_failure_time = time.time()
        self._consecutive_failures += 1

        if self._state == CircuitState.CLOSED:
            if self._consecutive_failures >= self.failure_threshold:
                logger.warning(
                    "Circuit %s: Failure threshold (%d) reached. Tripping CLOSED -> OPEN.",
                    self.name,
                    self.failure_threshold,
                )
                self._state = CircuitState.OPEN
                self._tripped_count += 1
                self._last_state_change = time.time()
        elif self._state == CircuitState.HALF_OPEN:
            logger.warning(
                "Circuit %s: Probe failed in HALF_OPEN. Tripping back to OPEN.",
                self.name,
            )
            self._state = CircuitState.OPEN
            self._tripped_count += 1
            self._last_state_change = time.time()

    def reset(self) -> None:
        """Manually resets the circuit to CLOSED."""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_failure_time = 0.0
        self._last_state_change = time.time()
        logger.info("Circuit %s: Manually reset to CLOSED.", self.name)

    def trip(self) -> None:
        """Manually trips the circuit to OPEN."""
        self._state = CircuitState.OPEN
        self._last_failure_time = time.time()
        self._tripped_count += 1
        self._last_state_change = time.time()
        logger.warning("Circuit %s: Manually tripped to OPEN.", self.name)

    def get_metrics(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "total_requests": self._total_requests,
            "total_failures": self._total_failures,
            "tripped_count": self._tripped_count,
            "last_failure_time": self._last_failure_time,
            "recovery_timeout": self.recovery_timeout,
        }


class CircuitBreakerRegistry:
    """Registry maintaining per-provider circuit breakers with failover support."""

    def __init__(
        self,
        default_failure_threshold: int = 3,
        default_recovery_timeout: float = 30.0,
    ) -> None:
        self.default_failure_threshold = default_failure_threshold
        self.default_recovery_timeout = default_recovery_timeout
        self._circuits: Dict[str, ProviderCircuit] = {}

    def get_circuit(self, provider_id: str) -> ProviderCircuit:
        if provider_id not in self._circuits:
            self._circuits[provider_id] = ProviderCircuit(
                name=provider_id,
                failure_threshold=self.default_failure_threshold,
                recovery_timeout=self.default_recovery_timeout,
            )
        return self._circuits[provider_id]

    def select_healthy_provider(
        self,
        primary_id: str,
        secondary_id: Optional[str] = None,
    ) -> Tuple[Optional[str], bool]:
        """Returns (selected_provider_id, is_failover).

        If primary circuit is available -> (primary_id, False).
        If primary is OPEN and secondary is available -> (secondary_id, True).
        If both are OPEN -> (None, False).
        """
        primary_circuit = self.get_circuit(primary_id)
        if primary_circuit.can_execute():
            return primary_id, False

        if secondary_id:
            secondary_circuit = self.get_circuit(secondary_id)
            if secondary_circuit.can_execute():
                logger.info(
                    "Primary circuit %s is OPEN. Failing over to secondary %s.",
                    primary_id,
                    secondary_id,
                )
                return secondary_id, True

        logger.warning(
            "Primary circuit %s is OPEN and no healthy secondary available (secondary=%s).",
            primary_id,
            secondary_id,
        )
        return None, False

    def reset_all(self) -> None:
        for circuit in self._circuits.values():
            circuit.reset()

    def get_all_metrics(self) -> dict:
        return {name: c.get_metrics() for name, c in self._circuits.items()}
