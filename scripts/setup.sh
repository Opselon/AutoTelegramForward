#!/usr/bin/env bash
# One-command bootstrap for a fresh AutoTelegramForward checkout.
#
#   ./scripts/setup.sh              # full guided setup (preflight → env → build → start → health)
#   ./scripts/setup.sh --check      # read-only readiness report, changes nothing
#
# It installs the CLI (virtualenv + `atf` on PATH) and then hands off to
# `atf setup`, which owns the rest of the sequence. Every step it takes is
# idempotent, so running it again is safe.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. prerequisites
command -v python3 >/dev/null 2>&1 || die "python3 is required"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 >= 3.11 is required (found $(python3 -V 2>&1))"

if command -v docker >/dev/null 2>&1; then
  docker info >/dev/null 2>&1 || warn "docker daemon is not reachable — start it before the stack can come up"
else
  warn "docker is not installed — install it (https://docs.docker.com/engine/install/) to run the stack"
fi

# ---------------------------------------------------------------- 2. virtualenv
if [ ! -x .venv/bin/python ]; then
  say "creating virtualenv .venv"
  python3 -m venv .venv
fi
say "installing python dependencies"
.venv/bin/python -m pip install --quiet --upgrade pip
installed_any=0
for req in requirements.txt core/requirements.txt; do
  if [ -f "$req" ]; then
    .venv/bin/python -m pip install --quiet -r "$req"
    installed_any=1
  fi
done
[ "$installed_any" = "1" ] || warn "no requirements file found — dependencies were not installed"

# ---------------------------------------------------------------- 3. CLI on PATH
chmod +x "$ROOT/atf" 2>/dev/null || true
if [ -w /usr/local/bin ] 2>/dev/null; then
  ln -sf "$ROOT/atf" /usr/local/bin/atf
  say "linked /usr/local/bin/atf -> $ROOT/atf"
else
  warn "cannot write /usr/local/bin — add $ROOT to PATH to use the 'atf' command"
fi

# ---------------------------------------------------------------- 4. hand off
say "handing off to: atf setup $*"
exec "$ROOT/atf" setup "$@"
