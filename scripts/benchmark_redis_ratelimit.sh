#!/usr/bin/env sh
# Reproducible rate-limit benchmark harness: baseline (no Redis) vs final (Redis
# store) under identical conditions, with an outage/recovery scenario.
#
#   scripts/benchmark_redis_ratelimit.sh build
#   scripts/benchmark_redis_ratelimit.sh run   base|final
#   scripts/benchmark_redis_ratelimit.sh attack
#   scripts/benchmark_redis_ratelimit.sh outage
#
# Environment knobs:
#   ATF_BENCH_REDIS_ADDR  (default 127.0.0.1:6399)  the store under test
#   VEGETA                (default /root/.hermes/cache/scratch/redis-bench/vegeta)
#   BENCH_DURATION        (default 30s)
#   BENCH_OUT_DIR         (default docs/benchmarks/results)
#
# Everything the harness starts is torn down again; the API it starts connects
# to the bench store only.
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GO_IMAGE="${GO_IMAGE:-golang:1.27-alpine}"
ATF_BENCH_REDIS_ADDR="${ATF_BENCH_REDIS_ADDR:-127.0.0.1:6399}"
VEGETA="${VEGETA:-/root/.hermes/cache/scratch/redis-bench/vegeta}"
BENCH_DURATION="${BENCH_DURATION:-30s}"
BENCH_OUT_DIR="${BENCH_OUT_DIR:-/root/.hermes/cache/scratch/redis-bench/results}"
API_PORT="${API_PORT:-8087}"
BENCH_API_NAME="atf-api-bench"

echo "== benchmark harness; repo $REPO_DIR"

build() {
    # Builds the API inside the pinned build image; CGO off keeps the binary
    # static so the throwaway runtime image needs only a shell.
    mkdir -p "$BENCH_OUT_DIR"
    docker run --rm -v "$REPO_DIR":/repo -v "$BENCH_OUT_DIR":/out -w /repo/api \
        -e CGO_ENABLED=0 "$GO_IMAGE" \
        sh -c "go build -trimpath -o /out/atf-api-bench ."
    ls -l "$BENCH_OUT_DIR/atf-api-bench"
}

start_api() {  # $1 = phase label (base|final): base runs with the store unset.
    mkdir -p "$BENCH_OUT_DIR"
    package_image
    docker rm -f "$BENCH_API_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$BENCH_API_NAME" --network host \
        -v "$REPO_DIR":/repo -w "$BENCH_OUT_DIR" \
        -e ATF_API_ADDR=":$API_PORT" \
        -e ATF_JWT_SECRET="${BENCH_JWT_SECRET:-bench-not-used-for-limits}" \
        -e ATF_REDIS_ADDR="$(case "$1" in base) echo "";; *) echo "$ATF_BENCH_REDIS_ADDR";; esac)" \
        -e ATF_RATELIMIT_PROBE_PER_MINUTE=600000 \
        -e ATF_RATELIMIT_PROBE_BURST=600000 \
        atf-api-bench-image:latest >/dev/null
    sleep 1
    i=0
    until curl -fsS "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1; do
        i=$((i + 1)); [ "$i" -gt 15 ] && { echo "FAIL: bench API never became ready"; return 1; }
        sleep 1
    done
    echo "bench API $1 ready on :$API_PORT"
}

# The binary is built statically (CGO off) and shipped into a throwaway image.
package_image() {
    cat >"$BENCH_OUT_DIR/Dockerfile.bench" <<'EOF'
FROM golang:1.27-alpine
WORKDIR /srv
COPY atf-api-bench /srv/atf-api-bench
ENTRYPOINT ["/srv/atf-api-bench"]
EOF
    (cd "$BENCH_OUT_DIR" && docker build -q -f Dockerfile.bench -t atf-api-bench-image . >/dev/null)
    rm -f "$BENCH_OUT_DIR/Dockerfile.bench"
}

attack() {  # $1 = label, $2 = rate/s, $3 = target file name
    label="$1"; rate="$2"; targets="$3"
    $VEGETA attack \
        -targets "$BENCH_OUT_DIR/$targets" \
        -rate "${rate}/1s" \
        -duration "$BENCH_DURATION" \
        -name "$label" \
        -output "$BENCH_OUT_DIR/bin.$label"
    $VEGETA report -type json "$BENCH_OUT_DIR/bin.$label" >"$BENCH_OUT_DIR/json.$label"
}

outage() {  # auth must fail closed while the store is gone, and recover.
    label=outage; rate=500
    start_attack_bg() {
        :;
    }
    log="$BENCH_OUT_DIR/bin.$label"
    # Attack and crash the store 10s in, restart it after another 10s.
    ($VEGETA attack -targets "$BENCH_OUT_DIR/targets_login" \
        -rate "${rate}/1s" -duration 30s -name "$label" -output "$log") &
    runner=$!
    sleep 10
    docker kill --signal=KILL atf-bench-redis >/dev/null
    sleep 10
    docker start atf-bench-redis >/dev/null
    wait "$runner"
    $VEGETA report -type json "$log" >"$BENCH_OUT_DIR/json.$label"
}

case "${1:-}" in
    build) build ;;
    run) start_api "$2" ;;
    attack) shift; attack "$@" ;;
    outage) outage "$@" ;;
    *) echo "usage: $0 build|run base\\|final|attack label rate targetfile|outage"; exit 2 ;;
esac
