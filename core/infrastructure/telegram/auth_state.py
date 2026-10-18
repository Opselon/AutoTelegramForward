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

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.application.repositories import IAuthFlowRepository

logger = logging.getLogger(__name__)


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
    a no-op rather than a second side effect. Concurrent updates are refused by
    the version guard and re-applied to the revision that won, so neither
    writer loses its side effect.
    """

    # A refused write re-reads and re-applies. Three attempts is enough for the
    # realistic case (two handlers for one user); past that the flow is being
    # rewritten continuously and giving up loudly beats spinning.
    MAX_WRITE_ATTEMPTS = 3

    def __init__(self, repo: IAuthFlowRepository, ttl_seconds: int = 3600) -> None:
        self._repo = repo
        self._ttl = int(ttl_seconds)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _persist(self, flow: AuthFlow) -> bool:
        """Write ``flow`` one revision ahead of the one it was read at.

        False means another writer committed first: the row now holds their
        revision and this call wrote nothing. The caller decides what that
        means — re-apply (a transition) or stand down (a reset).
        """
        row = flow.to_row()
        row["version"] = flow.version + 1
        if await self._repo.save(row):
            flow.version = row["version"]
            return True
        return False

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
            # merely observed in memory. The returned flow is the revision that
            # was actually written — this object may have been superseded.
            flow = await self.transition(user_id, AuthState.AUTH_EXPIRED, flow=flow)
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
        """Move a flow to ``target``. Idempotent; raises on an illegal jump.

        The write is version-guarded, so a transition that races another writer
        is refused rather than silently overwriting it. A refusal is not an
        error here: the flow is re-read and the same transition re-applied to
        the current revision, which is what keeps two concurrent callbacks from
        losing one of their side effects. If the other writer moved the flow
        somewhere this transition may not go, the usual InvalidTransition is
        raised — for the state it is actually in, not the stale one.
        """
        latest: Optional[AuthFlow] = flow
        refusal: Optional[_StaleFlowWrite] = None
        for _attempt in range(self.MAX_WRITE_ATTEMPTS):
            try:
                return await self._transition_once(
                    user_id,
                    target,
                    flow=latest,
                    phone_number=phone_number,
                    phone_code_hash=phone_code_hash,
                    credential_id=credential_id,
                    last_error=last_error,
                    bump_code=bump_code,
                    bump_password=bump_password,
                    extend=extend,
                )
            except _StaleFlowWrite as exc:
                # another writer committed first: start over from what it wrote
                refusal = exc
                latest = None
        raise ConcurrentFlowUpdate(
            f"auth flow for user {user_id} was rewritten by another writer on "
            f"every one of {self.MAX_WRITE_ATTEMPTS} attempts"
        ) from refusal

    async def _transition_once(
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
        """One guarded read-modify-write; raises ``_StaleFlowWrite`` if refused."""
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
                if not await self._persist(flow):
                    raise _StaleFlowWrite(f"stale write for user {user_id}")
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

        if not await self._persist(flow):
            raise _StaleFlowWrite(f"stale write for user {user_id}")
        return flow

    async def reset(self, user_id: int, *, keep_counters: bool = False) -> None:
        """Clear a finished/failed flow (kept out of the way of retries).

        Idempotent: resetting a user with no flow is not an error, because the
        caller's intent — "no live flow for this user" — already holds.

        Both forms are revision-guarded, because the flow this call read may
        have been superseded: a plain delete would remove a login the user
        started in the meantime, and a plain re-state would roll it back.
        """
        flow = await self.current(user_id)
        if flow is None:
            return
        if keep_counters:
            for _attempt in range(self.MAX_WRITE_ATTEMPTS):
                flow.state = AuthState.NEW
                flow.updated_at = _now()
                if await self._persist(flow):
                    return
                flow = await self.current(user_id)
                if flow is None:
                    return
            logger.warning(
                "auth flow for user %s kept changing while being reset; left the "
                "newer revision in place",
                user_id,
            )
            return
        if not await self._repo.delete(user_id, version=flow.version):
            logger.debug(
                "auth flow for user %s superseded while resetting; nothing deleted",
                user_id,
            )

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


class ConcurrentFlowUpdate(InvalidTransition):
    """Another writer rewrote the flow on every attempt.

    Subclasses InvalidTransition on purpose: callers that already treat "this
    flow moved on without you" as a non-event keep working, and nothing is left
    half-applied — the newer revision stays exactly as the other writer wrote it.
    """


class _StaleFlowWrite(Exception):
    """Internal: a revision-guarded write was refused because a newer one exists."""
