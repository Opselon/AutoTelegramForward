# AutoTelegramForward (v2.0)

<p align="center">
  <img src="docs/assets/social-preview.png" alt="AutoTelegramForward — Production Telegram Auto-Forwarding & Routing Suite" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Opselon/AutoTelegramForward/releases/latest"><img src="https://img.shields.io/github/v/release/Opselon/AutoTelegramForward?color=38bdf8&style=flat-square&logo=github&label=Release" alt="Release"></a>
  <a href="https://github.com/Opselon/AutoTelegramForward/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Opselon/AutoTelegramForward/ci.yml?branch=master&style=flat-square&logo=githubactions&label=CI" alt="CI Status"></a>
  <a href="https://github.com/Opselon/AutoTelegramForward/actions/workflows/codeql.yml"><img src="https://img.shields.io/github/actions/workflow/status/Opselon/AutoTelegramForward/codeql.yml?branch=master&style=flat-square&logo=github&label=CodeQL" alt="CodeQL Status"></a>
  <a href="https://opselon.github.io/AutoTelegramForward/"><img src="https://img.shields.io/badge/Docs-Official%20Documentation-0284c7?style=flat-square&logo=gitbook" alt="Docs"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square" alt="License: MIT"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Go-1.27+-00ADD8?style=flat-square&logo=go&logoColor=white" alt="Go">
  <img src="https://img.shields.io/badge/Rust-1.90+-DEA584?style=flat-square&logo=rust&logoColor=white" alt="Rust">
  <img src="https://img.shields.io/badge/Flutter-3.29+-02569B?style=flat-square&logo=flutter&logoColor=white" alt="Flutter">
  <img src="https://img.shields.io/badge/Docker-Multi--Arch-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Locales-10%20Languages%20(1:1%20Parity)-10b981?style=flat-square&logo=translate" alt="Locales">
  <img src="https://img.shields.io/badge/Health-Live%20Topology%20Healthy-10b981?style=flat-square&logo=statuspage" alt="Live Health">
</p>

> 🌐 **Official Website, Documentation & Live Transformation Playground:** [https://opselon.github.io/AutoTelegramForward/](https://opselon.github.io/AutoTelegramForward/)

---

## ⚡ One-Command Installation (Zero Setup Hassle)

Install, configure, and launch the complete 4-microservice suite in seconds with a single command:

### 🐧 Linux / macOS / WSL (Universal Installer)
```bash
curl -fsSL https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/install.sh | bash
```

### 🪟 Windows (PowerShell)
```powershell
irm https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/install.ps1 | iex
```

### 📦 Standalone Single-File Binary (Zero Host Dependencies)
No Python, Go, Rust, Node.js, or compiler toolchains required on your machine:
```bash
curl -fsSL https://github.com/Opselon/AutoTelegramForward/releases/latest/download/atf-linux-x64 -o atf && chmod +x atf
./atf setup    # Interactive onboarding wizard (asks only for your Telegram Bot Token)
./atf start    # Launches all 4 microservices simultaneously
```

### 🐳 Docker Compose (Multi-Arch: `amd64` + `arm64`)
```bash
curl -fsSL https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

---

## 🏛️ Architecture & Microservice Topology

AutoTelegramForward v2.0 is engineered with strict, fail-closed microservice boundaries and Domain-Driven Design (DDD):

```
┌────────────────────────┐      ┌─────────────────────────┐
│   React SPA Dashboard  │      │   Flutter Mobile App    │
│  (Modern Browser UI)   │      │     (Android / iOS)     │
└───────────┬────────────┘      └────────────┬────────────┘
            │ HTTP / WebSocket               │ REST / JSON
            ▼                                │
┌────────────────────────┐                   │
│    Rust Web Gateway    │ HTTP Reverse Proxy│
│   + Axum BFF (:8088)   │ ──────────────────┼─────────────────────────┐
└────────────────────────┘                   │                         │
                                             ▼                         │
                                ┌─────────────────────────┐            │
                                │   Go REST API Gateway   │            │
                                │  Control Plane (:8080)  │            │
                                └────────────┬────────────┘            │
                                             │ gRPC (:50051)           │ gRPC (:50052)
                                             ▼                         ▼
                                ┌─────────────────────────┐ ┌─────────────────────────┐
                                │   Python MTProto Core   │ │   Central Logger Svc    │
                                │  Forwarder Engine (:50051) │ (:50052, SQLite DB) │
                                │  ClientPool + SQLite DB │ └─────────────────────────┘
                                └─────────────────────────┘
```

1. **Rust Axum Web Gateway & React Dashboard (`:8088`)**
   - Zero-dependency embedded web server: React SPA assets are compiled directly into the binary.
   - High-throughput reverse proxy routing `/api/*` to the Go Control Plane.
   - Hardened security headers (`CSP`, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `X-XSS-Protection`).
   - Public health endpoint (`/healthz`) and live system topology viewer.

2. **Go REST API & Control Plane (`:8080`)**
   - Authoritative public REST control interface with OpenAPI documentation.
   - High-performance authentication (Bearer JWT + TMA Telegram Mini App validation).
   - Strict multi-tenant isolation guarding rules, sessions, filters, and AI configurations.
   - Strictly decoupled from databases; communicates with backend engines solely via gRPC.

3. **Python Core / Telegram MTProto Engine (`:50051`)**
   - Exclusive owner of Telegram MTProto client pools, MTProto event loops, and business routing logic.
   - Domain-Driven Design (DDD) rule evaluation pipeline, content sanitization, deduplication, and AI transformations.
   - Encrypted SQLite persistence (`data/atf.db`) utilizing AES-256-GCM for sessions and keys.

4. **Python Central Logger Service (`:50052`)**
   - Autonomous telemetry and structured observability microservice with dedicated storage (`data/atf_logs.db`).
   - Standardized `[PIPELINE:...]` logging with live retention, filtering, and queryable gRPC/REST APIs.

5. **Flutter Mobile Client (Android & iOS)**
   - Production native mobile application with secure credential and password persistence.
   - Automated server URL reachability probing and protocol version compatibility checks.
   - Full RTL/LTR multilingual interface mirroring 100% of dashboard capabilities.

6. **`atf` Unified Process Supervisor CLI**
   - Single-file orchestrator that coordinates orderly startup (`logger` -> `core` -> `api` -> `web`), graceful shutdown, crash recovery, and health checks across Linux, macOS, and Windows.

---

## 🚀 What is New in v2.0

- 🧹 **Advanced Content Cleaner & Sanitizer**:
  Strip links, `@mentions`, `#hashtags`, emojis, and custom regex patterns per rule, with preview and dynamic toggle.
- 🧬 **Message Deduplication Engine**:
  SHA-256 content and album fingerprinting to eliminate duplicate and repeated forwards across all channels.
- 🔁 **Reply Synchronization (`sync_replies`)**:
  Preserves conversational hierarchy across channels by automatically mapping and linking replies between source and target.
- 🏷️ **Show Forward Header Toggle**:
  Seamlessly choose between native Telegram quote forwarding (preserving author channel header) and clean direct copy.
- 🌐 **10-Language 1:1 Parity**:
  Comprehensive, 100% catalog parity (378 keys) across English (`en`), Persian (`fa`), Russian (`ru`), Chinese (`zh`), Portuguese (`pt`), Uzbek (`uz`), Indonesian (`id`), Spanish (`es`), French (`fr`), and Italian (`it`) with fail-closed English fallback.
- 🎛️ **Interactive Telegram Bot Submenus**:
  Full visual configuration for Cleaner, Filters, Delay, Blacklist, Whitelist, and Replacements with dynamic status badges.
- 📊 **Modular Task Overview Modal**:
  6 modular inspection cards in the React Dashboard for in-depth rule auditing and live parameter adjustments.
- 🩺 **Live Topology & Services Health**:
  Real-time sub-millisecond health probing across all 4 services with latency, uptime, and dynamic alerts.
- 🛡️ **Hardened Multi-Tenant Defense**:
  Strict fail-closed numeric Telegram ID authorization; zero cross-tenant leak of sessions, rules, or logs.
- 📋 **Professional Structured Pipeline Logging**:
  Uniform `[PIPELINE:DISPATCH]`, `[PIPELINE:DELIVERED]`, `[PIPELINE:DROP]` logs for complete auditing and troubleshooting.

---

## 🎛️ Feature Comparison Matrix

| Feature | Telegram Bot UI | Web Dashboard | Mobile App (Flutter) | Go REST API |
|---|:---:|:---:|:---:|:---:|
| **Channel / Group / PV Forwarding** | ✅ | ✅ | ✅ | ✅ |
| **Interactive Login (Phone/OTP/2FA)** | ✅ | ✅ | ✅ | ✅ |
| **Cleaner & Pattern Removal** | ✅ | ✅ | ✅ | ✅ |
| **Deduplication & Content Hashing** | ✅ | ✅ | ✅ | ✅ |
| **Reply & Edit Synchronization** | ✅ | ✅ | ✅ | ✅ |
| **Forward Header (Quote) Mode** | ✅ | ✅ | ✅ | ✅ |
| **Delay & Scheduled Auto-Delete** | ✅ | ✅ | ✅ | ✅ |
| **10 Languages Localization** | ✅ | ✅ | ✅ | ✅ |
| **AI Rewriting (OpenAI/Claude/Gemini/DeepSeek)** | ✅ | ✅ | ✅ | ✅ |
| **Live Topology & Health Probing** | ✅ | ✅ | ✅ | ✅ |
| **Encrypted Backup & Migration (.atf)** | ✅ | ✅ | ✅ | ✅ |

---

## 📥 Download Official Releases

Every platform release provides a self-contained executable embedding all microservices with zero external dependencies:

| Binary Artifact | Platform | Architecture | Description |
|---|---|---|---|
| `atf-linux-x64` | Linux | x86_64 | Self-contained standalone single executable |
| `atf-linux-arm64` | Linux | ARM64 / aarch64 | Raspberry Pi 4/5, ARM VPS, AWS Graviton |
| `atf-macos-x64` | macOS | Intel x86_64 | macOS standalone binary |
| `atf-macos-arm64` | macOS | Apple Silicon (M1–M4) | Native Apple Silicon binary |
| `atf-windows-x64.exe` | Windows | x64 / ARM64 | Native Windows executable |
| `AutoTelegramForward-*-arm64.apk` | Android | ARM64 | Mobile release APK |
| `AutoTelegramForward-*-bundle.aab` | Android | Universal | Google Play Store bundle |

👉 **Browse all downloadable artifacts on [Releases Page](https://github.com/Opselon/AutoTelegramForward/releases/latest)**.

---

## 💻 CLI Command Reference

The unified `atf` CLI manages the entire lifecycle of all services:

```bash
atf setup             # First-time interactive or headless setup wizard
atf start             # Start ALL 4 microservices (logger -> core -> api -> web)
atf stop              # Gracefully stop all microservices with state cleanup
atf restart           # Safe sequential restart
atf status            # Check live status, PIDs, and listening ports
atf health            # Deep multi-layer diagnostics (DB, binaries, ports, gRPC)
atf logs              # Real-time streaming service logs with filtering
atf version           # Display version and runtime execution mode
atf service install   # Install OS background daemon (systemd / launchd / TaskScheduler)
atf docker up/down    # Full Docker Compose orchestration
atf k8s apply/status  # Production Kubernetes cluster deployments
atf test              # Run multi-stack test suites
```

---

## 🤖 Telegram Bot — Pro Button UI

Start the bot with `/start` to access the interactive panel without typing complex commands:

| Panel | Functionality |
|---|---|
| 📱 **Login** | Link a user account: Phone number -> Telegram OTP -> 2FA Password -> Encrypted session |
| 🔀 **Rules** | Create, view, toggle, edit, or delete forwarding rules |
| 🧹 **Cleaner** | Strip URLs, @mentions, #hashtags, emojis, and custom regex patterns |
| 🎛️ **Filters** | Configure keyword whitelists, blacklists, and media filters |
| 🤖 **AI Rewrite** | Connect OpenAI, Anthropic, Gemini, Groq, DeepSeek, or custom LLMs |
| 📋 **Logs** | Browse debug logs filtered by service and log level |
| 📊 **Stats** | Real-time forwarding counts, rate metrics, and system throughput |
| 🌐 **Language** | Switch on-the-fly between all 10 supported languages |
| 💾 **Backup** | Export and restore AES-256-GCM encrypted `.atf` configurations |

---

## 🔌 REST API Endpoints (Go Control Plane, `:8080`)

All API endpoints are documented with OpenAPI schemas:

```http
# Health & Telemetry
GET    /healthz
GET    /api/v1/health/services

# Forwarding Rules & Settings
GET    /api/v1/rules
POST   /api/v1/rules
GET    /api/v1/rules/{id}
PUT    /api/v1/rules/{id}
DELETE /api/v1/rules/{id}
POST   /api/v1/rules/{id}/pause
POST   /api/v1/rules/{id}/resume

# Session Management
GET    /api/v1/sessions
POST   /api/v1/sessions/backup
POST   /api/v1/sessions/restore
DELETE /api/v1/sessions/{id}

# Filters & AI Configurations
GET    /api/v1/filters
POST   /api/v1/filters
PUT    /api/v1/filters/{id}
DELETE /api/v1/filters/{id}
GET    /api/v1/ai
POST   /api/v1/ai
POST   /api/v1/ai/{id}/test

# Observability Logs
GET    /api/v1/logs?service=&level=&search=&limit=&since=
GET    /api/v1/logs/stats
POST   /api/v1/logs/purge
```

---

## 🧪 Comprehensive Automated Test Suites

AutoTelegramForward maintains strict test gates across every tier:

```bash
# Python MTProto Core (507+ tests: DDD, crypto, cleaner, pipeline, gRPC, FSM)
pytest core/tests -q

# Go REST API Control Plane (100% passing: auth, tenant isolation, metadata validation)
cd api && go test -v ./...

# Flutter Mobile Application (50+ tests: models, storage, isolation, localization)
cd mobile && flutter test
```

---

## 🔒 Security & Privacy Standard

- **Zero Plaintext Credentials**: All session strings, bot tokens, and AI API keys are encrypted at rest with AES-256-GCM derived from your `master_key`.
- **Fail-Closed Multi-Tenant Defense**: Authorization is strictly enforced using immutable numeric Telegram User IDs (`user.id`), never mutable usernames.
- **Network Isolation**: Python Core and Logger microservices bind to private gRPC interfaces and do not expose unauthenticated public ports.
- **Audit Logging**: Sensitive tokens and private credentials are automatically redacted (`[REDACTED]`) before reaching logs or error traces.

---

## 🤖 Agent & Contributor Guidelines

Autonomous AI agents and developers working on this codebase must follow the mandatory engineering rules documented in [`docs/agent-rules.md`](docs/agent-rules.md) and [`AGENTS.md`](AGENTS.md):
- **Mandatory Multilingual UI**: Zero hardcoded UI strings; strict 1:1 catalog parity across all 10 supported locales (`en`, `fa`, `ru`, `zh`, `pt`, `uz`, `id`, `es`, `fr`, `it`) with English fallback.
- **Fail-Closed Multi-Tenant Isolation**: Authoritative checks using immutable numeric Telegram User IDs.
- **Pre-Commit Quality Gates**: Mandatory execution and passing of `scripts/audit_cross_language.py` and `scripts/audit_telegram_i18n.py`.

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

## One-command setup, startup, and validation

The `atf` CLI manages the whole stack (rules, sessions, logs, data, admin, config)
**and** the deployment (docker, build, deploy, restart, health, logs, net, certs, db)
with an automated full-system upgrade path.

```bash
./scripts/setup.sh          # fresh checkout: venv + CLI + preflight + build + start + health
atf setup                   # the same sequence, guided, with a per-step summary table
atf setup --check           # read-only readiness report — changes nothing
atf setup --source-wizard   # legacy bare-metal wizard (config.yaml + supervised processes)
```

### Everyday lifecycle

```bash
atf up                      # build + start the whole stack (shortcut for `system start`)
atf down                    # stop the containers (`system stop`)
atf restart                 # restart and re-verify health (`system restart`)
atf status                  # live board: services, ports, rules
atf doctor --fix            # repair whatever the checks flag
atf system upgrade          # git pull → build → recreate → alias repair → health gate
```

`atf up` is **validator-gated**: it runs the static validation below first and
refuses to start on a blocking failure (override with `--force`).

### `atf validate` — the CI-safe validator

Read-only: no builds, no container churn, no repairs. Exit code is `0` only when
every check passes, so it works as a pre-deploy gate (`--strict` promotes
warnings to failures; `--runtime` adds the live stack checks; `--deep` runs the
repo's own i18n audits).

| Check | What it proves |
| --- | --- |
| `layout` | complete checkout (compose, Dockerfiles, CLI, services) |
| `version` | `VERSION` is a sane semver |
| `env-file`, `env-keys`, `env-optional` | `.env` exists; required keys populated; optional keys not left at their example value |
| `env-secrets`, `env-perms` | secret length floor met; `.env` is not group/world readable |
| `env-typos` | no stray keys that the example file does not document |
| `env-ignored` | `.env` is git-ignored |
| `compose` | `docker-compose.yml` parses cleanly |
| `catalog`, `catalog-placeholders`, `catalog-rtl` | 1:1 key parity + placeholder parity across all 10 locales; English values carry no RTL script |
| `secrets-scan` | no credential-shaped literal in any tracked file |
| `database` | SQLite `integrity_check` passes (read-only) |
| `proto` | generated stubs are not older than the `.proto` sources |
| `ports` | the ATF ports are not held by a foreign process |

```bash
atf validate                 # human report with per-check fixes
atf validate --json          # exactly one JSON object on stdout (scripts, cron, CI)
atf validate --strict        # non-zero exit on warnings too
```

Every command supports `--json` (one JSON document on stdout, placed before or
after the group name), and every line the CLI writes goes through a single
redaction filter, so a leaked bot token or API key cannot appear in a transcript.
