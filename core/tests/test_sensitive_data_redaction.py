"""Regression tests for the sensitive-data handling the security scanner flags.

One file, three defects that shared a root cause — personal data treated as
log/file payload instead of as data to be masked or kept off the host:

1. a session identity (phone number, @username) written verbatim into a log
   line and into the boot status string,
2. a session backup bundle written to a file in clear text before upload,
3. a port probe that bound the wildcard address to answer a yes/no question.

Each test drives the real code with fakes and asserts on what actually left
the process (log records, the filesystem, the Telegram document object).
"""

from __future__ import annotations

import base64
import json
import logging
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.infrastructure.telegram.bot_manager import BotManager
from core.infrastructure.telegram.i18n import I18n
from core.infrastructure.telegram.identity_redaction import (
    mask_label,
    mask_phone,
    mask_username,
)
from core.infrastructure.telegram.login_flow import LoginFlowManager, LoginState
from core.infrastructure.telegram.session_boot import SessionBootManager

PHONE = "+989121234567"
USERNAME = "realaccount"


# --------------------------------------------------------------------------- #
# 1. identity masking
# --------------------------------------------------------------------------- #
def test_phone_and_username_masks_keep_shape_but_not_identity():
    masked = mask_phone(PHONE)
    assert PHONE not in masked
    assert masked.startswith("+9891") and masked.endswith("4567")
    # short numbers still lose their middle
    assert mask_phone("1234567") == "1234****4567"
    # anything that is not a phone number is not echoed back
    assert mask_phone("not-a-phone") == "[REDACTED]"
    assert mask_phone("") == "[REDACTED]"
    assert mask_phone(None) == "[REDACTED]"

    assert mask_username(USERNAME) == "re" + "*" * 9
    assert USERNAME not in mask_username(USERNAME)
    assert mask_username("@x") == "x*"
    assert mask_username("") == "[REDACTED]"

    # label resolution masks whatever identity is actually present, including
    # the phone-number fallback
    assert mask_label(None, " ", PHONE) == mask_phone(PHONE)
    assert mask_label(USERNAME, PHONE) == mask_username(USERNAME)
    assert mask_label() == "[REDACTED]"


# --------------------------------------------------------------------------- #
# 2. boot + login log lines
# --------------------------------------------------------------------------- #
def _boot_manager(me):
    session = SimpleNamespace(
        id="sess-abcdef12",
        is_active=True,
        is_authorized=True,
        session_string_encrypted="enc-session-string",
        phone_number=PHONE,
        api_credential_id="default",
    )
    sessions = MagicMock()
    sessions.get = AsyncMock(return_value=session)
    pool = MagicMock()
    pool.start_with_session_string = AsyncMock(
        return_value=SimpleNamespace(get_me=AsyncMock(return_value=me))
    )
    dispatcher = MagicMock()
    dispatcher.bind_session = AsyncMock(return_value=False)  # skip channel catch-up
    return SessionBootManager(sessions, MagicMock(), pool, dispatcher, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "me, secret",
    [
        (SimpleNamespace(username=USERNAME, first_name="Real"), USERNAME),
        (SimpleNamespace(username=None, first_name=None), PHONE),  # phone fallback
    ],
)
async def test_session_boot_logs_a_masked_identity_only(caplog, me, secret):
    manager = _boot_manager(me)
    with caplog.at_level(logging.INFO, logger="atf.boot"):
        status = await manager.restore_session("sess-abcdef12")

    assert status == "live_no_bind"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logged, logged
    assert PHONE not in logged, logged


@pytest.mark.asyncio
async def test_login_finalize_logs_the_session_id_not_the_phone(caplog):
    client = SimpleNamespace(
        export_session_string=AsyncMock(return_value="session-string"),
        get_me=AsyncMock(return_value=SimpleNamespace(id=42, username=USERNAME, first_name="R")),
        disconnect=AsyncMock(),
    )
    state = LoginState(phone_number=PHONE, client=client)
    session = SimpleNamespace(id="sid-12345678", api_credential_id="default")
    session.activate = lambda: None
    sessions = MagicMock()
    sessions.create = AsyncMock(return_value=session)
    sessions._repo = MagicMock(update=AsyncMock())
    pool = MagicMock()
    pool.start_with_session_string = AsyncMock()

    flow = LoginFlowManager(pool, sessions)
    with caplog.at_level(logging.INFO):
        result = await flow._finalize(777, state)

    assert result == "login_success:sid-12345678"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert PHONE not in logged, logged
    assert "sid-1234" in logged


# --------------------------------------------------------------------------- #
# 3. the bot's /backup command must not touch the filesystem
# --------------------------------------------------------------------------- #
class _HandlerRecorder:
    """Captures the handlers BotManager registers, keyed by function name."""

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on_message(self, *_args, **_kwargs):
        def decorate(func):
            self.handlers[func.__name__] = func
            return func

        return decorate


def _manager_for_backup(session):
    manager = BotManager.__new__(BotManager)  # no Client(), no network
    manager.bot = _HandlerRecorder()
    manager._admin_ids = {777}
    manager.i18n = I18n()
    manager.i18n.set_language("en")
    manager._sessions = MagicMock()
    manager._sessions.list_by_owner = AsyncMock(return_value=[session])
    manager.ui_step_checker = None
    manager._ui_state_repo = None
    manager._register_handlers()
    return manager


@pytest.mark.asyncio
async def test_bot_backup_streams_its_bundle_and_writes_nothing(tmp_path, monkeypatch):
    session = SimpleNamespace(
        id="sess-abcdef12",
        phone_number=PHONE,
        session_string_encrypted="enc-session-string",
        user_id="42",
        username=USERNAME,
        first_name="Real",
    )
    manager = _manager_for_backup(session)
    message = SimpleNamespace(
        text="/backup",
        chat=SimpleNamespace(id=777),
        from_user=SimpleNamespace(id=777),
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
    )

    monkeypatch.chdir(tmp_path)
    await manager.bot.handlers["_backup"](None, message)

    message.reply_text.assert_not_awaited()
    document = message.reply_document.await_args.kwargs["document"]
    # a real stream, not a path: nothing of this bundle ever hit the disk
    assert not isinstance(document, str)
    assert document.name.endswith(".atf")
    payload = json.loads(base64.urlsafe_b64decode(document.read()).decode("utf-8"))
    assert payload["phone"] == PHONE
    assert payload["session_string"] == "enc-session-string"
    assert sorted(tmp_path.iterdir()) == [], "a backup bundle was left on disk"


# --------------------------------------------------------------------------- #
# 4. the port probe must not open a listener on every interface
# --------------------------------------------------------------------------- #
def test_valid_port_probes_without_binding_a_wildcard_address():
    from atfcli import ui

    assert ui.valid_port("0") is not None
    assert ui.valid_port("70000") is not None
    assert ui.valid_port("not-a-port") is not None

    # a port with a listener behind it is rejected. The listener is bound to
    # loopback on purpose: a regression test for "do not publish a wildcard
    # listener" has no business publishing one itself, and the kernel's table
    # reports a loopback listener exactly like any other.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    busy = listener.getsockname()[1]
    try:
        assert ui.valid_port(str(busy)) is not None
        assert 0 < busy < 65536
    finally:
        listener.close()

    # a free port is accepted
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free = probe.getsockname()[1]
    probe.close()
    assert ui.valid_port(str(free)) is None

    with open(ui.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert '"0.0.0.0"' not in source


def test_python_redactor_covers_the_credential_shapes_the_engine_masks() -> None:
    """The Python safety net must mask what the engine masks on ingest.

    Field report: `redact("token=abc123def")` and `redact("api_key=sk-live")`
    came back untouched, so a tracer line written to the container log carried
    the credential in clear text while the Rust ingest path masked the same
    shape.
    """
    from core.infrastructure.telegram.errors import redact

    for raw, secret in [
        ("token=abc123def", "abc123def"),
        ("api_key=sk-live-abc", "sk-live-abc"),
        ("secret: hunter2", "hunter2"),
        ("access_token=ya29.token", "ya29.token"),
        ("access_hash=8130875772", "8130875772"),
        ("file_reference=ABCDEFpeer", "ABCDEFpeer"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi.payload.sig"),
        (
            "bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
            "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        ),
    ]:
        out = redact(raw)
        assert secret not in out, f"{raw!r} leaked: {out!r}"
        assert "[REDACTED]" in out, f"{raw!r} -> {out!r}"

    # Ordinary identifiers and prose survive: over-redaction hides real defects.
    for raw in ("chat_id=-1001234567890", "forwarded 42 messages", "msg_id=930629"):
        assert redact(raw) == raw, raw
