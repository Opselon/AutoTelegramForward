# Self-hosting AutoTelegramForward

Run the full stack (core + logger + Go API + web gateway) on your own
server with Docker Compose.

## 1. Fork `.env` setup

```bash
cp .env.example .env
# edit .env and fill in real values:
#   ATF_API_ID / ATF_API_HASH — from https://my.telegram.org
#   ATF_BOT_TOKEN             — from https://t.me/BotFather
#   ATF_MASTER_KEY            — long random string (e.g. `openssl rand -hex 32`)
#   ATF_API_KEY               — optional REST API key
#   ATF_WEB_URL               — public dashboard URL shown in the bot panel
```

Never commit `.env`. Forks keep their own copy; CI never reads it.

## 2. Compose up

```bash
docker compose up -d --build
docker compose ps
```

Services and ports:

| Service | Container   | Port  |
|---------|-------------|-------|
| logger  | atf-logger  | —     |
| core    | atf-core    | —     |
| api     | atf-api     | 8080  |
| web     | atf-web     | 8088  |

## 3. Health validation

```bash
curl -sf http://127.0.0.1:8088/healthz
curl -sf http://127.0.0.1:8080/api/v1/health/services | python3 -m json.tool
```

Expected: web `/healthz` returns `ok`; the API endpoint returns
`{"status": "healthy"|"degraded", "services": [...]}` with one entry per
service (core → logger → api → web). `degraded` means a dependency probe
failed — check `docker compose logs <service>`. The release pipeline runs
this same check in its `clean-install` job and tears the stack down with
`docker compose down -v` afterwards.

## 4. Volumes / backup / restore

Persistent state lives in two named volumes:

- `atf-data` — core state: `/app/data/atf.db`, plus the **retired** Python
  logger archive `/app/data/logger.db`. Nothing writes that archive any more
  (the logging service is the Rust engine); it is kept as a historical record
  and must not be deleted.
- `logger-rust-data` — the Rust logging engine's archive:
  `/app/data/atf_logs_rust.db`.

```bash
# backup (both volumes)
docker compose stop
for v in atf-data logger-rust-data; do
  docker run --rm -v "$v":/data -v "$PWD":/backup alpine \
    tar -czf "/backup/$v-$(date +%F).tar.gz" -C /data .
done
docker compose start

# restore
docker compose down
docker volume rm <project>_logger-rust-data  # project prefix from `docker volume ls`
docker volume create <project>_logger-rust-data
docker run --rm -v <project>_logger-rust-data:/data -v "$PWD":/backup alpine \
  tar -xzf /backup/logger-rust-data-<date>.tar.gz -C /data
docker compose up -d
```

SQLite files must be copied while the stack is stopped; copying a live
`.db` can produce a corrupt backup.

## 5. Upgrade / rollback

Two ways, and they are not equivalent. Prefer the first.

### Run CI-built images (no compilation on the server)

Every push to `master` publishes all four service images to GHCR (the `Publish`
workflow) as an immutable `sha-<short>` tag plus a moving `master` tag. Pulling
that tag runs the exact artifact the pipeline built and tested:

```bash
# one-time: the packages are private by default
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin

# deploy a tag (pulls first, refuses to compile, rolls back if unhealthy)
ATF_IMAGE_TAG=sha-abc1234 scripts/deploy_release.sh
```

`scripts/deploy_release.sh` pulls every service *before* starting anything —
with `image:` and `build:` both in the merged configuration, `docker compose up`
would compile the first image it cannot find. It records what it deployed in
`.atf/deployed-tag` and rolls back to the previous tag when a service does not
become healthy. `--dry-run` prints what would change; `--pull` is the default.

The published tag goes straight into compose if you prefer to drive it yourself:

```bash
ATF_IMAGE_TAG=sha-abc1234 docker compose \
  --env-file .env -f docker-compose.yml -f docker-compose.ghcr.yml \
  pull && ATF_IMAGE_TAG=sha-abc1234 docker compose \
  --env-file .env -f docker-compose.yml -f docker-compose.ghcr.yml up -d
```

Rollback is the same command with the previous `sha-<short>`.

### Build on this host

For a fork, an offline host, or a version tag you want to compile yourself:

```bash
# upgrade: check out the tag and compile (data volume is untouched)
git fetch --tags && git checkout vX.Y.Z
docker compose up -d --build

# rollback: same steps with the previous tag
git checkout vX.Y.(Z-1)
docker compose up -d --build
```

This path compiles Python/Node/Rust on the machine that also forwards messages,
and the result is not bit-identical to the CI image of the same commit.

The database schema is forward-compatible within a major product version;
if a release notes a breaking migration, back up both volumes (`atf-data`,
`logger-rust-data`) first.
Image tags follow the product version
(`ghcr.io/<owner>/atf-core:<version>`); see `release/manifest.json`
(`server.images`) for the exact tags of a release.

## 6. Signing-secret management

### Android keystore

The Gradle build (`mobile/android/app/build.gradle.kts`) signs release
builds from **either** a local `key.properties` file **or** CI environment
variables — never from anything committed to the repo.

Local device testing needs no keystore (debug keys are used). For Play
Store uploads, create the secrets on the maintainer side and wire them as
repository secrets; the release workflow decodes them into a keystore file
at build time:

| Secret                    | Content                              |
|---------------------------|--------------------------------------|
| `ANDROID_KEYSTORE_BASE64` | base64 of the upload `.jks`          |
| `ANDROID_KEYSTORE_PASSWORD` | keystore password                  |
| `ANDROID_KEY_ALIAS`       | key alias                            |
| `ANDROID_KEY_PASSWORD`    | key password                         |

Template: `mobile/android/key.properties.example`. Never commit
`key.properties` or any `.jks` / `.keystore` file.

### iOS certificates

Signed IPAs need an Apple Developer distribution certificate, a provisioning
profile, and an export-options plist, all stored as repository secrets
(values are never written anywhere except the ephemeral macOS runner):

| Secret                         | Content                                    |
|--------------------------------|--------------------------------------------|
| `IOS_CERT_P12_BASE64`          | base64 of the distribution `.p12`          |
| `IOS_CERT_PASSWORD`            | `.p12` import password                     |
| `IOS_PROVISION_PROFILE_BASE64` | base64 of the `.mobileprovision` profile   |
| `IOS_EXPORT_PLIST_BASE64`      | base64 of the `ExportOptions.plist`        |

When these secrets are absent (forks, community PRs), the `signed-ios`
release job logs a documented SKIP and produces nothing — it never fakes a
successful build. The unsigned `ios-simulator` job still validates the iOS
target on every release.

## 7. Automated Continuous Deployment via GitHub Actions (CD)

AutoTelegramForward supports automated remote deployment via GitHub Actions (`.github/workflows/cd.yml`). When triggered (automatically after `Publish` builds GHCR images or manually from the **Actions** tab), GitHub connects securely via SSH, updates configurations, pulls pre-built images, cleanly tears down previous containers, boots the fresh stack, and verifies the readiness gate (`/readyz`).

### GitHub Secrets Configuration

To enable automated CD to your server, add the following secrets in your repository settings (**Settings ➔ Secrets and variables ➔ Actions**):

| Secret Name | Description |
|---|---|
| `SERVER_HOST` | Target server IP address or hostname (e.g. `80.47.236.99`) |
| `SERVER_PORT` | SSH port (default: `22`) |
| `SERVER_USER` | SSH user (e.g. `root`) |
| `SERVER_SSH_KEY` | Private SSH key (e.g. OpenSSH Ed25519) |
| `SERVER_PASSWORD` | SSH password (optional, if not using SSH key) |
| `ATF_BOT_TOKEN` | Telegram Bot token from @BotFather |
| `ATF_API_ID` | Telegram API ID from my.telegram.org |
| `ATF_API_HASH` | Telegram API Hash from my.telegram.org |
| `ATF_MASTER_KEY` | Master encryption key (`openssl rand -hex 32`) |
| `ATF_API_KEY` | REST API key |
| `ATF_WEB_URL` | Public web dashboard URL (e.g. `https://your-domain.com`) |
| `ADMIN_USER_ID` | Primary admin Telegram user ID |
| `ADMIN_IDS` | Comma-separated admin IDs |
| `ATF_JWT_SECRET` | Secret key for JWT session signing |
| `ATF_REDIS_ADDR` | Redis address (default: `redis:6379`) |
| `ATF_REDIS_PASSWORD`| Redis password |
| `ATF_CORE_RPC_TOKEN`| Token for internal gRPC RPC communication |

### Migrating to a New Server in the Future
When moving to a new server:
1. Update `SERVER_HOST` (and `SERVER_SSH_KEY` or `SERVER_PASSWORD`) in your GitHub Secrets.
2. Go to the **Actions** tab in GitHub, select the **cd** workflow, and click **Run workflow**.
3. GitHub Actions will SSH into the new server, pull latest GHCR images, cleanly recreate the containers, and verify readiness automatically.

