"""Telegram-native error model + deterministic translation.

Every Telegram failure is normalized once, here, into a ``TelegramErrorEvent``
that carries the *exact* signal Telegram itself reported (RPC code, error
type/id, error message, wait value) plus the operational context the caller
already has (method, operation, chat/session/rule ids, attempt, correlation id).

Rules (spec §3):
  * Never fabricate a Telegram error message.
  * Never hide the original error internally — the event is always built.
  * Never expose secrets / session strings / API keys. ``redact()`` is applied
    to every free-text field that could carry one.
  * Unknown Telegram errors stay visible as ``unknown`` — never silently
    mapped onto a wrong category.
  * User-facing text is an i18n key; developer logs keep full forensic detail.

Pyrogram's error model (verified against pyrogram 2.0.106):
  * every RPC error class subclasses ``RPCError`` and exposes class attributes
    ``CODE`` (bucket, e.g. 400/420/401/500), ``ID`` (the Telegram error type
    string, e.g. ``FLOOD_WAIT_X``) and ``MESSAGE`` (template).
  * ``FloodWait``/``SlowmodeWait``/``FloodPremiumWait`` subclass ``Flood`` and
    take ``value`` = the number of seconds Telegram demands.
  * Bot API (HTTPS) failures surface as ``pyrogram.exceptions.Client.Timeout``
    or raw transport errors; those have no RPC code and are reported as
    ``transport=http`` with ``error_code=<http status>``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger("atf.errors")

# --------------------------------------------------------------------------- #
# Severity → how the UI and the retry policy react
# --------------------------------------------------------------------------- #
SEV_INFO = "info"        # user mistake: fix and retry (wrong code, bad phone)
SEV_WARN = "warn"        # transient / rate limit → retry with backoff
SEV_ERROR = "error"      # operation failed; something must change
SEV_AUTH = "auth"        # session died → needs re-login
SEV_FATAL = "fatal"      # account-level ban / app-level bug → admin

SEVERITIES = (SEV_INFO, SEV_WARN, SEV_ERROR, SEV_AUTH, SEV_FATAL)

#: diagnostic categories — deterministic, one per failure class. These are the
#: filter facets of the in-bot Log Center (spec §28).
CAT_LOGIN = "login"
CAT_FORWARD = "forward"
CAT_AI = "ai"
CAT_AUTH = "auth"
CAT_BOT = "bot"
CAT_DB = "database"
CAT_SYSTEM = "system"

CATEGORIES = (CAT_LOGIN, CAT_FORWARD, CAT_AI, CAT_AUTH, CAT_BOT, CAT_DB, CAT_SYSTEM)

# row shape: (class_name, i18n_key, severity, category, retryable, action_key)
_UNKNOWN = ("__unknown__", "err_unknown", SEV_ERROR, CAT_SYSTEM, False, "")
_INTERNAL = ("__internal__", "err_internal", SEV_FATAL, CAT_SYSTEM, False, "")
_RPC_UNKNOWN = ("__rpc_unknown__", "err_rpc_unknown", SEV_ERROR, CAT_SYSTEM, False, "")

#: MRO base classes that must never count as a classification match — reaching
#: them means the error is genuinely unknown (test subclasses inherit from
#: ``RPCError`` directly, and pyrogram's own ``BadRequest``/``Forbidden`` are
#: meant to be terminal fallbacks, not catch-alls for unlisted subclasses).
_BASE_NAMES = frozenset(
    {"RPCError", "Exception", "BaseException", "object", "BadRequest", "Forbidden"}
)


# --------------------------------------------------------------------------- #
# The normalized model (spec §3)
# --------------------------------------------------------------------------- #
@dataclass
class TelegramErrorEvent:
    """One Telegram failure, normalized once, with full forensic context."""

    event_id: str = ""
    provider: str = "mtproto"            # mtproto | bot_api
    transport: str = "rpc"               # rpc | http
    error_code: str = ""                 # Telegram RPC code, e.g. "400"
    error_type: str = ""                 # Telegram error id, e.g. "PEER_ID_INVALID"
    error_message: str = ""              # Telegram's own message (redacted)
    error_value: str = ""                # the "X" in FLOOD_WAIT_X (seconds)
    method: str = ""                     # requested Telegram method
    operation: str = ""                  # what we were doing (forward, login…)
    chat_id: str = ""
    user_id: str = ""
    session_id: str = ""
    rule_id: str = ""
    retryable: bool = False
    retry_after: int = 0                 # seconds Telegram demanded (0 = unknown)
    migration_target: str = ""           # chat id when Telegram says "migrate"
    dc_id: str = ""
    attempt: int = 0
    correlation_id: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    category: str = CAT_SYSTEM           # deterministic diagnostic category
    severity: str = SEV_ERROR
    message_key: str = "err_unknown"     # i18n key the bot renders
    explanation: str = ""                # operator-facing English line (redacted)
    action_key: str = ""                 # suggested recovery action, i18n key
    outcome: str = "failed"              # failed | retried | recovered | abandoned

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @property
    def diagnostic_id(self) -> str:
        """Short greppable id for the in-bot Log Center (spec §29): ATF-XXXXXX."""
        return "ATF-" + (self.event_id or "000000")[:6].upper()

    @property
    def display_code(self) -> str:
        """``400 PEER_ID_INVALID`` — the Telegram-native pair users see."""
        return f"{self.error_code} {self.error_type}".strip()

    @property
    def is_unknown(self) -> bool:
        """An unrecognized Telegram error stays *visible as unknown*.

        Keyed on the classification decision rather than on the type string
        alone: a named-but-uncurated RPC error keeps its Telegram type while
        still being reported through the unknown channel.
        """
        return self.message_key in ("err_unknown", "err_rpc_unknown")


# --------------------------------------------------------------------------- #
# Secret hygiene — never log tokens / session strings / 2FA / codes
# --------------------------------------------------------------------------- #
_SECRET_FIELD = re.compile(
    r"(?i)(api[_-]?hash|api[_-]?id|bot[_-]?token|master[_-]?key|"
    r"session[_-]?(string)?|app[_-]?hash)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?hash|api[_-]?id|bot[_-]?token|master[_-]?key|"
    r"session[_-]?string|password|phone[_-]?code|2fa|verification[_-]?code)"
    r"\s*[:=]\s*\S+"
)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_PHONE = re.compile(r"\+\d{7,16}\b")

_REDACTED = "[REDACTED]"


def redact(text: Any, *, keep_phone: bool = False) -> str:
    """Strip anything that must never reach a log or a user message.

    Total: never raises. Redaction is a safety net, not a parsing step — a
    failure here must not turn a reported error into a lost one.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    try:
        out = _SECRET_VALUE.sub(r"\1=" + _REDACTED, text)
        out = _SECRET_FIELD.sub(_REDACTED, out)
        out = _LONG_HEX.sub(_REDACTED, out)
        if not keep_phone:
            out = _PHONE.sub("[PHONE]", out)
        return out
    except Exception:  # pragma: no cover — safety net
        return _REDACTED


# --------------------------------------------------------------------------- #
# Extraction from a live exception
# --------------------------------------------------------------------------- #
def _class_attr(exc: BaseException, name: str) -> str:
    """Walk the MRO for a pyrogram class attribute (CODE / ID / MESSAGE)."""
    for klass in type(exc).__mro__:
        val = getattr(klass, name, None)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, int) and name == "CODE":
            return str(val)
    return ""


def wait_value(exc: BaseException) -> int:
    """Seconds Telegram demanded, from ``Flood.value`` / ``SlowmodeWait.value``.

    Returns 0 when the error carries no wait (caller supplies its own default).
    Never guesses: the value comes from Telegram or from nowhere.
    """
    for name in ("value", "seconds", "retry_after"):
        val = getattr(exc, name, None)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return wait_from_message(str(exc or ""))


def wait_from_message(text: str) -> int:
    """Parse the wait out of Telegram's message: ``A wait of 42 seconds…``."""
    match = re.search(r"(\d+)\s*seconds?", text or "", re.IGNORECASE)
    return int(match.group(1)) if match else 0


def is_mtproto(exc: BaseException) -> bool:
    """True for a pyrogram MTProto RPC error (vs a Bot-API/HTTP failure)."""
    try:
        from pyrogram.errors import RPCError

        return isinstance(exc, RPCError)
    except Exception:  # pragma: no cover
        return False


def extract(
    exc: Any,
    *,
    method: str = "",
    operation: str = "",
    chat_id: Any = None,
    user_id: Any = None,
    session_id: str = "",
    rule_id: str = "",
    attempt: int = 0,
    correlation_id: str = "",
) -> TelegramErrorEvent:
    """Build the normalized event from any exception.

    Total: never raises. Losing the diagnostic is always worse than reporting
    an unknown error, so a translator failure degrades to a valid event.
    """
    try:
        return _extract(exc, method=method, operation=operation, chat_id=chat_id,
                        user_id=user_id, session_id=session_id, rule_id=rule_id,
                        attempt=attempt, correlation_id=correlation_id)
    except Exception as fatal:  # pragma: no cover
        logger.error("error translator failed: %s", fatal, exc_info=True)
        event = TelegramErrorEvent(
            event_id=_new_id(), error_type="UNKNOWN",
            error_message=redact(str(exc)),
            method=redact(method), operation=redact(operation, keep_phone=True),
            chat_id=_str(chat_id), user_id=_str(user_id),
            session_id=_str(session_id), rule_id=rule_id,
            attempt=attempt, correlation_id=correlation_id or _new_id(),
        )
        _apply(event, _INTERNAL)
        return event


def _extract(exc, *, method, operation, chat_id, user_id, session_id, rule_id,
             attempt, correlation_id) -> TelegramErrorEvent:
    rpc = is_mtproto(exc)
    name = type(exc).__name__ if isinstance(exc, BaseException) else "NotAnException"

    event = TelegramErrorEvent(
        event_id=_new_id(),
        provider="mtproto" if rpc else "bot_api",
        transport="rpc" if rpc else "http",
        method=redact(method),
        operation=redact(operation, keep_phone=True),
        chat_id=_str(chat_id),
        user_id=_str(user_id),
        session_id=_str(session_id),
        rule_id=rule_id,
        attempt=attempt,
        correlation_id=correlation_id or _new_id(),
    )

    if not isinstance(exc, BaseException):
        event.error_message = "non-exception raised"
        _apply(event, _UNKNOWN)
        return event

    template = _class_attr(exc, "MESSAGE")
    # ``str(exc)`` can itself raise (a hostile/buggy __str__); stay total
    try:
        text = str(exc)
    except Exception:
        text = template or ""
    event.error_message = redact(text or template, keep_phone=True)

    if rpc:
        event.error_code = _class_attr(exc, "CODE")
        # capture the Telegram error type up-front: ``_classify`` runs after the
        # migration check and the classification itself never overwrites it
        event.error_type = _class_attr(exc, "ID") or "UNKNOWN"
        event.retry_after = wait_value(exc)
        if event.retry_after:
            event.error_value = str(event.retry_after)
    else:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        event.error_code = str(status) if isinstance(status, int) else ""
        # non-RPC failure: the class name is the closest thing to an error id
        event.error_type = name
        retry_after = getattr(exc, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            event.retry_after = int(retry_after)
            event.error_value = str(event.retry_after)

    # Telegram "migrate to the supergroup": the new chat id rides on the error.
    # Compare against the resolved Telegram type so a subclass whose class name
    # differs still matches (and non-migration errors never accidentally fire).
    migrated = getattr(exc, "value", None)
    if isinstance(migrated, (int, float)) and event.error_type in (
        "CHAT_MIGRATE_X", "CHAT_ID_INVALID", "CHANNEL_ID_INVALID",
    ):
        event.migration_target = f"-100{int(migrated)}"

    _classify(event, exc)
    return event


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Deterministic classification table
# --------------------------------------------------------------------------- #
# Ordered: exact class-name match wins, then an MRO walk. Keyed by pyrogram
# *class name* (the stable surface across TL layers) rather than by ID string.
_TABLE: tuple = (
    # --- phone / code / 2FA --------------------------------------------------
    ("PhoneNumberInvalid",     "err_phone_invalid",    SEV_INFO,  CAT_LOGIN, False, "act_check_phone"),
    ("PhoneNumberUnoccupied",  "err_phone_unoccupied", SEV_INFO,  CAT_LOGIN, False, "act_check_phone"),
    ("PhoneCodeInvalid",       "err_code_invalid",     SEV_INFO,  CAT_LOGIN, False, "act_recheck_code"),
    ("PhoneCodeExpired",       "err_code_expired",     SEV_INFO,  CAT_LOGIN, False, "act_resend_code"),
    ("PhoneCodeHashEmpty",     "err_code_empty",       SEV_INFO,  CAT_LOGIN, False, "act_resend_code"),
    ("PhoneCodeHashInvalid",   "err_code_empty",       SEV_INFO,  CAT_LOGIN, False, "act_resend_code"),
    ("SessionPasswordNeeded",  "err_2fa_needed",       SEV_INFO,  CAT_LOGIN, False, "act_enter_2fa"),
    ("PasswordHashInvalid",    "err_2fa_wrong",        SEV_INFO,  CAT_LOGIN, False, "act_recheck_2fa"),
    ("PasswordEmpty",          "err_2fa_empty",        SEV_INFO,  CAT_LOGIN, False, "act_enter_2fa"),
    # --- session / auth key --------------------------------------------------
    ("AuthBytesInvalid",       "err_auth_key",         SEV_AUTH,  CAT_AUTH,  False, "act_relogin"),
    ("AuthKeyInvalid",         "err_auth_key",         SEV_AUTH,  CAT_AUTH,  False, "act_relogin"),
    ("AuthKeyUnregistered",    "err_auth_key",         SEV_AUTH,  CAT_AUTH,  False, "act_relogin"),
    ("AuthKeyDuplicated",      "err_auth_key",         SEV_AUTH,  CAT_AUTH,  False, "act_relogin"),
    ("AuthKeyExpired",         "err_auth_key",         SEV_AUTH,  CAT_AUTH,  False, "act_relogin"),
    ("SessionRevoked",         "err_session_revoked",  SEV_AUTH,  CAT_AUTH,  False, "act_relogin"),
    ("UserDeactivated",        "err_account_banned",   SEV_FATAL, CAT_AUTH,  False, "act_contact_support"),
    ("UserDeactivatedBan",     "err_account_banned",   SEV_FATAL, CAT_AUTH,  False, "act_contact_support"),
    ("PhoneNumberBanned",      "err_account_banned",   SEV_FATAL, CAT_AUTH,  False, "act_contact_support"),
    ("ApiIdInvalid",           "err_api_id_invalid",   SEV_ERROR, CAT_AUTH,  False, "act_check_api_id"),
    ("ApiIdPublishedFlood",    "err_api_id_flood",     SEV_ERROR, CAT_AUTH,  False, "act_check_api_id"),
    # --- rate limiting (retryable, wait comes from Telegram) ------------------
    ("FloodWait",              "err_flood_wait",       SEV_WARN,  CAT_FORWARD, True, "act_wait"),
    ("FloodPremiumWait",       "err_flood_wait",       SEV_WARN,  CAT_FORWARD, True, "act_wait"),
    ("SlowmodeWait",           "err_slowmode",         SEV_WARN,  CAT_FORWARD, True, "act_wait"),
    ("FloodTestWait",          "err_flood_wait",       SEV_WARN,  CAT_FORWARD, True, "act_wait"),
    # --- permission / target -------------------------------------------------
    ("ChatAdminRequired",      "err_admin_required",   SEV_ERROR, CAT_FORWARD, False, "act_check_admin"),
    ("ChatWriteForbidden",     "err_write_forbidden",  SEV_ERROR, CAT_FORWARD, False, "act_check_target"),
    ("ChatForbidden",          "err_write_forbidden",  SEV_ERROR, CAT_FORWARD, False, "act_check_target"),
    ("SendMessageDenied",      "err_write_forbidden",  SEV_ERROR, CAT_FORWARD, False, "act_check_target"),
    ("ChannelPrivate",         "err_channel_private",  SEV_ERROR, CAT_FORWARD, False, "act_refresh_chats"),
    ("ChatForwardsRestricted", "err_forwards_restricted", SEV_ERROR, CAT_FORWARD, False, "act_use_copy_mode"),
    ("UserPrivacyRestricted",  "err_privacy_restricted", SEV_ERROR, CAT_FORWARD, False, "act_check_target"),
    ("ChatIdInvalid",          "err_peer_invalid",     SEV_ERROR, CAT_FORWARD, False, "act_refresh_chats"),
    ("PeerIdInvalid",          "err_peer_invalid",     SEV_ERROR, CAT_FORWARD, False, "act_refresh_chats"),
    ("UserNotParticipant",     "err_not_participant",  SEV_INFO,  CAT_FORWARD, False, "act_check_target"),
    ("UsernameNotOccupied",    "err_username_invalid", SEV_ERROR, CAT_FORWARD, False, "act_refresh_chats"),
    ("UsernameInvalid",        "err_username_invalid", SEV_ERROR, CAT_FORWARD, False, "act_refresh_chats"),
    ("UsernameOccupied",       "err_username_invalid", SEV_ERROR, CAT_FORWARD, False, "act_refresh_chats"),
    ("ChatNotModified",        "err_not_modified",     SEV_INFO,  CAT_FORWARD, True,  ""),
    ("ChatMigrate",            "err_chat_migrated",    SEV_INFO,  CAT_FORWARD, True,  "act_migrate_rule"),
    # --- message content -----------------------------------------------------
    ("MediaEmpty",             "err_media_empty",      SEV_INFO,  CAT_FORWARD, False, "act_check_content"),
    ("MediaInvalid",           "err_media_empty",      SEV_INFO,  CAT_FORWARD, False, "act_check_content"),
    ("WebpageMediaEmpty",      "err_webpage_empty",    SEV_INFO,  CAT_FORWARD, False, "act_check_content"),
    ("MediaCaptionTooLong",    "err_caption_too_long", SEV_ERROR, CAT_FORWARD, True,  "act_shorten"),
    ("EntitiesTooLong",        "err_caption_too_long", SEV_ERROR, CAT_FORWARD, True,  "act_shorten"),
    ("MessageIdInvalid",       "err_message_id_invalid", SEV_INFO, CAT_FORWARD, False, "act_check_source"),
    ("MsgIdInvalid",           "err_message_id_invalid", SEV_INFO, CAT_FORWARD, False, "act_check_source"),
    ("MessageNotModified",     "err_not_modified",     SEV_INFO,  CAT_FORWARD, True,  ""),
    ("MessageCfidsNotModified", "err_not_modified",    SEV_INFO,  CAT_FORWARD, True,  ""),
    # --- transport / infrastructure -----------------------------------------
    ("Timeout",                "err_timeout",          SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    ("TimeoutError",           "err_timeout",          SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    ("NetworkError",           "err_connection",       SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    ("ConnectionError",        "err_connection",       SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    ("OSError",                "err_connection",       SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    ("RPC_CALL_FAIL",          "err_rpc_fail",         SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    ("RPC_MCERROR_FAIL",       "err_rpc_fail",         SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    ("InterdcFail",            "err_interdc_fail",     SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    ("InterdcCallFail",        "err_interdc_fail",     SEV_WARN,  CAT_SYSTEM, True,  "act_retry"),
    # --- Bot API specifics ---------------------------------------------------
    ("Conflict",               "err_conflict",         SEV_WARN,  CAT_BOT,   True,  "act_retry"),
    ("Forbidden",              "err_forbidden",        SEV_AUTH,  CAT_BOT,   False, "act_check_bot_token"),
    ("BadRequest",             "err_bad_request",      SEV_ERROR, CAT_BOT,   False, "act_check_request"),
)


def _classify(event: TelegramErrorEvent, exc: BaseException) -> None:
    """Apply the deterministic mapping. Unknown errors stay *unknown*."""
    name = type(exc).__name__
    for row in _TABLE:
        if row[0] == name:
            _apply(event, row)
            return
    # MRO walk: a subclass still classifies as its parent (Flood → FloodWait).
    # The base classes themselves (RPCError / Exception / BaseException) are not
    # entries — reaching them means the error is unknown, not "classified as
    # RPCError", so the *specific* unknown branch below stays reachable.
    base_names = _base_names_of(exc)
    for row in _TABLE:
        if row[0] in base_names:
            _apply(event, row)
            return
    if is_mtproto(exc):
        _apply(event, _RPC_UNKNOWN)      # visible unknown, never a wrong category
        return
    _apply(event, _UNKNOWN)


def _base_names_of(exc: BaseException) -> set:
    """MRO class names excluding the terminal bases (see ``_BASE_NAMES``)."""
    return {b.__name__ for b in type(exc).__mro__[1:]} - _BASE_NAMES


def _apply(event: TelegramErrorEvent, row: tuple) -> None:
    """row = (class_name, i18n_key, severity, category, retryable, action_key)."""
    event.message_key = row[1]
    event.severity = row[2]
    event.category = row[3]
    event.retryable = bool(row[4])
    event.action_key = row[5] if len(row) > 5 else ""
    event.explanation = _explain(event)


def _explain(event: TelegramErrorEvent) -> str:
    """One-line operator explanation (English, redacted)."""
    parts = [event.display_code or event.error_type or "UNKNOWN"]
    if event.error_message and event.error_message != event.error_type:
        parts.append(redact(event.error_message, keep_phone=True))
    if event.retry_after:
        parts.append(f"wait={event.retry_after}s")
    if event.chat_id:
        parts.append(f"chat={event.chat_id}")
    if event.rule_id:
        parts.append(f"rule={str(event.rule_id)[:8]}")
    if event.attempt:
        parts.append(f"attempt={event.attempt}")
    if event.method:
        parts.append(f"method={event.method}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Convenience: classify without an exception (e.g. for stored RPC strings)
# --------------------------------------------------------------------------- #
def from_rpc_string(
    code: str,
    error_type: str,
    message: str = "",
    *,
    operation: str = "",
    chat_id: Any = None,
    rule_id: str = "",
    attempt: int = 0,
    correlation_id: str = "",
) -> TelegramErrorEvent:
    """Rebuild an event from a stored ``code``/``type`` pair.

    Used when replaying a persisted failure (Log Center details screen) without
    the original exception object. Keeps the same deterministic classification.
    """
    event = TelegramErrorEvent(
        event_id=_new_id(), provider="mtproto", transport="rpc",
        error_code=str(code or ""), error_type=str(error_type or "UNKNOWN"),
        error_message=redact(message, keep_phone=True),
        operation=redact(operation, keep_phone=True),
        chat_id=_str(chat_id), rule_id=rule_id, attempt=attempt,
        correlation_id=correlation_id or _new_id(),
    )
    _classify_by_type(event)
    return event


def _classify_by_type(event: TelegramErrorEvent) -> None:
    """Classify an event built from strings, by matching pyrogram class attrs."""
    try:
        import pyrogram.errors as _pe
    except Exception:  # pragma: no cover
        _apply(event, _RPC_UNKNOWN)
        return
    target = event.error_type.upper()
    for _, obj in vars(_pe).items():
        if not isinstance(obj, type) or not issubclass(obj, _pe.RPCError):
            continue
        if str(getattr(obj, "ID", "")).upper() == target:
            event.error_code = event.error_code or str(getattr(obj, "CODE", ""))
            seconds = wait_from_message(event.error_message)
            event.retry_after = seconds
            event.error_value = str(seconds) if seconds else ""
            name = obj.__name__
            for row in _TABLE:
                if row[0] == name:
                    _apply(event, row)
                    return
            # found the real class but have no curated entry → unknown RPC
            _apply(event, _RPC_UNKNOWN)
            return
    _apply(event, _RPC_UNKNOWN)


# --------------------------------------------------------------------------- #
# Backwards-compatible facade over the normalized model
# --------------------------------------------------------------------------- #
# The internal consumers (dispatcher, pro_bot_ui) were written against the
# original triple-return API. This facade keeps them working while they migrate
# to ``TelegramErrorEvent``, and every call here is a thin view over the same
# deterministic classification — there is no second mapping table.


def tg_error(exc: BaseException, operation: str = "") -> tuple:
    """``(i18n_key, severity, retryable)`` for a raised exception."""
    event = extract(exc, operation=operation)
    return event.message_key, event.severity, event.retryable


def tg_detail(exc: BaseException, operation: str = "") -> str:
    """Forensic one-liner for logs and the operator UI: ``400 PEER_ID_INVALID …``."""
    return extract(exc, operation=operation).explanation


def tg_event(exc: BaseException, **context) -> TelegramErrorEvent:
    """Full normalized event — preferred entry point for new callers."""
    return extract(exc, **context)


def wait_seconds(exc: BaseException, default: int = 0) -> int:
    """Telegram's demanded wait, or ``default`` when the error carries none.

    Never guesses: the value comes from the ``Flood`` family's ``value`` or
    from nowhere.
    """
    return wait_value(exc) or int(default or 0)
