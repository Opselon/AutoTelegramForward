# AutoTelegramForward — Web Service

The web microservice is **one deployable** in two layers:

- `web/frontend/` — the React dashboard (React 19 + TypeScript + Vite). Owns
  presentation, navigation, client-side state, and user experience. Every
  visible string resolves through a ten-locale catalog.
- `web/backend/` — the Rust Axum **Web Gateway** (`atf-web-backend`, :8088).
  Serves the compiled SPA from the binary (rust-embed) and reverse-proxies
  `/api/*` to the Go Control Plane. It owns HTTP, not business.

Deeper references: [`docs/web-gateway.md`](../docs/web-gateway.md) (gateway
internals, ops view), [`web/frontend/README.md`](frontend/README.md) (design
system + frontend rules), [`docs/ui-ux-system.md`](../docs/ui-ux-system.md)
(the binding design contract for all three surfaces).

---

## 1. Purpose and responsibility

The Web Service is the operator-facing surface of AutoTelegramForward: a
SaaS-style dashboard for managing forwarding tasks, sessions, filters,
replacements, AI configuration, and observability — usable in a browser and
inside Telegram as a Mini App (TMA).

The gateway is a **BFF/reverse proxy, not a business service**. It exists so
the browser talks to exactly one origin (same-origin `/api`, no CORS on the
happy path, one TLS termination point, one CSP) while the authoritative
business API stays the Go Control Plane.

**The gateway owns:** SPA serving and embedding, `/health` + `/healthz`,
`/api/gateway` self-description, `/api/*` reverse proxying, HTTP connection
management, request/response streaming, security headers + CSP, CORS,
rate limiting, static asset compression + caching, correlation IDs, gateway
observability, graceful shutdown.

**The gateway is NOT responsible for** (and must never grow): Telegram
MTProto logic, user business rules, database ownership, authentication policy,
multi-tenant authorization, session management, message processing, queue
execution, AI provider orchestration, logger queries. **Go remains the
authoritative business API and authorization boundary** — the gateway never
connects to PostgreSQL, SQLite, Python Core, or the Logger directly, and never
answers a business question from its own state.

## 2. Topology

```
                       browser / Telegram Mini App
                                   │  same origin
                                   ▼
              ┌────────────────────────────────────────────┐
              │  atf-web-backend (Rust, Axum, :8088)       │
              │                                            │
              │  static   ← rust-embed(dist/) + gzip/br    │
              │  /api/*   ← streaming reverse proxy        │
              │  rate limit · CSP · CORS · correlation ids │
              └───────────────┬────────────────────────────┘
                              │ HTTP (ATF_API_ADDR, default api:8080)
                              ▼
                   Go Control Plane (api, :8080)
              authoritative REST API · authz · business data
                    │                       │
                    ▼                       ▼
          Python Core (:50051)      Rust Logger (:50052)
          Telegram MTProto          central log store
```

- The React SPA is compiled by Vite into `web/frontend/dist/` and embedded
  into the Rust binary at build time (`rust-embed`). One artifact serves UI +
  proxy.
- `Dockerfile.web` builds both stages; `docker-compose.yml` wires the `web`
  service to `api` on the internal network. An optional Caddy profile
  terminates TLS in front.
- Python Core and Rust Logger are reached **only** by the Control Plane. The
  web tier has no route to them.

## 3. Request flow (browser → gateway → Go)

1. **Static:** `GET /` or a deep link (`/#/rules`, `/some/route`) →
   `static_assets.rs` answers `index.html` with `Cache-Control: no-cache` and
   the derived CSP. Content-hashed assets under `assets/*` are `immutable` for
   one year; un-fingerprinted files (`favicon.ico`, …) get `max-age=3600`.
   A missing hashed bundle answers **404, never index.html with 200**.
2. **API:** `GET/POST/… /api/*` → `proxy.rs`:
   - rate limit (per-client token bucket, `/api` only) → `429` + `Retry-After`
   - correlation id honoured/generated (`X-Correlation-ID`, also sent as
     `X-Request-ID`), echoed on the response and stamped on every log line
     inside the request's tracing span
   - hop-by-hop headers stripped both ways (RFC 7230 §6.1 + `Connection`-named)
   - request body **streamed** to the upstream (oversized `Content-Length` →
     `413` up front; chunked uploads capped by a counting limiter → `413`)
   - upstream response streamed back with hop-by-hop stripped, gateway-owned
     security headers re-asserted (upstream copies removed first), and the
     gateway's own CORS verdict applied (upstream `Access-Control-*` dropped)
   - failures are generic (`502 upstream_unavailable` + correlation id);
     internal addresses stay in the logs
3. **Auth:** the gateway forwards whatever `Authorization` header the browser
   sent. The Control Plane validates the JWT and derives the tenant. The
   gateway verifies nothing, sees no credentials at rest, and logs none. A
   `401` from upstream passes through; the SPA's `api.ts` translates any
   non-`/api/auth/*` 401 into a single session-reset + login-screen
   transition.

## 4. Security model

| Concern | Mechanism |
|---|---|
| CSP | derived at boot from the *actual* embedded `index.html`: every inline script's SHA-256 is pinned (`script-src 'self' '<hashes>'`), `style-src 'unsafe-inline'` for React inline styles, `frame-ancestors 'self' https://web.telegram.org https://telegram.org` so TMA framing works. No `X-Frame-Options` on `index.html` (TMA requirement); `DENY` on other HTML. Operator override: `ATF_WEB_CSP`. |
| Security headers | `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, `COOP: same-origin` on the static surface; `nosniff` + `XFO: DENY` + `Referrer-Policy` re-asserted on proxied API responses. |
| CORS | same-origin by construction; an allow-list (`ATF_WEB_ALLOWED_ORIGINS`) is validated at boot (a bad origin refuses to start). Empty = permissive with a loud boot warning. Upstream CORS headers never leak through. |
| Rate limiting | per-client token bucket on `/api` only (default 20 rps / burst 40). The client key is the **socket peer** unless the operator opts into a forwarded header (`ATF_WEB_CLIENT_IP_HEADER`) — read **rightmost-first**, because leftmost `X-Forwarded-For` entries are client-controlled and would let an attacker mint fresh buckets. Static assets and health endpoints are exempt. Amortised pruning bounds memory without per-request scans. |
| Client-IP spoofing | no forwarded header is trusted by default. A direct client cannot influence its rate-limit key; behind a trusted proxy the operator names the header that proxy overwrites. |
| Body limits | `ATF_WEB_MAX_BODY_BYTES` (default 10 MiB) enforced before and during streaming. |
| Topology secrecy | error bodies are generic; the upstream URL is not published by `/health`, `/healthz`, or `/api/gateway`. |
| Logging | structured `tracing` (pretty or JSON). No tokens, session strings, or request bodies are logged; correlation ids join gateway logs with Control Plane logs. |
| TMA | the Telegram Mini App boot flow is unchanged: TMA `initData` is exchanged for a dashboard JWT by the Control Plane (`/api/auth/tma`) through the same proxy. |

## 5. Frontend architecture (what lives where)

| Path | Owns |
|---|---|
| `src/main.tsx` | boot: pre-paint lang/dir/theme, single-use login-code exchange, root `ErrorBoundary`, session verification |
| `src/App.tsx` | application shell: hash routing, navigation, header, data loaders (`loadData`), handlers, DashboardCtx |
| `src/api.ts` | the one HTTP client: bearer auth, error normalization at the boundary, 401 → session reset, shared `/queue` read |
| `src/session.ts` | auth-token ownership, JWT expiry check, cross-tab session events |
| `src/theme.ts` | the only writer of `<html data-theme>`; pre-paint application in `index.html` |
| `src/locales/` | engine + ten catalogs; `t(key, params)` with English fallback; missing keys render loudly |
| `src/panels/` | route-level lazy chunks (one per functional area) |
| `src/components/` | shared pieces: `Navigation`, modals (`ModalShell`), `ChatPicker`, task control center, admin/audit/jobs panels |
| `src/components/ui/` | the primitives: `Card`, `Button`, `Field`, `Badge`, `Skeleton`, `EmptyState`/`ErrorState`, `Toast`, `ModalShell` |

Design system: role tokens (`--color-*`), the `p-*` panel toolkit, and the
`tom-*` task-modal family in `src/index.css` ("DESIGN SYSTEM v2" block).
`tests/responsive.contract.test.mjs` enforces tokens, logical (RTL-safe)
properties, 44 px targets, a 12 px type floor, and 4.5:1 contrast **in both
themes**. Two first-class themes (dark default, light) — components never read
the theme.

## 6. Environment configuration (gateway)

| Variable | Default | Meaning |
|---|---|---|
| `ATF_WEB_ADDR` | `0.0.0.0:8088` | bind address |
| `ATF_API_ADDR` | `http://127.0.0.1:8080` | Control Plane base URL (aliases: `ATF_UPSTREAM_API`, `ATF_API_URL`) |
| `ATF_WEB_ALLOWED_ORIGINS` | *(empty = permissive + warning)* | comma-separated CORS allow-list |
| `ATF_WEB_CSP` | derived from embedded HTML | full CSP override |
| `ATF_WEB_RATELIMIT_RPS` / `_BURST` | 20 / 40 | token bucket per client |
| `ATF_WEB_CLIENT_IP_HEADER` | *(unset = socket peer)* | opt-in forwarded header for rate-limit keying (rightmost entry) |
| `ATF_WEB_MAX_BODY_BYTES` | 10485760 | request body cap (413 beyond) |
| `ATF_WEB_UPSTREAM_CONNECT_TIMEOUT_SECS` | 5 | dial bound |
| `ATF_WEB_UPSTREAM_READ_TIMEOUT_SECS` | 30 | max idle gap mid-response (not a total timeout) |
| `ATF_WEB_SHUTDOWN_GRACE_SECS` | 25 | drain deadline after SIGTERM/Ctrl-C |
| `ATF_WEB_LOG_FORMAT` | pretty | `json` for collectors |

Invalid values **refuse to start** with a message naming the variable.

## 7. Local development

```bash
# terminal 1 — API (needed for real data; a stub that answers /api/* also works)
docker compose up api          # or your usual api launcher

# terminal 2 — frontend dev server (vite on :3000, /api proxied to :8088)
cd web/frontend && npm ci && npm run dev

# terminal 3 — gateway (serves the last built dist/ in debug mode)
cd web/backend && cargo run
```

- `npm run dev` proxies `/api` to `http://127.0.0.1:8088` (the gateway), which
  proxies to the Control Plane. Point the browser at :3000.
- The gateway in debug builds reads `web/frontend/dist` from disk, so re-run
  `npm run build` (or use `npm run dev` for HMR instead of the gateway's
  static surface).

## 8. Production build

```bash
cd web/frontend && npm ci && npm run build    # → web/frontend/dist
cd web/backend && cargo build --release       # embeds dist/ via rust-embed
docker build -f Dockerfile.web -t atf-web .   # or: docker compose build web
```

Release builds fail to compile if `web/frontend/dist` is missing — the
frontend build is part of the artifact contract.

## 9. Testing

Frontend (from `web/frontend`, after `npm ci`):

```bash
npx tsc -p tsconfig.app.json --noEmit          # typecheck (build alone does not)
npx tsc -p tsconfig.node.json --noEmit
npm run lint                                   # oxlint
npm run build                                  # chunk budgets: initial ≤130 KB gzip, chunk ≤200 KB
node tests/responsive.contract.test.mjs        # token/RTL/a11y/contrast contract (55 checks)
node tests/uiCatalog.test.mjs                  # 1166 keys × 10 locales, placeholders, fallback   (compile prelude: see test header)
node tests/actionResult.test.mjs && node tests/providerKey.test.mjs
node tests/sessionIdentity.test.mjs && node tests/chatOrder.test.mjs && node tests/settingProvenance.test.mjs
# repo root:
node scripts/audit_web_ui_copy.js              # no unlocalized user-facing copy
python3 scripts/audit_feature_ui_coverage.py --gaps
```

Backend (from `web/backend`):

```bash
cargo fmt --check
cargo test                                     # 40 unit + stub-upstream router tests
cargo clippy --all-targets --all-features -- -D warnings
```

Performance (repo root):

```bash
python3 scripts/bench/ui_perf_bench.py --label before
# ...change...
python3 scripts/bench/ui_perf_bench.py --label after
python3 scripts/bench/ui_perf_compare.py docs/benchmarks/ui-perf-before.json docs/benchmarks/ui-perf-after.json
```

Budgets (`docs/ui-ux-system.md` §5): initial JS ≤ 130 KB gzip, any chunk
≤ 200 KB, ≥ 6 route chunks, ~50 mounted rows max on long lists.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `502` on every API call | Control Plane down or `ATF_API_ADDR` wrong. Check the gateway log (the correlation id in the response body joins it with the upstream log). `docker compose ps api` / hit `http://api:8080/healthz` from inside the network. |
| `/healthz` reports `upstream_connected: false` | same as above; the gateway itself is fine. |
| Dashboard loads but all panels empty + load-failure banner | one or more upstream feeds failed; the banner names them — Retry re-fetches. |
| Login loops back to the sign-in screen | the JWT is missing/expired; `api.ts` resets on any 401. Check system clock skew (JWT `exp`) and that `/api/auth/*` reaches the Control Plane. |
| Blank screen on a tab | every tab renders inside `PanelBoundary`; a chunk error shows the panel error state with Retry. If you see a *fully* blank page, check the browser console and the root boundary. |
| Persian/RTL looks LTR | `<html dir>` is set at boot from the stored locale; a hard reload re-applies it. `prefers-reduced-motion` and logical properties are contract-enforced. |
| New panel copy shows English in another locale | the key is missing from that catalog — CI (`uiCatalog.test.mjs`) fails before this can ship. |
| Rate-limited in dev (`429` + `Retry-After`) | the bucket keys on your socket peer at 20 rps / burst 40; raise the env vars or slow the poller. |
| Light theme looks broken in one panel | that panel still carries a dark hex literal — this is a bug, not a theme; see the color-token mapping in the frontend README. |
| `cargo build --release` fails on missing assets | run `npm run build` in `web/frontend` first (rust-embed bakes `dist/` at compile time). |
| Healthcheck flapping in compose | the check is `wget http://127.0.0.1:8088/healthz` every 15 s / 5 s timeout; a stuck drain is bounded by `ATF_WEB_SHUTDOWN_GRACE_SECS`. |

## 11. Change rules for this directory

1. Stay inside `web/`. Go, Python Core, Logger, mobile, schemas, and gRPC
   contracts are other services' territory; document a missing upstream
   capability as a dependency instead of implementing it here.
2. No new business logic in the gateway. If a screen needs new data, the
   Control Plane exposes it (or must), and the SPA consumes it through
   `src/api.ts`.
3. Every user-visible string goes through the ten-locale catalog — CI fails
   otherwise (`audit_web_ui_copy.js`, `uiCatalog.test.mjs`).
4. Design tokens only: a new hex literal or ad-hoc spacing in a component is a
   review failure (`responsive.contract.test.mjs` polices the CSS layer).
5. Both themes, all ten locales, RTL, 360–1440 px: part of done, not polish.
