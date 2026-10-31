# Web Gateway architecture (`web/`) — v2.1

The web microservice is one deployable: an Axum binary that **serves the embedded
React dashboard and reverse-proxies `/api` to the Go Control Plane**. Same
external contract as 2.0; the internals were rebuilt for production.

```
                      ┌──────────────────────────────────────────────┐
 operator / TMA ────▶ │  atf-web-backend (Axum, :8088)               │
                      │                                              │
                      │  /health /healthz      health.rs             │
                      │  /api/gateway          health.rs (self-info) │
                      │  /api/* ──▶ proxy.rs ── streaming ───────┐   │
                      │  fallback ─▶ static_assets.rs (embed)    │   │
                      │              + compression (this hop)    │   │
                      │  security.rs  CSP + headers              │   │
                      │  rate_limit.rs token bucket (/api only)  │   │
                      └──────────────────────────────────────────┼───┘
                                                                 ▼
                                                   Go Control Plane (api:8080)
                                                                 ▼
                                            core :50051 · logger :50052
```

## Modules

| module            | owns                                                                 |
|-------------------|----------------------------------------------------------------------|
| `config.rs`       | env parsing + validation — invalid values **refuse to start**         |
| `state.rs`        | shared state, tuned `reqwest` client, rate limiter, resolved CSP      |
| `proxy.rs`        | `/api` streaming reverse proxy, hop-by-hop hygiene, correlation ids   |
| `static_assets.rs`| embedded SPA, cache contract, compression (static surface only)       |
| `security.rs`     | CSP derivation (hashes the embedded HTML's inline scripts)            |
| `rate_limit.rs`   | per-client token bucket (in-memory, lazy-pruned)                      |
| `health.rs`       | `/health`, `/healthz`, `/api/gateway` (shapes are load-bearing)       |

## What 2.1 changed (and why)

- **Streaming request bodies.** 2.0 buffered up to 10 MB before forwarding. The
  cap is still enforced: oversized `Content-Length` is refused with 413 up
  front, and chunked uploads are capped by a counting `Limited` body wrapper.
- **Hop-by-hop headers stripped both ways** (RFC 7230 §6.1), including names
  listed in `Connection`. 2.0 forwarded `Connection` verbatim across two
  independent connections, corrupting keep-alive semantics.
- **Timeouts split.** Connect timeout bounds the dial; `read_timeout` bounds
  each *idle gap*. The old blanket 30 s total timeout silently killed any
  response that streamed longer than 30 s.
- **`Content-Length` is never forwarded.** The body leaves as a stream
  (chunked); forwarding both framing headers is a hyper error.
- **One connect-retry** for bodyless `GET`/`HEAD` on connect errors — the only
  requests whose replay cannot double-apply anything.
- **Graceful shutdown** on SIGTERM/Ctrl-C: stop accepting, drain in-flight.
- **Compression** (gzip/brotli) is scoped to the static surface. Proxied
  responses are never routed through it, so a streaming upstream is untouched.
- **Rate limiting** on `/api` only: 20 rps / burst 40 per client by default,
  `429` + `Retry-After`. Best-effort cushion, not an auth boundary — the
  Control Plane still authenticates everything.
- **CSP derived from the shipped asset.** `security.rs` SHA-256-hashes the
  embedded index.html's inline scripts and pins them, so the pre-paint
  lang/theme snippet stays legal without `script-src 'unsafe-inline'`.
  `frame-ancestors` deliberately allows Telegram's web clients (TMA runs in an
  iframe); `X-Frame-Options: DENY` is asserted on proxied API responses and
  non-entry HTML only.
- **CORS tightened by configuration.** Same-origin by construction; empty
  `ATF_WEB_ALLOWED_ORIGINS` keeps 2.0's permissive behaviour *with a boot
  warning*. Set the variable in production.
- **Module split + tests.** 20 unit/integration tests, including a real
  loopback stub upstream proving header hygiene, streaming, 413, 429 and 502
  behaviour through the actual router.

## Configuration

| Variable (prefix `ATF_`)            | Default              | Meaning                                   |
|-------------------------------------|----------------------|-------------------------------------------|
| `ATF_WEB_ADDR`                      | `0.0.0.0:8088`       | bind address                               |
| `ATF_API_ADDR` (aliases accepted)   | `http://127.0.0.1:8080` | Control Plane base URL                  |
| `ATF_WEB_ALLOWED_ORIGINS`           | *(empty = permissive)* | comma-separated CORS allow-list          |
| `ATF_WEB_RATELIMIT_RPS` / `_BURST`  | `20` / `40`          | token bucket per client                    |
| `ATF_WEB_MAX_BODY_BYTES`            | `10485760`           | request body cap (413 beyond)              |
| `ATF_WEB_UPSTREAM_CONNECT_TIMEOUT_SECS` | `5`              | dial timeout                               |
| `ATF_WEB_UPSTREAM_READ_TIMEOUT_SECS`    | `30`             | max idle gap mid-response                  |
| `ATF_WEB_CLIENT_IP_HEADER`          | `X-Forwarded-For`    | client key source for the limiter          |
| `ATF_WEB_CSP`                       | *(derived)*          | full CSP override                          |
| `ATF_WEB_LOG_FORMAT`                | `pretty`             | `json` for collectors                      |

### Client-IP caveat

`X-Forwarded-For` is trusted by default because behind the Caddy TLS profile
the direct peer is the proxy and the header is the only per-client signal. A
client connecting *directly* can spoof the header and skew (only) its own
bucket. It guards no security boundary.

## Frontend (`web/frontend`)

React 19 + Vite (Rolldown) + TypeScript, zero UI framework. One CSS design
system (`src/index.css`) holds the tokens; `docs/ui-ux-system.md` is the
contract both themes must satisfy (the CI test `tests/responsive.contract.test.mjs`
asserts it, including per-theme contrast).

- **Two first-class themes.** Dark (default, the product's identity) and light
  (engineering-paper). A theme is one attribute — `<html data-theme>` — plus a
  role-token override block; no component reads the theme. The header and the
  login screen carry the toggle; `index.html` applies the stored choice before
  first paint (no flash), and `src/theme.ts` is the only writer.
- **Accent shades are derived**, not stored: components use
  `color-mix(in oklab, var(--color-x) NN%, transparent)` for borders/glows, so
  both themes inherit them. Hex inside a component is a review failure.
- Copy is catalog-only: every string resolves through `t()` across the ten
  locales (`scripts/audit_web_ui_copy.js` + `tests/uiCatalog.test.mjs`).

## Verification recipes

```bash
# frontend
cd web/frontend && npm ci && npm run build && npm run lint
node tests/responsive.contract.test.mjs        # + the other tests/*.test.mjs per CI
node ../../scripts/audit_web_ui_copy.js --root .

# backend
cd web/backend && cargo test

# the whole microservice, locally
(cd web/frontend && npm run build)
cargo run --release   # serves the embedded dashboard on :8088
```
