# Python Telegram Runtime

> Implemented: `telegram_runtime/` — the narrow Telegram connectivity and
> MTProto execution adapter the Go Worker talks to.

## What Python owns here, and nothing else

| Owned | NOT owned (Go owns it) |
|---|---|
| Telegram session lifecycle: creation, restoration, validation, reconnection | business routing |
| MTProto request execution through Pyrogram | rule evaluation, filtering |
| Receiving updates and normalizing them into application events | forward/copy decisions |
| Telegram-specific retrieval, sending, forwarding, copying | retry orchestration |
| Telegram-specific media download / upload / album operations | application-level scheduling |
| **Telegram RPC error normalization** into the shared typed vocabulary | durable job lifecycle, queue semantics |
| Runtime health and connection status | business-level delivery status |

**Go decides WHAT should happen; the runtime executes the Telegram-specific
operation Go asked for and reports WHAT Telegram actually reported.**

## Structure

```
telegram_runtime/
├── main.py                        entrypoint (refuses an empty RPC token)
├── errors.py                      the shared typed taxonomy (19 codes)
├── app/
│   ├── session_manager.py         readiness + client lookup over the Core's ClientPool
│   ├── pool_bootstrap.py          standalone-path session start (tests/dev)
│   └── bot_store.py               Stage 3 read-only, owner-scoped bot token resolution
├── adapters/
│   ├── pyrogram/client.py         ONE pyrogram client behind the typed contract
│   └── bot_api/__init__.py        Stage 3 Bot API adapter (BotApiTransport reuse)
├── grpc/server.py                 the tfgw/v1 servicer + bearer auth interceptor
└── tests/                         16+ tests: taxonomy parity, contract, switch
```

## Stage 3 Bot Token Security Boundary

The Go Worker **never** receives, logs, or stores raw bot tokens.

1. The wire carries `bot_token_id` + `owner_user_id` inside `BotTarget`.
2. The Runtime resolves the token through `telegram_runtime/app/bot_store.py`
   via `get_owned(token_id, owner_user_id)` where `owner_user_id` is part of
   the SQL `WHERE` clause: a transplanted id never even round-trips the
   secret.
3. Decryption happens strictly inside the Python boundary using the Core's
   `CryptoService` (`ATF_MASTER_KEY`).
4. Re-checked on every call, **including cache hits**: a pooled transport
   keyed by token id alone is never handed to a different tenant.
5. Bot API connection pooling (`BotApiTransport`, `httpx.AsyncClient`) is
   reused across messages; tokens are never logged or echoed in errors.

## Ownership decisions and why

* **The runtime is an adapter *over* the Core's `ClientPool`, not a second
  owner of sessions.** `session_manager.py` reads readiness and clients from
  the pool the Core already runs; a second pool would be a second source of
  truth for exactly the state that must never split.
* **Session strings never cross the boundary.** The contract has no
  `session_string` field (asserted in `test_grpc_contract.py`); the runtime
  holds the string in memory and exposes only the stable session id.
* **Error normalization delegates to the Core's
  `core/infrastructure/telegram/errors.py`** when importable, so the two
  boundaries cannot drift; a standalone import falls back to a conservative
  local classifier that never leaks a secret (redaction is always applied).
* **A runtime restart never destroys session state, and a worker restart
  never asks a Telegram user to authenticate again** — the worker holds no
  session material at all.

## Media

Downloads write to a path the **Go Worker owns** (it owns the scratch
volume's lifecycle and cleanup); uploads are staged from that same path. The
runtime never buffers a whole file, and the opaque
`PrepareMediaUpload`/`SendMedia` token is the staged path itself — no second
copy of the bytes exists anywhere.

## Security

* The gRPC server requires the shared `ATF_CORE_RPC_TOKEN` bearer credential
  on every RPC (`hmac.compare_digest`, no timing leak) and refuses with
  `UNAUTHENTICATED` otherwise, keeping the called method's cardinality so an
  unauthenticated caller cannot probe which methods exist.
* An empty token is a **refuse-to-start** condition (`main.py` exits 2).
* Every free-text field that could carry a secret is passed through the
  Core's `redact()` before it reaches a response (test:
  `test_redaction_never_leaks_a_secret`).
