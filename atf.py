#!/usr/bin/env python
"""AutoTelegramForward — single-file CLI that runs EVERYTHING.

One binary / one script manages all microservices (core + logger + api):

    atf setup     first-time setup (asks only for your bot token)
    atf start     start core + logger + api (all services, one command)
    atf stop      stop everything started by `atf start`
    atf restart   stop + start
    atf status    live status of every service
    atf logs      tail recent logs of every service
    atf health    deep health & diagnostics check
    atf service   install OS service (systemd / launchd / Task Scheduler)
    atf docker    docker compose lifecycle (up/down/logs/ps)
    atf test      run the test-suite
    atf update    git pull + refresh dependencies
    atf restore   restore a session backup file
    atf version   print version

Works in two modes:
  * source mode   — `python atf.py ...`  (creates .venv on first run)
  * frozen mode   — the PyInstaller `atf` binary (deps baked in, no venv)
"""

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths & mode detection
# --------------------------------------------------------------------------- #
FROZEN = bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))

if FROZEN:
    # The binary lives next to user data (config.yaml, data/).
    ROOT = Path(sys.executable).resolve().parent
    BUNDLE = Path(sys._MEIPASS)  # read-only bundled resources
else:
    ROOT = Path(__file__).resolve().parent
    BUNDLE = ROOT

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

if FROZEN:
    PY_EXE = Path(sys.executable)  # the interpreter IS the binary env
else:
    VENV = ROOT / ".venv"
    PY_EXE = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")

CONFIG = ROOT / "config.yaml"
PID_FILE = ROOT / "data" / "atf.pids"
VERSION_FILE = BUNDLE / "VERSION" if FROZEN else ROOT / "VERSION"

# `atf-api` (Go REST API) is an optional sidecar: prefer a binary sitting
# next to us (release bundles ship it), else build from source if Go exists.
API_CANDIDATES = [
    ROOT / ("atf-api.exe" if IS_WIN else "atf-api"),
    ROOT / "api" / ("atf-api.exe" if IS_WIN else "atf-api"),
]

# Public api_id/api_hash from the open-source Telegram Desktop client.
DEFAULT_API_ID = 17349
DEFAULT_API_HASH = "344583e45741c457fe1862106095a5eb"

APP_VERSION = "dev"


def _detect_version() -> str:
    for vf in (VERSION_FILE, ROOT / "VERSION"):
        try:
            if vf.exists():
                v = vf.read_text(encoding="utf-8").strip().lstrip("v")
                if v:
                    return v
        except OSError:
            pass
    try:
        cfg_py = BUNDLE / "core" / "config.py"
        if not cfg_py.exists():
            cfg_py = ROOT / "core" / "config.py"
        txt = cfg_py.read_text(encoding="utf-8")
        import re

        m = re.search(r'version:\s*str\s*=\s*"([^"]+)"', txt)
        if m:
            return m.group(1)
    except OSError:
        pass
    return APP_VERSION


APP_VERSION = _detect_version()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def run(cmd, **kw):
    kw.setdefault("cwd", ROOT)
    return subprocess.run(cmd, check=True, **kw)


def sh(cmd, **kw):
    kw.setdefault("cwd", ROOT)
    kw.setdefault("shell", True)
    return subprocess.run(cmd, **kw)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def step(msg: str) -> None:
    print(f"\n▶ {msg}")


def ok(msg: str) -> None:
    print(f"✅ {msg}")


def warn(msg: str) -> None:
    print(f"⚠️  {msg}")


def die(msg: str, code: int = 1) -> "typing.NoReturn":  # noqa: F821
    print(f"✗ {msg}")
    raise SystemExit(code)


import typing  # noqa: E402  (kept late: cheap stdlib import)


def proc_alive(pid: int) -> bool:
    try:
        if IS_WIN:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def read_pids() -> dict:
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_pids(pids: dict) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(pids), encoding="utf-8")


def kill_pid(pid: int, name: str) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not proc_alive(pid):
                    break
                time.sleep(0.25)
            else:
                os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    print(f"  • {name} (pid {pid}) stopped")


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _systemd_state(unit: str) -> str:
    """'active' / 'inactive' / 'failed' / '' (no systemd or unknown)."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        state = (out.stdout or "").strip()
        return state if state in (
            "active", "inactive", "failed", "activating", "deactivating",
        ) else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------- #
# Environment bootstrap (source mode only)
# --------------------------------------------------------------------------- #
def ensure_venv() -> None:
    if FROZEN:
        return  # everything is baked into the binary
    if PY_EXE.exists():
        return
    import venv

    step("Creating virtual environment ...")
    venv.create(str(ROOT / ".venv"), with_pip=True)
    step("Installing dependencies (this takes ~30s) ...")
    run([str(PY_EXE), "-m", "pip", "install", "--quiet",
         "--upgrade", "pip"])
    req = ROOT / "requirements.txt"
    if req.exists():
        run([str(PY_EXE), "-m", "pip", "install", "--quiet",
             "-r", str(req)])


def go_available() -> bool:
    return have("go")


def find_api() -> "Path | None":
    for cand in API_CANDIDATES:
        if cand.exists():
            return cand
    return None


def build_api() -> "Path | None":
    found = find_api()
    if found:
        return found
    if not go_available():
        print("[api] Go toolchain not found — REST API skipped "
              "(bot still works 100%).")
        return None
    step("Building Go control API ...")
    out = ROOT / "api" / ("atf-api.exe" if IS_WIN else "atf-api")
    sh(f"go build -o {out.name} .", cwd=ROOT / "api")
    return out if out.exists() else None


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #
def cmd_setup(_args=None) -> None:
    p = argparse.ArgumentParser(description="AutoTelegramForward Setup")
    p.add_argument("--token", help="Telegram Bot Token from @BotFather")
    p.add_argument("--admin", default="",
                   help="Telegram user ID for admin rights")
    p.add_argument("--lang", default="fa",
                   choices=["en", "fa", "ru", "zh"],
                   help="Default language")
    p.add_argument("--service", action="store_true",
                   help="Auto install and start OS service")
    p.add_argument("--non-interactive", action="store_true",
                   help="Do not prompt for input")
    args, _ = p.parse_known_args(sys.argv[2:])

    token = args.token or os.environ.get("ATF_BOT_TOKEN")
    admin = args.admin or os.environ.get("ATF_ADMIN_ID", "")
    lang = args.lang or os.environ.get("ATF_LANG", "fa")

    if not token and not args.non_interactive and sys.stdin.isatty():
        print("=" * 62)
        print("  AutoTelegramForward — Easy Setup")
        print("=" * 62)
        token = input("\n(1/3) Paste your BOT TOKEN (from @BotFather): ").strip()
        if not admin:
            admin = input("(2/3) Your Telegram user ID for admin rights "
                          "(Enter to skip): ").strip()
        print("(3/3) Language: 1=English  2=فارسی  3=Русский  4=中文  (Enter=2)")
        ans = input("> ").strip()
        lang = {"1": "en", "en": "en", "3": "ru", "ru": "ru",
                "4": "zh", "zh": "zh"}.get(ans, "fa")

    if not token or ":" not in token:
        die("Invalid or missing bot token. Provide --token or get one "
            "from @BotFather.")

    ensure_venv()
    build_api()

    import secrets

    master_key = secrets.token_urlsafe(32)
    CONFIG.write_text(
        f"api_id: {DEFAULT_API_ID}\n"
        f'api_hash: "{DEFAULT_API_HASH}"\n'
        f'bot_token: "{token}"\n'
        f"admin_ids: [{admin}]\n"
        f'master_key: "{master_key}"\n'
        f'db_path: "data/atf.db"\n'
        f'grpc_host: "127.0.0.1"\n'
        f'grpc_port: 6001\n'
        f'language: "{lang}"\n',
        encoding="utf-8",
    )
    ok("Setup complete!  config.yaml written (master key auto-generated).")
    if args.service:
        cmd_service_install()
    else:
        print("▶  Start now with:   atf start "
              "(or:  atf service install for autostart)\n")


# --------------------------------------------------------------------------- #
# start / stop / restart / status / logs  (single command, all services)
# --------------------------------------------------------------------------- #
def _service_env() -> dict:
    return {**os.environ,
            "ATF_GRPC_PORT": "6001",
            "ATF_LOGGER_PORT": "6002",
            "ATF_LOGGER_ADDR": "localhost:6002"}


def cmd_start(_args=None) -> None:
    if not CONFIG.exists():
        die("No config.yaml — run:  atf setup")
    ensure_venv()
    api_exe = build_api()
    env = _service_env()

    # Refuse double-start.
    pids = {k: v for k, v in read_pids().items() if proc_alive(v)}
    if pids:
        print("Already running: " +
              ", ".join(f"{k} (pid {v})" for k, v in pids.items()))
        print("Use `atf status` / `atf stop` to manage.")
        return

    new_pids: dict = {}
    log_file = ROOT / "data" / "atf.out.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    lf = open(log_file, "ab")

    step("Starting Logger service (debug log store) ...")
    logger_proc = subprocess.Popen(
        [str(PY_EXE), "-m", "logger.main"] if not FROZEN
        else [str(PY_EXE), "logger"],
        cwd=ROOT, env=env, stdout=lf, stderr=subprocess.STDOUT)
    new_pids["logger"] = logger_proc.pid
    time.sleep(2)

    step("Starting Python core (bot + gRPC) ...")
    core = subprocess.Popen(
        [str(PY_EXE), "-m", "core.main"] if not FROZEN
        else [str(PY_EXE), "core"],
        cwd=ROOT, env=env, stdout=lf, stderr=subprocess.STDOUT)
    new_pids["core"] = core.pid

    if api_exe:
        time.sleep(3)
        api_env = {**env, "ATF_CORE_GRPC": "localhost:6001"}
        step("Starting Go API on http://localhost:8080 ...")
        api = subprocess.Popen([str(api_exe)], cwd=ROOT / "api",
                               env=api_env, stdout=lf, stderr=subprocess.STDOUT)
        new_pids["api"] = api.pid
        print("\n✅ Running!  Bot is live;  REST API: http://localhost:8080")
    else:
        print("\n✅ Running!  Bot is live (REST API skipped — no Go toolchain).")

    write_pids(new_pids)
    print(f"   Logs: {log_file}  (or: atf logs)")
    print("   Manage: atf status | atf stop | atf restart\n")

    # Foreground supervision: `atf start --no-daemonize` (systemd/launchd/
    # Task Scheduler) or ATF_FOREGROUND=1 must NOT return — the supervisor
    # treats an early exit as a crash and restart-loops.
    foreground = os.environ.get("ATF_FOREGROUND") == "1" or \
        "--no-daemonize" in sys.argv
    if foreground:
        try:
            logger_proc.wait()
            core.wait()
        except KeyboardInterrupt:
            cmd_stop()


def cmd_stop(_args=None) -> None:
    pids = read_pids()
    live = {k: v for k, v in pids.items() if proc_alive(v)}
    if not live:
        # Fall back to OS-service stop when nothing was started via `atf start`.
        print("Nothing started via `atf start` is running.")
        return
    step("Stopping all services ...")
    for name, pid in live.items():
        kill_pid(pid, name)
    write_pids({k: v for k, v in pids.items() if proc_alive(v)})
    ok("All services stopped.")


def cmd_restart(_args=None) -> None:
    cmd_stop()
    time.sleep(1)
    cmd_start()


def _describe() -> "list[tuple[str, str, str]]":
    """(name, state, detail) rows for status output."""
    rows = []
    pids = read_pids()
    for name in ("logger", "core", "api"):
        pid = pids.get(name)
        if pid and proc_alive(pid):
            rows.append((name, "RUNNING", f"pid {pid}"))
        elif pid:
            rows.append((name, "DEAD", f"pid {pid} exited"))
        else:
            # No `atf start` record — maybe the OS service runs it.
            # (The Go API has no OS unit: its liveness is the :8080 port.)
            if name == "api":
                rows.append((name, "RUNNING" if port_open(8080) else "STOPPED",
                             "port :8080" if port_open(8080) else "—"))
                continue
            svc_state = _systemd_state(
                "atf-logger.service" if name == "logger" else "atf.service"
            ) if not IS_WIN and not IS_MAC else ""
            if svc_state == "active":
                rows.append((name, "RUNNING", "via OS service"))
            elif svc_state:
                rows.append((name, "STOPPED", f"os service: {svc_state}"))
            else:
                rows.append((name, "STOPPED", "—"))
    rows.append(("core gRPC :6001",
                 "LISTENING" if port_open(6001) else "CLOSED", ""))
    rows.append(("logger :6002",
                 "LISTENING" if port_open(6002) else "CLOSED", ""))
    rows.append(("api :8080",
                 "LISTENING" if port_open(8080) else "CLOSED", ""))
    return rows


def cmd_status(_args=None) -> None:
    print(f"\n  AutoTelegramForward v{APP_VERSION} — service status")
    print("  " + "-" * 44)
    for name, state, detail in _describe():
        mark = "🟢" if state in ("RUNNING", "LISTENING") else \
               "🔴" if state in ("DEAD", "CLOSED") else "⚪"
        print(f"  {mark} {name:<16} {state:<9} {detail}")
    print()


def cmd_logs(_args=None) -> None:
    p = argparse.ArgumentParser(description="Show recent service logs")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("--service", action="store_true",
                   help="Read from the OS service journal instead")
    args, _ = p.parse_known_args(sys.argv[2:])
    if args.service and not IS_WIN:
        sh("journalctl --user -u atf.service -u atf-logger.service "
           f"-n {args.lines} --no-pager")
        return
    log_file = ROOT / "data" / "atf.out.log"
    if not log_file.exists():
        if IS_WIN or IS_MAC or not _systemd_state("atf.service"):
            print("No logs yet — start services first (`atf start`).")
            return
        # OS-service mode: fall through to the journal automatically.
        print("(local log empty — reading OS service journal …)")
        args.service = True
    if args.service and not IS_WIN:
        sh("journalctl --user -u atf.service -u atf-logger.service "
           f"-n {args.lines} --no-pager")
        return
    lines = log_file.read_text(encoding="utf-8",
                               errors="replace").splitlines()
    print("\n".join(lines[-args.lines:]))


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
def cmd_health(_args=None) -> None:
    print("=" * 60)
    print(f"  AutoTelegramForward v{APP_VERSION} — Health & Diagnostics")
    print("=" * 60)
    print(f"• Mode               : {'frozen binary' if FROZEN else 'source'}")
    print(f"• Python executable  : {PY_EXE} "
          f"({'OK' if Path(PY_EXE).exists() else 'MISSING'})")
    print(f"• Config file        : {CONFIG} "
          f"({'OK' if CONFIG.exists() else 'MISSING'})")
    db_file = ROOT / "data" / "atf.db"
    print(f"• SQLite Database    : {db_file} "
          f"({'OK' if db_file.exists() else 'MISSING'})")
    print(f"• Go API binary      : {find_api() or 'not built (optional)'}")
    print(f"• Core gRPC port 6001: {'ACTIVE' if port_open(6001) else 'INACTIVE'}")
    print(f"• Logger port 6002   : {'ACTIVE' if port_open(6002) else 'INACTIVE'}")
    print(f"• REST API port 8080 : {'ACTIVE' if port_open(8080) else 'INACTIVE'}")
    print(f"• Docker             : {'available' if have('docker') else 'not installed'}")
    print("=" * 60)


def cmd_version(_args=None) -> None:
    print(f"atf {APP_VERSION} ({platform.system()}/{platform.machine()})"
          f"{' [frozen]' if FROZEN else ''}")


# --------------------------------------------------------------------------- #
# OS service install (systemd / launchd / Task Scheduler)
# --------------------------------------------------------------------------- #
def cmd_service_install(_args=None) -> None:
    ensure_venv()
    exe = str(Path(sys.executable if FROZEN else sys.argv[0]).resolve())
    if not IS_WIN and not FROZEN:
        exe = f"{PY_EXE} {ROOT / 'atf.py'}"
    if IS_WIN:
        _service_install_windows(exe)
    elif IS_MAC:
        _service_install_launchd(exe)
    else:
        _service_install_systemd(exe)


def _service_install_systemd(exe: str) -> None:
    d = Path.home() / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    logger_unit = f"""[Unit]
Description=AutoTelegramForward Logger Microservice
After=network.target

[Service]
Type=simple
WorkingDirectory={ROOT}
Environment="ATF_LOGGER_PORT=6002"
Environment="ATF_LOGGER_HOST=127.0.0.1"
ExecStart={exe} logger
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""
    atf_unit = f"""[Unit]
Description=AutoTelegramForward Core Service & Bot
After=network.target atf-logger.service
Wants=atf-logger.service

[Service]
Type=simple
WorkingDirectory={ROOT}
Environment="ATF_GRPC_PORT=6001"
Environment="ATF_LOGGER_PORT=6002"
Environment="ATF_LOGGER_ADDR=localhost:6002"
ExecStart={exe} start --no-daemonize
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""
    (d / "atf-logger.service").write_text(logger_unit, encoding="utf-8")
    (d / "atf.service").write_text(atf_unit, encoding="utf-8")
    ok("Systemd user units written to ~/.config/systemd/user/")
    sh("systemctl --user daemon-reload")
    sh("systemctl --user enable --now atf-logger.service atf.service")
    sh("loginctl enable-linger $(whoami) 2>/dev/null || true")
    ok("Services enabled, started, and linger enabled.")
    cmd_service_status()


def _service_install_launchd(exe: str) -> None:
    d = Path.home() / "Library" / "LaunchAgents"
    d.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.autotelegramforward.core</string>
  <key>ProgramArguments</key>
  <array><string>{exe}</string><string>start</string><string>--no-daemonize</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{ROOT}/data/atf.out.log</string>
  <key>StandardErrorPath</key><string>{ROOT}/data/atf.out.log</string>
</dict>
</plist>
"""
    f = d / "com.autotelegramforward.core.plist"
    f.write_text(plist, encoding="utf-8")
    sh(f"launchctl unload {f} 2>/dev/null || true")
    sh(f"launchctl load {f}")
    ok(f"launchd agent installed: {f}")


def _service_install_windows(exe: str) -> None:
    # Autostart via Task Scheduler (no admin rights needed for logon tasks).
    task = "AutoTelegramForward"
    sh(f'schtasks /delete /tn "{task}" /f 2>NUL || exit 0')
    rc = sh(f'schtasks /create /tn "{task}" /tr "\\"{exe}\\" start" '
            f'/sc onlogon /rl limited /f').returncode
    if rc == 0:
        ok(f'Scheduled task "{task}" created — starts at logon.')
    else:
        warn("Could not create scheduled task; run `atf start` manually.")
    cmd_service_status()


def cmd_service_status(_args=None) -> None:
    cmd_status()
    if not IS_WIN:
        if (Path.home() / ".config/systemd/user/atf.service").exists():
            sh("systemctl --user status atf.service atf-logger.service "
               "--no-pager 2>/dev/null || true")


def cmd_service_uninstall(_args=None) -> None:
    if IS_WIN:
        sh('schtasks /delete /tn "AutoTelegramForward" /f')
    elif IS_MAC:
        f = Path.home() / "Library/LaunchAgents/com.autotelegramforward.core.plist"
        sh(f"launchctl unload {f} 2>/dev/null || true")
        if f.exists():
            f.unlink()
    else:
        sh("systemctl --user disable --now atf.service atf-logger.service "
           "2>/dev/null || true")
        d = Path.home() / ".config" / "systemd" / "user"
        for name in ("atf.service", "atf-logger.service"):
            f = d / name
            if f.exists():
                f.unlink()
        sh("systemctl --user daemon-reload")
    ok("OS service entries removed.")


def cmd_service(_args=None) -> None:
    sub = sys.argv[2] if len(sys.argv) > 2 else "status"
    if sub in ("install", "setup"):
        cmd_service_install()
    elif sub == "status":
        cmd_service_status()
    elif sub == "start":
        if IS_WIN or IS_MAC:
            cmd_start()
        else:
            sh("systemctl --user start atf-logger.service atf.service")
            cmd_service_status()
    elif sub == "stop":
        if IS_WIN or IS_MAC:
            cmd_stop()
        else:
            sh("systemctl --user stop atf.service atf-logger.service")
            print("🛑 Services stopped.")
    elif sub == "restart":
        if IS_WIN or IS_MAC:
            cmd_restart()
        else:
            sh("systemctl --user restart atf-logger.service atf.service")
            cmd_service_status()
    elif sub in ("logs", "log"):
        cmd_logs(["--service"])
    elif sub in ("uninstall", "remove"):
        cmd_service_uninstall()
    else:
        print("Usage: atf service [install|status|start|stop|restart|logs|uninstall]")


# --------------------------------------------------------------------------- #
# docker lifecycle — one file to rule the containers too
# --------------------------------------------------------------------------- #
def _compose_file() -> Path:
    for cand in (ROOT / "docker-compose.yml", ROOT / "docker-compose.yaml"):
        if cand.exists():
            return cand
    die("docker-compose.yml not found next to the atf binary.")


def _compose_base() -> "list[str]":
    if have("docker") and sh("docker compose version",
                             capture_output=True).returncode == 0:
        return ["docker", "compose"]
    if have("docker-compose"):
        return ["docker-compose"]
    die("Docker is not installed. Get it at https://docs.docker.com/get-docker/")


def cmd_docker(_args=None) -> None:
    sub = sys.argv[2] if len(sys.argv) > 2 else "ps"
    compose = _compose_file()
    base = _compose_base()

    if sub == "up":
        step("Building + starting all containers (multi-arch images) ...")
        run([*base, "-f", str(compose), "up", "-d", "--build"])
        run([*base, "-f", str(compose), "ps"])
        ok("Containers up. API: http://localhost:8080")
    elif sub == "down":
        run([*base, "-f", str(compose), "down"])
        ok("Containers stopped and removed.")
    elif sub == "restart":
        run([*base, "-f", str(compose), "restart"])
        ok("Containers restarted.")
    elif sub in ("logs", "log"):
        svc = sys.argv[3] if len(sys.argv) > 3 else None
        run([*base, "-f", str(compose), "logs", "--tail=100", "-f"]
            + ([svc] if svc else []))
    elif sub == "ps":
        run([*base, "-f", str(compose), "ps"])
    elif sub == "pull":
        run([*base, "-f", str(compose), "pull"])
        ok("Images pulled.")
    else:
        print("Usage: atf docker [up|down|restart|logs [service]|ps|pull]")


# --------------------------------------------------------------------------- #
# test / update / restore
# --------------------------------------------------------------------------- #
def cmd_test(_args=None) -> None:
    ensure_venv()
    print("▶ Python tests ...")
    run([str(PY_EXE), "-m", "pytest", "-q", *sys.argv[2:]])
    if go_available() and (ROOT / "api").exists():
        print("▶ Go tests ...")
        sh("go test ./...", cwd=ROOT / "api")
    ok("All tests passed.")


def cmd_update(_args=None) -> None:
    if FROZEN:
        print("Frozen binary — download the new release instead:\n"
              "  https://github.com/Opselon/AutoTelegramForward/releases/latest")
        return
    sh("git pull")
    ensure_venv()
    run([str(PY_EXE), "-m", "pip", "install", "--quiet", "-U",
         "pyrogram", "tgcrypto", "grpcio", "httpx", "cryptography", "pyyaml"])
    if find_api():
        build_api()
    ok("Updated.")


def cmd_restore(_args=None) -> None:
    """Restore a session backup:  atf restore <file.atf>"""
    if len(sys.argv) < 3:
        die("Usage: atf restore <backup-file.atf>")
    ensure_venv()
    run([str(PY_EXE), "-c",
         "import sys; from core.recovery import restore_from_cli; "
         "restore_from_cli(sys.argv[1])",
         sys.argv[2]])


# --------------------------------------------------------------------------- #
# Frozen-mode service shims: `atf core` / `atf logger` run one service in the
# foreground (used by systemd units, launchd, Task Scheduler AND by `start`).
# --------------------------------------------------------------------------- #
def cmd_core(_args=None) -> None:
    os.environ.setdefault("ATF_GRPC_PORT", "6001")
    os.environ.setdefault("ATF_LOGGER_ADDR", "localhost:6002")
    if FROZEN:
        sys.path.insert(0, str(BUNDLE))
    from core.main import main
    import asyncio

    asyncio.run(main())


def cmd_logger(_args=None) -> None:
    os.environ.setdefault("ATF_LOGGER_PORT", "6002")
    if FROZEN:
        sys.path.insert(0, str(BUNDLE))
    from logger.main import main
    import asyncio

    asyncio.run(main())


# --------------------------------------------------------------------------- #
COMMANDS = {
    "setup": cmd_setup,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "status": cmd_status,
    "logs": cmd_logs,
    "health": cmd_health,
    "version": cmd_version,
    "service": cmd_service,
    "docker": cmd_docker,
    "test": cmd_test,
    "update": cmd_update,
    "restore": cmd_restore,
    # frozen-mode shims (also handy for debugging single services)
    "core": cmd_core,
    "logger": cmd_logger,
}


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("-v", "--version"):
        cmd_version()
        return
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("Commands: " + ", ".join(sorted(COMMANDS)))
        return
    cmd = argv[0] if argv else None
    if cmd in COMMANDS:
        COMMANDS[cmd]()
        return
    if cmd:
        print(f"Unknown command: {cmd}\n")
    print(__doc__)
    print("Commands: " + ", ".join(sorted(COMMANDS)))


if __name__ == "__main__":
    main()
