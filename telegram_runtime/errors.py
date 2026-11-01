"""Shared error taxonomy for the Telegram Runtime.

This is the Python half of the contract the Go Worker and the Runtime share:
every Telegram failure is normalized ONCE, here, into a typed
``TelegramOperationError`` carrying the exact signal Telegram reported (RPC
code, error type, wait value) plus the operational context the caller already
has. Go owns the retry decision from there.

The classification is derived from the Core's existing, battle-tested
``core.infrastructure.telegram.errors`` module rather than reinvented: the
same severity/retryable logic already drives the in-bot Log Center, so a
FloodWait means the same thing on both sides of the process boundary.

Rules (mirrors core/infrastructure/telegram/errors.py):
  * Never fabricate a Telegram error message.
  * Never hide the original error internally.
  * Never expose secrets / session strings / API keys: ``redact()`` is
    applied to every free-text field that could carry one.
  * Unknown Telegram errors stay visible as unknown — never silently mapped
    onto a wrong category.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("atf.telegram_runtime.errors")

# ---------------------------------------------------------------------------
# The shared vocabulary. These names are the Go-side DeliveryFailureCode enum
# values; the .proto file is the source of truth and this list must stay in
# sync with it (worker/proto/telegram_gateway.proto).
# ---------------------------------------------------------------------------

AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
SESSION_UNAVAILABLE = "SESSION_UNAVAILABLE"
PERMISSION_DENIED = "PERMISSION_DENIED"
CHAT_NOT_FOUND = "CHAT_NOT_FOUND"
MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
FORWARDING_RESTRICTED = "FORWARDING_RESTRICTED"
FLOOD_WAIT = "FLOOD_WAIT"
RATE_LIMITED = "RATE_LIMITED"
NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
NETWORK_DISCONNECTED = "NETWORK_DISCONNECTED"
MEDIA_DOWNLOAD_FAILED = "MEDIA_DOWNLOAD_FAILED"
MEDIA_UPLOAD_FAILED = "MEDIA_UPLOAD_FAILED"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
TELEGRAM_TEMPORARY_ERROR = "TELEGRAM_TEMPORARY_ERROR"
TELEGRAM_PERMANENT_ERROR = "TELEGRAM_PERMANENT_ERROR"
UNKNOWN_DELIVERY_OUTCOME = "UNKNOWN_DELIVERY_OUTCOME"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: Failures whose outcome on Telegram's side is unknown. Retrying blindly
#: here is what produces duplicate messages, so the Go side records UNKNOWN
#: instead and lets reconciliation decide.
AMBIGUOUS = frozenset({NETWORK_TIMEOUT, UNKNOWN_DELIVERY_OUTCOME})

#: Failures that may resolve on their own. Classification only — the Go
#: Worker owns the retry decision.
TRANSIENT = frozenset({
    FLOOD_WAIT, RATE_LIMITED, NETWORK_TIMEOUT, NETWORK_DISCONNECTED,
    SESSION_UNAVAILABLE, TELEGRAM_TEMPORARY_ERROR, SOURCE_UNAVAILABLE,
    TARGET_UNAVAILABLE, INTERNAL_ERROR,
})


@dataclass
class TelegramOperationError:
    """One Telegram failure, normalized once, with forensic context."""

    code: str = INTERNAL_ERROR
    error_type: str = ""
    error_code: str = ""
    retry_after_seconds: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "error_type": self.error_type,
            "error_code": self.error_code,
            "retry_after_seconds": self.retry_after_seconds,
            "message": self.message,
        }


def _redact(text: Any) -> str:
    """Strip anything that must never reach a log or a response field.

    Delegates to the Core's redactor when it is importable so the two
    boundaries cannot drift apart; a standalone import failure falls back to
    a conservative local strip (the runtime must never leak a secret just
    because the Core moved a module).
    """
    s = "" if text is None else str(text)
    try:
        from core.infrastructure.telegram.errors import redact
        return redact(s, keep_phone=True)
    except Exception:
        pass
    # Conservative local fallback: never raise, never leak.
    try:
        import re
        s = re.sub(r"(?i)(api[_-]?hash|api[_-]?id|token|session[_-]?string|"
                   r"password|phone[_-]?code|auth[_-]?key)\s*[:=]\s*\S+", "[REDACTED]", s)
        s = re.sub(r"\b[A-Za-z0-9+/]{80,}={0,2}\b", "[REDACTED]", s)
    except Exception:
        return "[redaction failed]"
    return s


def _class_attr(exc: BaseException, name: str) -> str:
    value = getattr(exc, name, None)
    return "" if value is None else str(value)


def wait_value(exc: BaseException) -> int:
    """Seconds a Flood/Slowmode error demands (0 when not applicable)."""
    value = getattr(exc, "value", None)
    if isinstance(value, int) and value > 0:
        return value
    return 0


def _is_restricted(exc: BaseException) -> bool:
    """A source that forbids forwarding/copying."""
    text = " ".join(filter(None, [_class_attr(exc, "ID"), type(exc).__name__]))
    lowered = text.lower()
    if "forbidden" in lowered and "forward" in lowered:
        return True
    # Pyrogram raises BotMethodInvalid / a 400 with a clear type name for
    # protected content; the Core's own detector is authoritative when
    # importable.
    try:
        from core.infrastructure.telegram.errors import is_restricted_source_error
        return bool(is_restricted_source_error(exc))
    except Exception:
        return False


def _is_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "Timeout" in name or "timeout" in name:
        return True
    try:
        from core.infrastructure.telegram.errors import is_timeout_error
        return bool(is_timeout_error(exc))
    except Exception:
        return False


def _is_auth(exc: BaseException) -> bool:
    text = " ".join(filter(None, [_class_attr(exc, "ID"), type(exc).__name__])).lower()
    return (
        "unauthenticated" in text
        or "auth_key" in text
        or "unauthorized" in text
        or "user_deactivated" in text
        or "session_revoked" in text
        or "AUTH_KEY_UNREGISTERED" in text.upper()
    )


def _is_permission(exc: BaseException) -> bool:
    text = " ".join(filter(None, [_class_attr(exc, "ID"), type(exc).__name__])).lower()
    return "forbidden" in text or "chat_write_forbidden" in text or "button_data_invalid" in text


def _is_chat_missing(exc: BaseException) -> bool:
    text = _class_attr(exc, "ID").upper()
    return "PEER_ID_INVALID" in text or "CHAT_ID_INVALID" in text or "CHANNEL_PRIVATE" in text


def _is_message_missing(exc: BaseException) -> bool:
    text = _class_attr(exc, "ID").upper()
    return "MESSAGE_ID_INVALID" in text or "MESSAGE_NOT_MODIFIED" in text


def _is_flood(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "Flood" in name or "Slowmode" in name:
        return True
    return "FLOOD_WAIT" in _class_attr(exc, "ID").upper() or "SLOWMODE" in _class_attr(exc, "ID").upper()


def _is_network(exc: BaseException) -> bool:
    name = type(exc).__name__
    return (
        "Connection" in name
        or "Network" in name
        or "OSError" in name
        or isinstance(exc, (ConnectionError, OSError))
    )


def normalize(exc: BaseException, *, operation: str = "") -> TelegramOperationError:
    """Normalize ONE Telegram failure into the shared typed vocabulary.

    Never fabricates: when nothing recognizable matches, the outcome is
    UNKNOWN_DELIVERY_OUTCOME (a timeout-shaped ambiguity) or
    TELEGRAM_PERMANENT_ERROR for a named-but-uncurated RPC error — visible,
    never silently mapped onto a wrong category.
    """
    error_type = _class_attr(exc, "ID") or type(exc).__name__
    error_code = _class_attr(exc, "CODE")
    retry = wait_value(exc)
    detail = _redact(str(exc) or type(exc).__name__)

    if _is_flood(exc):
        return TelegramOperationError(
            code=FLOOD_WAIT, error_type=error_type, error_code=error_code,
            retry_after_seconds=retry, message=detail,
        )
    if _is_timeout(exc):
        return TelegramOperationError(
            code=NETWORK_TIMEOUT, error_type=error_type, error_code=error_code,
            message=detail,
        )
    if _is_restricted(exc):
        return TelegramOperationError(
            code=FORWARDING_RESTRICTED, error_type=error_type, error_code=error_code,
            message=detail,
        )
    if _is_auth(exc):
        return TelegramOperationError(
            code=AUTHENTICATION_ERROR, error_type=error_type, error_code=error_code,
            message=detail,
        )
    if _is_permission(exc):
        return TelegramOperationError(
            code=PERMISSION_DENIED, error_type=error_type, error_code=error_code,
            message=detail,
        )
    if _is_chat_missing(exc):
        return TelegramOperationError(
            code=CHAT_NOT_FOUND, error_type=error_type, error_code=error_code,
            message=detail,
        )
    if _is_message_missing(exc):
        return TelegramOperationError(
            code=MESSAGE_NOT_FOUND, error_type=error_type, error_code=error_code,
            message=detail,
        )
    if _is_network(exc):
        return TelegramOperationError(
            code=NETWORK_DISCONNECTED, error_type=error_type, error_code=error_code,
            message=detail,
        )

    # Named RPC errors keep their Telegram type and are classified by the
    # bucket Telegram itself uses: 5xx is a temporary server condition, 4xx
    # is a permanent refusal the retry loop cannot fix.
    if error_code.startswith("5") or error_code in ("500", "502", "503", "520"):
        return TelegramOperationError(
            code=TELEGRAM_TEMPORARY_ERROR, error_type=error_type, error_code=error_code,
            message=detail,
        )
    if error_code.startswith("4") or error_code in ("400", "401", "403", "406", "420"):
        return TelegramOperationError(
            code=TELEGRAM_PERMANENT_ERROR, error_type=error_type, error_code=error_code,
            message=detail,
        )
    return TelegramOperationError(
        code=UNKNOWN_DELIVERY_OUTCOME, error_type=error_type, error_code=error_code,
        message=detail,
    )


def failed(code: str, message: str, *, error_type: str = "", error_code: str = "",
           retry_after_seconds: int = 0) -> TelegramOperationError:
    """Build a typed failure directly (no exception to normalize).

    Used for runtime-side preconditions that are not Telegram errors at all —
    a session that has no client, a media scratch path that cannot be
    written — so the Go side still receives the shared vocabulary instead of
    a bare string.
    """
    return TelegramOperationError(
        code=code, error_type=error_type or "RUNTIME",
        error_code=error_code, retry_after_seconds=retry_after_seconds,
        message=_redact(message),
    )
