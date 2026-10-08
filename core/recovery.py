"""Session restore helper used by `python atf.py restore <file.atf>`."""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import Config  # noqa: E402
from core.main import build_container  # noqa: E402
from core.infrastructure.telegram.client_pool import ClientPool  # noqa: E402


def restore_from_cli(backup_path: str) -> None:
    blob = Path(backup_path).read_text(encoding="utf-8").strip()
    bundle = json.loads(base64.urlsafe_b64decode(blob.encode("ascii")))

    cfg = Config.load()
    container = build_container(cfg)
    session = container["sessions"].create_sync(
        phone_number=bundle["phone"],
        session_string_encrypted=bundle["session_string"],
    )
    print(f"✅ Session restored: {bundle['phone']} (id={session.id[:8]}…)")
    print("   Start the bot with: python atf.py start")
