# Failure & Recovery — Go Worker

> The behaviour described here is implemented and tested: see
> `worker/internal/engine/delivery/delivery_test.go` for the exact test
> behind every claim.

## The error taxonomy (shared with Python, 19 codes)

| Code | Meaning | Go action |
|---|---|---|
| `FLOOD_WAIT` | Telegram demanded a wait (`retry_after_seconds` is authoritative) | **defer** for exactly that window — never spend an attempt |
| `RATE_LIMITED` | rate limited without a wait value | defer, backoff |
| `NETWORK_TIMEOUT` | response never arrived — **ambiguous** | `MarkUnknown`, never re-dispatch |
| `UNKNOWN_DELIVERY_OUTCOME` | Telegram reported nothing usable | `MarkUnknown` |
| `NETWORK_DISCONNECTED` | transport down | defer (environment, not the job) |
| `SESSION_UNAVAILABLE` | no live client / connecting | defer |
| `AUTHENTICATION_ERROR` | session cannot operate at all | **FAILED** — the retry loop cannot fix it |
| `PERMISSION_DENIED` | no write permission in the target | **FAILED** |
| `CHAT_NOT_FOUND` / `MESSAGE_NOT_FOUND` | the peer vanished | **FAILED** |
| `FORWARDING_RESTRICTED` | the source forbids it | copy/re-upload **only if** `fallback_enabled`, else FAILED |
| `MEDIA_DOWNLOAD_FAILED` / `MEDIA_UPLOAD_FAILED` | media bypass failed | retry with backoff |
| `SOURCE_UNAVAILABLE` / `TARGET_UNAVAILABLE` | a side is gone | defer |
| `TELEGRAM_TEMPORARY_ERROR` | 5xx | retry with backoff |
| `TELEGRAM_PERMANENT_ERROR` | 4xx | **FAILED** |
| `INTERNAL_ERROR` | the runtime's own fault | retry with backoff |

`IsTransient` is a *classification*, not the decision: the retry policy may
still refuse (budget spent, backpressure). `IsAmbiguous` outranks
`IsTransient` — an ambiguous failure is never retried blindly.

## Recovery matrix

| Failure | Job state | Attempts spent | Re-dispatch? |
|---|---|---|---|
| Telegram refused, unambiguous (4xx) | `FAILED` | unchanged | no |
| FloodWait | `RETRY_WAIT` (next_retry_at = demanded window) | **0** | yes, after the wait |
| transient (5xx, media) | `RETRY_WAIT`, exponential backoff, cap 5m | +1 | yes, until the budget |
| budget exhausted | `FAILED` + a `dead_letter_queue` row | — | only by owner replay |
| timeout / ambiguous | `UNKNOWN` | unchanged | **never automatically** |
| session offline | `RETRY_WAIT` | **0** | when the session returns |
| rule deleted / disabled / auto-paused | `FAILED` | — | no |

## Restart recovery

* **Worker restart mid-dispatch:** the job stays CLAIMED; the lease (default
  30s) expires; the next claim re-claims it; the fence guarantees that
  re-claim cannot double-send (already_sent / unknown are never
  re-dispatched).
* **Crash between Telegram's accept and the commit:** the fence row was
  stamped SENT *before* the job-row write, so recovery sees
  `already_sent` and closes the job without re-sending.
* **Python Runtime restart:** sessions survive (the Core's encrypted store
  owns them); the worker's jobs on that session defer until the session is
  ready again — they are never failed for it.
* **Database temporarily unavailable:** the claim loop reports `degraded`,
  retries each second, and `/healthz` answers 503. Jobs are the database, so
  nothing is lost.
* **Redis temporarily unavailable:** single-replica deployments never
  depended on it. (Multi-replica lease coordination is the Python loop's
  existing behaviour; the Go worker's lane isolation is in the database.)

## The reconciliation that remains the owner's job

`DeliveryReconciler` (the Python Core's existing reconciler) is untouched:
orphan mappings, stuck PENDINGs, expired claims, unknown deliveries,
repeated failures and open ingestion gaps are still its findings. A Go-Worker
deployment runs both — the reconciler reads rows, it does not dispatch.
