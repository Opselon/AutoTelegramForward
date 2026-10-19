"""What a Telegram failure *means* for a task — and what the engine must do.

The queue used to guess inside its `except` block: a hard-coded string list for
"permanent", and a blanket ``sev == "CRITICAL"`` that swept network errors in
with dead chats. Two consequences the owner could see:

* a chat that was merely unreachable (or a 500 from Telegram) disabled the
  task, and nothing said why;
* a chat that really was gone (chat deleted, account kicked, target banned)
  disabled the task with no reason stored, no notification, and no way back
  except toggling the whole task off and on.

This module is the one classification. It reads the normalized error model in
``errors.py`` (never a second table of strings) and answers three questions:

* **account** — the *session* is restricted or dead (a flood wait, a revoked
  key, a deactivated account): every task on that session is affected;
* **permanent** — this chat, and only this chat, can never accept the message
  until a human changes something (deleted channel, kicked bot, banned user);
* **transient** — retry later; the task is fine.

The classification is deliberately narrow: an unknown error is *unknown*, and
the caller keeps retrying it rather than pausing a task on a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import SEV_AUTH, extract

__all__ = [
    "FailureDecision",
    "ACCOUNT_ERRORS",
    "PERMANENT_CHAT_ERRORS",
    "classify_failure",
    "decision_from_code",
    "rate_limit_seconds",
    "error_code",
]

#: The chat is gone, or the sending account may never post there again.
#: Each of these is a *state*, not a rate: retrying cannot change it.
PERMANENT_CHAT_ERRORS = frozenset({
    "CHANNEL_INVALID",
    "CHANNEL_PRIVATE",
    "CHAT_ID_INVALID",
    "CHAT_WRITE_FORBIDDEN",
    "CHAT_ADMIN_REQUIRED",
    "CHAT_RESTRICTED",
    "CHAT_SEND_MEDIA_FORBIDDEN",
    "CHAT_SEND_PLAIN_FORBIDDEN",
    "CHAT_SEND_PHOTOS_FORBIDDEN",
    "CHAT_SEND_VIDEOS_FORBIDDEN",
    "CHAT_SEND_AUDIOS_FORBIDDEN",
    "CHAT_SEND_DOCS_FORBIDDEN",
    "CHAT_SEND_STICKERS_FORBIDDEN",
    "CHAT_SEND_GIFS_FORBIDDEN",
    "CHAT_SEND_POLLS_FORBIDDEN",
    "CHAT_SEND_ROUNDVIDEOS_FORBIDDEN",
    "CHAT_SEND_VOICES_FORBIDDEN",
    "PEER_ID_INVALID",
    "USERNAME_INVALID",
    "USERNAME_NOT_OCCUPIED",
    "USER_BANNED_IN_CHANNEL",
    "USER_IS_BLOCKED",
    "USER_NOT_MUTUAL_CONTACT",
    "INPUT_USER_DEACTIVATED",
    "USER_DEACTIVATED",
})

#: The *account* is restricted/dead: everything routed through it stops.
#: A flood wait is not here — it arrives as a `Flood` exception and the queue
#: already turns it into a timed `ClientPool` suspension with Telegram's own
#: duration. These are the ones that need a human (re-login, unban).
ACCOUNT_ERRORS = frozenset({
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_DUPLICATED",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
    "USER_DEACTIVATED_BAN",
    "ACCOUNT_BANNED",
})

#: Network/transport markers. A message containing one of these is transient
#: even when the exception class name looks fatal (`RPCError` with no ID).
_TRANSIENT_HINTS = (
    "TIMEOUT",
    "TIMED OUT",
    "CONNECTION",
    "NETWORK",
    "SERVER_ERROR",
    "INTERNAL",
    "RPC_CALL_FAIL",
    "RPC_MCGET_FAIL",
    "SERVICE_UNAVAILABLE",
    "-503",
    "BAD_GATEWAY",
    "GATEWAY_TIMEOUT",
)


@dataclass(frozen=True)
class FailureDecision:
    """One failure, classified. ``kind`` is what the queue branches on."""

    kind: str          # "account" | "permanent" | "transient" | "unknown"
    code: str          # Telegram error id, uppercased ("" when unknown)
    retryable: bool
    detail: str = ""

    @property
    def pauses_task(self) -> bool:
        return self.kind == "permanent"

    @property
    def suspends_account(self) -> bool:
        return self.kind == "account"


def decision_from_code(code: str) -> FailureDecision:
    code = str(code or "").strip().upper()
    if code in ACCOUNT_ERRORS:
        return FailureDecision("account", code, retryable=False)
    if code in PERMANENT_CHAT_ERRORS:
        return FailureDecision("permanent", code, retryable=False)
    if any(h in code for h in _TRANSIENT_HINTS):
        return FailureDecision("transient", code, retryable=True)
    return FailureDecision("unknown", code, retryable=True)


#: Error ids Telegram uses for "wait N seconds". Only these suspend an account;
#: every other retryable error gets an ordinary backoff, so a flaky network can
#: never be mistaken for a restriction.
_RATE_LIMIT_PREFIXES = (
    "FLOOD_WAIT",
    "FLOOD_PREMIUM_WAIT",
    "FLOOD_TEST",
    "SLOWMODE_WAIT",
)


def rate_limit_seconds(exc: BaseException) -> int:
    """Telegram's own wait in seconds when this *is* a rate-limit error, else 0.

    Deliberately strict: a `Flood`-family class or a `FLOOD_*`/`SLOWMODE_*`
    id, and a duration that Telegram actually supplied. "Recoverable" is not
    enough — that is how a network error used to suspend a whole account.
    """
    try:
        event = extract(exc)
    except Exception:
        return 0
    code = (event.error_type or "").upper()
    name = type(exc).__name__
    is_flood = code.startswith(_RATE_LIMIT_PREFIXES) or "Flood" in name or "Slowmode" in name
    if not is_flood:
        return 0
    return int(event.retry_after or 0)


def error_code(exc: BaseException) -> str:
    """Telegram's error id for ``exc`` (``""`` when it carries none)."""
    try:
        return (extract(exc).error_type or "").strip().upper()
    except Exception:
        return ""


def classify_failure(exc: BaseException) -> FailureDecision:
    """Classify a raised Telegram error without ever guessing.

    The normalized event is the source of truth: its ``error_type`` is the
    Telegram id, and its ``retryable``/``severity`` carry what the error model
    already decided. A transport failure (HTTP timeout, connection reset) has
    no error id and is transient.
    """
    try:
        event = extract(exc)
    except Exception:  # never let diagnostics turn a failure into a crash
        return FailureDecision("unknown", "", retryable=True)

    code = (event.error_type or "").strip().upper()
    detail = event.explanation or str(exc)[:300]

    if code in ACCOUNT_ERRORS or (event.severity == SEV_AUTH and not code):
        return FailureDecision("account", code or "ACCOUNT_LEVEL", retryable=False, detail=detail)
    if code in PERMANENT_CHAT_ERRORS:
        return FailureDecision("permanent", code, retryable=False, detail=detail)
    if code and any(h in code for h in _TRANSIENT_HINTS):
        return FailureDecision("transient", code, retryable=True, detail=detail)
    if event.retryable:
        return FailureDecision("transient", code, retryable=True, detail=detail)
    if not code:
        return FailureDecision("transient", "", retryable=True, detail=detail)
    return FailureDecision("unknown", code, retryable=True, detail=detail)
