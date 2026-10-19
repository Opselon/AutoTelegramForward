"""Telegram sessions: list, inspect a session's chats, back up, terminate.

Terminating is the one destructive action here, so the use case insists on an
identifier and refuses an ambiguous one — a session id that matches two rows is
not resolved by guessing.
"""

from __future__ import annotations

from ...domain.errors import NotFoundError, UsageError
from ...domain.models import Session, SessionChat
from . import _payload


class Sessions:
    """Use cases over Telegram user sessions."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway

    def list(self) -> list[Session]:
        return self.gateway.list_sessions()

    def find(self, ref: str) -> Session:
        if not ref:
            raise UsageError("a session id is required")
        sessions = self.gateway.list_sessions()
        exact = [session for session in sessions if session.id == ref]
        if exact:
            return exact[0]
        prefix = [session for session in sessions if session.id.startswith(ref) and len(ref) >= 4]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise UsageError(
                f"{ref!r} matches {len(prefix)} sessions; use a longer prefix",
                hint=", ".join(session.id[:12] for session in prefix[:5]),
            )
        by_phone = [session for session in sessions if session.phone and session.phone.endswith(ref.lstrip("+"))]
        if len(by_phone) == 1:
            return by_phone[0]
        raise NotFoundError(
            f"no session matches {ref!r}",
            hint="`atf sessions list` shows the session ids",
        )

    def chats(self, ref: str) -> list[SessionChat]:
        session = self.find(ref)
        return self.gateway.session_chats(session.id)

    def backup(self, ref: str) -> _payload.Outcome:
        session = self.find(ref)
        document = _payload.mapping(self.gateway.backup_session(session.id))
        path = document.get("path") or document.get("file") or document.get("backup")
        detail = f"backed up session {session.id[:8]}"
        if path:
            detail += f" to {path}"
        return _payload.Outcome(document, detail)

    def terminate(self, ref: str, *, confirm: bool) -> _payload.Outcome:
        session = self.find(ref)
        if not confirm:
            raise UsageError(
                f"terminating {session.id[:8]} logs the account out",
                hint="pass --yes to confirm",
            )
        document = _payload.mapping(self.gateway.terminate_session(session.id))
        return _payload.Outcome(document, f"terminated session {session.id[:8]}")
