# Troubleshooting — Go Worker + Telegram Runtime

## "Messages stopped being delivered"

1. Which engine is consuming? `ATF_WORKER_ENABLED` decides.
   * Unset/`0` → the **Python Core's** `DurableQueueManager` loop — its logs
     say `Durable queue started with N workers`.
   * Set → the **Go Worker** — its logs say `worker starting`, and the Core
     logs `ATF_WORKER_ENABLED is set — ... standing down`.
   * **Both running** is a configuration error, not a feature. Exactly one
     may consume.
2. Is the worker healthy? `curl <worker>:8090/healthz` — 503 means the
   delivery store is unreachable.
3. Is the queue moving? `SELECT status, COUNT(*) FROM delivery_jobs GROUP BY status;`
   * A growing `PENDING` with an idle worker → the worker cannot claim
     (check its log for `claim failed`).
   * A growing `CLAIMED` → dead leases; they expire, but raise
     `ATF_QUEUE_LEASE_SECONDS` if dispatch latency is near the lease.
   * A growing `UNKNOWN` → posts may or may not have landed; **do not
     re-dispatch blindly**. See `queue-recovery.md`.

## "A message was delivered twice"

The system provides at-most-once for a single logical send, and at-least-once
for a send that never reached Telegram. A duplicate means one of:

* the fence was bypassed (a path that sends without calling `FenceClaim`
  first — there should be none);
* the fence key diverged (the Go and Python key formats must be
  byte-identical: `rule:source_chat:source_message:target[:kind]`);
* an UNKNOWN delivery was re-dispatched manually without checking the
  target.

Check `delivery_attempts` for the logical send:

```sql
SELECT status, worker_id, target_message_id, last_error
FROM delivery_attempts WHERE id = '<rule>:<src_chat>:<src_msg>:<target>';
```

## "ATF_CORE_RPC_TOKEN is empty" at startup

The fail-closed secret gate. Set the token to the same value the Go API uses
against the Core. **Do not** work around it by allowing an empty token — an
unauthenticated internal RPC lets any caller on the network drive Telegram
sessions.

## Filter decisions changed after enabling the worker

The Go filter engine is a parity port of Python's `FilterEngine`; the
reason strings (`blacklist_keyword`, `regex_block:...`, `whitelist_mismatch`,
...) are identical. If a task's filter starts deciding differently:

* run `worker/internal/engine/filter` tests (the parity suite);
* check the reason string in the worker's log — it names the exact gate;
* confirm the *job payload's* media classification matches what the Python
  pipeline recorded (the filter reads `media_type` from the payload, not
  from a fresh read).

## Compile / build problems

| Error | Fix |
|---|---|
| `worker/proto/gen` stale | regenerate (CI checks this): `protoc` commands in `worker/README.md` |
| `no required module provides package ...` | `cd worker && go mod tidy` — the worker is its own module |
| `sqlite3-binding.c` warnings on build | cosmetic; the driver is CGO (same as any cgo sqlite build) |

## Logs: what you will and will not see

You will see: `correlation_id`, `job_id`, `rule_id`, chat ids, message ids,
session ids **truncated to 8 characters**, error classifications.

You will never see: message text, session strings, api hashes, bot tokens,
passwords, 2FA codes. Not because they are filtered, but because they never
enter the worker process at all.
