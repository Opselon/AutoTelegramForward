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
# Two gates must pass before anything is recorded as deployed: container health
# AND readiness (the API's /readyz, ATF_READY_URL · ATF_READY_TIMEOUT). Health
# alone was not enough — a stack whose Redis credential was missing stayed healthy
# while refusing every authentication request. Set ATF_SKIP_READY_GATE=1 to deploy
# without that verification.
#
# Exit codes: 0 deployed, healthy and ready · 1 deploy, health or readiness check
# failed (the previous tag is restored first) · 2 refused (bad usage, missing
# login).
set -euo pipefail

COMPOSE_PROJECT="${COMPOSE_PROJECT:-autotelegramforward}"
COMPOSE_DIR="${ATF_COMPOSE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${ATF_ENV_FILE:-$COMPOSE_DIR/.env}"
# Which services to move. All four by default; override to move a subset
# (ATF_SERVICES="core api") when only some images changed.
read -r -a SERVICES <<< "${ATF_SERVICES:-core api web logger}"
HEALTH_TIMEOUT="${ATF_HEALTH_TIMEOUT:-90}"
# Readiness gate. Container health says the processes answer; readiness says the
# stack can enforce what it promises (see the wait_ready function below). The URL
# is where the API publishes its /readyz, from the host's point of view.
READY_URL="${ATF_READY_URL:-http://127.0.0.1:8080/readyz}"
READY_TIMEOUT="${ATF_READY_TIMEOUT:-30}"
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

# wait_ready probes the API's readiness endpoint until it reports ready, because
# "the containers are healthy" is not the same claim as "this deployment works".
#
# A live example this gate exists for: compose substituted an empty
# ATF_REDIS_PASSWORD for the api and core containers (the deploy was pointed at an
# env file that did not carry it) while redis still required one. Every container
# became healthy, /healthz answered 200, and the deploy finished with its
# "== healthy ==" banner — but the api refused every token exchange with
# 503 rate_limit_unavailable and core degraded to "assume it is the only
# replica". /readyz answers 503 for exactly that state, so a deploy that cannot
# enforce its limits now fails here instead of being recorded as deployed.
wait_ready() {
  local deadline=$((SECONDS + READY_TIMEOUT)) body
  if [ "${ATF_SKIP_READY_GATE:-0}" = "1" ]; then
    echo "warning: readiness not verified (ATF_SKIP_READY_GATE=1) — the stack may be unable to enforce its limits" >&2
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    # Never report a stack verified that nobody checked: without curl the gate
    # cannot answer its own question, so it refuses rather than waving the deploy
    # through. ATF_SKIP_READY_GATE=1 is the explicit way to accept that risk.
    echo "error: curl is required to verify readiness at $READY_URL; install it or set ATF_SKIP_READY_GATE=1" >&2
    return 1
  fi
  while [ "$SECONDS" -lt "$deadline" ]; do
    if body=$(curl -fsS --max-time 4 "$READY_URL" 2>/dev/null); then
      echo "== ready: $body"
      return 0
    fi
    sleep 2
  done
  echo "error: $READY_URL did not report ready within ${READY_TIMEOUT}s" >&2
  # Show the endpoint's own explanation (it names the dependency and, for the
  # limiter, the redacted store error) so the failure is actionable from the log.
  curl -sS --max-time 4 "$READY_URL" >&2 2>/dev/null || true
  return 1
}

# rollback restores the previously deployed tag. Shared by both gates so a stack
# that is healthy but not ready is rolled back the same way as one that never
# became healthy.
rollback() {
  if [ "$MODE" = "pull" ] && [ -n "$BEFORE" ] && [ "$BEFORE" != "$TAG" ]; then
    echo "== rolling back to $BEFORE ==" >&2
    ATF_IMAGE_TAG="$BEFORE" "$0" --pull || echo "rollback also failed" >&2
  else
    echo "== no previous tag recorded; leaving the stack as is for inspection ==" >&2
  fi
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
  rollback
  exit 1
fi

if ! wait_ready; then
  echo "error: containers are healthy but the deployment is not ready: it cannot enforce what it promises" >&2
  compose ps >&2 || true
  rollback
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
