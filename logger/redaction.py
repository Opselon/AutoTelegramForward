"""Ingest-time secret redaction for the Central Logger (mission §6.5).

Defense-in-depth: producers (Core audit pipeline) already redact at
AuditEvent construction; the Logger applies its own pass at ingest so events
from any producer (including raw python logging mirrors) can never persist
tokens, session strings, passwords, phone codes, or bearer headers to disk.

This module is intentionally dependency free and standalone on purpose:
the Logger runs as an isolated microservice with no core import path.
"""

import re

_SECRET_KEYS = (
    r"bot_?token|api_?hash|api_?id|session_?string|auth_?key|"
    r"passwo?rd|passwd|phone_?code|login_?code|2fa|two_?factor|secret|"
    r"access_?token|refresh_?token|bearer|authorization|api_?key|"
    r"cookie|x-?api-?key|x-?auth-?token"
)

# Quoted key/value: "password": "hunter2", 'api_key': "sk-abc..."
_SECRET_KV_QUOTED = re.compile(
    r'(?i)(["\']?\b(?:' + _SECRET_KEYS + r')\b["\']?\s*[=:]\s*["\'])[^"\']*?(["\'])'
)

# Unquoted key/value: token=abc123, api_key: sk-xyz
_SECRET_KV = re.compile(
    r"(?i)\b((?:" + _SECRET_KEYS + r")\s*[=:]\s*)([^\s;,&\"']+)"
)

# Bearer/authorization header of any casing: "Bearer eyJ...", "token eyJ..."
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+([A-Za-z0-9_\-\.=]{8,})")

# Telegram session strings are long base64-ish blobs.
_SESSION_BLOB = re.compile(r"\b([A-Za-z0-9+/]{80,}={0,2})\b")

# Telegram bot token shape: 123456789:AAxx... (8-12 digits, colon, 30+ chars).
_BOT_TOKEN = re.compile(r"\b(\d{8,12}:[A-Za-z0-9_\-]{30,})\b")

# Long hex blobs (64+ chars = sha256 hash) — keep short hex like chat ids.
_LONG_HEX = re.compile(r"\b[0-9a-f]{64,}\b", re.IGNORECASE)

# E.164-ish phone numbers.
_PHONE = re.compile(r"(?<![0-9])(\+?\d{10,15})(?![0-9])")

REPLACEMENT = "[REDACTED]"


def redact(text: str) -> str:
    """Return `text` with secret-bearing substrings replaced."""
    if not text:
        return text
    # Bearer first: the kv pass would otherwise eat the "Bearer" keyword and
    # leave the actual credential behind.
    out = _BEARER.sub(lambda m: m.group(1) + " " + REPLACEMENT, text)
    out = _SECRET_KV_QUOTED.sub(lambda m: m.group(1) + REPLACEMENT + m.group(2), out)
    out = _SECRET_KV.sub(lambda m: m.group(1) + REPLACEMENT, out)
    out = _BOT_TOKEN.sub(REPLACEMENT, out)
    out = _SESSION_BLOB.sub(REPLACEMENT, out)
    out = _LONG_HEX.sub(REPLACEMENT, out)
    out = _PHONE.sub(lambda m: m.group(0)[:4] + "[PHONE]", out)
    return out
