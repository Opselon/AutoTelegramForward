"""Lease-based coordination for the Core's singleton loops.

The primitives are the same ones the Go API uses, with the same Lua, so a
deployment can reason about one lock implementation rather than two:

  acquire  SET key <random token> NX PX <ttl>
  renew    Lua: if GET key == token then PEXPIRE key ttl; return 1 else 0
  release  Lua: if GET key == token then DEL key; return 1 else 0

The token check is not decoration. Without it, a process that stalled past its
TTL would renew — or worse, DELETE — a lease another replica has since acquired,
and the lock would silently stop excluding anything.

Usage, which is the only shape callers need:

    coord = LeaseCoordinator.from_config(cfg)
    await coord.connect()
    ...
    if await coord.try_hold("queue-watchdog", ttl=30.0):
        ...            # one replica at a time
    ...
    await coord.close()

`try_hold` is deliberately a boolean, not an exception: "another replica is
already doing this" is the normal outcome for every replica but one, and a
caller that retries in a loop turns a lock into a stampede.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Namespaced under the same prefix the API's limiter uses, so one Redis can host
# both without a key ever resolving to the same name.
DEFAULT_PREFIX = "atf:rl:"

# The acquire script: SET NX PX is atomic on its own, so only renewal and
# release need Lua (they are read-compare-write).
_LEASE_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""

_LEASE_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class Lease:
    """A held lease. Never construct one directly: ask the coordinator."""

    def __init__(self, coordinator: "LeaseCoordinator", name: str, token: str, ttl: float) -> None:
        self._coordinator = coordinator
        self.name = name
        self.token = token
        self.ttl = ttl
        self.acquired_at = time.time()
        self.lost = False

    @property
    def key(self) -> str:
        return self._coordinator.key_for(self.name)

    async def renew(self) -> bool:
        """Extend the lease. False means someone else owns it now."""
        if self.lost:
            return False
        try:
            renewed = await self._coordinator._eval(
                _LEASE_RENEW, [self.key], [self.token, int(self.ttl * 1000)]
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised upwards
            logger.warning("lease %s: renew failed: %s", self.name, exc)
            self.lost = True
            return False
        if int(renewed or 0) != 1:
            self.lost = True
            return False
        return True

    async def release(self) -> bool:
        """Give the lease back. Only the owner can: the token is checked."""
        try:
            released = await self._coordinator._eval(
                _LEASE_RELEASE, [self.key], [self.token]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("lease %s: release failed (the TTL will free it): %s", self.name, exc)
            return False
        return int(released or 0) == 1


class LeaseCoordinator:
    """Holds leases for the Core's singleton loops.

    One connection pool for the whole process, bounded timeouts, and no
    exception ever escaping a lease operation: coordination is an optimisation,
    and an optimisation that can take the Core down is worse than no
    optimisation.
    """

    def __init__(
        self,
        url: str = "",
        prefix: str = DEFAULT_PREFIX,
        *,
        enabled: bool = True,
        socket_timeout: float = 1.0,
        connect_timeout: float = 1.0,
        max_connections: int = 8,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.url = (url or "").strip()
        self.prefix = prefix or DEFAULT_PREFIX
        self.enabled = bool(enabled) and bool(self.url)
        self.socket_timeout = socket_timeout
        self.connect_timeout = connect_timeout
        self.max_connections = max_connections
        self._log = log or logger
        self._client: Any = None
        self._leases: Dict[str, Lease] = {}
        self._warned = False
        self._available = False
        # A transient failure at connect time (DNS not ready, store still
        # starting) must not disable coordination for the process's lifetime:
        # try_hold re-probes the store at this interval, bounded so a dead
        # store is never hammered per call.
        self._reconnect_every = 30.0
        self._last_probe = 0.0

    # ---------------------------------------------------------------- setup
    @classmethod
    def from_config(cls, cfg: Any) -> "LeaseCoordinator":
        """Build from the canonical configuration object (never a second source)."""
        return cls(
            url=getattr(cfg, "redis_url", "") or "",
            prefix=getattr(cfg, "redis_prefix", DEFAULT_PREFIX) or DEFAULT_PREFIX,
            enabled=bool(getattr(cfg, "redis_coordination", True)),
            socket_timeout=float(getattr(cfg, "redis_timeout_seconds", 1.0) or 1.0),
        )

    @property
    def available(self) -> bool:
        """True when a lease can actually be taken from this process.

        When False the Core is single-replica by assumption and every caller
        runs its work (see the outage policy in the package docstring).
        """
        return self.enabled and self._available

    async def connect(self) -> bool:
        """Create the client and prove it answers. Never raises."""
        if not self.enabled:
            self._log.info("redis coordination disabled (no ATF_REDIS_ADDR configured)")
            return False
        try:
            import redis.asyncio as aioredis  # imported lazily: optional dependency
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "redis coordination unavailable (%s): the Core will assume it is the only replica",
                exc,
            )
            return False
        try:
            self._client = aioredis.from_url(
                self.url,
                socket_timeout=self.socket_timeout,
                socket_connect_timeout=self.connect_timeout,
                max_connections=self.max_connections,
                decode_responses=True,
                health_check_interval=15,
            )
            await self._client.ping()
            self._available = True
            self._log.info("redis coordination enabled (%s)", _redact(self.url))
        except Exception as exc:  # noqa: BLE001
            # The failed client is discarded so a later probe does not reuse a
            # socket pool nobody is maintaining.
            stale, self._client = self._client, None
            if stale is not None:
                with contextlib.suppress(Exception):
                    await stale.aclose()
            self._available = False
            self._log.warning(
                "redis coordination unreachable at %s (%s): the Core will assume it is the only replica",
                _redact(self.url),
                exc,
            )
        return self._available

    def _probe_due(self) -> bool:
        if time.monotonic() - self._last_probe < self._reconnect_every:
            return False
        self._last_probe = time.monotonic()
        return True

    async def close(self) -> None:
        for lease in list(self._leases.values()):
            with contextlib.suppress(Exception):
                await lease.release()
        self._leases.clear()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._client = None
        self._available = False

    # ------------------------------------------------------------- leasing
    def key_for(self, name: str) -> str:
        return f"{self.prefix}lock:{name}"

    async def _eval(self, script: str, keys: list, args: list):
        if self._client is None:
            raise RuntimeError("lease coordinator is not connected")
        return await self._client.eval(script, len(keys), *keys, *args)

    async def try_hold(self, name: str, ttl: float = 30.0) -> bool:
        """Hold `name` if it is free or already ours.

        Returns True when this process may run the work. A False is a normal
        answer, not an error, and never blocks.
        """
        if not self.available:
            # Re-probe a store that failed once (bounded, never per call): a
            # store that was briefly unreachable at startup must not leave
            # coordination off until the process is restarted.
            if self.enabled and self._probe_due():
                if await self.connect():
                    self._log.info("redis coordination recovered: leases are enforced again")
            if not self.available:
                # Single-replica assumption (or Redis down): run the work. See
                # the outage policy in the package docstring.
                if not self._warned and self.enabled:
                    self._log.warning(
                        "redis coordination is unavailable: singleton loops run on every replica"
                    )
                    self._warned = True
                return True

        held = self._leases.get(name)
        if held is not None:
            if not held.lost and await held.renew():
                return True
            # Lost or expired: drop it and try to take it back cleanly.
            self._leases.pop(name, None)

        token = secrets.token_hex(16)
        try:
            acquired = await self._client.set(self.key_for(name), token, nx=True, px=int(ttl * 1000))
        except Exception as exc:  # noqa: BLE001
            self._available = False
            self._log.warning("lease %s: acquire failed (%s): running without coordination", name, exc)
            return True
        if not acquired:
            return False
        self._leases[name] = Lease(self, name, token, ttl)
        return True

    @contextlib.asynccontextmanager
    async def leadership(self, name: str, ttl: float = 30.0):
        """Yield True when this process holds `name` for the block.

        The lease is released on exit even when the body raises, so a failing
        iteration cannot wedge the lock for the whole TTL.
        """
        acquired = await self.try_hold(name, ttl=ttl)
        try:
            yield acquired
        finally:
            lease = self._leases.pop(name, None)
            if lease is not None:
                await lease.release()

    # ------------------------------------------------------------ reporting
    def snapshot(self) -> Dict[str, Any]:
        """State for the status surface and for the tests (never a secret)."""
        return {
            "enabled": self.enabled,
            "available": self.available,
            "url": _redact(self.url) if self.url else "",
            "held": sorted(self._leases.keys()),
        }


def _redact(url: str) -> str:
    """Remove credentials from a URL-shaped address before it is logged."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://[REDACTED]@{host}" if scheme else f"[REDACTED]@{host}"


async def _await_or_none(coro):
    """Test helper: run a coroutine and return None on failure."""
    try:
        return await coro
    except Exception:  # noqa: BLE001
        return None


__all__ = ["Lease", "LeaseCoordinator", "DEFAULT_PREFIX"]

# Kept importable for tests that want to drive the loop without a coordinator.
asyncio  # noqa: B018 - re-exported for callers that only need asyncio semantics
