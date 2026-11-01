#!/usr/bin/env python3
"""Cross-engine parity: the reference decision runner (Python side).

Loads every fixture in worker/internal/parity/fixtures, runs the REAL
core.domain.services.FilterEngine on it — the same class the live pipeline
uses at delivery time — and writes one normalized decision per fixture.

The harness never sends a Telegram message, never touches a session, never
mutates production data: it exercises one pure decision function against a
fixture table. Normalization is deliberately lossy in the right direction:
`regex_block:<pattern>` keeps the pattern but drops nothing else, so the
comparison is on the *decision* (allow/drop + why), not on enum spelling.

Output: one JSON object per line, {"name":..., "action":..., "reason":...}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# The Core imports as the top-level package `core` (its own process runs with
# the repo root on sys.path), so the reference resolves from the repository
# root, not from core/ or the parity directory.
ROOT = Path(__file__).resolve().parents[3]          # repo root
sys.path.insert(0, str(ROOT))

from core.domain.entities import (  # noqa: E402
    FilterRule,
    MediaType,
    MessagePayload,
)
from core.domain.services import FilterEngine  # noqa: E402

FIXTURES = ROOT / "worker" / "internal" / "parity" / "fixtures"


def payload_from(fx: dict) -> MessagePayload:
    # The Core's MediaType members carry lowercase values ("video"), so the
    # enum lookup must preserve the case the fixture wrote;upper() would
    # find VIDEO by name, but a value-based comparison like the engine's own
    # `media in blocked_lower` happens on the lowercase value.
    media = fx.get("media_type") or "text"
    try:
        media_enum = MediaType(media)          # by value, e.g. "video"
    except ValueError:
        try:
            media_enum = MediaType(media.upper())  # by name fallback
        except ValueError:
            media_enum = MediaType.TEXT
    return MessagePayload(
        text=fx.get("text") or "",
        media_type=media_enum,
        message_id=1,
        chat_id="-100parity",
        is_service=bool(fx.get("is_service", False)),
    )


def filter_from(raw: dict | None):
    if raw is None:
        return None
    known = {
        "id", "is_enabled", "whitelist_keywords", "blacklist_keywords",
        "regex_patterns", "regex_block", "regex_require",
        "allowed_media_types", "blocked_media_types",
        "drop_service_messages", "min_message_length", "max_message_length",
    }
    kwargs = {k: v for k, v in raw.items() if k in known}
    return FilterRule(**kwargs)


def normalize_reason(reason: str) -> str:
    """Collapse only the spelling differences both engines are allowed.

    A truncated pattern is still the same decision: keep the pattern text,
    drop nothing, and let the Go side apply the identical rule.
    """
    if reason.startswith("regex_block:") or reason.startswith("invalid_regex_block:"):
        return reason.split(":", 1)[0] + ":" + reason.split(":", 1)[1]
    return reason


def main() -> int:
    engine = FilterEngine()
    results = []
    for path in sorted(FIXTURES.glob("*.json")):
        fx = json.loads(path.read_text(encoding="utf-8"))
        outcome = engine.evaluate(payload_from(fx), filter_from(fx.get("filter")))
        results.append({
            "name": path.stem,
            "action": str(outcome.action.name),
            "reason": normalize_reason(outcome.reason),
        })
    # A JSON array, not JSONL: the Go harness embeds the file whole and
    # unmarshals it once, so the committed artifact is what CI compares.
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
