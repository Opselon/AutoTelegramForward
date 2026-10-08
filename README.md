# AutoTelegramForward

A microservice-based **Telegram auto-forwarder** with a Go REST control API and a Python MTProto core.

## Architecture

```
┌──────────────┐   HTTP/JSON    ┌─────────────┐     gRPC      ┌──────────────────┐
│  Dashboard / │ ─────────────► │   Go API    │ ────────────► │   Python Core    │
│  Client      │                │  (:8080)    │  (:50051)     │ Bot + Forwarder  │
└──────────────┘                └─────────────┘               │  + SQLite (DDD)  │
                                                              └──────────────────┘
```

- **`proto/`** — shared protobuf contract (single source of truth)
- **`api/`** — GoLang control-plane microservice (REST, auth, logging)
- **`core/`** — Python service: Telegram Bot UI, MTProto forwarder, AI rewriter, gRPC server
  - `domain/` — pure entities, value objects, filter engine, routing policy
  - `application/` — use cases + repository ports
  - `infrastructure/` — SQLite repos (AES-256-GCM at rest), Pyrogram client pool, multi-provider AI adapters, i18n, gRPC servicers

## Features

- ✅ Auto-forward **Channel→Channel**, **PV→PV**, **Channel→PV**, **PV→Channel**
- ✅ **Forward mode**: native forward (keeps author header) or clean copy
- ✅ **Filters**: keyword whitelist/blacklist, regex, media types, length bounds, link removal
- ✅ **AI rewrite**: OpenAI, Anthropic, Gemini, Groq, DeepSeek, 9Router, OpenRouter, any OpenAI-compatible endpoint
- ✅ **In-bot account login** (`/login`): phone → code → 2FA password → encrypted session stored
- ✅ **Backup / restore** sessions as encrypted `.atf` files (`/backup`, `/restore`)
- ✅ **Multi-language bot UI**: English, فارسی, Русский, 中文 (`/lang`)
- ✅ **SQLite-first** multi-DB design — repository ports ready for PostgreSQL/MySQL
- ✅ **Session & API-key encryption** at rest (AES-256-GCM)
- ✅ **Bot-token-only operation** — every command works through the Telegram bot

## Quick Start (very easy — one command)

```bash
# Just paste your bot token when asked. Everything else is automatic.
python atf.py setup
python atf.py start      # bot + REST API live
```

That's it. `setup` creates the venv, installs dependencies, generates an
encryption master key, and writes `config.yaml`. `start` launches both
services (Go REST API on :8080 is skipped automatically if Go isn't installed).

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

## Telegram Bot Commands

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
```

Auth: set `ATF_API_KEY` and pass `X-API-Key` (or `Authorization: Bearer`) on every request.

## Tests

```bash
.venv\Scripts\python.exe -m pytest -q    # 35 tests: crypto, filters, repos, pipeline, gRPC roundtrip
cd api && go test ./...                  # middleware + handler tests
```

## Security Notes

- Session strings and AI API keys are **never stored in plaintext** — AES-256-GCM with a key derived from `master_key`.
- Keep `master_key` safe; rotating it invalidates all stored sessions.
- Set `admin_ids` in config to restrict bot admin commands.
