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
python3 "$ROOT/worker/internal/parity/reference_branding.py" > "$ROOT/worker/internal/parity/reference_branding.json"

python3 - "$ROOT" <<'EOF'
import json, sys

root = sys.argv[1]
for name in ("reference_filter.json", "reference_branding.json"):
    path = f"{root}/worker/internal/parity/{name}"
    n = len(json.load(open(path)))
    print(f"regen: {n} reference decisions -> {path}")
EOF
