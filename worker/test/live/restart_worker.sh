#!/usr/bin/env bash
# restart_worker.sh — host-side companion to worker/test/live/run_matrix.py.
#
# The matrix runs INSIDE atf-core (pyrogram + CryptoService live there), but
# atf-core has no docker CLI and cannot restart atf-worker. This watcher
# polls the in-container signal file /tmp/atf_matrix_restart_now, bounces
# atf-worker exactly once per signal, and removes the file.
#
# Usage:  bash worker/test/live/restart_worker.sh
set -u

SIG=/tmp/atf_matrix_restart_now
echo "[restart_worker] watching $SIG (docker restart atf-worker on demand)"

while true; do
  if docker exec atf-core test -f "$SIG" 2>/dev/null; then
    echo "[restart_worker] signal seen — restarting atf-worker"
    docker restart atf-worker
    rc=$?
    echo "[restart_worker] docker restart rc=$rc"
    docker exec atf-core rm -f "$SIG"
    echo "[restart_worker] signal consumed; resuming watch"
  fi
  sleep 1
done
