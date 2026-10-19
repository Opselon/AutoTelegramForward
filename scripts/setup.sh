#!/usr/bin/env bash
# One-command bootstrap for a fresh AutoTelegramForward checkout (Linux/macOS).
#
#   ./scripts/setup.sh                 # venv + deps + `atf` on PATH + guided setup
#   ./scripts/setup.sh --check         # read-only readiness report, changes nothing
#   ./scripts/setup.sh --no-run        # install the CLI only
#   ./scripts/setup.sh -- --no-start   # extra args after `--` go to `atf setup`
#
# On Windows run the same program directly:  python scripts\bootstrap.py
# The work lives in scripts/bootstrap.py so all three platforms behave the same;
# this file only locates a Python 3.11+ interpreter and hands over.
set -euo pipefail

# `$0` instead of ${BASH_SOURCE[0]} so this also works when invoked as
# `sh scripts/setup.sh` (dash, minimal images) and not only as `bash ...`.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

say() { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# `python3` is not always present on minimal images, and some hosts only expose
# `python3.12`. Walk the supported versions newest-first, then fall back to
# `python3` *only if* it is inside the range: the ceiling is real (see
# `atfcli/platforms.py` — pyrogram's sync layer needs the implicit event loop
# Python 3.14 removed), and silently picking a 3.14 interpreter builds a venv
# whose core cannot import pyrogram.
PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; then
      PY="$candidate"
      break
    fi
  fi
done
[ -n "$PY" ] || die "python 3.11-3.13 is required and was not found on PATH (found: $(command -v python3 >/dev/null 2>&1 && python3 -V 2>&1 || echo none)). Python 3.14 is not supported yet — pyrogram's sync layer needs the implicit event loop Python 3.14 removed. Install python3.12/3.13, or run the stack in docker mode (docker compose up -d)."

say "using $("$PY" -V 2>&1) at $(command -v "$PY")"
exec "$PY" "$ROOT/scripts/bootstrap.py" "$@"
