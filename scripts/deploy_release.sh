#!/usr/bin/env bash
# Deploy published images on this host — no compilation here.
#
# The images come from the Publish workflow (CI), tagged `sha-<short>` with a
# moving `master` tag. This script pulls that tag and recreates the services, so
# the running container is byte-for-byte the artifact CI built and tested: a
# local `docker compose build` produces a *different* image from the same commit
# and cannot be compared with what the pipeline verified.
#
#   ATF_IMAGE_TAG=sha-abc1234 scripts/deploy_release.sh            # pull + deploy
#   ATF_IMAGE_TAG=sha-abc1234 scripts/deploy_release.sh --dry-run  # say what would change
#   scripts/deploy_release.sh --build                             # legacy local build
#
# Rollback: pass the previous `sha-<short>`. The script writes the tag it
# deployed to .atf/deployed-tag so the previous one is always recoverable.
#
# Exit codes: 0 deployed and healthy · 1 deploy or health check failed (the
# previous tag is restored first) · 2 refused (bad usage, missing login).
set -euo pipefail

COMPOSE_PROJECT="${COMPOSE_PROJECT:-autotelegramforward}"
COMPOSE_DIR="${ATF_COMPOSE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${ATF_ENV_FILE:-$COMPOSE_DIR/.env}"
# Which services to move. All four by default; override to move a subset
# (ATF_SERVICES="core api") when only some images changed.
read -r -a SERVICES <<< "${ATF_SERVICES:-core api web logger}"
HEALTH_TIMEOUT="${ATF_HEALTH_TIMEOUT:-90}"
DEPLOYED_TAG_FILE="$COMPOSE_DIR/.atf/deployed-tag"

MODE="pull"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --build) MODE="build" ;;
    --pull) MODE="pull" ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

TAG="${ATF_IMAGE_TAG:-master}"
OWNER="${ATF_GHCR_OWNER:-opselon}"
REGISTRY="${ATF_REGISTRY:-ghcr.io}"
IMAGE_PREFIX="$REGISTRY/$OWNER/atf"

compose() {
  local files=(-f "$COMPOSE_DIR/docker-compose.yml")
  [ -f "$COMPOSE_DIR/docker-compose.9router.yml" ] && files+=(-f "$COMPOSE_DIR/docker-compose.9router.yml")
  if [ "$MODE" = "pull" ]; then
    files+=(-f "$COMPOSE_DIR/docker-compose.ghcr.yml")
  fi
  docker compose -p "$COMPOSE_PROJECT" --env-file "$ENV_FILE" "${files[@]}" "$@"
}

# The images this stack is actually running, newest first — the rollback target.
previous_tag() {
  local image
  image=$(docker inspect -f '{{.Config.Image}}' atf-core 2>/dev/null || true)
  case "$image" in
    *"$IMAGE_PREFIX-core:"*) echo "${image##*:}" ;;
    *) echo "" ;;
  esac
}

# Pull every service before `up`. With `image:` and `build:` both set in the
# merged configuration, `up` would *compile* the one image it cannot find — the
# exact cost this overlay exists to avoid. Pulling all of them first makes the
# pull path fail closed (exit 2) instead of quietly building on the host.
pull_images() {
  if compose pull --quiet "${SERVICES[@]}"; then
    return 0
  fi
  cat >&2 <<EOF

error: could not pull $IMAGE_PREFIX-*:$TAG

The package is private by default. Either log in first:

    echo "\$GHCR_TOKEN" | docker login $REGISTRY -u <github-user> --password-stdin

(the token needs read:packages), or make the package public in the org's
package settings. Until then, deploy with --build (compiles on this host) or
publish images with the Publish workflow.
EOF
  exit 2
}

wait_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) name status
  while [ "$SECONDS" -lt "$deadline" ]; do
    local all_up=1
    for name in "${SERVICES[@]}"; do
      name="atf-$name"
      status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || echo missing)
      case "$status" in
        healthy|running) ;;
        *) all_up=0 ;;
      esac
    done
    [ "$all_up" = "1" ] && return 0
    sleep 3
  done
  return 1
}

if [ "$MODE" = "build" ]; then
  echo "== local build (no registry): this compiles on this host by request =="
  compose build "${SERVICES[@]}"
else
  echo "== deploying published images: $IMAGE_PREFIX-*:$TAG =="
  [ "$DRY_RUN" = "0" ] && pull_images
fi

BEFORE="$(previous_tag || true)"
echo "== currently running: ${BEFORE:-unknown} =="

if [ "$DRY_RUN" = "1" ]; then
  echo "== dry run: what 'up' would change =="
  compose up -d --no-deps --dry-run "${SERVICES[@]}" 2>&1 || compose config --services >/dev/null
  exit 0
fi

compose up -d --no-deps "${SERVICES[@]}"

if ! wait_healthy; then
  echo "error: services did not become healthy within ${HEALTH_TIMEOUT}s" >&2
  compose ps >&2 || true
  if [ "$MODE" = "pull" ] && [ -n "$BEFORE" ] && [ "$BEFORE" != "$TAG" ]; then
    echo "== rolling back to $BEFORE ==" >&2
    ATF_IMAGE_TAG="$BEFORE" "$0" --pull || echo "rollback also failed" >&2
  else
    echo "== no previous tag recorded; leaving the stack as is for inspection ==" >&2
  fi
  exit 1
fi

mkdir -p "$(dirname "$DEPLOYED_TAG_FILE")"
echo "$TAG" > "$DEPLOYED_TAG_FILE"

echo "== healthy =="
compose ps
if [ "$MODE" = "pull" ]; then
  # What is actually running, per service. `RepoDigests` is empty for an image
  # fetched by tag through compose, so it reported "unknown" for every service on
  # the first pull-based deploy — the tag deployed and the resolved image ID are
  # the handles that always exist.
  for service in "${SERVICES[@]}"; do
    ref=$(docker inspect -f '{{.Config.Image}}' "atf-$service" 2>/dev/null || true)
    id=$(docker inspect -f '{{.Image}}' "atf-$service" 2>/dev/null || true)
    echo "atf-$service  ${ref:-unknown}  ${id:0:19}"
  done
fi
echo "deployed tag: $TAG   (previous: ${BEFORE:-none})"
