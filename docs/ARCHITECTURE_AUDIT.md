# AutoTelegramForward — Architecture Audit & Production Engineering Specification
**Target Release:** v1.2.0 → v1.2.1 / Next Unified Release  
**Audit Date:** October 2026  
**Scope:** Deep Repository Audit, Microservice Boundaries, Data Ownership, True Single-File Release Matrix

---

## 1. Executive Summary & Architecture Reality

AutoTelegramForward is composed of four distinct languages:
- **Python:** Telegram MTProto Engine (`core/`) + Observability Logger daemon (`logger/`)
- **Go:** Control Plane & REST API (`api/`)
- **Rust (Axum):** Web Gateway & BFF (`web/backend/`)
- **React (TypeScript):** Modern Management Dashboard (`web/frontend/`)

### Core Findings & Audit Discrepancies
1. **Data Ownership Violation (Critical):** `web/backend` (Rust) previously connected directly to `data/atf.db` via `rusqlite`, executing raw SQL queries and mutations simultaneously with Python Core. This violated SQLite single-writer semantics and bypassed domain validations.
2. **Duplication of Domain Logic (Critical):** `web/backend` in Rust re-implemented rule routing and simulation in `engine.rs`, creating two diverging routing implementations with Python Core.
3. **Non-Self-Contained Web Gateway (Critical):** `web/backend` depended on filesystem paths (`web/frontend/dist`) to serve static files. When distributed as a standalone binary, the UI was unavailable without cloning the repo.
4. **False "Single-File" Packaging in Releases:** PyInstaller previously only bundled Python code into `atf-linux-x64`. Go API was built separately as `atf-api-*`, and Rust Web Gateway was not embedded. Clean systems were forced to skip the web dashboard or compile from source.
5. **Supervisor Lifecycle Gaps:** `atf.py` relied on blind `time.sleep()` calls instead of deterministic readiness probes, and lacked process group termination on shutdown.

---

## 2. Microservice Boundary & Responsibility Contract

| Service | Language | Port | Primary Responsibility | Data Ownership | Communication |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Core** | Python 3.12 | `:6001` (gRPC) | Telegram MTProto, session client pool, rule pipeline, VIP hops, media matrix, deduplication | **Exclusive owner** of `data/atf.db` | gRPC server |
| **Logger** | Python 3.12 | `:6002` (gRPC) | Structured log ingestion, search, audit trail, retention | **Exclusive owner** of `data/atf_logs.db` | gRPC server |
| **Control Plane** | Go 1.24+ | `:8080` (HTTP) | REST API, validation, auth, rate limiting, OpenAPI, coordinating Core & Logger | **No direct DB writes**; talks over gRPC | gRPC client → Core & Logger; HTTP server |
| **Web Gateway** | Rust (Axum) | `:8088` (HTTP) | Web Gateway / BFF, embeds React SPA, reverse proxies `/api/*` to Go REST API, security headers, web sessions | **No direct DB writes**; proxies to Go API | Upstream HTTP client → Go API; HTTP server for Web UI |
| **Frontend** | React 19 / TS | Browser | Management UI, responsive views, zero secrets embedded | N/A | HTTP client → Web Gateway `:8088` |

---

## 3. Data Flow & Security Boundary

```
[Browser (React SPA)]
         │
         │ HTTP (:8088)
         ▼
[Rust Axum Web Gateway] (Embeds React static assets via rust-embed)
         │
         │ HTTP Reverse Proxy / BFF (:8080)
         ▼
[Go Control Plane REST API]
         │
         ├── gRPC (:6001) ──► [Python Core Engine] ──► SQLite (atf.db)
         │                                       ──► Telegram MTProto
         └── gRPC (:6002) ──► [Python Logger]     ──► SQLite (atf_logs.db)
```

- **Loopback isolation:** On local host deployments, gRPC ports (`:6001`, `:6002`) and Go API (`:8080`) bind to `127.0.0.1` by default. Only the Web Gateway (`:8088`) is exposed to the user.
- **Single Writer per Database:** `atf.db` is exclusively accessed by Core; `atf_logs.db` is exclusively accessed by Logger.

---

## 4. True Single-File Release Design

For each release target:
- `atf-linux-x64`
- `atf-linux-arm64`
- `atf-macos-x64`
- `atf-macos-arm64`
- `atf-windows-x64.exe`

### Packaging Pipeline
1. `web/frontend`: `npm run build` compiles React TypeScript into `dist/`.
2. `web/backend`: `cargo build --release` compiles Rust Axum, embedding `dist/` directly via `rust-embed`.
3. `api`: `go build -o atf-api .` compiles Go REST API into a static binary.
4. `atf.py`: PyInstaller builds the one-file binary, bundling `atf-api` and `atf-web-backend` using `--add-binary`.
5. At runtime on a clean machine:
   - `atf.py` starts Logger and Core internally from the bundled Python runtime.
   - `atf.py` extracts and launches `atf-api` and `atf-web-backend` from `sys._MEIPASS` with readiness probes.
   - Zero dependencies required on host.
