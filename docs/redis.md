# Redis: rate limiting and worker coordination

The optional shared Redis instance accelerates two things and owns nothing:

1. **The API boundary's rate limits** (`api/internal/ratelimit/`) — the Go
   service answers "may this request proceed?" with one round trip.
2. **The Core's worker coordination** (`core/infrastructure/redis/lease.py`) —
   when the stack runs more than one replica, the singleton maintenance loops
   (queue recovery/watchdog, channel polling) hold a lease so only one replica
   runs them at a time.

Everything durable stays where it was: user data, rules and the delivery queue
in PostgreSQL/SQLite (`delivery_jobs`' unique constraint and its claim/lease
columns remain the guarantee against duplicate Telegram delivery — a lease is a
work-saver, never a delivery receipt). A restarted Redis rebuilds its state
from the database without operator action; that is why the store is
deliberately un-persisted.

## Deployment

The stack ships with a `redis` service in `docker-compose.yml`: pinned image,
health-checked, internal-only networking (no published port), bounded memory
with `noeviction`, no persistence, `restart: unless-stopped`. Deployment wiring
is enforced by `scripts/tests/test_redis_compose_contract.py`.

The single deployment-facing variable is the address, which both consumers
read:

```env
ATF_REDIS_ADDR=redis:6379      # the compose service name
ATF_REDIS_PASSWORD=<openssl rand -hex 24>   # set one; never commit it
```

Resource ceiling and policy are configurable but the policy default
(`noeviction`) is deliberate: evicting a token bucket silently resets that
scope's allowance, which a client could provoke by inventing scopes. When the
store is full the limiter's per-class fail policy decides — visible, not
silent.

**Running without Redis.** A single-node install does not need it. Remove (or
do not start) the service **and** set `ATF_REDIS_ADDR=` (empty): the API falls
back to its per-process limits, the Core runs every loop un-coordinated.
Deleting the service while leaving the address set leaves the API pointing at
a name that no longer exists — and the authentication endpoints refuse
requests while their limit cannot be enforced. That is the documented
fail-closed posture of the `auth` class, not a bug.

### Startup, recovery, shutdown

* The API starts without waiting for Redis; if the store is unreachable the
  limiter marks itself degraded, exposes that on `/api/admin/ratelimit`, and a
  circuit breaker stops an outage from becoming an API-wide latency spike.
  The `auth` and `shield` classes fail **closed** during an outage; the
  authenticated classes fall back to the same budget enforced per-process.
* The Core connects before its coordinated loops start; an unreachable store
  logs a warning and the loops run un-coordinated.
* After a Redis restart both consumers reconnect on the next use. The Core
  re-acquires leases within their TTL; the limiter's counters start empty,
  which grants each scope its current allowance once — the store, not the
  client, decides correctness.

## Rate limiting at the API

Keys are derived from the **verified** identity only (JWT user id, or the
transport peer address for unauthenticated traffic) plus a class bucket taken
from the first two meaningful path segments — a caller can never move a
request into a larger budget by choosing a URL. `ATF_TRUSTED_PROXIES`
(never client-supplied) declares the reverse proxy whose `X-Forwarded-For`
may be honoured.

Default policies (per minute rate / burst, fail mode when the store is gone):

| class  | covers                              | budget    | fail mode |
|--------|-------------------------------------|-----------|-----------|
| auth   | login/register/auth-exchange        | 30 / 10   | closed    |
| shield | reachable without a verified token  | 600 / 120 | closed    |
| read   | authenticated GET                   | 600 / 120 | local     |
| write  | authenticated mutations             | 120 / 30  | local     |
| admin  | /api/admin/*                        | 300 / 60  | local     |
| probe  | /healthz, /api/version              | 1200 / 300| local     |

Algorithms: the **token bucket** (`ATF_RATELIMIT_ALGO=tb`, default) keeps an
exact burst ceiling in one atomic Lua script; the bucketed **sliding window**
(`sw`) trades that exactness for fewer keys (~10% allowance drift measured —
see `docs/benchmarks/redis-ratelimit.md` for the numbers and why `tb` won).
The exact sorted-set window exists (`swe`) as the correctness reference.

Operators can inspect the live state (effective policies, breaker, counters —
no keys, no addresses) at `GET /api/admin/ratelimit`.

## Worker coordination in the Core

`LeaseCoordinator` (and the Go `ratelimit.Lease`) implement the same pattern:
`SET key token NX PX <ttl>`, plus Lua scripts that check the token on renew
and release. Only the holder can renew or delete its lease; a lease that
expires frees the name for any replica; a stalled process that comes back
discovers it lost leadership instead of renewing blindly. In the Core:

* `DurableQueueManager` gates startup recovery and the periodic watchdog with
  the `queue-watchdog` lease.
* `ChannelPollingService` gates the channel sweep with the `channel-poller`
  lease.

Neither loop changes what is delivered — the queue's own state machine and
idempotency are untouched — only which replica performs the pass.

## Verification

```bash
# Go (miniredis always; real server when ATF_TEST_REDIS_ADDR is set)
cd api && ATF_TEST_REDIS_ADDR=127.0.0.1:6399 go test ./...
go test ./internal/ratelimit/ -race -bench . -benchmem

# Python (real server for the lease suites; skip without the variable)
ATF_TEST_REDIS_ADDR=127.0.0.1:6399 python -m pytest core/tests/test_redis_lease_coordination.py

# Deployment contract
python -m pytest scripts/tests/test_redis_compose_contract.py
docker compose config --quiet

# Live smoke: readiness, auth refusal on outage, recovery (see docs/benchmarks)
scripts/redis_smoke.sh
```

Benchmark methodology and results: `docs/benchmarks/redis-ratelimit.md`.
