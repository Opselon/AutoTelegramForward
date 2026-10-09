#!/usr/bin/env python
"""AutoTelegramForward PRO — single-file CLI that runs EVERYTHING.

One binary / one script manages all microservices (core + logger + web + api):

    atf setup     interactive easy setup wizard (bot token, admin ID, web port)
    atf start     start core + logger + web dashboard + api (all services in 1 cmd)
    atf stop      stop everything started by `atf start`
    atf restart   stop + start all microservices
    atf status    live status & ports of every service + dashboard URL
    atf logs      tail recent logs of all services
    atf health    deep health, diagnostic & connectivity check
    atf service   install OS service (systemd / launchd / Task Scheduler)
    atf docker    docker compose lifecycle (up/down/restart/logs/ps)
    atf k8s       kubernetes lifecycle & deployment (apply/status/logs/generate)
    atf test      run the test-suite (pytest + go)
    atf update    git pull + refresh dependencies
    atf restore   restore a session backup file
    atf version   print version
    atf web       run web gateway in foreground

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

# Candidates for Go REST API binary
API_CANDIDATES = [
    ROOT / ("atf-api.exe" if IS_WIN else "atf-api"),
    ROOT / "api" / ("atf-api.exe" if IS_WIN else "atf-api"),
]

# Candidates for Rust Web Gateway binary (Axum + React)
WEB_CANDIDATES = [
    ROOT / ("atf-web-backend.exe" if IS_WIN else "atf-web-backend"),
    ROOT / "web" / "backend" / "target" / "release" / ("atf-web-backend.exe" if IS_WIN else "atf-web-backend"),
    ROOT / "web" / "backend" / "target" / "debug" / ("atf-web-backend.exe" if IS_WIN else "atf-web-backend"),
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
# Helpers & process management
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


def die(msg: str, code: int = 1):
    print(f"✗ {msg}")
    raise SystemExit(code)


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
# Environment & toolchain bootstrapping
# --------------------------------------------------------------------------- #
def ensure_venv() -> None:
    if FROZEN:
        return
    if PY_EXE.exists():
        return
    import venv

    step("Creating virtual environment (.venv) ...")
    venv.create(str(ROOT / ".venv"), with_pip=True)
    step("Installing Python requirements (pip) ...")
    run([str(PY_EXE), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    req = ROOT / "requirements.txt"
    if req.exists():
        run([str(PY_EXE), "-m", "pip", "install", "--quiet", "-r", str(req)])


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
        return None
    step("Building Go control API ...")
    out = ROOT / "api" / ("atf-api.exe" if IS_WIN else "atf-api")
    sh(f"go build -o {out.name} .", cwd=ROOT / "api")
    return out if out.exists() else None


def find_web() -> "Path | None":
    for cand in WEB_CANDIDATES:
        if cand.exists():
            return cand
    return None


def build_web() -> "Path | None":
    found = find_web()
    if found:
        return found
    if not have("cargo"):
        return None
    step("Building Rust Web Gateway (Axum + React) ...")
    sh("cargo build --release", cwd=ROOT / "web" / "backend")
    return find_web()


# --------------------------------------------------------------------------- #
# Easy Setup Wizard (`atf setup`)
# --------------------------------------------------------------------------- #
def cmd_setup(_args=None) -> None:
    p = argparse.ArgumentParser(description="AutoTelegramForward PRO Setup Wizard")
    p.add_argument("--token", help="Telegram Bot Token from @BotFather")
    p.add_argument("--admin", default="", help="Telegram user ID for admin rights")
    p.add_argument("--lang", default="fa", choices=["en", "fa", "ru", "zh"], help="Default language")
    p.add_argument("--web-port", type=int, default=8088, help="Web dashboard gateway port")
    p.add_argument("--service", action="store_true", help="Auto install and start OS service")
    p.add_argument("--start", action="store_true", help="Start all microservices immediately after setup")
    p.add_argument("--non-interactive", action="store_true", help="Do not prompt for interactive input")
    args, _ = p.parse_known_args(sys.argv[2:])

    token = args.token or os.environ.get("ATF_BOT_TOKEN")
    admin = args.admin or os.environ.get("ATF_ADMIN_ID", "")
    lang = args.lang or os.environ.get("ATF_LANG", "fa")
    web_port = args.web_port or int(os.environ.get("ATF_WEB_PORT", "8088"))

    if not token and not args.non_interactive and sys.stdin.isatty():
        print("\n" + "=" * 68)
        print("  🚀 AutoTelegramForward PRO — Easy Setup Wizard")
        print("  Unified Cross-Platform Telegram Forwarding Microservice Platform")
        print("=" * 68)
        print("  راهنمای راه‌اندازی سریع و آسان — فقط در چند ثانیه:\n")

        while not token:
            print("▶ [۱/۴] توکن ربات تلگرام خود را وارد کنید:")
            print("   (دریافت از بات رسمی تلگرام: @BotFather)")
            token = input("   BOT TOKEN: ").strip()
            if not token or ":" not in token:
                print("   ⚠️ توکن نامعتبر است! توکن تلگرام باید شامل ':' باشد (مانند: 123456789:ABC...).")
                token = ""

        if not admin:
            print("\n▶ [۲/۴] شناسه کاربری عددی ادمین (Admin User ID):")
            print("   (دریافت شناسه از @userinfobot — برای رد شدن Enter بزنید)")
            admin = input("   Admin ID [اختیاری]: ").strip()

        print("\n▶ [۳/۴] زبان پیش‌فرض پنل و ربات (Language):")
        print("   1) فارسی (پیش‌فرض)  |  2) English  |  3) Русский  |  4) 中文")
        ans = input("   انتخاب [1]: ").strip()
        lang = {"1": "fa", "fa": "fa", "2": "en", "en": "en", "3": "ru", "ru": "ru", "4": "zh", "zh": "zh"}.get(ans, "fa")

        print(f"\n▶ [۴/۴] پورت درگاه وب و داشبورد کنترل: [پیش‌فرض: {web_port}]")
        port_ans = input(f"   Port [{web_port}]: ").strip()
        if port_ans.isdigit():
            web_port = int(port_ans)

    if not token or ":" not in token:
        die("Invalid or missing bot token. Provide --token or get one from @BotFather.")

    ensure_venv()
    build_api()
    build_web()

    import secrets
    master_key = secrets.token_urlsafe(32)

    config_content = (
        f"# AutoTelegramForward PRO Configuration\n"
        f"api_id: {DEFAULT_API_ID}\n"
        f'api_hash: "{DEFAULT_API_HASH}"\n'
        f'bot_token: "{token}"\n'
        f"admin_ids: [{admin}]\n"
        f'master_key: "{master_key}"\n'
        f'db_path: "data/atf.db"\n'
        f'grpc_host: "127.0.0.1"\n'
        f'grpc_port: 6001\n'
        f'language: "{lang}"\n'
        f'web_port: {web_port}\n'
        f'web_addr: "0.0.0.0:{web_port}"\n'
    )
    CONFIG.write_text(config_content, encoding="utf-8")
    ok("Setup complete! config.yaml written (master encryption key auto-generated).")

    print("\n" + "-" * 68)
    print("  🎉 تنظیمات با موفقیت ذخیره شد! سیستم آماده اجراست.")
    print(f"  • وب‌داشبورد کنترل:  http://localhost:{web_port}")
    print(f"  • هسته gRPC تلگرام: localhost:6001")
    print(f"  • سرویس لاگ‌گیری:   localhost:6002")
    print("-" * 68 + "\n")

    if args.service:
        cmd_service_install()
        return

    should_start = args.start
    if not should_start and not args.non_interactive and sys.stdin.isatty():
        launch_ans = input("▶ آیا مایلید همه میکروسرویس‌ها همین الان با هم اجرا شوند؟ [Y/n]: ").strip().lower()
        if launch_ans in ("", "y", "yes"):
            should_start = True

    if should_start:
        cmd_start()
    else:
        print("▶ برای اجرای همه سرویس‌ها با هم:     atf start")
        print("▶ برای نصب سرویس سیستم عامل:        atf service install")
        print("▶ برای اجرا با کانتینرهای داکر:     atf docker up")
        print("▶ برای استقرار روی کوبرنتس:         atf k8s apply\n")


# --------------------------------------------------------------------------- #
# start / stop / restart / status / logs (all 4 microservices unified)
# --------------------------------------------------------------------------- #
def _service_env() -> dict:
    return {
        **os.environ,
        "ATF_GRPC_PORT": "6001",
        "ATF_LOGGER_PORT": "6002",
        "ATF_LOGGER_ADDR": "localhost:6002",
        "ATF_CORE_GRPC": "localhost:6001",
    }


def cmd_start(_args=None) -> None:
    if not CONFIG.exists():
        die("No config.yaml found — run: atf setup")
    ensure_venv()
    api_exe = build_api()
    web_exe = find_web() or build_web()
    env = _service_env()

    # Refuse double-start
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

    step("Starting Logger microservice (gRPC port 6002) ...")
    logger_proc = subprocess.Popen(
        [str(PY_EXE), "-m", "logger.main"] if not FROZEN
        else [str(PY_EXE), "logger"],
        cwd=ROOT, env=env, stdout=lf, stderr=subprocess.STDOUT)
    new_pids["logger"] = logger_proc.pid
    time.sleep(1.5)

    step("Starting Core microservice (bot + gRPC port 6001) ...")
    core = subprocess.Popen(
        [str(PY_EXE), "-m", "core.main"] if not FROZEN
        else [str(PY_EXE), "core"],
        cwd=ROOT, env=env, stdout=lf, stderr=subprocess.STDOUT)
    new_pids["core"] = core.pid
    time.sleep(1.5)

    if web_exe:
        step("Starting Rust Web Gateway & Dashboard (port 8088) ...")
        web_env = {
            **env,
            "ATF_WEB_ADDR": "0.0.0.0:8088",
            "ATF_DB_PATH": str(ROOT / "data" / "atf.db"),
            "ATF_LOGS_DB_PATH": str(ROOT / "data" / "atf_logs.db"),
        }
        web_proc = subprocess.Popen(
            [str(web_exe)], cwd=ROOT,
            env=web_env, stdout=lf, stderr=subprocess.STDOUT)
        new_pids["web"] = web_proc.pid

    if api_exe:
        step("Starting Go REST API on http://localhost:8080 ...")
        api_env = {**env, "ATF_CORE_GRPC": "localhost:6001"}
        api_proc = subprocess.Popen([str(api_exe)], cwd=ROOT / "api",
                                    env=api_env, stdout=lf, stderr=subprocess.STDOUT)
        new_pids["api"] = api_proc.pid

    write_pids(new_pids)
    print("\n" + "=" * 64)
    print("  ✅ All microservices are live and operational!")
    print(f"  • 🌐 Web Dashboard:    http://localhost:8088")
    print(f"  • 🤖 Telegram Bot:     Active (core:6001)")
    print(f"  • 📝 Logger Daemon:    Active (logger:6002)")
    if api_exe:
        print(f"  • 🔌 Go REST API:      http://localhost:8080")
    print(f"  • 📄 Consolidated Log: {log_file}  (or: atf logs)")
    print("=" * 64 + "\n")

    foreground = os.environ.get("ATF_FOREGROUND") == "1" or "--no-daemonize" in sys.argv
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
    rows = []
    pids = read_pids()
    for name in ("logger", "core", "web", "api"):
        pid = pids.get(name)
        if pid and proc_alive(pid):
            rows.append((name, "RUNNING", f"pid {pid}"))
        elif pid:
            rows.append((name, "DEAD", f"pid {pid} exited"))
        else:
            if name == "api":
                rows.append((name, "RUNNING" if port_open(8080) else "STOPPED",
                             "http://localhost:8080" if port_open(8080) else "—"))
                continue
            if name == "web":
                svc_state = _systemd_state("atf-web.service") if not IS_WIN and not IS_MAC else ""
                if svc_state == "active" or port_open(8088):
                    rows.append((name, "RUNNING", "http://localhost:8088"))
                elif svc_state:
                    rows.append((name, "STOPPED", f"os service: {svc_state}"))
                else:
                    rows.append((name, "STOPPED", "—"))
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
    rows.append(("core gRPC :6001", "LISTENING" if port_open(6001) else "CLOSED", ""))
    rows.append(("logger :6002", "LISTENING" if port_open(6002) else "CLOSED", ""))
    rows.append(("web :8088", "LISTENING" if port_open(8088) else "CLOSED", "http://localhost:8088"))
    rows.append(("api :8080", "LISTENING" if port_open(8080) else "CLOSED", "http://localhost:8080"))
    return rows


def cmd_status(_args=None) -> None:
    print(f"\n  AutoTelegramForward v{APP_VERSION} — Unified Microservices Status")
    print("  " + "-" * 56)
    for name, state, detail in _describe():
        mark = "🟢" if state in ("RUNNING", "LISTENING") else \
               "🔴" if state in ("DEAD", "CLOSED") else "⚪"
        print(f"  {mark} {name:<18} {state:<9} {detail}")
    print()


def cmd_logs(_args=None) -> None:
    p = argparse.ArgumentParser(description="Show recent service logs")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("--service", action="store_true",
                   help="Read from the OS service journal instead")
    args, _ = p.parse_known_args(sys.argv[2:])
    if args.service and not IS_WIN:
        sh("journalctl --user -u atf.service -u atf-logger.service -u atf-web.service "
           f"-n {args.lines} --no-pager")
        return
    log_file = ROOT / "data" / "atf.out.log"
    if not log_file.exists():
        if IS_WIN or IS_MAC or not _systemd_state("atf.service"):
            print("No logs yet — start services first (`atf start`).")
            return
        print("(local log empty — reading OS service journal …)")
        args.service = True
    if args.service and not IS_WIN:
        sh("journalctl --user -u atf.service -u atf-logger.service -u atf-web.service "
           f"-n {args.lines} --no-pager")
        return
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.lines:]))


def cmd_health(_args=None) -> None:
    print("=" * 64)
    print(f"  AutoTelegramForward v{APP_VERSION} — Health & Diagnostics Check")
    print("=" * 64)
    print(f"• Mode               : {'frozen binary' if FROZEN else 'source'}")
    print(f"• Python executable  : {PY_EXE} ({'OK' if Path(PY_EXE).exists() else 'MISSING'})")
    print(f"• Config file        : {CONFIG} ({'OK' if CONFIG.exists() else 'MISSING'})")
    db_file = ROOT / "data" / "atf.db"
    print(f"• SQLite Database    : {db_file} ({'OK' if db_file.exists() else 'MISSING'})")
    print(f"• Web Gateway binary : {find_web() or 'built in web/backend (Axum)'}")
    print(f"• Go API binary      : {find_api() or 'not built (optional)'}")
    print(f"• Core gRPC port 6001: {'ACTIVE' if port_open(6001) else 'INACTIVE'}")
    print(f"• Logger port 6002   : {'ACTIVE' if port_open(6002) else 'INACTIVE'}")
    print(f"• Web Gateway :8088  : {'ACTIVE (http://localhost:8088)' if port_open(8088) else 'INACTIVE'}")
    print(f"• REST API port 8080 : {'ACTIVE (http://localhost:8080)' if port_open(8080) else 'INACTIVE'}")
    print(f"• Docker Engine      : {'available' if have('docker') else 'not installed'}")
    print(f"• Kubernetes kubectl : {'available' if have('kubectl') else 'not installed'}")
    print("=" * 64)


def cmd_version(_args=None) -> None:
    print(f"atf v{APP_VERSION} ({platform.system()}/{platform.machine()})"
          f"{' [frozen]' if FROZEN else ''}")


# --------------------------------------------------------------------------- #
# OS Service Management (systemd / launchd / Task Scheduler)
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
    web_exe = find_web() or ROOT / "web" / "backend" / "target" / "release" / "atf-web-backend"

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
ExecStart={exe} core
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""
    web_unit = f"""[Unit]
Description=AutoTelegramForward Web Microservice (Rust + Axum & React)
After=network.target atf.service
Wants=atf.service

[Service]
Type=simple
WorkingDirectory={ROOT}
Environment="ATF_WEB_ADDR=0.0.0.0:8088"
Environment="ATF_DB_PATH=data/atf.db"
Environment="ATF_LOGS_DB_PATH=data/atf_logs.db"
ExecStart={web_exe if web_exe.exists() else f"{exe} web"}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""
    (d / "atf-logger.service").write_text(logger_unit, encoding="utf-8")
    (d / "atf.service").write_text(atf_unit, encoding="utf-8")
    (d / "atf-web.service").write_text(web_unit, encoding="utf-8")
    ok("Systemd user units written to ~/.config/systemd/user/")
    sh("systemctl --user daemon-reload")
    sh("systemctl --user enable --now atf-logger.service atf.service atf-web.service")
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
    if not IS_WIN and not IS_MAC:
        sh("systemctl --user status atf.service atf-logger.service atf-web.service --no-pager 2>/dev/null || true")


def cmd_service_uninstall(_args=None) -> None:
    if IS_WIN:
        sh('schtasks /delete /tn "AutoTelegramForward" /f')
    elif IS_MAC:
        f = Path.home() / "Library/LaunchAgents/com.autotelegramforward.core.plist"
        sh(f"launchctl unload {f} 2>/dev/null || true")
        if f.exists():
            f.unlink()
    else:
        sh("systemctl --user disable --now atf.service atf-logger.service atf-web.service 2>/dev/null || true")
        d = Path.home() / ".config" / "systemd" / "user"
        for name in ("atf.service", "atf-logger.service", "atf-web.service"):
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
            sh("systemctl --user start atf-logger.service atf.service atf-web.service")
            cmd_service_status()
    elif sub == "stop":
        if IS_WIN or IS_MAC:
            cmd_stop()
        else:
            sh("systemctl --user stop atf.service atf-logger.service atf-web.service")
            print("🛑 Services stopped.")
    elif sub == "restart":
        if IS_WIN or IS_MAC:
            cmd_restart()
        else:
            sh("systemctl --user restart atf-logger.service atf.service atf-web.service")
            cmd_service_status()
    elif sub in ("logs", "log"):
        cmd_logs(["--service"])
    elif sub in ("uninstall", "remove"):
        cmd_service_uninstall()
    else:
        print("Usage: atf service [install|status|start|stop|restart|logs|uninstall]")


# --------------------------------------------------------------------------- #
# Docker compose lifecycle
# --------------------------------------------------------------------------- #
def _compose_file() -> Path:
    for cand in (ROOT / "docker-compose.yml", ROOT / "docker-compose.yaml"):
        if cand.exists():
            return cand
    die("docker-compose.yml not found next to the atf binary.")


def _compose_base() -> "list[str]":
    if have("docker") and sh("docker compose version", capture_output=True).returncode == 0:
        return ["docker", "compose"]
    if have("docker-compose"):
        return ["docker-compose"]
    die("Docker is not installed. Please install Docker first: https://docs.docker.com/get-docker/")


def cmd_docker(_args=None) -> None:
    sub = sys.argv[2] if len(sys.argv) > 2 else "ps"
    compose = _compose_file()
    base = _compose_base()

    if sub == "up":
        step("Building & launching microservices containers (core + logger + web + api) ...")
        run([*base, "-f", str(compose), "up", "-d", "--build"])
        run([*base, "-f", str(compose), "ps"])
        ok("All containers started!\n  • Web Dashboard: http://localhost:8088\n  • REST API:      http://localhost:8080")
    elif sub == "down":
        run([*base, "-f", str(compose), "down"])
        ok("Containers stopped and removed.")
    elif sub == "restart":
        run([*base, "-f", str(compose), "restart"])
        ok("Containers restarted.")
    elif sub in ("logs", "log"):
        svc = sys.argv[3] if len(sys.argv) > 3 else None
        run([*base, "-f", str(compose), "logs", "--tail=100", "-f"] + ([svc] if svc else []))
    elif sub == "ps":
        run([*base, "-f", str(compose), "ps"])
    elif sub == "pull":
        run([*base, "-f", str(compose), "pull"])
        ok("Images pulled.")
    else:
        print("Usage: atf docker [up|down|restart|logs [service]|ps|pull]")


# --------------------------------------------------------------------------- #
# Kubernetes lifecycle (`atf k8s`)
# --------------------------------------------------------------------------- #
def cmd_k8s(_args=None) -> None:
    sub = sys.argv[2] if len(sys.argv) > 2 else "status"
    k8s_dir = ROOT / "k8s"
    all_in_one = k8s_dir / "all-in-one.yaml"

    if not have("kubectl"):
        warn("`kubectl` command line tool not found in PATH.")
        print("Install kubectl: https://kubernetes.io/docs/tasks/tools/")

    if sub == "apply":
        if not all_in_one.exists():
            die(f"Manifest not found at {all_in_one}")
        step("Deploying AutoTelegramForward microservices to Kubernetes ...")
        sh(f"kubectl apply -f {all_in_one}")
        ok("Kubernetes manifests applied. Checking pod status in namespace `autoforward` ...")
        time.sleep(2)
        sh("kubectl get pods,svc,pvc -n autoforward")
    elif sub == "status":
        step("Kubernetes microservices status in namespace `autoforward`:")
        sh("kubectl get all,pvc -n autoforward")
    elif sub in ("logs", "log"):
        svc = sys.argv[3] if len(sys.argv) > 3 else "core"
        sh(f"kubectl logs -n autoforward -l app=atf-{svc} --tail=100 -f")
    elif sub == "delete":
        if not all_in_one.exists():
            die(f"Manifest not found at {all_in_one}")
        step("Deleting AutoTelegramForward from Kubernetes ...")
        sh(f"kubectl delete -f {all_in_one}")
        ok("Kubernetes resources removed.")
    elif sub == "generate":
        step("Generating custom Kubernetes deployment manifest from config.yaml ...")
        token = "CHANGE_ME_BOT_TOKEN"
        admin = "0"
        master_key = "CHANGE_ME_MASTER_KEY"
        if CONFIG.exists():
            try:
                import yaml
                data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
                token = data.get("bot_token", token)
                admin_ids = data.get("admin_ids", [0])
                admin = str(admin_ids[0]) if admin_ids else "0"
                master_key = data.get("master_key", master_key)
            except Exception:
                pass
        custom_out = k8s_dir / "custom-deploy.yaml"
        raw_manifest = (k8s_dir / "all-in-one.yaml").read_text(encoding="utf-8")
        raw_manifest = raw_manifest.replace("CHANGE_ME_BOT_TOKEN", token)
        raw_manifest = raw_manifest.replace("CHANGE_ME_RANDOM_MASTER_KEY_32_CHARS", master_key)
        raw_manifest = raw_manifest.replace('ATF_ADMIN_ID: "0"', f'ATF_ADMIN_ID: "{admin}"')
        custom_out.write_text(raw_manifest, encoding="utf-8")
        ok(f"Generated ready-to-apply manifest with your secrets: {custom_out}")
        print("Apply it anytime with:   kubectl apply -f k8s/custom-deploy.yaml")
    else:
        print("Usage: atf k8s [apply|status|logs [core|logger|web|api]|delete|generate]")


# --------------------------------------------------------------------------- #
# test / update / restore
# --------------------------------------------------------------------------- #
def cmd_test(_args=None) -> None:
    ensure_venv()
    print("▶ Running Python test-suite ...")
    run([str(PY_EXE), "-m", "pytest", "-q", *sys.argv[2:]])
    if go_available() and (ROOT / "api").exists():
        print("▶ Running Go API test-suite ...")
        sh("go test ./...", cwd=ROOT / "api")
    if have("cargo") and (ROOT / "web" / "backend").exists():
        print("▶ Running Rust Web tests ...")
        sh("cargo test", cwd=ROOT / "web" / "backend")
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
    if find_web():
        build_web()
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
# Foreground service shims (`atf core` / `atf logger` / `atf web`)
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


def cmd_web(_args=None) -> None:
    web_exe = find_web() or build_web()
    if not web_exe:
        die("Web binary not found and cargo not available.")
    env = _service_env()
    env["ATF_WEB_ADDR"] = "0.0.0.0:8088"
    env["ATF_DB_PATH"] = str(ROOT / "data" / "atf.db")
    env["ATF_LOGS_DB_PATH"] = str(ROOT / "data" / "atf_logs.db")
    subprocess.run([str(web_exe)], cwd=ROOT, env=env)


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
    "k8s": cmd_k8s,
    "test": cmd_test,
    "update": cmd_update,
    "restore": cmd_restore,
    # Foreground microservice shims
    "core": cmd_core,
    "logger": cmd_logger,
    "web": cmd_web,
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
