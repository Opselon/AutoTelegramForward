#!/usr/bin/env python
"""AutoTelegramForward — one-command CLI.

Very easy setup:  python atf.py setup   (paste your bot token, done)
Start:            python atf.py start
Tests:            python atf.py test
"""

import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
CONFIG = ROOT / "config.yaml"
IS_WIN = platform.system() == "Windows"
PY_EXE = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
API_EXE = ROOT / "api" / ("atf-api.exe" if IS_WIN else "atf-api")

# Public api_id/api_hash from the open-source Telegram Desktop client.
DEFAULT_API_ID = 17349
DEFAULT_API_HASH = "344583e45741c457fe1862106095a5eb"

STEPS = "  [1/5]"


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, cwd=ROOT, **kw)


# --------------------------------------------------------------------------- #
def py_in_venv(*args):
    return run([str(PY_EXE), *args])


def python_ok() -> bool:
    for cand in ("python", "python3"):
        try:
            v = sh(f"{cand} --version").stdout.decode().strip()
            major, minor = v.split()[1].split(".")[:2]
            if (int(major), int(minor)) >= (3, 10):
                return True
        except Exception:
            continue
    return False


def ensure_venv():
    if PY_EXE.exists():
        return
    print(f"{STEPS} Creating virtual environment ...")
    venv.create(str(VENV), with_pip=True)
    print(f"{STEPS} Installing dependencies (this takes ~30s) ...")
    py_in_venv("-m", "pip", "install", "--quiet", "--upgrade", "pip")
    req_file = ROOT / "requirements.txt"
    if req_file.exists():
        py_in_venv("-m", "pip", "install", "--quiet", "-r", str(req_file))
    else:
        py_in_venv(
            "-m", "pip", "install", "--quiet",
            "pyrogram", "tgcrypto", "grpcio", "grpcio-tools", "protobuf",
            "httpx", "cryptography", "pyyaml", "pytest", "pytest-asyncio", "asyncpg",
        )


def go_available() -> bool:
    try:
        sh("go version")
        return True
    except Exception:
        return False


def build_api():
    if not go_available():
        print("[api] Go toolchain not found — REST API skipped (bot still works 100%).")
        return False
    if API_EXE.exists():
        return True
    print("[api] Building Go control API ...")
    sh("go build -o " + ("atf-api.exe" if IS_WIN else "atf-api") + " .", cwd=ROOT / "api")
    return True


# --------------------------------------------------------------------------- #
def cmd_setup():
    import argparse
    parser = argparse.ArgumentParser(description="AutoTelegramForward Setup")
    parser.add_argument("--token", help="Telegram Bot Token from @BotFather")
    parser.add_argument("--admin", default="", help="Telegram user ID for admin rights")
    parser.add_argument("--lang", default="fa", choices=["en", "fa", "ru", "zh"], help="Default language")
    parser.add_argument("--service", action="store_true", help="Auto install and start systemd service")
    parser.add_argument("--non-interactive", action="store_true", help="Do not prompt for input")
    args, _ = parser.parse_known_args(sys.argv[2:])

    token = args.token or os.environ.get("ATF_BOT_TOKEN")
    admin = args.admin or os.environ.get("ATF_ADMIN_ID", "")
    lang = args.lang or os.environ.get("ATF_LANG", "fa")

    if not token and not args.non_interactive and sys.stdin.isatty():
        print("=" * 62)
        print("  AutoTelegramForward — Easy Setup")
        print("=" * 62)
        token = input("\n(1/3) Paste your BOT TOKEN (from @BotFather): ").strip()
        if not admin:
            admin = input("(2/3) Your Telegram user ID for admin rights (Enter to skip): ").strip()
        print("(3/3) Language: 1=English  2=فارسی  3=Русский  4=中文  (Enter=2)")
        ans = input("> ").strip()
        if ans in ("1", "en"):
            lang = "en"
        elif ans in ("2", "fa", ""):
            lang = "fa"
        elif ans in ("3", "ru"):
            lang = "ru"
        elif ans in ("4", "zh"):
            lang = "zh"

    if not token or ":" not in token:
        sys.exit("✗ Invalid or missing bot token. Provide --token or get one from @BotFather.")

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
    print("\n✅ Setup complete!  config.yaml written (master key auto-generated).")
    if args.service:
        cmd_service_install()
    else:
        print("▶  Start now with:   python atf.py start (or: python atf.py service install)\n")


def cmd_start():
    if not CONFIG.exists():
        sys.exit("✗ No config.yaml — run:  python atf.py setup")
    ensure_venv()
    has_api = build_api()
    env = {**os.environ, "ATF_GRPC_PORT": "6001",
           "ATF_LOGGER_PORT": "6002", "ATF_LOGGER_ADDR": "localhost:6002"}

    print("▶ Starting Logger service (debug log store) ...")
    logger_proc = subprocess.Popen(
        [str(PY_EXE), "-m", "logger.main"], cwd=ROOT, env=env)
    procs = [logger_proc]

    import time
    time.sleep(2)

    print("▶ Starting Python core (bot + gRPC) ...")
    core = subprocess.Popen([str(PY_EXE), "-m", "core.main"], cwd=ROOT, env=env)
    procs.append(core)

    if has_api:
        import time
        time.sleep(3)
        grpc_port = env.get("ATF_GRPC_PORT", "6001")
        api_env = {**env, "ATF_CORE_GRPC": f"localhost:{grpc_port}"}
        print("▶ Starting Go API on http://localhost:8080 ...")
        api = subprocess.Popen([str(API_EXE)], cwd=ROOT / "api", env=api_env)
        procs.append(api)
        print("\n✅ Running!  Bot is live;  REST API: http://localhost:8080")
    else:
        print("\n✅ Running!  Bot is live (REST API skipped — no Go toolchain).")

    print("   Press Ctrl+C to stop.\n")
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


def cmd_test():
    ensure_venv()
    print("▶ Python tests ...")
    py_in_venv("-m", "pytest", "-q", *sys.argv[2:])
    if go_available():
        print("▶ Go tests ...")
        sh("go test ./...", cwd=ROOT / "api")
    print("✅ All tests passed.")


def cmd_update():
    sh("git pull")
    py_in_venv("-m", "pip", "install", "--quiet", "-U",
               "pyrogram", "tgcrypto", "grpcio", "httpx", "cryptography", "pyyaml")
    if API_EXE.exists():
        build_api()
    print("✅ Updated.")


def cmd_restore():
    """Restore a session backup:  python atf.py restore <file.atf>"""
    if len(sys.argv) < 3:
        sys.exit("Usage: python atf.py restore <backup-file.atf>")
    ensure_venv()
    py_in_venv("-c",
               "import sys; from core.recovery import restore_from_cli; "
               "restore_from_cli(sys.argv[1])",
               sys.argv[2])


def cmd_service_install():
    if IS_WIN:
        print("Systemd service is only supported on Linux.")
        return
    ensure_venv()
    systemd_user_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_user_dir.mkdir(parents=True, exist_ok=True)

    logger_unit = f"""[Unit]
Description=AutoTelegramForward Logger Microservice
After=network.target

[Service]
Type=simple
WorkingDirectory={ROOT}
Environment="ATF_LOGGER_PORT=6002"
Environment="ATF_LOGGER_HOST=127.0.0.1"
ExecStart={PY_EXE} -m logger.main
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

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
ExecStart={PY_EXE} -m core.main
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
    (systemd_user_dir / "atf-logger.service").write_text(logger_unit, encoding="utf-8")
    (systemd_user_dir / "atf.service").write_text(atf_unit, encoding="utf-8")
    print("✅ Systemd user service files created in ~/.config/systemd/user/")
    sh("systemctl --user daemon-reload")
    sh("systemctl --user enable --now atf-logger.service atf.service")
    sh("loginctl enable-linger $(whoami) 2>/dev/null || true")
    print("🚀 Services enabled and started! Linger enabled.")
    cmd_service_status()


def cmd_service_status():
    if IS_WIN:
        print("Systemd service is only supported on Linux.")
        return
    sh("systemctl --user status atf.service atf-logger.service --no-pager")


def cmd_service_uninstall():
    if IS_WIN:
        return
    sh("systemctl --user disable --now atf.service atf-logger.service 2>/dev/null || true")
    systemd_user_dir = Path.home() / ".config" / "systemd" / "user"
    for name in ("atf.service", "atf-logger.service"):
        f = systemd_user_dir / name
        if f.exists():
            f.unlink()
    sh("systemctl --user daemon-reload")
    print("🗑 Services disabled and removed.")


def cmd_service():
    sub = sys.argv[2] if len(sys.argv) > 2 else "status"
    if sub in ("install", "setup"):
        cmd_service_install()
    elif sub == "status":
        cmd_service_status()
    elif sub == "start":
        sh("systemctl --user start atf-logger.service atf.service")
        cmd_service_status()
    elif sub == "stop":
        sh("systemctl --user stop atf.service atf-logger.service")
        print("🛑 Services stopped.")
    elif sub == "restart":
        sh("systemctl --user restart atf-logger.service atf.service")
        cmd_service_status()
    elif sub in ("logs", "log"):
        sh("journalctl --user -u atf.service -u atf-logger.service -n 50 --no-pager")
    elif sub in ("uninstall", "remove"):
        cmd_service_uninstall()
    else:
        print("Usage: python atf.py service [install|status|start|stop|restart|logs|uninstall]")


def cmd_health():
    print("=" * 60)
    print("  AutoTelegramForward — Health & Diagnostics Check")
    print("=" * 60)
    print(f"• Python executable  : {PY_EXE} ({'OK' if PY_EXE.exists() else 'MISSING'})")
    print(f"• Config file        : {CONFIG} ({'OK' if CONFIG.exists() else 'MISSING'})")
    db_file = ROOT / "data" / "atf.db"
    print(f"• SQLite Database    : {db_file} ({'OK' if db_file.exists() else 'MISSING'})")
    import socket
    def port_open(p):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(('127.0.0.1', p)) == 0
    print(f"• Core gRPC port 6001: {'ACTIVE' if port_open(6001) else 'INACTIVE'}")
    print(f"• Logger port 6002   : {'ACTIVE' if port_open(6002) else 'INACTIVE'}")
    print("=" * 60)


COMMANDS = {
    "setup": cmd_setup,
    "start": cmd_start,
    "service": cmd_service,
    "health": cmd_health,
    "test": cmd_test,
    "update": cmd_update,
    "restore": cmd_restore,
}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd in COMMANDS:
        COMMANDS[cmd]()
        return
    print(__doc__)
    print("Commands: " + ", ".join(COMMANDS))


if __name__ == "__main__":
    main()
