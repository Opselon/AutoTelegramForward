#!/usr/bin/env bash
# ============================================================================
#  AutoTelegramForward — one-command installer  (Linux / macOS / WSL / *BSD)
# ----------------------------------------------------------------------------
#  Install:
#     curl -fsSL https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/install.sh | bash
#
#  What this does:
#    1. Makes sure git and Python >= 3.10 (with pip + venv) are available,
#       installing them with your system package manager if they are missing.
#    2. Clones AutoTelegramForward (unless you are already inside a copy).
#    3. Hands off to:   python atf.py setup
#       which asks ONLY for your Telegram bot token and does the rest:
#       virtualenv, dependencies, config.yaml, and an auto-generated
#       encryption master key.
#
#  Options (also available as environment variables):
#     --dir PATH      Directory to clone into          ATF_INSTALL_DIR
#     --branch NAME   Branch/tag to clone              ATF_BRANCH
#     --with-go       Also install the Go toolchain (optional REST API)
#     --force         Replace an existing clone
#     --help          Show usage
#
#  Running Windows (Git Bash / MSYS / Cygwin)? Use install.ps1 instead:
#     irm https://raw.githubusercontent.com/Opselon/AutoTelegramForward/master/install.ps1 | iex
# ============================================================================

set -euo pipefail

REPO="Opselon/AutoTelegramForward"
REPO_URL="https://github.com/${REPO}.git"
DEFAULT_BRANCH="master"

INSTALL_DIR="${ATF_INSTALL_DIR:-}"
BRANCH="${ATF_BRANCH:-$DEFAULT_BRANCH}"
WITH_GO="${ATF_WITH_GO:-0}"
FORCE=0
WORK=""
SUDO=""
OS=""
ARCH=""
PY=""

# ---------------------------------------------------------------- output ----
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BLUE=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
fi

banner() {
  printf '%s\n' "${C_BOLD}${C_BLUE}"
  printf '%s\n' "    ___                _____         _            _____           _ "
  printf '%s\n' "   / _ |  ___  ___ _ / ___/  ____  (_)__  ___   / ___/  ____ __ (_)__ ___"
  printf '%s\n' "  / __ | / _ \/ _  // /__   / __ \/ / _ \(_-<  / /__   / __// // / -_) _ \\"
  printf '%s\n' " /_/ |_|/ .__/\_,_/ \___/  /_/ /_/_/\___/___/  \___/  /_/   \_,_/\__/_//_/"
  printf '%s\n' "        /_/    Telegram auto-forwarder  •  bot + Go REST API${C_RESET}"
  printf '%s\n\n' "${C_DIM}One-command installer — you only need a Telegram bot token (from @BotFather).${C_RESET}"
}

step()  { printf '%s %s%s%s\n' "${C_GREEN}✔${C_RESET}" "${C_BOLD}" "$1" "${C_RESET}"; }
info()  { printf '  %s%s%s\n' "${C_DIM}" "$1" "${C_RESET}"; }
warn()  { printf '%s %s%s%s\n' "${C_YELLOW}!${C_RESET}" "${C_YELLOW}" "$1" "${C_RESET}" >&2; }
die()   { printf '%s %s%s%s\n' "${C_RED}✗${C_RESET}" "${C_RED}" "$1" "${C_RESET}" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

on_exit() {
  [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"
}
trap on_exit EXIT

usage() {
  sed -n '3,26p' "$0" 2>/dev/null | sed 's/^#\s\?//' || true
  exit 0
}

# ------------------------------------------------------------------ args ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)      INSTALL_DIR="${2:-}"; shift 2 ;;
    --dir=*)    INSTALL_DIR="${1#*=}"; shift ;;
    --branch)   BRANCH="${2:-}"; shift 2 ;;
    --branch=*) BRANCH="${1#*=}"; shift ;;
    --with-go)  WITH_GO=1; shift ;;
    --force)    FORCE=1; shift ;;
    -h|--help)  usage ;;
    *)          die "Unknown option: $1 (try --help)" ;;
  esac
done

banner

# ------------------------------------------------------------------ env -----
[[ $EUID -eq 0 ]] || SUDO="sudo"
[[ "$WITH_GO" == "1" || "$WITH_GO" == "true" || "$WITH_GO" == "yes" ]] || WITH_GO=0

WORK="$(mktemp -d 2>/dev/null || echo "/tmp/atf-install.$$")"
mkdir -p "$WORK"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m | tr '[:upper:]' '[:lower:]')"
case "$OS" in
  linux*)   OS="linux" ;;
  darwin*)  OS="darwin" ;;
  freebsd*|netbsd*|openbsd*) OS="bsd" ;;
  mingw*|msys*|cygwin*)
    OS="msys"
    warn "You are running Git Bash / MSYS / Cygwin on Windows. The recommended"
    warn "installer for Windows is the PowerShell one:"
    warn "  irm https://raw.githubusercontent.com/${REPO}/master/install.ps1 | iex"
    warn "Continuing with the POSIX script because git + python are usable from bash..."
    ;;
esac
case "$ARCH" in
  x86_64|amd64|i386|i686) ARCH="amd64" ;;
  aarch64|arm64|armv8l)   ARCH="arm64" ;;
esac
info "Platform: ${OS} (${ARCH})"

# ------------------------------------------------------------- packages -----
install_pkgs() {  # install_pkgs <manager> <pkg...>
  local mgr="$1"; shift
  case "$mgr" in
    apt-get) $SUDO apt-get update -qq; $SUDO apt-get install -y -qq "$@" ;;
    dnf)     $SUDO dnf install -y -q "$@" ;;
    yum)     $SUDO yum install -y -q "$@" ;;
    apk)     $SUDO apk add --no-cache --quiet "$@" ;;
    pacman)  $SUDO pacman -Sy --noconfirm --needed --quiet "$@" ;;
    zypper)  $SUDO zypper --non-interactive --quiet install "$@" ;;
    pkg)     $SUDO pkg install -y "$@" ;;
    *)       return 1 ;;
  esac
}

pkg_manager() {
  for m in apt-get dnf yum apk pacman zypper pkg; do
    have "$m" && { echo "$m"; return 0; }
  done
  return 1
}

# --------------------------------------------------------------- python -----
py_ok() {  # py_ok <interpreter> -> 0 if it is Python >= 3.10
  local v
  v="$("$1" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
  [[ -n "$v" ]] || return 1
  local major="${v%%.*}" minor="${v##*.}"
  [[ "$major" -ge 4 || ( "$major" -eq 3 && "$minor" -ge 10 ) ]]
}

resolve_python() {
  local c
  for c in python3 python python3.12 python3.11 python3.10; do
    if have "$c" && py_ok "$c"; then echo "$c"; return 0; fi
  done
  # Homebrew keg-only formulae are not on PATH by default.
  local prefix
  if have brew; then
    prefix="$(brew --prefix 2>/dev/null || true)"
    for v in 3.12 3.11 3.10; do
      if [[ -x "${prefix}/opt/python@${v}/bin/python${v}" ]] \
         && py_ok "${prefix}/opt/python@${v}/bin/python${v}"; then
        echo "${prefix}/opt/python@${v}/bin/python${v}"
        return 0
      fi
    done
  fi
  # macOS python.org installer
  for v in 3.12 3.11 3.10; do
    for p in "/Library/Frameworks/Python.framework/Versions/${v}/bin/python${v}" \
             "/usr/local/bin/python${v}"; do
      if [[ -x "$p" ]] && py_ok "$p"; then echo "$p"; return 0; fi
    done
  done
  return 1
}

bootstrap_pip() {  # bootstrap_pip <interpreter>
  local py="$1" getpip
  "$py" -m pip --version >/dev/null 2>&1 && return 0
  "$py" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$py" -m pip --version >/dev/null 2>&1 && return 0
  warn "pip is missing — bootstrapping with get-pip.py"
  getpip="$WORK/get-pip.py"
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$getpip" \
    || curl -fsSL https://raw.githubusercontent.com/pypa/get-pip/main/public/get-pip.py -o "$getpip" \
    || return 1
  "$py" "$getpip" --quiet
}

verify_venv() {  # verify_venv <interpreter> -> 0 if `python -m venv` produces a pip-enabled venv
  local py="$1" v="$WORK/venv-check" vp
  rm -rf "$v"
  "$py" -m venv "$v" >/dev/null 2>&1 || return 1
  vp="$v/bin/python"; [[ -x "$vp" ]] || vp="$v/Scripts/python.exe"
  "$vp" -m pip --version >/dev/null 2>&1 && return 0
  "$py" -m ensurepip --upgrade >/dev/null 2>&1 || true
  rm -rf "$v"
  "$py" -m venv "$v" >/dev/null 2>&1 || return 1
  vp="$v/bin/python"; [[ -x "$vp" ]] || vp="$v/Scripts/python.exe"
  "$vp" -m pip --version >/dev/null 2>&1
}

install_python() {
  local mgr pkgs
  case "$OS" in
    linux|bsd)
      mgr="$(pkg_manager || true)"
      [[ -n "$mgr" ]] || die "No package manager found and Python is missing.
  Install Python >= 3.10 manually (https://www.python.org/downloads/) and re-run this script."
      case "$mgr" in
        apt-get) pkgs="python3 python3-pip python3-venv ca-certificates" ;;
        dnf|yum) pkgs="python3 python3-pip" ;;
        apk)     pkgs="python3 py3-pip ca-certificates" ;;
        pacman)  pkgs="python python-pip" ;;
        zypper)  pkgs="python3 python3-pip" ;;
        pkg)     pkgs="python3 py3-pip" ;;
      esac
      [[ "$mgr" == "apt-get" ]] && pkgs="$pkgs git curl"
      step "Installing Python via ${mgr}: ${pkgs}"
      install_pkgs "$mgr" $pkgs \
        || die "Failed to install Python packages with ${mgr}. Install Python >= 3.10 manually and re-run."
      # Soft optional: makes the documented `python atf.py ...` work verbatim.
      if [[ "$mgr" == "apt-get" ]]; then
        install_pkgs apt-get python-is-python3 >/dev/null 2>&1 || true
      fi
      ;;
    darwin)
      if have brew; then
        step "Installing Python via Homebrew (python@3.12)"
        brew install python@3.12 >/dev/null 2>&1 \
          || brew install python >/dev/null 2>&1 \
          || die "brew install python failed. Install Python >= 3.10 manually and re-run."
      else
        local v url pkg
        for v in 3.12.10 3.11.9 3.10.16; do
          url="https://www.python.org/ftp/python/${v}/python-${v}-macos11.pkg"
          if curl -fsIL --max-time 10 "$url" >/dev/null 2>&1; then break; fi
          url=""
        done
        [[ -n "$url" ]] || die "Could not find a Python installer for macOS.
  Install Homebrew (https://brew.sh) or Python >= 3.10 (https://www.python.org/downloads/) and re-run."
        pkg="$WORK/python.pkg"
        step "Downloading official Python ${v} for macOS"
        curl -fsSL "$url" -o "$pkg" || die "Download failed: $url"
        step "Installing Python ${v} (you may be asked for your macOS password)"
        $SUDO installer -pkg "$pkg" -target / >/dev/null 2>&1 \
          || die "installer failed. Install Python >= 3.10 manually and re-run."
      fi
      ;;
    msys)
      die "Python was not found. On Windows, use the PowerShell installer instead:
  irm https://raw.githubusercontent.com/${REPO}/master/install.ps1 | iex
or install Python >= 3.10 from https://www.python.org/downloads/ and re-run."
      ;;
  esac
  hash -r 2>/dev/null || true
  resolve_python >/dev/null 2>&1 || die "Python was installed but is still not usable.
  Open a new terminal and re-run this script."
}

ensure_python() {
  if PY="$(resolve_python 2>/dev/null)"; then
    step "Found Python: $(command -v "$PY" 2>/dev/null || echo "$PY")  ($( "$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo ok))"
  else
    warn "Python >= 3.10 not found — installing it"
    install_python
    PY="$(resolve_python)" || die "Unable to locate a usable Python interpreter."
    step "Python ready: $( "$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo "$PY")"
  fi
  bootstrap_pip "$PY" \
    || die "pip could not be bootstrapped for $(command -v "$PY" 2>/dev/null || echo python).
  Try:  $(command -v "$PY" 2>/dev/null || echo python) -m ensurepip --upgrade"
  if ! verify_venv "$PY"; then
    if [[ "$OS" == "linux" ]] && have apt-get; then
      warn "venv needs the python3-venv package (Debian/Ubuntu strip ensurepip)."
      install_pkgs apt-get python3-venv >/dev/null 2>&1 || true
      verify_venv "$PY" || warn "venv still unavailable — 'python atf.py setup' will likely fail."
    else
      warn "'python -m venv' is not fully working — 'python atf.py setup' may fail."
    fi
  fi
}

# ------------------------------------------------------------------ git -----
ensure_git() {
  have git && { step "Found git: $(git --version 2>/dev/null | awk '{print $3}')"; return 0; }
  warn "git not found — installing it"
  case "$OS" in
    linux|bsd)
      local mgr
      mgr="$(pkg_manager || true)"
      [[ -n "$mgr" ]] || die "No package manager found and git is missing. Install git manually and re-run."
      install_pkgs "$mgr" git \
        || die "Failed to install git with ${mgr}. Install it manually and re-run."
      ;;
    darwin)
      if have brew; then
        brew install git >/dev/null 2>&1 || true
      else
        warn "Installing Xcode Command Line Tools (brings git) — accept the prompt..."
        xcode-select --install >/dev/null 2>&1 || true
        echo "${C_DIM}  Press Enter in the GUI prompt, then re-run this script once it finishes.${C_RESET}" >&2
        die "git is required to clone the repository."
      fi
      ;;
    msys)
      die "git is required. On Windows, use the PowerShell installer:
  irm https://raw.githubusercontent.com/${REPO}/master/install.ps1 | iex"
      ;;
  esac
  hash -r 2>/dev/null || true
  have git || die "git was installed but is not on PATH. Open a new terminal and re-run."
  step "git ready: $(git --version 2>/dev/null | awk '{print $3}')"
}

# ------------------------------------------------------------------- go -----
ensure_go() {
  [[ "$WITH_GO" -eq 1 ]] || return 0
  have go && { step "Found Go: $(go version 2>/dev/null | awk '{print $3}')"; return 0; }
  warn "Go not found — installing it (only needed for the optional REST API)"
  case "$OS" in
    linux|bsd)
      local mgr
      mgr="$(pkg_manager || true)"
      if [[ -n "$mgr" ]]; then
        case "$mgr" in
          apt-get) install_pkgs apt-get golang-go >/dev/null 2>&1 || true ;;
          dnf|yum) install_pkgs "$mgr" golang >/dev/null 2>&1 || true ;;
          apk)     install_pkgs apk go >/dev/null 2>&1 || true ;;
          pacman)  install_pkgs pacman go >/dev/null 2>&1 || true ;;
          zypper)  install_pkgs zypper go >/dev/null 2>&1 || true ;;
          pkg)     install_pkgs pkg go >/dev/null 2>&1 || true ;;
        esac
      fi
      ;;
    darwin)
      have brew && brew install go >/dev/null 2>&1 || true
      ;;
  esac
  if ! have go; then
    # Last resort: official tarball, installed under $HOME (no root needed).
    local ver tarball target
    ver="$(curl -fsSL --max-time 10 https://go.dev/VERSION?m=text 2>/dev/null | head -n 1 || true)"
    [[ -n "$ver" ]] || ver="go1.23.4"
    tarball="$WORK/${ver}.${OS}-${ARCH}.tar.gz"
    target="${HOME}/.local/go"
    step "Downloading ${ver} from go.dev"
    if curl -fsSL "https://go.dev/dl/${ver}.${OS}-${ARCH}.tar.gz" -o "$tarball"; then
      mkdir -p "${HOME}/.local"
      rm -rf "$target"
      tar -C "${HOME}/.local" -xzf "$tarball"
      export PATH="${target}/bin:${PATH}"
      info "Go installed to ${target} — add it to your shell PATH to keep it:"
      info "    export PATH=\"\$HOME/.local/go/bin:\$PATH\""
    else
      warn "Could not download Go. The bot still works 100% without it; the REST API will be skipped."
      return 0
    fi
  fi
  have go && step "Go ready: $(go version 2>/dev/null | awk '{print $3}')"
}

# ----------------------------------------------------------------- clone ----
TARGET=""
if [[ -n "$INSTALL_DIR" ]]; then
  case "$INSTALL_DIR" in /*) TARGET="$INSTALL_DIR" ;; *) TARGET="$PWD/$INSTALL_DIR" ;; esac
elif [[ -f "$PWD/atf.py" ]] && git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
     && [[ "$(git config --get remote.origin.url 2>/dev/null || true)" == *"$REPO"* ]]; then
  TARGET="$PWD"
  info "Already inside an AutoTelegramForward checkout — using $TARGET"
else
  TARGET="$PWD/AutoTelegramForward"
fi

ensure_git
ensure_python
ensure_go

NEED_CLONE=0
if [[ -e "$TARGET" ]]; then
  if [[ -f "$TARGET/atf.py" ]]; then
    if ( cd "$TARGET" && git rev-parse --is-inside-work-tree >/dev/null 2>&1 ); then
      step "Updating existing clone: $TARGET  (git pull)"
      ( cd "$TARGET" && git pull --ff-only --quiet 2>/dev/null ) \
        || ( cd "$TARGET" && git pull --quiet 2>/dev/null ) \
        || warn "git pull failed — continuing with the local copy."
    else
      info "Using existing directory: $TARGET"
    fi
  elif [[ "$FORCE" -eq 1 ]] && { [[ -z "$(ls -A "$TARGET" 2>/dev/null)" ]]; }; then
    NEED_CLONE=1
  else
    die "$TARGET exists but is not an AutoTelegramForward clone.
  Move it away, choose another directory with --dir PATH, or (if it is empty) re-run with --force."
  fi
else
  NEED_CLONE=1
fi

if [[ "$NEED_CLONE" -eq 1 ]]; then
  step "Cloning AutoTelegramForward (branch ${BRANCH}) into: $TARGET"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TARGET" \
    || git clone --branch "$BRANCH" "$REPO_URL" "$TARGET" \
    || die "git clone failed. Check your network, or download manually from
  https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip"
fi

cd "$TARGET" || die "cannot cd into $TARGET"
[[ -f "atf.py" ]] || die "$TARGET does not contain atf.py — unexpected clone contents."

printf '\n%s\n' "${C_BOLD}${C_GREEN}Environment ready.${C_RESET}"
info "Location : $TARGET"
info "Python   : $(command -v "$PY" 2>/dev/null || echo "$PY")"
info "Next     : ${C_BOLD}python atf.py start${C_RESET}  (after setup)"
printf '%s\n' "${C_DIM}---------------------------------------------------------------------------${C_RESET}"
printf '%s\n' "${C_BOLD}Setup now — you only need your Telegram bot token (get one from @BotFather).${C_RESET}"
printf '%s\n\n' "${C_DIM}Everything else is automatic: virtualenv, dependencies, config.yaml, master key.${C_RESET}"

# Hand off to the project's own setup flow. exec keeps stdin/stdout attached so
# the interactive bot-token prompt works even under `curl ... | bash`.
exec "$PY" atf.py setup
