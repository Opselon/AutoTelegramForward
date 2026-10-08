#!/usr/bin/env bash
# Run both services: Python core (bot + forwarder + gRPC) and Go API
# Configure via config.yaml or ATF_* environment variables first.

set -e
cd "$(dirname "$0")/.."

echo "Starting Python core (bot + gRPC)..."
.venv/Scripts/python.exe -m core.main &
CORE_PID=$!
sleep 3

echo "Starting Go API..."
(cd api && go build -o atf-api.exe . && ./atf-api.exe) &
API_PID=$!

trap "kill $CORE_PID $API_PID 2>/dev/null" EXIT
wait
