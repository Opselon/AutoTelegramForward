# TASK 01 — Repository Audit (source of truth: code + runtime, not README)

Branch: `work/realtime-flex-update` @ `b9eec01`. Repo ~7.5k LOC Python + Go control API.

## Architecture map (verified)

```
proto/autoforward.proto  →  Go control API (api/)  ─┐
                                                     ├─→ gRPC → core
atf.py (CLI) → config.yaml/env → core/main.py ──────┘
                            │
   ┌────────────────────────┴─────────────── DDD layers ────────────────┐
   │ domain/        entities, value_objects, services (FilterEngine,    │
   │                RoutingPolicy) — pure, no infra. ✓ clean            │
   │ application/   use_cases (Session/ForwardRule/FilterRule/AIConfig/ │
   │                ApiCredential/BotToken/MessageForwarding) + repos   │
   │ infrastructure/persistence  SQLite + SCHEMA_VERSION=3, WAL, FKs    │
   │ infrastructure/telegram     client_pool, dispatcher, login_flow,   │
   │                session_boot, bot_manager, pro_bot_ui, errors, i18n │
   │ infrastructure/ai           providers (OpenAI-compat, Anthropic,   │
   │                Gemini) via registry factory                        │
   │ infrastructure/grpc_server   servicers                             │
   │ infrastructure/security      crypto (Fernet)                       │
   │ logger/         separate gRPC log microservice + LogClient         │
   └────────────────────────────────────────────────────────────────────┘
```

Real-time path (verified live): pyrogram MessageHandler on each user session
→ `payload_from_pyrogram` → `MessageForwardingUseCase.process_message`
→ `list_active_by_source` + `RoutingPolicy.matching_rules` + trigger filter
→ since_ts / dedupe / FilterEngine / remove_links / AI rewrite → `dispatcher._send`
→ per-target throttle (25/min) → `_deliver` → `mark_processed`.

## What's genuinely good (do not redesign)

- Clean architecture holds: domain owns ports; pyrogram confined to adapters.
- Real-time forwarding works and is restart-safe via `processed_messages` dedupe.
- `errors.py` maps ~35 pyrogram exceptions by class name → (i18n key, severity,
  recoverable); RPCError fallback keeps exact code+message. Not generic.
- Encrypted vault for api_hash / bot tokens / session strings (Fernet).
- Login flow: rate limits (per-user 30s, per-phone 60s), TTL 10m, attempt lockout.
- DB: WAL, FK on, incremental migrations, indexes on real query paths.
- i18n: en/fa/ru/zh, 156–161 keys each.
- Logger microservice + LogClient now surfaces failures (was silent `pass`).

## CRITICAL defects (P0) — ranked by user-visible damage

### D1 — Auth state is IN-MEMORY ONLY. Login dies on every restart. *(Task 03)*
`LoginFlowManager._pending: Dict[int, LoginState]` — process-local dict. The
schema spec requires a *persisted* state machine. Any restart mid-login (code
sent, user typing) loses everything. Also `state.client` holds a live pyrogram
client which cannot be pickled, so a naive serialization is impossible — the
client handle must be re-derived on recovery.
- **Fix:** new `auth_states` table + `LoginFlowManager` rewrite to read/write
  it; recovery re-binds a client from the stored phone + phone_code_hash.

### D2 — `processed_messages` dedupe is WRITE-AHEAD, not deliver-confirm. *(Task 09)*
`mark_processed` runs after `sender()` returns True — but `dispatcher._send`
returns `ok_any` = True if **any** target succeeded. If a rule has 3 targets
and target #2 FloodWaits, that message is marked processed forever and target
#2 NEVER gets it. On retry there is no way to know it was partially delivered.
- **Fix:** per-target delivery state (bounded), make `mark_processed` accept
  the set of targets actually delivered, retry only missing ones.

### D3 — Auth failures silent on hot path; watchdog gives up permanently. *(Task 02/05)*
`client_pool._watchdog`: on `AuthKeyInvalid`/`UserDeactivated` → `return`.
No state change, no DB record, no user notification. Dashboard keeps showing
"🟢 connected". Same for `session_boot.drop_session`.
- **Fix:** mark session unauthorized in DB + record incident + tell the user.

### D4 — Album dedupe leaks memory + duplicates. *(Task 11/20)*
`_album_pending` keyed by `(str(client), chat_id, media_group_id)`, released by
`_release_album` after 2.5s. But `_send` applies a per-rule `delay_seconds` up
to 3600s — the marker is gone long before the delayed send happens, so albums
forwarded with a delay can double-fire. Unbounded dict (one entry per album,
never capped).
- **Fix:** release only after send completes; cap the dict; LRU eviction.

### D5 — Zero idempotency on buttons. Old callbacks re-fire. *(Task 04/09)*
`pro_bot_ui` callbacks have no idempotency key or staleness guard. A user
tapping a stale "delete rule" button after a restart runs the delete again.
Spec explicitly requires idempotent transitions.
- **Fix:** callback carries state version; reject when stale.

### D6 — No queue. `_send` blocks the event loop. *(Task 20/31)*
`dispatcher._send` awaits `asyncio.sleep(delay)` inline — with `delay_seconds`
up to 3600, that coroutine (and the handler that triggered it) is pinned for an
hour. No bounded queue, no backpressure, no worker separation. Spec requires
control-plane/data-plane separation and bounded queues.
- **Fix:** bounded send-queue + worker tasks; delay applied by the scheduler.

### D7 — Chat IDs typed as raw text. *(Task 06/10)*
Rule creation asks the user to TYPE `source target` as chat IDs. Spec: never
make users type IDs. No chat picker from the live session's dialogs.
- **Fix:** chat picker built from `client.get_dialogs()`.

### D8 — `users` table exists but no multi-tenant isolation. *(Task 33)*
No `user_id` on `forward_rules`, `filter_rules`, `ai_configs`, `error_log` has
it but nothing filters by it. `_is_admin` returns **True when no admins
configured** — open mode. Cross-user access is untested and unenforced.
- **Fix:** ownership columns + repo filtering + tests.

### D9 — No edit/delete sync despite `sync_edits`/`sync_deletes` columns. *(Task 12)*
Columns added in migration v3 but `_deliver` has no delete handling and edited
messages are re-copied as new messages (no destination-message mapping).
`edited_message` handler exists but no `message_map` table tracks
source→destination ids, so edits cannot update the copy.
- **Fix:** `message_map` table + edit/delete propagation.

### D10 — Duplicated keys in `pro_bot_ui`: `ui_login_fail_send_failed` etc.
present in fa/en, missing in ru/zh → `i18n.t` falls back to the key name for
those users. zh has 156 keys vs en 161 — 5 keys short.

### D11 — `atf.db` 4096 bytes + WAL 436KB with **0 sessions**: the DB file was
created at 15:47 and never written by the running core except ui_states. Not a
bug per se, but means **nothing has ever been forwarded** — no production data.

### D12 — `restore` handler writes the received backup to CWD and `open()`s it
without size/type validation; `message.download()` path is attacker-influenced.
Path traversal-lite. Low risk (admin-only) but real.

### D13 — CI runs on Python 3.11 but the project pins tgcrypto which has no
3.14 wheel; CI will break on 3.12+ only if tgcrypto is built from source —
currently CI installs latest pyrogram (unpinned) → drift from requirements.txt.
requirements pins pyrogram==2.0.106 but CI does `pip install pyrogram` (floats).
`pytest-asyncio` in requirements but **asyncio_mode=auto is set in pytest.ini**.
CI does not install pytest-asyncio → CI test job likely fails today.

### D14 — No PostgreSQL provider. Spec requires SQLite + PostgreSQL.
Only `SqliteDatabase` exists. The claim "a future PostgreSQL adapter only needs
to provide the same repository interfaces" is an aspiration, not code.

### D15 — README claims (600 msg/min, 99.9% uptime, millions of users) are
unmeasured. Zero benchmark tests exist. Claim validation (spec §40) fails.

### D16 — `api/internal/handlers/handlers_test.go` exists but Go API handlers
call gRPC; if the gRPC contract drifts from `proto/` there is no generated-code
check. No proto-compile step in CI.

## Smaller defects (P1/P2)

- `logger/main.py` serves the LogControlService but core's `LogClient` uses a
  raw channel — if the logger restarts, core's channel is dead and only the
  *new* local-fallback path logs it (added in b9eec01). No reconnect.
- `MessageForwardingUseCase.stats` is a non-thread-safe in-process counter —
  fine for one process, but it's what `/stats` reports as "real metrics".
- `pro_bot_ui` is 1205 lines in one file (4 separate FSM concerns: login,
  credentials, rules, filters, AI, logs). Needs splitting for maintainability.
- `bot_manager._route_text` still has `unknown_command` fallback that can fire
  for UI-owned flows (guarded now, but the guard depends on `ui_states` being
  present — a race if the UI's `_text` handler hasn't persisted yet).
- `create_sync` uses `asyncio.run()` inside an async codebase — will explode
  if called from a running loop. Dead code risk.
- `errors.py` `_is()` helper is defined but never used.
- `dispatcher.py:185` `register_handler` is marked legacy/back-compat — dead.
- `sqlite_repositories.py` (680 lines) — need to verify N+1 and index usage
  per query (audit shows `list_active_by_source` uses idx_rules_source ✓).

## Anti-pattern count (from the sweep)
`except Exception` occurrences: **112** across the codebase. Many are
legitimate (handler must not crash pyrogram), but `pro_bot_ui` alone has ~50,
most with `pass` — the class that caused the silent-failure incident.

## Task order (revised from spec, dependency-driven)

```
TASK 02  Telegram error forensics  (errors.py is 80% there → close gaps + tests)
TASK 03  Auth state machine        ← D1, unblocks 05/22
TASK 04  Button-first UI           ← D5, D7 (needs 03's persisted state)
TASK 05  Account management        ← D3
TASK 09  Idempotency/queue/worker  ← D2, D6
TASK 08  State storage             ← D8, D14 (PostgreSQL provider)
TASK 10  Error & log center        ← existing error_log, needs UI viewer
TASK 11  Advanced forwarding       ← D4
TASK 12  Sync engine               ← D9
TASK 13  History cloning
TASK 14-18 AI features             ← providers exist; rewrite/translate only
TASK 19  Dashboard                 ← metrics repos exist, no real wiring
TASK 20  Performance               ← benchmark suite (D15)
TASK 21  Security audit            ← D12
TASK 22  E2E                       ← needs 03+04+09
TASK 23  Load testing
TASK 24  Warning cleanup
TASK 25  Release hardening         ← D13 (CI drift)
```

## Verified live runtime (this session)
- core on :6001, logger on :6002, both up 24m.
- `/start by 5094837833` now traced in `data/atf_logs.db` (was silently
  dropped before b9eec01).
- `atf.db`: 0 sessions, 0 rules, 0 errors, 1 ui_state, 0 metrics.
- 41 tests pass (`pytest core/tests`).
