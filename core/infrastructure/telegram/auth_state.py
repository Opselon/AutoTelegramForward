"""Persisted authentication state machine (audit D1 / TASK 03 / spec section 5).

The previous flow kept every login in ``LoginFlowManager._pending``, a plain
dict. A restart — the container being recycled, a crash, a deploy — killed the
flow with no trace: the user pressed "send code", nothing happened, and the bot
had no idea why.

This module is the persisted half. It owns the *durable* facts (state, phone,
credential, retry counters, expiry, correlation id) and the legal transitions
between them.

What it deliberately does NOT persist: the live MTProto client and the
``phone_code_hash``. ``ClientPool.create_login_client`` builds an
``in_memory=True`` client, so its ephemeral key lives in RAM only. A code
submitted against a hash from a dead process is rejected by Telegram, and a
new client cannot be constructed without the user re-confirming the phone. So
after a restart an in-flight flow degrades honestly to ``AUTH_EXPIRED`` — the
phone and the attempt counters survive, so the user resumes with one tap and
Telegram's own attempt budget intact — instead of the bot pretending a
half-dead session is still usable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.application.repositories import IAuthFlowRepository


class AuthState(str, Enum):
    """Every state an account-linking flow can be in."""

    NEW = "NEW"
    WELCOME = "WELCOME"
    AUTH_PHONE_REQUIRED = "AUTH_PHONE_REQUIRED"
    AUTH_PHONE_SUBMITTED = "AUTH_PHONE_SUBMITTED"
    AUTH_CODE_REQUIRED = "AUTH_CODE_REQUIRED"
    AUTH_CODE_SUBMITTED = "AUTH_CODE_SUBMITTED"
    AUTH_2FA_REQUIRED = "AUTH_2FA_REQUIRED"
    AUTH_2FA_SUBMITTED = "AUTH_2FA_SUBMITTED"
    AUTHENTICATING = "AUTHENTICATING"
    AUTHENTICATED = "AUTHENTICATED"
    AUTH_FAILED = "AUTH_FAILED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    SESSION_REVOKED = "SESSION_REVOKED"
    READY = "READY"


#: states that mean "the bot is waiting on the user" and must block routing
ACTIVE_STATES = frozenset(
    {
        AuthState.AUTH_PHONE_REQUIRED,
        AuthState.AUTH_PHONE_SUBMITTED,
        AuthState.AUTH_CODE_REQUIRED,
        AuthState.AUTH_CODE_SUBMITTED,
        AuthState.AUTH_2FA_REQUIRED,
        AuthState.AUTH_2FA_SUBMITTED,
        AuthState.AUTHENTICATING,
    }
)

#: states that a completed flow may legally be in
TERMINAL_STATES = frozenset(
    {AuthState.AUTHENTICATED, AuthState.READY, AuthState.SESSION_REVOKED}
)

#: legal successors for each state. Anything not listed is rejected.
_TRANSITIONS: dict = {
    AuthState.NEW: {
        AuthState.WELCOME,
        AuthState.AUTH_PHONE_REQUIRED,
    },
    AuthState.WELCOME: {
        AuthState.AUTH_PHONE_REQUIRED,
        AuthState.AUTH_EXPIRED,
    },
    AuthState.AUTH_PHONE_REQUIRED: {
        AuthState.AUTH_PHONE_SUBMITTED,
        AuthState.AUTH_EXPIRED,
        AuthState.AUTH_FAILED,
        AuthState.WELCOME,
    },
    AuthState.AUTH_PHONE_SUBMITTED: {
        AuthState.AUTH_CODE_REQUIRED,
        AuthState.AUTH_FAILED,
        AuthState.AUTH_EXPIRED,
        AuthState.AUTH_PHONE_REQUIRED,  # invalid phone -> ask again
    },
    AuthState.AUTH_CODE_REQUIRED: {
        AuthState.AUTH_CODE_SUBMITTED,
        AuthState.AUTH_EXPIRED,
        AuthState.AUTH_FAILED,
        AuthState.AUTH_PHONE_REQUIRED,  # "re-send / change number"
    },
    AuthState.AUTH_CODE_SUBMITTED: {
        AuthState.AUTH_2FA_REQUIRED,
        AuthState.AUTHENTICATING,
        AuthState.AUTHENTICATED,
        AuthState.AUTH_FAILED,
        AuthState.AUTH_EXPIRED,
        AuthState.AUTH_CODE_REQUIRED,  # wrong code -> ask again
    },
    AuthState.AUTH_2FA_REQUIRED: {
        AuthState.AUTH_2FA_SUBMITTED,
        AuthState.AUTH_EXPIRED,
        AuthState.AUTH_FAILED,
    },
    AuthState.AUTH_2FA_SUBMITTED: {
        AuthState.AUTHENTICATING,
        AuthState.AUTHENTICATED,
        AuthState.AUTH_FAILED,
        AuthState.AUTH_EXPIRED,
        AuthState.AUTH_2FA_REQUIRED,  # wrong password -> ask again
    },
    AuthState.AUTHENTICATING: {
        AuthState.AUTHENTICATED,
        AuthState.AUTH_FAILED,
        AuthState.AUTH_EXPIRED,
    },
    AuthState.AUTHENTICATED: {AuthState.READY, AuthState.SESSION_REVOKED},
    AuthState.READY: {AuthState.SESSION_REVOKED, AuthState.AUTH_PHONE_REQUIRED},
    AuthState.AUTH_FAILED: {
        AuthState.AUTH_PHONE_REQUIRED,
        AuthState.WELCOME,
        AuthState.AUTH_EXPIRED,
    },
    AuthState.AUTH_EXPIRED: {AuthState.AUTH_PHONE_REQUIRED, AuthState.WELCOME},
    AuthState.SESSION_REVOKED: {AuthState.AUTH_PHONE_REQUIRED, AuthState.WELCOME},
}


def _now() -> int:
    return int(time.time())


def new_correlation_id() -> str:
    return uuid.uuid4().hex


@dataclass
class AuthFlow:
    """A persisted authentication flow. Durable facts only."""

    user_id: int
    state: AuthState
    phone_number: str = ""
    credential_id: str = "default"
    phone_code_hash: str = ""
    code_attempts: int = 0
    password_attempts: int = 0
    version: int = 1
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)
    expires_at: int = field(default_factory=_now)
    last_error: str = ""
    correlation_id: str = field(default_factory=new_correlation_id)

    @classmethod
    def fresh(cls, user_id: int, ttl_seconds: int) -> "AuthFlow":
        return cls(
            user_id=user_id,
            state=AuthState.NEW,
            expires_at=_now() + ttl_seconds,
        )

    @property
    def expired(self) -> bool:
        return _now() > self.expires_at and self.state not in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    def can_go(self, target: AuthState) -> bool:
        return target in _TRANSITIONS.get(self.state, frozenset())

    def to_row(self) -> dict:
        return {
            "user_id": int(self.user_id),
            "state": self.state.value,
            "phone_number": self.phone_number,
            "credential_id": self.credential_id or "default",
            "phone_code_hash": self.phone_code_hash or "",
            "code_attempts": int(self.code_attempts),
            "password_attempts": int(self.password_attempts),
            "version": int(self.version),
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
            "expires_at": int(self.expires_at),
            "last_error": self.last_error or "",
            "correlation_id": self.correlation_id or new_correlation_id(),
        }

    @classmethod
    def from_row(cls, row) -> "AuthFlow":
        return cls(
            user_id=int(row["user_id"]),
            state=AuthState(row["state"]),
            phone_number=row["phone_number"] or "",
            credential_id=row["credential_id"] or "default",
            phone_code_hash=row["phone_code_hash"] or "",
            code_attempts=int(row["code_attempts"] or 0),
            password_attempts=int(row["password_attempts"] or 0),
            version=int(row["version"] or 1),
            created_at=int(row["created_at"] or _now()),
            updated_at=int(row["updated_at"] or _now()),
            expires_at=int(row["expires_at"] or _now()),
            last_error=row["last_error"] or "",
            correlation_id=row["correlation_id"] or "",
        )


class AuthStateMachine:
    """Idempotent persisted transitions, guarded by optimistic concurrency.

    Duplicate updates (a double-fired handler, the user pressing an old button,
    a retry) are absorbed: the same target state from the same source state is
    a no-op rather than a second side effect.
    """

    def __init__(self, repo: IAuthFlowRepository, ttl_seconds: int = 3600) -> None:
        self._repo = repo
        self._ttl = int(ttl_seconds)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def current(self, user_id: int) -> Optional[AuthFlow]:
        row = await self._repo.get(user_id)
        if row is None:
            return None
        flow = AuthFlow.from_row(row)
        if flow.expired and flow.state != AuthState.AUTH_EXPIRED:
            # expiry is itself a transition, so it is recorded rather than
            # merely observed in memory
            await self.transition(user_id, AuthState.AUTH_EXPIRED, flow=flow)
            flow.state = AuthState.AUTH_EXPIRED
        return flow

    async def is_blocked(self, user_id: int) -> bool:
        """True while a flow owns the conversation (routing must stay silent)."""
        flow = await self.current(user_id)
        return bool(flow and flow.is_active)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def transition(
        self,
        user_id: int,
        target: AuthState,
        *,
        flow: Optional[AuthFlow] = None,
        phone_number: Optional[str] = None,
        phone_code_hash: Optional[str] = None,
        credential_id: Optional[str] = None,
        last_error: Optional[str] = None,
        bump_code: bool = False,
        bump_password: bool = False,
        extend: Optional[int] = None,
    ) -> AuthFlow:
        """Move a flow to ``target``. Idempotent; raises on an illegal jump."""
        flow = flow if flow is not None else await self.current(user_id)
        if flow is None:
            if target in (AuthState.NEW, AuthState.WELCOME, AuthState.AUTH_PHONE_REQUIRED):
                flow = AuthFlow.fresh(user_id, self._ttl)
            else:
                raise InvalidTransition(
                    f"no auth flow for user {user_id}; cannot go to {target.value}"
                )

        # idempotency: re-asserting the current state is a no-op *for the
        # state*, but side effects the caller asked for (attempt bumps, error
        # text, a fresh ttl) still apply — a duplicate callback must not mean a
        # lost attempt count
        if flow.state == target:
            if bump_code:
                flow.code_attempts += 1
            if bump_password:
                flow.password_attempts += 1
            if last_error is not None:
                flow.last_error = last_error
            if extend is not None:
                flow.expires_at = _now() + int(extend)
            if bump_code or bump_password or last_error is not None or extend is not None:
                flow.updated_at = _now()
                row = flow.to_row()
                row["version"] = flow.version + 1
                await self._repo.save(row)
                flow.version = row["version"]
            return flow

        if not flow.can_go(target):
            raise InvalidTransition(
                f"illegal auth transition for user {user_id}: "
                f"{flow.state.value} -> {target.value}"
            )

        flow.state = target
        if phone_number is not None:
            flow.phone_number = phone_number
        if phone_code_hash is not None:
            flow.phone_code_hash = phone_code_hash
        if credential_id is not None:
            flow.credential_id = credential_id or "default"
        if last_error is not None:
            flow.last_error = last_error
        if bump_code:
            flow.code_attempts += 1
        if bump_password:
            flow.password_attempts += 1
        flow.updated_at = _now()
        flow.expires_at = _now() + int(extend or self._ttl)

        row = flow.to_row()
        row["version"] = flow.version + 1
        await self._repo.save(row)
        flow.version = row["version"]
        return flow

    async def reset(self, user_id: int, *, keep_counters: bool = False) -> None:
        """Clear a finished/failed flow (kept out of the way of retries).

        Idempotent: resetting a user with no flow is not an error, because the
        caller's intent — "no live flow for this user" — already holds.
        """
        flow = await self.current(user_id)
        if flow is None:
            return
        if keep_counters:
            row = flow.to_row()
            row["state"] = AuthState.NEW.value
            row["version"] = flow.version + 1
            row["updated_at"] = _now()
            await self._repo.save(row)
        else:
            await self._repo.delete(user_id)

    async def sweep_expired(self) -> int:
        """Expire every ttl-lapsed flow. Called on startup and on a timer."""
        rows = await self._repo.expired()
        for row in rows:
            await self.transition(int(row["user_id"]), AuthState.AUTH_EXPIRED)
        return len(rows)

    async def expire_active(self) -> int:
        """Expire every flow still waiting on the user.

        Used after a restart: the login client was in_memory=True, so the flow
        cannot resume even though its ttl has not lapsed. Transitioning it here
        is what makes the degradation visible to the user instead of the bot
        silently ignoring the code they type.
        """
        rows = await self._repo.active()
        n = 0
        for row in rows:
            uid = int(row["user_id"])
            try:
                await self.transition(uid, AuthState.AUTH_EXPIRED, last_error="restart")
            except InvalidTransition:
                # already terminal (e.g. authenticated between the query and
                # the write) — nothing to recover
                continue
            n += 1
        return n


class InvalidTransition(Exception):
    """A state jump the machine refuses to make."""
