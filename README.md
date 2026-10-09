# AutoTelegramForward

A microservice-based **Telegram auto-forwarder** with a Go REST control API and a Python MTProto core.

## Architecture

```
┌──────────────┐   HTTP/JSON    ┌─────────────┐     gRPC      ┌──────────────────┐
│  Dashboard / │ ─────────────► │   Go API    │ ────────────► │   Python Core    │
│  Client      │                │  (:8080)    │  (:50051)     │ Bot + Forwarder  │
└──────────────┘                └──────┬──────┘               │  + SQLite (DDD)  │
                                       │  gRPC                └────────┬─────────┘
                                       │  (:50052)                     │ logs
                                       └─────────────► ┌───────────────▼────────┐
                                                       │   Logger Service       │
                                                       │  debug logs + own      │
                                                       │  SQLite DB             │
                                                       └────────────────────────┘
```

- **`proto/`** — shared protobuf contract (single source of truth)
- **`api/`** — GoLang control-plane microservice (REST, auth, logging proxy)
- **`core/`** — Python service: Telegram Bot UI, MTProto forwarder, AI rewriter, gRPC server
  - `domain/` — pure entities, value objects, filter engine, routing policy
  - `application/` — use cases + repository ports
  - `infrastructure/` — SQLite repos (AES-256-GCM at rest), Pyrogram client pool, multi-provider AI adapters, i18n, gRPC servicers
- **`logger/`** — Python microservice: debug-log store with its **own SQLite DB**, gRPC `LogControlService`
- **`install.sh` / `install.ps1`** — one-command installers (Linux/macOS & Windows)

## Features

- ✅ Auto-forward **Channel→Channel**, **PV→PV**, **Channel→PV**, **PV→Channel**
- ✅ **Forward mode**: native forward (keeps author header) or clean copy
- ✅ **Filters**: keyword whitelist/blacklist, regex, media types, length bounds, link removal
- ✅ **AI rewrite**: OpenAI, Anthropic, Gemini, Groq, DeepSeek, 9Router, OpenRouter, any OpenAI-compatible endpoint
- ✅ **Button-driven Pro UI** — everything in Telegram via inline buttons:
  login (phone → code → 2FA), rules, filters, AI configs, logs, stats, backup
- ✅ **Logger microservice** — debug logs queryable per-service/level/category,
  in its own SQLite DB, via bot 📋 Logs panel or `GET /api/v1/logs`
- ✅ In-bot account login (`/login` or 📱 button): phone → code → 2FA → encrypted session
- ✅ **Backup / restore** sessions as encrypted `.atf` files
- ✅ **Multi-language bot UI**: English, فارسی, Русский, 中文 (`/lang`)
- ✅ **SQLite-first** multi-DB design — repository ports ready for PostgreSQL/MySQL
- ✅ **Session & API-key encryption** at rest (AES-256-GCM)
- ✅ **Bot-token-only operation** — every command works through the Telegram bot

## Download & Install (one file — every service)

Grab the single `atf` binary for your OS/CPU from
[**Releases**](https://github.com/Opselon/AutoTelegramForward/releases/latest)
— one command runs **everything** (core + logger + REST API):

| File | OS | CPU |
|---|---|---|
| `atf-linux-x64` | Linux | x86_64 |
| `atf-linux-arm64` | Linux | aarch64 (Raspberry Pi 4/5, ARM VPS) |
| `atf-macos-x64` | macOS Intel | x86_64 |
| `atf-macos-arm64` | macOS Apple Silicon | arm64 |
| `atf-windows-x64.exe` | Windows | x86_64 |
| `atf-windows-arm64.exe` | Windows on ARM | ARM64 |

**Linux / macOS / WSL:**

```bash
curl -fsSL https://github.com/Opselon/AutoTelegramForward/releases/latest/download/atf-linux-x64 \
  -o atf && chmod +x atf
./atf setup      # asks ONLY for your bot token
./atf start      # core + logger + api — all up with one command
./atf status     # live status of every service
```

Verify integrity: `sha256sum -c SHA256SUMS.txt` (from the release page).

**Windows (PowerShell):**

```powershell
curl.exe -fsSL https://github.com/Opselon/AutoTelegramForward/releases/latest/download/atf-windows-x64.exe -o atf.exe
.\atf.exe setup
.\atf.exe start
```

**Docker (any platform, amd64 + arm64):**

```bash
curl -fsSL https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/docker-compose.yml \
  -o docker-compose.yml
docker compose up -d        # or: atf docker up
```

CLI commands (binary or source — identical):

```bash
atf setup       # first-time setup (bot token only)
atf start       # start ALL services (core + logger + api)
atf stop        # stop everything
atf restart     # stop + start
atf status      # live status of every service
atf logs        # tail recent logs (auto-falls-back to OS journal)
atf health      # deep health & diagnostics check
atf service install   # autostart: systemd (Linux) / launchd (macOS) / Task Scheduler (Windows)
atf docker up   # docker lifecycle: up | down | restart | logs | ps | pull
atf test        # run the test-suite
atf version     # print version
```

<details>
<summary>Install from source (advanced)</summary>

**Linux / macOS / WSL:**

```bash
curl -fsSL https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/install.ps1 | iex
```

Both installers set up git/Python, clone the repo, then hand off to `atf setup`,
which asks **only for your Telegram bot token** (get one from [@BotFather](https://t.me/BotFather)).
Everything else — venv, dependencies, config, encryption key — is automatic.

```bash
python atf.py start      # logger + bot + REST API live
```

`start` launches three services: Logger (:6002) → Python core/bot (gRPC :6001)
→ Go REST API (:8080, skipped automatically if Go isn't installed).

Other commands:

```bash
python atf.py test          # run Python + Go test suites
python atf.py update        # git pull + dependency refresh
python atf.py restore x.atf # restore a session backup
```

<details>
<summary>Manual setup (advanced)</summary>

```bash
# 1. Environment
uv venv .venv --python 3.11
uv pip install pyrogram tgcrypto grpcio grpcio-tools protobuf httpx cryptography pyyaml pytest pytest-asyncio

# 2. Configure
copy config.example.yaml config.yaml   # fill in api_id, api_hash, bot_token, master_key

# 3. Regenerate proto (only if you edit proto/autoforward.proto)
python scripts/gen_proto.py

# 4. Build & run
cd api && go build -o atf-api.exe . && cd ..
.venv\Scripts\python.exe -m core.main   # terminal 1 (bot + gRPC)
cd api && .\atf-api.exe                  # terminal 2 (REST API)
```

</details>

<details>
<summary>Docker</summary>

```bash
cp .env.example .env        # paste your bot token
docker compose up -d        # core + api, data persisted in a volume
```

</details>

## Telegram Bot — Pro Button UI

Open the bot and press **🚀 Start Panel** (`/start`). Everything is buttons —
no typing commands:

| Panel | Does |
|-------|------|
| 📱 Login | link a user account: phone → code → 2FA → encrypted session |
| 🔀 Rules | list / add (source → target, routing, mode) / enable / delete |
| 🎛 Filters | list / add (type, pattern, action) / delete |
| 🤖 AI | list / add (provider, model, key, prompt) / delete |
| 📋 Logs | browse debug logs (filter by service / level), powered by the Logger service |
| 📊 Stats | live forwarding statistics |
| 💾 Backup | export encrypted `.atf` session backups |
| 🌐 Language | switch en / fa / ru / zh |

## Telegram Bot Commands (also available)

| Command | Purpose |
|---------|---------|
| `/start` `/help` | welcome + command list |
| `/login` | link a user account (phone → code → 2FA) |
| `/sessions` | list linked sessions |
| `/rules` | list & add forwarding rules (`source_id target_id`) |
| `/ai` | list & add AI configs (`provider model api_key`) |
| `/lang` | switch language (en/fa/ru/zh) |
| `/backup` `/restore` | session backup / restore |
| `/stats` | live forwarding statistics |
| `/cancel` | abort current operation |

## REST API (Go, :8080)

```
GET    /healthz
GET    /api/v1/sessions        POST /api/v1/sessions/backup  POST /api/v1/sessions/restore
DELETE /api/v1/sessions/{id}
GET    /api/v1/rules           POST /api/v1/rules            PUT/DELETE /api/v1/rules/{id}
GET    /api/v1/filters         POST /api/v1/filters          PUT/DELETE /api/v1/filters/{id}
GET    /api/v1/ai              POST /api/v1/ai               PUT/DELETE /api/v1/ai/{id}
POST   /api/v1/ai/{id}/test    GET /api/v1/stats
GET    /api/v1/logs?service=&level=&search=&limit=&since=     GET /api/v1/logs/stats
```

Auth: set `ATF_API_KEY` and pass `X-API-Key` (or `Authorization: Bearer`) on every request.

## Tests

```bash
.venv\Scripts\python.exe -m pytest core/tests logger/tests -q    # 42 tests: crypto, filters, repos, pipeline, gRPC, logger, pro-UI
cd api && go test ./...                  # middleware + handler tests
```

## Security Notes

- Session strings and AI API keys are **never stored in plaintext** — AES-256-GCM with a key derived from `master_key`.
- Keep `master_key` safe; rotating it invalidates all stored sessions.
- Set `admin_ids` in config to restrict bot admin commands.
