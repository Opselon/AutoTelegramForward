"""Turning a typed answer into the type the use case expects.

Small on purpose: this is the one place the console decides that `true` is a
boolean and `8080` a number, so a guided edit writes the same values a
`--set key=value` flag would (both end in `json`-ish coercion, not a string that
looks like a boolean).
"""

from __future__ import annotations

import json
from typing import Any


def coerce(text: str) -> Any:
    """`text` as the most specific JSON scalar it can be, else the string."""
    raw = (text or "").strip()
    if raw == "":
        return ""
    lowered = raw.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", "unset"):
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


__all__ = ["coerce"]
