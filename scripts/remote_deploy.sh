#!/usr/bin/env bash
# scripts/remote_deploy.sh — Turnkey Continuous Deployment runner for AutoTelegramForward
#
# Designed to run remotely via GitHub Actions (appleboy/ssh-action) or directly
# on any Linux host. Ingests GitHub Secrets passed as environment variables,
# writes/syncs .env securely, pulls the latest GHCR images, cleanly tears down
# previous application containers, starts the fresh stack, and verifies
# health and readiness endpoints (/readyz and /healthz).
#
# Usage:
#   scripts/remote_deploy.sh
#   ATF_IMAGE_TAG=sha-abc1234 scripts/remote_deploy.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/root/AutoTelegramForward}"
COMPOSE_PROJECT="autotelegramforward"
ATF_IMAGE_TAG="${ATF_IMAGE_TAG:-master}"
ATF_GHCR_OWNER="${ATF_GHCR_OWNER:-opselon}"
HEALTH_TIMEOUT="${ATF_HEALTH_TIMEOUT:-90}"
READY_TIMEOUT="${ATF_READY_TIMEOUT:-45}"
READY_URL="${ATF_READY_URL:-http://127.0.0.1:8080/readyz}"
HEALTH_URL="${ATF_HEALTH_URL:-http://127.0.0.1:8080/healthz}"

echo "============================================================"
echo "  AutoTelegramForward — Continuous Deployment Runner"
echo "  Target directory: $APP_DIR"
echo "  Image tag:        $ATF_IMAGE_TAG"
echo "  GHCR owner:       $ATF_GHCR_OWNER"
echo "============================================================"

# 1. Ensure project repository exists
if [ ! -d "$APP_DIR/.git" ]; then
  echo "== repository not found at $APP_DIR; cloning from GitHub =="
  mkdir -p "$(dirname "$APP_DIR")"
  git clone https://github.com/Opselon/AutoTelegramForward.git "$APP_DIR"
fi

cd "$APP_DIR"

# 2. Update git repository if git remote is accessible
if git remote >/dev/null 2>&1; then
  echo "== updating repository worktree =="
  git fetch origin master || true
  git checkout master 2>/dev/null || git checkout main 2>/dev/null || true
  git pull --ff-only origin master 2>/dev/null || git pull --ff-only origin main 2>/dev/null || true
fi

# 3. Synchronize environment variables and secrets into .env safely
ENV_FILE="$APP_DIR/.env"
TMP_ENV="$(mktemp "$APP_DIR/.env.tmp.XXXXXX")"
trap 'rm -f "$TMP_ENV"' EXIT

if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$TMP_ENV"
else
  touch "$TMP_ENV"
fi

# List of managed secrets passed via environment
SECRET_KEYS=(
  "ATF_BOT_TOKEN"
  "ATF_API_ID"
  "ATF_API_HASH"
  "ATF_MASTER_KEY"
  "ATF_API_KEY"
  "ATF_WEB_URL"
  "ADMIN_USER_ID"
  "ADMIN_IDS"
  "ATF_JWT_SECRET"
  "ATF_REDIS_ADDR"
  "ATF_REDIS_PASSWORD"
  "ATF_CORE_RPC_TOKEN"
  "ATF_RAW_API_TRACE"
)

# Helper to upsert key=value into temporary env file
upsert_env_key() {
  local key="$1"
  local val="$2"
  if grep -q "^${key}=" "$TMP_ENV"; then
    # Use python for safe line replacement without sed delimiter collision
    python3 -c "
import sys
key = sys.argv[1]
val = sys.argv[2]
path = sys.argv[3]
with open(path, 'r') as f:
    lines = f.readlines()
with open(path, 'w') as f:
    for line in lines:
        if line.startswith(key + '='):
            f.write(f'{key}={val}\n')
        else:
            f.write(line)
" "$key" "$val" "$TMP_ENV"
  else
    echo "${key}=${val}" >> "$TMP_ENV"
  fi
}

for key in "${SECRET_KEYS[@]}"; do
  val="${!key:-}"
  if [ -n "$val" ]; then
    upsert_env_key "$key" "$val"
  fi
done

# Ensure defaults for required fields if still missing
[ -n "${ATF_IMAGE_TAG:-}" ] && upsert_env_key "ATF_IMAGE_TAG" "$ATF_IMAGE_TAG"
[ -n "${ATF_GHCR_OWNER:-}" ] && upsert_env_key "ATF_GHCR_OWNER" "$ATF_GHCR_OWNER"

mv "$TMP_ENV" "$ENV_FILE"
chmod 0600 "$ENV_FILE"
echo "== synced environment secrets to .env (chmod 0600) =="

# 4. Prepare directories and volumes
mkdir -p "$APP_DIR/.atf" "$APP_DIR/atf-data" "$APP_DIR/logger-rust-data"
chmod 0750 "$APP_DIR/.atf" "$APP_DIR/atf-data" "$APP_DIR/logger-rust-data"

# 5. Pull latest Docker images from GHCR
echo "== pulling fresh images for tag '$ATF_IMAGE_TAG' =="
ATF_IMAGE_TAG="$ATF_IMAGE_TAG" ATF_GHCR_OWNER="$ATF_GHCR_OWNER" docker compose \
  -p "$COMPOSE_PROJECT" \
  --env-file "$ENV_FILE" \
  -f docker-compose.yml \
  -f docker-compose.ghcr.yml \
  pull --quiet redis logger core api web

# 6. Clean container teardown ("همیشه داکر قبلی رو بخوابونه جدید و تمیز بیاد")
echo "== cleanly stopping and removing previous application containers =="
docker stop -t 15 atf-web atf-api atf-core atf-logger 2>/dev/null || true
docker rm -f atf-web atf-api atf-core atf-logger 2>/dev/null || true

# 7. Start fresh stack
echo "== launching clean containers =="
ATF_IMAGE_TAG="$ATF_IMAGE_TAG" ATF_GHCR_OWNER="$ATF_GHCR_OWNER" docker compose \
  -p "$COMPOSE_PROJECT" \
  --env-file "$ENV_FILE" \
  -f docker-compose.yml \
  -f docker-compose.ghcr.yml \
  up -d --remove-orphans redis logger core api web

# 8. Verify container health
echo "== verifying container health (timeout: ${HEALTH_TIMEOUT}s) =="
SERVICES=(redis logger core api web)
deadline=$((SECONDS + HEALTH_TIMEOUT))
while [ "$SECONDS" -lt "$deadline" ]; do
  all_ok=1
  for svc in "${SERVICES[@]}"; do
    cname="atf-$svc"
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cname" 2>/dev/null || echo missing)
    case "$status" in
      healthy|running) ;;
      *)
        all_ok=0
        ;;
    esac
  done
  if [ "$all_ok" -eq 1 ]; then
    echo "== all containers are healthy/running =="
    break
  fi
  sleep 2
done

if [ "$all_ok" -ne 1 ]; then
  echo "error: one or more services failed to report healthy within ${HEALTH_TIMEOUT}s" >&2
  docker compose -p "$COMPOSE_PROJECT" ps >&2 || true
  exit 1
fi

# 9. Verify readiness gate (/readyz)
echo "== verifying application readiness at $READY_URL (timeout: ${READY_TIMEOUT}s) =="
ready_deadline=$((SECONDS + READY_TIMEOUT))
ready_passed=0
while [ "$SECONDS" -lt "$ready_deadline" ]; do
  if body=$(curl -fsS --max-time 4 "$READY_URL" 2>/dev/null); then
    echo "== readiness confirmed: $body =="
    ready_passed=1
    break
  fi
  sleep 2
done

if [ "$ready_passed" -ne 1 ]; then
  echo "error: readiness check at $READY_URL failed" >&2
  curl -sS --max-time 4 "$READY_URL" >&2 2>/dev/null || true
  exit 1
fi

# 10. Verify /healthz
if curl -fsS --max-time 4 "$HEALTH_URL" >/dev/null 2>&1; then
  echo "== /healthz probe passed =="
fi

# 11. Record deployed tag
echo "$ATF_IMAGE_TAG" > "$APP_DIR/.atf/deployed-tag"

# 12. Prune dangling images to conserve server disk space
echo "== pruning dangling docker images =="
docker image prune -f || true

echo "============================================================"
echo "  Continuous Deployment Completed Successfully!"
echo "  Active Tag: $ATF_IMAGE_TAG"
echo "============================================================"
docker compose -p "$COMPOSE_PROJECT" ps
