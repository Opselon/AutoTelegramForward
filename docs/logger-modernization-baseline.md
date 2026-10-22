# Logger modernization — baseline record

A factual snapshot taken before the Go producer-policy work on this branch. Every
line here is an observed command result, not an inference. Later phases append to
this file; nothing is edited to make history look tidier than it was.

## Repository state at the start of this branch

| Fact | How it was observed |
| --- | --- |
| Branch `master`, HEAD `608f605` (PR #90, "enforce the collection policy at the client door") | `git log --oneline -6`, `git rev-parse --abbrev-ref HEAD` |
| No open pull requests | `gh pr list --state open` → empty |
| Working tree clean except untracked `rust/` and other agents' trees | `git status --porcelain` |
| CI green on HEAD | `gh run list -L 6` |

## Rust Logger: the four states, kept separate

| State | Observed value | Command |
| --- | --- | --- |
| Source exists | Partially — 17 `.rs` files on disk | `find rust -name '*.rs' \| wc -l` |
| Source is tracked in git | **No** — 0 files | `git ls-files rust \| wc -l` |
| Compiles | **Unverified** — no host toolchain, no completed build output | `which cargo rustc` → nothing |
| Tests pass | **Unverified** — never observed | — |
| Binary runs | **No binary exists** | `ls rust/logger/target/release/atf-logger` → not found |

`rust/logger/Cargo.lock` exists, which proves a dependency resolution ran at some
point, and nothing more. A background build agent is still running as this record
is written; its result is not assumed here. **A source tree that is absent from
git, has never been observed compiling, and ships no binary is not an
implementation**, and this record does not treat it as one.

## Where `api/http` INFO rows actually come from

Not from a text match — from the two call sites that build a `LogEntry`:

| Emission path | File:line | When it fires | Level |
| --- | --- | --- | --- |
| `AccessLogger` | `api/internal/middleware/middleware.go:130` | **every** non-probe request | INFO (2xx/3xx), WARN (4xx), ERROR (5xx) |
| `ErrorLogger` | `api/internal/middleware/middleware.go:187` | responses ≥ 400 only | WARN/ERROR |

`grep -rn "\.Log(ctx" --include="*.go" api` returns exactly these two sites.
`main.go:158` constructs the sink with `Client: clients.Logs` and **no policy**,
which is why every successful request was persisted: the Logger stores whatever it
is handed, so "INFO is not collected by default" was not true of this service.

## The #88 / #90 guarantees, re-verified against the files

`core/application/log_policy.py` was read in full and the guarantees hold in the
code, not only in the PR description: `WARN,ERROR,CRITICAL` are the defaults;
`WARNING→WARN`, `ERR→ERROR`, `FATAL→CRITICAL`, `NOTICE→INFO`; an unknown level
becomes `INFO` and is never upgraded; `transform`-free `is_security_relevant`
keeps `auth.`/`security.`/`tenant.`/`admin.`/`permission.` prefixes and the eight
reason codes exempt from threshold and rate limit; the diagnostic window clamps to
at least one second and expires from the injected clock; `suppressed_level` and
`suppressed_rate` are separate from any delivery counter; the durable audit write
in `pipeline.py` shares no gate with the log emission.

## Defects and gaps open at the start of this branch

1. **No Go producer policy** (fixed on this branch) — the residual `api/http`
   INFO stream, measured live as 28 rows in 4 minutes.
2. **`LogPolicy.snapshot()` is not reachable** by any operator: no API route, no
   dashboard view, no bot control. A diagnostic window can only be opened by
   setting an environment variable and restarting.
3. **No Rust Logger.** See the table above.
4. **No PostgreSQL backend.** SQLite only (`logger/main.py`), so "both backends
   first-class" is unmet.
5. **No forwarding lifecycle events** for FloodWait, retry attempts, album
   aggregation, media download/upload, dead-letter transitions or end-to-end
   latency: Core has one `.log()` call site, and `_audit()` names only
   `forward.dropped/filtered/enqueued/enqueue_failed`.
6. **Configuration has no owner.** Each producer reads its own environment, so
   there is no persisted effective policy, no version, no propagation and no
   staleness detection. Documented as a limitation, not presented as a feature.

## Measured baseline (historical, not re-run here)

`scripts/bench/logger_baseline.py` reported ≈1,150 events/s saturation at p50
≈215 ms against the Python Logger with commit-per-insert and no admission control.
These numbers are from an earlier session and are **not** re-confirmed on this
branch; the benchmark that produced them is in the tree and re-runnable.

## PR-1 additions on this branch (structured context + recurring-error signature)

Additive only — every migration is `ADD COLUMN ... DEFAULT ''` (SQLite) or
`ADD COLUMN IF NOT EXISTS` (PostgreSQL); no renumbering, no rebuild, and the
legacy archive tests still open a Python-built v18 archive and migrate it in
place.

- **Schema v19**: `logs.request_id`, `logs.trace_id`, `logs.module`,
  `logs.fingerprint` (text, `''` default) + two equality indexes
  (`idx_logs_trace`, `idx_logs_fingerprint`).
- **Wire**: `LogEntry` grows fields 10–13; `QueryLogsRequest` grows exact-match
  filters 14–16 (indexed equality, never substring); `LogStatsRequest.include_errors`
  is opt-in; `LogStatsResponse.error_groups` carries the new `ErrorGroup`
  message. `proto/autoforward.proto` mirrors all of it.
- **Fingerprint**: computed by the engine at ingest (`ingest::fingerprint_of`,
  FNV-1a → 16 hex chars: birthday-safe far beyond this project's error count)
  over the *redacted and final* record shape — category + module + number-masked
  message. Severity and service are **reported** from the newest occurrence, not
  partitioned (the engine's pre-existing aggregate contract). Producer numbers,
  UUIDs and hex runs are masked so "quota for user 42" and "43" share a group;
  ids used as lookup keys are never part of the hash. The field is never read
  from the wire: the caller has no say in what counts as "the same failure".
- **Aggregate budget**: `MAX_ERROR_GROUPS = 50`, `MAX_ERROR_SCAN_ROWS = 10_000` —
  a window over the newest matching rows, never a full-archive scan, in both
  dialects; legacy rows (`fingerprint = ''`) are excluded so one giant fiction
  of a group cannot be manufactured from pre-v19 history.

Everything remaining is under PR-3 (see `docs/logger-hardening-audit.md`).

## PR-2 additions (capability-scoped authentication)

- **One gate per capability, one secret per gate**:
  `ATF_LOGGER_INGEST_TOKEN` / `ATF_LOGGER_READ_TOKEN` / `ATF_LOGGER_ADMIN_TOKEN`
  on the logger; empty = that scope open (previous behaviour, deployment picks,
  documented trade-off: an unattended rotation must not lock producers out).
- **Enforcement**: inside each RPC before the body is read — `Log` = ingest,
  `QueryLogs`/`LogStats` = read, `PurgeLogs` = admin; unknown method names
  default to admin (deny-by-default for existing tokens). Absent/invalid
  credential → `UNAUTHENTICATED`; a valid credential for a different capability
  → `PERMISSION_DENIED` (never escalate, never turn into a master key).
- **Comparison**: constant-time equality (`Failing lengths differ` is
  observable only in a length-timing sense the wire already carries); dotenv
  whitespace padding is trimmed so folded yaml cannot rot a secret into a
  permanent lockout. Never logged, never a metric label.
- **Observability**: `atf_logger_auth_failures_total{outcome}` with
  `(ingest|read|admin)_(missing|malformed|mismatch|forbidden)` = 12 bounded
  series; auth-refused RPCs also land in `rpc_status_total` as
  UNAUTHENTICATED/PERMISSION_DENIED while `events_received` is NOT incremented
  (a refused write never pretends to have arrived).
- **Producers**: Python `LogClient(token=…)` (env `ATF_LOGGER_INGEST_TOKEN`,
  wired in `core/main.py`) attaches `authorization: Bearer …` per call; Go API
  dials via `grpcclient.DialLogger` and picks the token by method through
  `LoggerAuthUnary/StreamClientInterceptor` — the shared `ATF_CORE_RPC_TOKEN`
  is still *never* sent to the logger (different trust domain, unchanged).
- **Compat**: both consumers send nothing when nothing is configured; every
  existing e2e/binary test path is unchanged with default config.
