"""In-bot interactive MTProto login flow (phone -> code -> 2FA -> session)."""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

from pyrogram import Client
from pyrogram.errors import (
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
)

from ...application.use_cases import SessionUseCases
from .client_pool import ClientPool

logger = logging.getLogger(__name__)


@dataclass
class LoginState:
    phone_number: str
    client: Optional[Client] = None
    phone_code_hash: str = ""
    step: str = "phone"  # phone -> code -> password -> done


@dataclass
class LoginFlowResult:
    success: bool
    session_id: str = ""
    message: str = ""


class LoginFlowManager:
    """State machine for linking user accounts inside the bot chat."""

    def __init__(self, pool: ClientPool, sessions: SessionUseCases) -> None:
        self._pool = pool
        self._sessions = sessions
        self._pending: Dict[int, LoginState] = {}  # keyed by bot user id

    def pending_for(self, user_id: int) -> Optional[LoginState]:
        return self._pending.get(user_id)

    def cancel(self, user_id: int) -> bool:
        state = self._pending.pop(user_id, None)
        if state and state.client:
            try:
                state.client.disconnect()
            except Exception:  # pragma: no cover
                pass
        return state is not None

    async def start(self, user_id: int, phone_number: str) -> str:
        """Step 1: send code. Returns next-step message key or error text."""
        self.cancel(user_id)
        state = LoginState(phone_number=phone_number)
        state.client = await self._pool.create_login_client(phone_number)
        sent = await state.client.send_code(phone_number)
        state.phone_code_hash = sent.phone_code_hash
        state.step = "code"
        self._pending[user_id] = state
        return "send_code"

    async def submit_code(self, user_id: int, code: str) -> str:
        """Step 2: submit the verification code.

        Returns "send_password", "login_success", or an error message.
        """
        state = self._pending.get(user_id)
        if not state or state.step != "code":
            return "no_pending_login"
        try:
            await state.client.sign_in(
                phone_number=state.phone_number,
                phone_code_hash=state.phone_code_hash,
                phone_code=code.strip(),
            )
        except (PhoneCodeInvalid, PhoneCodeExpired):
            return "invalid_code"
        except SessionPasswordNeeded:
            state.step = "password"
            return "send_password"
        return await self._finalize(user_id, state)

    async def submit_password(self, user_id: int, password: str) -> str:
        state = self._pending.get(user_id)
        if not state or state.step != "password":
            return "no_pending_login"
        try:
            await state.client.check_password(password)
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
            session.activate()
            await self._sessions._repo.update(session)
            await self._pool.start_with_session_string(session.id, session_string)
            state.client.disconnect()
            self._pending.pop(user_id, None)
            logger.info("Session %s linked for %s", session.id, state.phone_number)
            return f"login_success:{session.id}"
        except Exception as exc:
            logger.exception("Finalize login failed")
            return f"login_failed:{exc}"
