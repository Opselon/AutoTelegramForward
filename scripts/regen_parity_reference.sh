#!/usr/bin/env sh
# Regenerate the cross-engine parity reference (reference_filter.json).
#
# Run this whenever a fixture in worker/internal/parity/fixtures changes or
# the Python reference behavior itself changes. The output IS the committed
# ground truth the Go parity suite compares against, so every diff is a
# reviewable behavioral change — do not edit it by hand.
#
# Usage: scripts/regen_parity_reference.sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/worker/internal/parity/reference_filter.json"

python3 "$ROOT/worker/internal/parity/reference_filter.py" > "$OUT"

python3 - "$OUT" <<'EOF'
import json, sys
n = len(json.load(open(sys.argv[1])))
print(f"regen: {n} reference decisions -> {sys.argv[1]}")
EOF
