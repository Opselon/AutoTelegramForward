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
    py_in_venv(
        "-m", "pip", "install", "--quiet",
        "pyrogram", "tgcrypto", "grpcio", "grpcio-tools", "protobuf",
        "httpx", "cryptography", "pyyaml", "pytest", "pytest-asyncio",
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
    print("=" * 62)
    print("  AutoTelegramForward — Easy Setup")
    print("=" * 62)

    token = input("\n(1/3) Paste your BOT TOKEN (from @BotFather): ").strip()
    if not token or ":" not in token:
        sys.exit("✗ Invalid bot token. Get one from @BotFather on Telegram.")

    admin = input("(2/3) Your Telegram user ID for admin rights (Enter to skip): ").strip()

    print("(3/3) Language: 1=English  2=فارسی  3=Русский  4=中文  (Enter=1)")
    lang = {"1": "en", "2": "fa", "3": "ru", "4": "zh"}.get(input("> ").strip(), "en")

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
    print("▶  Start now with:   python atf.py start\n")


def cmd_start():
    if not CONFIG.exists():
        sys.exit("✗ No config.yaml — run:  python atf.py setup")
    ensure_venv()
    has_api = build_api()
    env = {**os.environ, "ATF_GRPC_PORT": "6001"}

    print("▶ Starting Python core (bot + gRPC) ...")
    core = subprocess.Popen([str(PY_EXE), "-m", "core.main"], cwd=ROOT, env=env)
    procs = [core]

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


COMMANDS = {
    "setup": cmd_setup,
    "start": cmd_start,
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
