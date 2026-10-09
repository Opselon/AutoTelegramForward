"""TASK 02 - Telegram error forensics: normalization + diagnostics.

Covers every error class the spec (section 3) lists, plus the contract rules:
  * the exact Telegram signal is always preserved internally
  * unknown errors stay visible as unknown, never a wrong category
  * secrets are redacted, never logged
  * classification is deterministic
  * FloodWait's real wait value is used, never guessed

Instantiation follows pyrogram 2.0.106's verified signature
RPCError(value, rpc_name, is_unknown, is_signed): value is the wait in
seconds for the Flood family, and the class supplies CODE/ID/MESSAGE itself.
"""

import asyncio
import json
import pytest

from pyrogram.errors import (
    ApiIdInvalid,
    AuthKeyInvalid,
    AuthKeyUnregistered,
    BadRequest,
    ChannelPrivate,
    ChatAdminRequired,
    ChatForbidden,
    ChatForwardsRestricted,
    ChatIdInvalid,
    ChatWriteForbidden,
    FloodWait,
    Forbidden,
    MediaCaptionTooLong,
    MediaEmpty,
    MessageIdInvalid,
    MessageNotModified,
    MsgIdInvalid,
    PasswordHashInvalid,
    PeerIdInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    PhoneNumberUnoccupied,
    RPCError,
    SessionPasswordNeeded,
    SessionRevoked,
    SlowmodeWait,
    Timeout,
    Unauthorized,
    UserDeactivated,
    UserNotParticipant,
    UserPrivacyRestricted,
)

from core.infrastructure.telegram import errors as E
from core.infrastructure.telegram.errors import (
    CAT_AUTH,
    CAT_BOT,
    CAT_FORWARD,
    CAT_LOGIN,
    SEV_AUTH,
    SEV_ERROR,
    SEV_FATAL,
    SEV_INFO,
    SEV_WARN,
    TelegramErrorEvent,
    extract,
    from_rpc_string,
    redact,
    wait_value,
)


def _ev(exc, **kw):
    """Extract with sensible defaults for every test."""
    return extract(exc, method="messages.sendMessage", operation="forward", **kw)


def _check_roundtrip(exc, want_type, want_code, **ctx):
    """One error, one event, all forensic fields asserted."""
    ev = _ev(exc, **ctx)
    assert isinstance(ev, TelegramErrorEvent)
    assert ev.error_type == want_type, "type mismatch for " + type(exc).__name__
    if want_code is not None:
        assert ev.error_code == want_code, "code mismatch for " + want_type
    assert ev.error_message, "message empty for " + want_type
    assert ev.method == "messages.sendMessage"
    assert ev.operation == "forward"
    assert ev.correlation_id, "correlation id missing"
    assert ev.event_id, "event id missing"
    assert ev.created_at > 0
    return ev


class _Novel(RPCError):
    """An RPC error Telegram defines but our table does not curate."""

    CODE = 520
    ID = "UNMAPPED_THING"
    MESSAGE = "not in the table"


def _chat_migrate(new_id):
    """Telegram's migration signal: new chat id rides on the error value."""

    class _ChatMigrate(RPCError):
        CODE = 400
        ID = "CHAT_MIGRATE_X"
        MESSAGE = "chat migrated"

    return _ChatMigrate(value=new_id)


class TestTelegramNativeSignalPreserved:
    def test_floodwait(self):
        ev = _check_roundtrip(FloodWait(value=90), "FLOOD_WAIT_X", "420")
        assert ev.retry_after == 90, "FloodWait value must come from Telegram"
        assert ev.error_value == "90"
        assert ev.retryable is True
        assert ev.severity == SEV_WARN
        assert ev.category == CAT_FORWARD

    def test_slowmode_wait(self):
        ev = _check_roundtrip(SlowmodeWait(value=30), "SLOWMODE_WAIT_X", "420")
        assert ev.retry_after == 30
        assert ev.retryable is True

    def test_unauthorized(self):
        # Unauthorized has no class-level ID; the extractor must still report it
        ev = _ev(Unauthorized())
        assert ev.error_code == "401"
        assert ev.retryable is False

    def test_session_revoked(self):
        ev = _check_roundtrip(SessionRevoked(), "SESSION_REVOKED", "401")
        assert ev.severity == SEV_AUTH
        assert ev.category == CAT_AUTH
        assert ev.action_key == "act_relogin"

    def test_session_password_needed(self):
        ev = _check_roundtrip(SessionPasswordNeeded(), "SESSION_PASSWORD_NEEDED", "401")
        assert ev.severity == SEV_INFO
        assert ev.category == CAT_LOGIN

    def test_phone_code_invalid(self):
        ev = _check_roundtrip(PhoneCodeInvalid(), "PHONE_CODE_INVALID", "400")
        assert ev.severity == SEV_INFO
        assert ev.category == CAT_LOGIN
        assert ev.retryable is False

    def test_phone_number_invalid(self):
        ev = _check_roundtrip(PhoneNumberInvalid(), "PHONE_NUMBER_INVALID", "406")
        assert ev.severity == SEV_INFO
        assert ev.category == CAT_LOGIN

    def test_phone_code_expired(self):
        ev = _check_roundtrip(PhoneCodeExpired(), "PHONE_CODE_EXPIRED", "400")
        assert ev.category == CAT_LOGIN
        assert ev.action_key == "act_resend_code"

    def test_phone_number_unoccupied(self):
        _check_roundtrip(PhoneNumberUnoccupied(), "PHONE_NUMBER_UNOCCUPIED", "400")

    def test_chat_id_invalid(self):
        ev = _check_roundtrip(ChatIdInvalid(), "CHAT_ID_INVALID", "400")
        assert ev.category == CAT_FORWARD
        assert ev.retryable is False

    def test_peer_id_invalid(self):
        ev = _check_roundtrip(PeerIdInvalid(), "PEER_ID_INVALID", "400")
        assert ev.category == CAT_FORWARD
        assert ev.action_key == "act_refresh_chats"

    def test_user_not_participant(self):
        ev = _check_roundtrip(UserNotParticipant(), "USER_NOT_PARTICIPANT", "400")
        assert ev.severity == SEV_INFO

    def test_chat_write_forbidden(self):
        ev = _check_roundtrip(ChatWriteForbidden(), "CHAT_WRITE_FORBIDDEN", "403")
        assert ev.severity == SEV_ERROR
        assert ev.retryable is False

    def test_channel_private(self):
        ev = _check_roundtrip(ChannelPrivate(), "CHANNEL_PRIVATE", "406")
        assert ev.severity == SEV_ERROR
        assert ev.retryable is False

    def test_message_not_modified(self):
        ev = _check_roundtrip(MessageNotModified(), "MESSAGE_NOT_MODIFIED", "400")
        assert ev.severity == SEV_INFO
        assert ev.retryable is True

    def test_message_id_invalid(self):
        ev = _check_roundtrip(MessageIdInvalid(), "MESSAGE_ID_INVALID", "400")
        assert ev.severity == SEV_INFO

    def test_msg_id_invalid(self):
        # distinct class from MessageIdInvalid; both must classify
        ev = _check_roundtrip(MsgIdInvalid(), "MSG_ID_INVALID", "400")
        assert ev.category == CAT_FORWARD

    def test_media_caption_too_long(self):
        ev = _check_roundtrip(MediaCaptionTooLong(), "MEDIA_CAPTION_TOO_LONG", "400")
        assert ev.retryable is True
        assert ev.action_key == "act_shorten"

    def test_media_empty(self):
        ev = _check_roundtrip(MediaEmpty(), "MEDIA_EMPTY", "400")
        assert ev.severity == SEV_INFO

    def test_api_id_invalid(self):
        ev = _check_roundtrip(ApiIdInvalid(), "API_ID_INVALID", "400")
        assert ev.category == CAT_AUTH

    def test_user_deactivated_is_fatal(self):
        ev = _check_roundtrip(UserDeactivated(), "USER_DEACTIVATED", "401")
        assert ev.severity == SEV_FATAL
        assert ev.retryable is False

    def test_password_hash_invalid(self):
        ev = _check_roundtrip(PasswordHashInvalid(), "PASSWORD_HASH_INVALID", "400")
        assert ev.category == CAT_LOGIN
        assert ev.action_key == "act_recheck_2fa"

    def test_chat_admin_required(self):
        ev = _check_roundtrip(ChatAdminRequired(), "CHAT_ADMIN_REQUIRED", "400")
        assert ev.action_key == "act_check_admin"

    def test_chat_forbidden(self):
        ev = _check_roundtrip(ChatForbidden(), "CHAT_FORBIDDEN", "403")
        assert ev.action_key == "act_check_target"

    def test_chat_forwards_restricted(self):
        ev = _check_roundtrip(ChatForwardsRestricted(), "CHAT_FORWARDS_RESTRICTED", "400")
        assert ev.action_key == "act_use_copy_mode"

    def test_user_privacy_restricted(self):
        ev = _check_roundtrip(UserPrivacyRestricted(), "USER_PRIVACY_RESTRICTED", "403")
        assert ev.retryable is False

    def test_auth_key_invalid(self):
        ev = _check_roundtrip(AuthKeyInvalid(), "AUTH_KEY_INVALID", "401")
        assert ev.severity == SEV_AUTH
        assert ev.action_key == "act_relogin"

    def test_auth_key_unregistered(self):
        ev = _check_roundtrip(AuthKeyUnregistered(), "AUTH_KEY_UNREGISTERED", "401")
        assert ev.severity == SEV_AUTH

    def test_rpc_error_base_still_produces_event(self):
        # RPCError() has no class-level ID -> must degrade to a valid event
        ev = _ev(RPCError())
        assert ev.event_id
        assert ev.provider == "mtproto"


class TestTransportErrors:
    def test_pyrogram_timeout_is_retryable(self):
        # pyrogram's Timeout subclasses RPCError -> it is an MTProto-layer error
        ev = _ev(Timeout())
        assert ev.retryable is True
        assert ev.severity == SEV_WARN
        assert ev.provider == "mtproto"
        assert ev.transport == "rpc"

    def test_generic_network_error(self):
        ev = _ev(OSError("connection reset"))
        assert ev.retryable is True
        assert ev.error_type == "OSError"

    def test_asyncio_timeout(self):
        ev = _ev(asyncio.TimeoutError())
        assert ev.retryable is True
        assert ev.message_key == "err_timeout"

    def test_forbidden(self):
        ev = _ev(Forbidden())
        assert ev.severity == SEV_AUTH
        assert ev.retryable is False

    def test_bad_request(self):
        ev = _ev(BadRequest())
        assert ev.retryable is False
        assert ev.category == CAT_BOT

    def test_bot_api_retry_after_from_exception(self):
        class WithRetryAfter(Exception):
            retry_after = 25

        ev = _ev(WithRetryAfter())
        assert ev.retry_after == 25
        assert ev.error_value == "25"


class TestUnknownErrorsStayUnknown:
    def test_unmapped_rpc_class_stays_visible(self):
        ev = _check_roundtrip(_Novel(), "UNMAPPED_THING", "520")
        # visible unknown RPC, not silently miscategorized
        assert ev.message_key == "err_rpc_unknown"
        assert ev.error_type == "UNMAPPED_THING"
        assert ev.error_code == "520"

    def test_arbitrary_python_error(self):
        # not an RPC error at all -> generic unknown
        ev = extract(ValueError("totally not telegram"))
        assert ev.message_key == "err_unknown"
        assert ev.is_unknown is True

    def test_rpc_error_with_no_class_id_stays_visible(self):
        # RPCError base has no class-level ID -> visible unknown RPC
        ev = extract(RPCError())
        assert ev.provider == "mtproto"
        assert ev.message_key == "err_rpc_unknown"
        assert ev.event_id
        assert ev.is_unknown is True

    def test_non_exception_input(self):
        ev = extract("just a string")
        assert ev.event_id
        assert ev.message_key == "err_unknown"


class TestDeterministicCategories:
    @pytest.mark.parametrize("exc,sev,cat", [
        (PhoneCodeInvalid(), SEV_INFO, CAT_LOGIN),
        (FloodWait(value=5), SEV_WARN, CAT_FORWARD),
        (PeerIdInvalid(), SEV_ERROR, CAT_FORWARD),
        (SessionRevoked(), SEV_AUTH, CAT_AUTH),
        (UserDeactivated(), SEV_FATAL, CAT_AUTH),
    ])
    def test_severity_and_category(self, exc, sev, cat):
        ev = _ev(exc)
        assert ev.severity == sev
        assert ev.category == cat

    def test_same_input_always_same_category(self):
        a = _ev(FloodWait(value=11))
        b = _ev(FloodWait(value=11))
        assert a.category == b.category
        assert a.message_key == b.message_key
        assert a.severity == b.severity

    def test_severity_domain_is_fixed(self):
        ev = _ev(FloodWait(value=1))
        assert ev.severity in E.SEVERITIES
        assert ev.category in E.CATEGORIES

    def test_retryable_is_not_assumed(self):
        """Spec: do not assume every Telegram error is retryable."""
        assert _ev(PhoneCodeInvalid()).retryable is False
        assert _ev(FloodWait(value=1)).retryable is True
        assert _ev(PeerIdInvalid()).retryable is False


class TestSecretRedaction:
    @pytest.mark.parametrize("leak", [
        "api_hash=abcdef0123456789abcdef0123456789",
        "api_id : 23302388 api_hash=deadbeefdeadbeefdeadbeefdeadbeef",
        "bot_token=123456:ABC-DEF_ghijklmnop",
        "session_string=AQG2xyz...",
        "password=[REDACTED]",
        "2fa_secret=[REDACTED]",
    ])
    def test_secrets_stripped(self, leak):
        out = redact(leak)
        assert "abcdef" not in out
        assert "deadbeef" not in out
        assert "ABC-DEF" not in out
        assert "MySecret" not in out
        assert "AQG2" not in out

    def test_long_hex_blobs_removed(self):
        out = redact("blob " + "a" * 40)
        assert "a" * 40 not in out

    def test_phone_redacted_by_default(self):
        assert redact("phone +989016807808 here") == "phone [PHONE] here"

    def test_phone_kept_when_context_allows(self):
        assert "+989016807808" in redact("phone +989016807808", keep_phone=True)

    def test_error_message_with_secret_is_redacted(self):
        class Leaky(RPCError):
            CODE = 400
            ID = "LEAK_X"
            MESSAGE = "bad api_hash=[REDACTED]"

        ev = extract(Leaky())
        assert "deadbeef" not in ev.error_message
        assert ev.error_message

    def test_redact_is_total(self):
        assert redact(None) == ""
        assert redact(12345) == "12345"
        assert redact(object())  # never raises

    def test_event_json_has_no_secrets(self):
        ev = _ev(FloodWait(value=5), chat_id=-100123)
        payload = ev.to_json()
        assert "api_hash" not in payload
        assert "session_string" not in payload


class TestDiagnostics:
    def test_diagnostic_id_format(self):
        ev = _ev(FloodWait(value=5))
        assert ev.diagnostic_id.startswith("ATF-")
        assert len(ev.diagnostic_id) == len("ATF-XXXXXX")

    def test_display_code(self):
        ev = _ev(PeerIdInvalid())
        assert ev.display_code == "400 PEER_ID_INVALID"

    def test_explanation_carries_forensics(self):
        ev = _ev(FloodWait(value=90), rule_id="rule-uuid-1234", chat_id=-100999)
        assert "FLOOD_WAIT_X" in ev.explanation
        assert "wait=90s" in ev.explanation
        assert "chat=-100999" in ev.explanation
        assert "attempt=" not in ev.explanation

    def test_explanation_includes_attempt(self):
        ev = _ev(FloodWait(value=90), attempt=2)
        assert "attempt=2" in ev.explanation

    def test_json_roundtrip(self):
        ev = _ev(FloodWait(value=7), chat_id=1, user_id=2, session_id="s", rule_id="r")
        restored = TelegramErrorEvent(**json.loads(ev.to_json()))
        assert restored.error_type == ev.error_type
        assert restored.retry_after == ev.retry_after
        assert restored.chat_id == "1"

    def test_context_fields_preserved(self):
        ev = extract(FloodWait(value=3), method="messages.sendCode",
                     operation="login", chat_id=None, user_id=5094837833,
                     session_id="sess-1", rule_id="rule-9", attempt=3,
                     correlation_id="corr-42")
        assert ev.user_id == "5094837833"
        assert ev.session_id == "sess-1"
        assert ev.rule_id == "rule-9"
        assert ev.attempt == 3
        assert ev.correlation_id == "corr-42"
        assert ev.operation == "login"

    def test_migration_target(self):
        ev = extract(_chat_migrate(123456789))
        assert ev.migration_target == "-100123456789"

    def test_migration_from_chat_id_invalid(self):
        # ChatIdInvalid carries the new supergroup id on its value attribute
        class _ChatIdInvalid(RPCError):
            CODE = 400
            ID = "CHAT_ID_INVALID"
            MESSAGE = "channel migrated"

        ev = extract(_ChatIdInvalid(value=4242))
        assert ev.migration_target == "-1004242"


class TestReplayFromStoredString:
    def test_replay_matches_live_extraction(self):
        live = _ev(FloodWait(value=90))
        replayed = from_rpc_string(live.error_code, live.error_type,
                                   live.error_message)
        assert replayed.error_type == live.error_type
        assert replayed.error_code == live.error_code
        assert replayed.message_key == live.message_key
        assert replayed.category == live.category
        assert replayed.severity == live.severity

    def test_replay_unknown_type_stays_unknown(self):
        ev = from_rpc_string("500", "TOTALLY_NEW_X", "no such thing")
        assert ev.message_key == "err_rpc_unknown"
        assert ev.error_type == "TOTALLY_NEW_X"

    def test_replay_parses_wait_from_message(self):
        ev = from_rpc_string("420", "FLOOD_WAIT_X",
                             "A wait of 45 seconds is required")
        assert ev.retry_after == 45


class TestWaitValue:
    def test_floodwait_value_attribute(self):
        assert wait_value(FloodWait(value=77)) == 77

    def test_zero_when_absent(self):
        assert wait_value(PhoneCodeInvalid()) == 0

    def test_parsed_from_message_when_no_attr(self):
        class NoAttr(RPCError):
            CODE = 420
            ID = "FLOOD_WAIT_X"
            MESSAGE = "A wait of 100 seconds is required"

        assert wait_value(NoAttr()) == 100

    def test_never_negative(self):
        assert wait_value(FloodWait(value=0)) == 0


class TestTranslatorRobustness:
    def test_extract_never_raises_on_weird_exception(self):
        class Weird(BaseException):
            def __str__(self):
                raise RuntimeError("boom")

        ev = extract(Weird())
        assert ev.event_id

    def test_extract_with_all_context_missing(self):
        ev = extract(FloodWait(value=1))
        assert ev.event_id
        assert ev.correlation_id

    def test_is_mtproto_detection(self):
        assert E.is_mtproto(FloodWait(value=1)) is True
        assert E.is_mtproto(ValueError()) is False
