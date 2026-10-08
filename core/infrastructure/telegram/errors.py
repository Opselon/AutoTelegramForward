"""Telegram error translation layer.

Goal: never swallow an error silently, never show the user a raw traceback.
Every pyrogram `RPCError` is mapped to (user_message_key, severity, retry_hint)
so the bot can answer with the *exact* reason Telegram itself reported, in the
user's language.

Usage in any handler:

    from .errors import tg_error
    try:
        await client.send_message(...)
    except Exception as exc:
        key, detail = tg_error(exc)
        await message.reply_text(i18n.t(key, error=detail))
"""

from __future__ import annotations

import logging
from typing import Tuple

from pyrogram.errors import RPCError

logger = logging.getLogger("atf.errors")

# --------------------------------------------------------------------------- #
# Severity → how the UI reacts
# --------------------------------------------------------------------------- #
SEV_INFO = "info"       # user mistake, fix and retry (e.g. wrong code)
SEV_WARN = "warn"       # transient / rate limit, retry with backoff
SEV_ERROR = "error"     # operation failed, must change something
SEV_AUTH = "auth"       # session died → needs re-login
SEV_FATAL = "fatal"     # app-level bug, admin must look

# (i18n key, severity, recoverable)
_NONE = ("err_internal", SEV_FATAL, False)


def _is(exc: BaseException, names: tuple) -> bool:
    """Class-name match (works across pyrogram minor versions)."""
    return type(exc).__name__ in names or any(
        getattr(exc, "__class__", None) and base.__name__ in names
        for base in type(exc).__mro__
    )


def _value(exc) -> int:
    """Extract the FloodWait/SlowmodeWait seconds value."""
    return int(getattr(exc, "value", 0) or 0)


# --------------------------------------------------------------------------- #
# The map — ordered by specificity, most specific first.
# --------------------------------------------------------------------------- #
def tg_error(exc: BaseException) -> Tuple[str, str, bool]:
    """Translate any exception into (i18n_key, severity, recoverable).

    The caller renders `i18n.t(key)` — translations carry the human wording and
    accept `{error}`, `{seconds}`, `{chat}`, `{reason}` placeholders.
    """
    if not isinstance(exc, BaseException):  # pragma: no cover
        return _NONE

    name = type(exc).__name__

    # --- login flow ------------------------------------------------------- #
    if name in ("PhoneNumberInvalid",):
        return ("err_phone_invalid", SEV_INFO, False)
    if name in ("PhoneCodeInvalid",):
        return ("err_code_invalid", SEV_INFO, False)
    if name in ("PhoneCodeExpired",):
        return ("err_code_expired", SEV_INFO, False)
    if name in ("PhoneCodeHashEmpty",):
        return ("err_code_empty", SEV_INFO, False)
    if name in ("SessionPasswordNeeded",):
        return ("err_2fa_needed", SEV_INFO, False)
    if name in ("PasswordHashInvalid",):
        return ("err_2fa_wrong", SEV_INFO, False)
    if name in ("PasswordEmpty",):
        return ("err_2fa_empty", SEV_INFO, False)
    if name in ("ApiIdInvalid",):
        return ("err_api_id_invalid", SEV_ERROR, False)
    if name in ("ApiIdPublishedFlood",):
        return ("err_api_id_flood", SEV_ERROR, False)
    if name in ("AuthKeyInvalid", "AuthKeyUnregistered", "AuthKeyDuplicated"):
        return ("err_auth_key", SEV_AUTH, False)
    if name in ("UserDeactivated", "UserDeactivatedBan",
                "PhoneNumberBanned", "PhoneNumberUnoccupied"):
        return ("err_account_banned", SEV_FATAL, False)

    # --- rate limiting ---------------------------------------------------- #
    if name in ("FloodWait",):
        return ("err_flood_wait", SEV_WARN, True)
    if name in ("SlowmodeWait",):
        return ("err_slowmode", SEV_WARN, True)
    if name in ("FloodPremiumWait",):
        return ("err_flood_wait", SEV_WARN, True)

    # --- permission / target problems ------------------------------------ #
    if name in ("ChatAdminRequired",):
        return ("err_admin_required", SEV_ERROR, False)
    if name in ("ChatWriteForbidden", "ChatForbidden", "SendMessageDenied"):
        return ("err_write_forbidden", SEV_ERROR, False)
    if name in ("ChannelPrivate",):
        return ("err_channel_private", SEV_ERROR, False)
    if name in ("ChatForwardsRestricted",):
        return ("err_forwards_restricted", SEV_ERROR, False)
    if name in ("ChatAboutTooLong",):
        return ("err_about_too_long", SEV_ERROR, False)
    if name in ("UserPrivacyRestricted",):
        return ("err_privacy_restricted", SEV_ERROR, False)
    if name in ("ChatIdInvalid", "PeerIdInvalid"):
        return ("err_peer_invalid", SEV_ERROR, False)
    if name in ("UsernameNotOccupied", "UsernameInvalid", "UsernameOccupied"):
        return ("err_username_invalid", SEV_ERROR, False)
    if name in ("ChatNotModified",):
        return ("err_not_modified", SEV_INFO, True)

    # --- message content -------------------------------------------------- #
    if name in ("MediaEmpty", "MediaInvalid"):
        return ("err_media_empty", SEV_INFO, False)
    if name in ("WebpageMediaEmpty",):
        return ("err_webpage_empty", SEV_INFO, False)
    if name in ("MediaCaptionTooLong",):
        return ("err_caption_too_long", SEV_ERROR, True)
    if name in ("MessageIdInvalid", "MsgIdInvalid"):
        return ("err_message_id_invalid", SEV_INFO, False)
    if name in ("MessageNotModified",):
        return ("err_not_modified", SEV_INFO, True)
    if name in ("ChatOccupyLocFailed",):
        return ("err_internal", SEV_ERROR, False)

    # --- generic pyrogram buckets ---------------------------------------- #
    if isinstance(exc, RPCError):
        # Everything else from Telegram's RPC layer: keep the exact code+msg
        # so debugging is possible without exposing internals to users.
        code = getattr(exc, "CODE", "?")
        return (f"err_rpc:{code}", SEV_ERROR, False)

    # --- client lifecycle ------------------------------------------------- #
    if name in ("ConnectionError", "OSError"):
        return ("err_connection", SEV_WARN, True)
    if name in ("TimeoutError", "asyncio.TimeoutError"):
        return ("err_timeout", SEV_WARN, True)

    return _NONE


def tg_detail(exc: BaseException) -> str:
    """Human-readable detail string for logs/admin messages (English)."""
    key, sev, _ = tg_error(exc)
    name = type(exc).__name__
    extra = ""
    if name in ("FloodWait", "SlowmodeWait", "FloodPremiumWait"):
        extra = f" ({_value(exc)}s)"
    text = str(exc).strip()
    if text and text != name:
        return f"{name}{extra}: {text}"
    return f"{name}{extra}"


def should_retry(exc: BaseException) -> bool:
    """True when the operation may succeed if retried after backoff."""
    return tg_error(exc)[2]


def wait_seconds(exc: BaseException, default: int = 30, cap: int = 3600) -> int:
    """Safe backoff for FloodWait-like errors."""
    secs = _value(exc)
    if secs <= 0:
        secs = default
    return max(1, min(secs, cap))


async def safe_call(coro_fn, *args, **kwargs):
    """Await a Telegram call; on a retryable error, sleep the wait time once.

    Returns the call result, or raises the original exception when not
    recoverable. Kept tiny — the pipeline's own backoff is elsewhere.
    """
    import asyncio

    try:
        return await coro_fn(*args, **kwargs)
    except Exception as exc:
        if not should_retry(exc):
            raise
        await asyncio.sleep(min(wait_seconds(exc) + 1, 3600))
        return await coro_fn(*args, **kwargs)
