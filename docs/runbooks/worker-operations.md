# Worker Operations — runbook

## Health

| Surface | URL | Meaning |
|---|---|---|
| Readiness | `http://<worker>:8090/healthz` | 200 only when the delivery store is reachable AND the loop is advancing |
| Liveness | `http://<worker>:8090/livez` | 200 while the loop is not stopping |
| Metrics | `http://<worker>:8090/metrics` | delivered / retried / failed / unknown / active counts |

Port 8090 is **internal-only**: the compose service deliberately publishes
nothing.

## The shared store: one file, one ownership constraint

The worker and the Python Core read and write **the same `atf.db`** — that
is the whole design (one queue, two engines). The Core creates the file as
**root**; the worker runs as **`atf` (uid 100, gid 101)**. Two things
follow:

1. **Prepare the volume once** before the first worker start, under the
   `worker` compose profile:

   ```bash
   docker compose --profile worker run --rm worker-store-init
   ```

   It grants group 101 write access to the store (idempotent — safe to
   re-run after any Core restart). This is the only deployment step that
   differs from a stock install, and it disappears with the PostgreSQL
   store in stage 2.
2. On startup the worker **proves the store is writable** (an immediate
   write transaction, rolled back). A worker that cannot write refuses to
   start rather than reporting `ready` while every claim fails — the
   fail-closed behaviour the health surface promises. If you see
   `probe write: attempt to write a readonly database`, re-run step 1.

## Turning the Go Worker on (the whole migration in one command)

```bash
# 1. The worker container must run (it is built but inert by default).
# 2. Set both variables in the environment:
export ATF_WORKER_ENABLED=1
export ATF_CORE_RPC_TOKEN=<the same shared secret the Go API uses>
# 3. Roll the Core and the Worker together:
docker compose -p autotelegramforward --env-file .env up -d
```

When the Core comes up with `ATF_WORKER_ENABLED` set, its
`DurableQueueManager` logs `[DURABLE_QUEUE] ATF_WORKER_ENABLED is set — the
Go Worker owns delivery execution; the Python queue loop is standing down`
and spawns **no worker tasks**. Enqueue keeps working exactly as before.

## Rolling back

```bash
unset ATF_WORKER_ENABLED
docker compose up -d
```

The Python loop resumes. Jobs still CLAIMED by the Go worker are reclaimed
when their leases expire (default 30 s). **The fence makes that handover
safe**: a job whose send the Go worker recorded as SENT is `already_sent`
for the Python loop too, because the fence key format is byte-identical.

## What a healthy deployment looks like

```
worker: {"status":"ready","state":"ready","delivered":N,"active":0..batch}
core:   [DURABLE_QUEUE] ATF_WORKER_ENABLED is set ... standing down
```

## What an unhealthy deployment looks like

| Symptom | Cause | Fix |
|---|---|---|
| `/healthz` 503, `store_error` | the delivery DB is unreachable | check the volume, the Core's own health, and `ATF_DB_PATH` |
| worker exit 1, `ATF_CORE_RPC_TOKEN is empty` | the fail-closed secret gate | set the token; never bypass it |
| worker exit 1, `could not bind` | port 8090 in use | `ATF_WORKER_LISTEN` |
| `fence sweep moved expired claims to UNKNOWN` (repeated) | the lease is shorter than dispatch latency | raise `ATF_QUEUE_LEASE_SECONDS` |
| `refused unauthenticated runtime rpc` | token mismatch between worker and runtime | both must use the same `ATF_CORE_RPC_TOKEN` |
| jobs stuck in `CLAIMED` | a worker died holding leases | normal — they expire and are reclaimed; the fence prevents double-sends |

## Logs

Structured JSON to stdout, collected by the same collector as every other
service. Every delivery line carries `correlation_id`, `job_id`, `rule_id`,
`target_chat_id`, `source_chat_id`, `source_message_id`. Session ids are
printed truncated (`first8***`). **Message content, session strings, api
hashes and tokens never appear in a worker log line** — they never enter the
process.

## Upgrading the contract

`worker/proto/telegram_gateway.proto` is the source of truth:

1. add optional fields / new RPCs only (never renumber, never remove);
2. regenerate both stubs (`worker/README.md` has the exact commands);
3. bump the `contract_major`/`contract_minor` in the health RPC when the
   surface changes;
4. CI verifies the committed stubs match a fresh `protoc` run.
