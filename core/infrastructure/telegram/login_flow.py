"""In-bot interactive MTProto login flow (phone -> code -> 2FA -> session).

Hardened for production:
  * per-user rate limit on code requests (anti FloodWait)
  * code resend with cooldown
  * login attempt expiry (pending state auto-cancels after TTL)
  * per-step attempt counters (wrong-code lockout)
  * credential selection: each login can use a different api_id/api_hash
  * FloodWait respected (returns wait time instead of crashing)
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from pyrogram.client import Client
from pyrogram.errors import (
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

from ...application.use_cases import SessionUseCases
from .auth_state import AuthState, AuthStateMachine
from .client_pool import ClientPool

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"^\+\d{7,16}$")

#: seconds a pending login stays valid without activity
LOGIN_TTL = 10 * 60
#: min seconds between two code sends to the same phone
RESEND_COOLDOWN = 60
#: min seconds between two code sends from the same bot user
USER_SEND_COOLDOWN = 30
#: max wrong-code/password attempts before the flow is cancelled
MAX_ATTEMPTS = 5


@dataclass
class LoginState:
    phone_number: str
    client: Optional[Client] = None
    phone_code_hash: str = ""
    step: str = "phone"  # phone -> code -> password -> done
    credential_id: str = "default"
    created_at: float = field(default_factory=time.time)
    last_code_sent_at: float = 0.0
    code_attempts: int = 0
    password_attempts: int = 0
    last_touch: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_touch = time.time()

    @property
    def expired(self) -> bool:
        return (time.time() - self.last_touch) > LOGIN_TTL


@dataclass
class LoginFlowResult:
    success: bool
    session_id: str = ""
    message: str = ""


_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class LoginFlowManager:
    """State machine for linking user accounts inside the bot chat."""

    def __init__(
        self,
        pool: ClientPool,
        sessions: SessionUseCases,
        auth_machine: Optional[AuthStateMachine] = None,
    ) -> None:
        self._pool = pool
        self._sessions = sessions
        self._machine = auth_machine
        self._pending: Dict[int, LoginState] = {}  # live client cache, keyed by bot user id
        self._last_send: Dict[int, float] = {}  # bot user id -> last send_code ts
        self._phone_last_send: Dict[str, float] = {}  # phone -> last send ts

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def pending_for(self, user_id: int) -> Optional[LoginState]:
        state = self._pending.get(user_id)
        if state and state.expired:
            self.cancel(user_id)
            return None
        return self._pending.get(user_id)

    async def auth_state(self, user_id: int) -> Optional[AuthState]:
        """The persisted state, which is what survives a restart."""
        if self._machine is None:
            return None
        flow = await self._machine.current(user_id)
        return flow.state if flow else None

    async def login_in_progress(self, user_id: int) -> bool:
        """True while any login step owns this conversation."""
        if await self.auth_state(user_id) in (
            None,
            AuthState.NEW,
            AuthState.WELCOME,
            AuthState.AUTHENTICATED,
            AuthState.READY,
            AuthState.AUTH_EXPIRED,
            AuthState.SESSION_REVOKED,
        ):
            return self.has_pending(user_id)
        return True

    def has_pending(self, user_id: int) -> bool:
        return self.pending_for(user_id) is not None

    def cancel(self, user_id: int) -> bool:
        state = self._pending.pop(user_id, None)
        if state and state.client:
            try:
                coro = state.client.disconnect()
                if asyncio.iscoroutine(coro):
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(coro)
            except Exception:  # pragma: no cover
                pass
        return state is not None

    def sweep_expired(self) -> int:
        """Cancel all expired pending logins. Returns count cancelled."""
        expired = [uid for uid, st in self._pending.items() if st.expired]
        for uid in expired:
            self.cancel(uid)
        return len(expired)

    async def recover_after_restart(self) -> int:
        """Reconcile in-memory clients against the persisted state.

        ``ClientPool.create_login_client`` builds an ``in_memory=True`` client,
        so its MTProto key and the ``phone_code_hash`` die with the process. A
        code submitted against a hash from a dead process is rejected by
        Telegram, and a fresh client cannot be built without the user
        confirming the phone again.

        So an in-flight flow degrades honestly: the state moves to
        ``AUTH_EXPIRED`` while the phone number and the attempt counters
        survive, and the user resumes with one tap and Telegram's own attempt
        budget intact. Nothing is silently dropped, and nothing pretends to be
        resumable that Telegram would reject.
        """
        if self._machine is None:
            self.sweep_expired()
            return 0
        # ttl-lapsed flows are expired by definition
        n = await self._machine.sweep_expired()
        # Then every flow still marked as *waiting on the user*: its login
        # client was in_memory=True, so its MTProto key and phone_code_hash
        # died with the process. Telegram would reject a code submitted
        # against a hash from a dead client, so the flow degrades to
        # AUTH_EXPIRED — the phone and attempt counters survive, so the user
        # resumes with one tap and Telegram's own attempt budget intact.
        #
        # Driven off the persisted state, not the in-memory cache: after a
        # crash the cache is empty, which is precisely the case that needs
        # recovering.
        n += await self._machine.expire_active()
        self._pending.clear()
        return n

    @staticmethod
    def normalize_phone(raw: str) -> str:
        """Accept '+98...', '989...', '09...' (IR), spaces/dashes → +E164."""
        translated = (raw or "").translate(_DIGIT_MAP)
        digits = re.sub(r"[^\d+]", "", translated.strip())
        if digits.startswith("00"):
            digits = "+" + digits[2:]
        if digits.startswith("09") and len(digits) >= 10:
            # Iranian mobile shorthand → +98
            digits = "+98" + digits[1:]
        elif digits and not digits.startswith("+") and digits.startswith("98"):
            digits = "+" + digits
        elif digits and not digits.startswith("+"):
            digits = "+" + digits
        return digits

    @classmethod
    def valid_phone(cls, phone: str) -> bool:
        return bool(PHONE_RE.match(cls.normalize_phone(phone)))

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    async def start(self, user_id: int, phone_number: str, credential_id: str = "default") -> str:
        """Step 1: send code. Returns next-step message key or error text.

        Possible returns: send_code | invalid_phone | flood_wait:<sec> |
        cooldown:<sec> | send_failed:<error>
        """
        phone = self.normalize_phone(phone_number)
        if not PHONE_RE.match(phone):
            return "invalid_phone"
        now = time.time()
        last_user = self._last_send.get(user_id, 0)
        if now - last_user < USER_SEND_COOLDOWN:
            return f"cooldown:{int(USER_SEND_COOLDOWN - (now - last_user))}"
        last_phone = self._phone_last_send.get(phone, 0)
        if now - last_phone < RESEND_COOLDOWN:
            return f"cooldown:{int(RESEND_COOLDOWN - (now - last_phone))}"

        self.cancel(user_id)
        state = LoginState(phone_number=phone, credential_id=credential_id or "default")
        try:
            client = await self._pool.create_login_client(phone, credential_id)
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 60) or 60)
            logger.warning("login client flood for %s: wait %ds", phone, wait)
            return f"flood_wait:{wait}"
        except Exception as exc:
            logger.exception("create login client failed for %s: %s", phone, exc)
            return f"send_failed:{type(exc).__name__}: {exc}"
        state.client = client
        try:
            logger.info("Calling client.send_code for %s ...", phone[:4] + "****")
            sent = await client.send_code(phone)
            logger.info(
                "send_code succeeded for %s: type=%s next_type=%s timeout=%s",
                phone[:4] + "****",
                getattr(sent, "type", "unknown"),
                getattr(sent, "next_type", "unknown"),
                getattr(sent, "timeout", "unknown"),
            )
        except PhoneNumberInvalid:
            logger.warning("PhoneNumberInvalid from Telegram for %s", phone)
            return "invalid_phone"
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 60) or 60)
            logger.warning("send_code flood for %s: wait %ds", phone, wait)
            return f"flood_wait:{wait}"
        except Exception as exc:
            logger.exception("send_code failed for %s: %s", phone, exc)
            return f"send_failed:{type(exc).__name__}: {exc}"
        state.phone_code_hash = sent.phone_code_hash
        state.step = "code"
        state.last_code_sent_at = now
        self._pending[user_id] = state
        self._last_send[user_id] = now
        self._phone_last_send[phone] = now
        return "send_code"

    async def resend(self, user_id: int) -> str:
        """Re-send the login code (sender-side cooldown enforced)."""
        state = self.pending_for(user_id)
        if not state or state.step not in ("code",):
            return "no_pending_login"
        if state.client is None:
            return "no_pending_login"
        now = time.time()
        if now - state.last_code_sent_at < RESEND_COOLDOWN:
            return f"cooldown:{int(RESEND_COOLDOWN - (now - state.last_code_sent_at))}"
        try:
            sent = await state.client.send_code(state.phone_number)
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 60) or 60)
            return f"flood_wait:{wait}"
        except Exception as exc:
            logger.exception("resend failed")
            return f"send_failed:{type(exc).__name__}"
        state.phone_code_hash = sent.phone_code_hash
        state.last_code_sent_at = now
        state.code_attempts = 0
        state.touch()
        return "send_code"

    async def submit_code(self, user_id: int, code: str) -> str:
        """Step 2: submit the verification code.

        Returns "send_password", "login_success", or an error message.
        """
        state = self.pending_for(user_id)
        if not state or state.step != "code":
            # Distinguish "nothing was ever started" from "the login client
            # died in a restart" - the second case is our bug, not the user's
            # code being wrong, and the UI must say so.
            flow = await self.auth_state(user_id)
            if flow == AuthState.AUTH_EXPIRED:
                return "auth_expired"
            return "no_pending_login"
        if state.client is None:
            return "no_pending_login"
        state.touch()
        translated = (code or "").translate(_DIGIT_MAP)
        clean = re.sub(r"\D", "", translated)
        if not clean:
            return "invalid_code"
        if state.code_attempts >= MAX_ATTEMPTS:
            self.cancel(user_id)
            return "too_many_attempts"
        try:
            await state.client.sign_in(
                phone_number=state.phone_number,
                phone_code_hash=state.phone_code_hash,
                phone_code=clean,
            )
        except (PhoneCodeInvalid, PhoneCodeExpired) as exc:
            state.code_attempts += 1
            if isinstance(exc, PhoneCodeExpired):
                return "code_expired"
            if state.code_attempts >= MAX_ATTEMPTS:
                self.cancel(user_id)
                return "too_many_attempts"
            return "invalid_code"
        except SessionPasswordNeeded:
            state.step = "password"
            return "send_password"
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 60) or 60)
            return f"flood_wait:{wait}"
        return await self._finalize(user_id, state)

    async def submit_password(self, user_id: int, password: str) -> str:
        state = self.pending_for(user_id)
        if not state or state.step != "password":
            flow = await self.auth_state(user_id)
            if flow == AuthState.AUTH_EXPIRED:
                return "auth_expired"
            return "no_pending_login"
        if state.client is None:
            return "no_pending_login"
        state.touch()
        if state.password_attempts >= MAX_ATTEMPTS:
            self.cancel(user_id)
            return "too_many_attempts"
        try:
            await state.client.check_password(password)
        except PasswordHashInvalid:
            state.password_attempts += 1
            if state.password_attempts >= MAX_ATTEMPTS:
                self.cancel(user_id)
                return "too_many_attempts"
            return "invalid_password"
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 60) or 60)
            return f"flood_wait:{wait}"
        except Exception:
            return "invalid_password"
        return await self._finalize(user_id, state)

    async def _finalize(self, user_id: int, state: LoginState) -> str:
        try:
            session_string = await state.client.export_session_string()
            me = await state.client.get_me()
            session = await self._sessions.create(
                phone_number=state.phone_number,
                session_string_encrypted=session_string,
            )
            session.user_id = str(me.id)
            session.username = me.username or ""
            session.first_name = me.first_name or ""
            session.api_credential_id = state.credential_id or "default"
            session.owner_user_id = int(user_id)
            session.activate()
            await self._sessions._repo.update(session)
            await self._pool.start_with_session_string(
                session.id, session_string, credential_id=session.api_credential_id
            )
            try:
                await state.client.disconnect()
            except Exception:
                pass
            self._pending.pop(user_id, None)
            logger.info("Session %s linked for %s", session.id, state.phone_number)
            return f"login_success:{session.id}"
        except Exception as exc:
            logger.exception("Finalize login failed")
            return f"login_failed:{exc}"
