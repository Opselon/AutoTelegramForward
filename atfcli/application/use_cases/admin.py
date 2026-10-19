"""Administration: users, platform settings, admins, extensions, broadcast.

Every rule here is one the server also enforces — the point of checking locally
is that the operator learns it before the round trip, and the point of stating
the vocabulary in one place is that `--help`, the wizard and the request all
describe the same two statuses the core accepts (``active``, ``suspended``; see
``admin_servicer.set_status``).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ...domain.errors import UsageError
from ...domain.models import AdminUser
from ...domain.secrets import is_secret_name
from ...domain.validators import suggest, valid_telegram_user_id
from . import _payload

#: The values the core accepts for a user's status (`admin_servicer`).
USER_STATUSES: tuple[str, ...] = ("active", "suspended")


class Administration:
    """Use cases over the admin surface."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway

    # -------------------------------------------------------------------- users
    def overview(self) -> Mapping[str, Any]:
        return self.gateway.overview()

    def users(self, *, limit: int = 50, offset: int = 0) -> list[AdminUser]:
        if limit <= 0:
            raise UsageError("--limit must be positive", hint="pass e.g. --limit 50")
        return self.gateway.list_users(limit=limit, offset=max(0, offset))

    def set_user_status(self, user_id: str, status: str) -> _payload.Outcome:
        problem = valid_telegram_user_id(user_id)
        if problem:
            raise UsageError(f"user id: {problem}")
        wanted = (status or "").strip().lower()
        if wanted not in USER_STATUSES:
            raise UsageError(
                f"unknown status {status!r}{suggest(wanted, USER_STATUSES)}",
                hint=f"statuses: {', '.join(USER_STATUSES)}",
            )
        document = _payload.mapping(self.gateway.set_user_status(user_id, wanted))
        if document.get("success") is False:
            raise UsageError(str(document.get("message") or f"could not set {user_id} to {wanted}"))
        return _payload.Outcome(document, f"user {user_id} → {wanted}")

    def broadcast(self, message: str, *, confirm: bool) -> _payload.Outcome:
        text = (message or "").strip()
        if not text:
            raise UsageError("the broadcast message is empty")
        if not confirm:
            raise UsageError(
                "this messages every user of the platform",
                hint="pass --yes to confirm",
            )
        document = _payload.mapping(self.gateway.broadcast(text))
        sent = document.get("sent", document.get("delivered", "?"))
        return _payload.Outcome(document, f"broadcast sent to {sent} user(s)")

    # ----------------------------------------------------------------- settings
    def settings(self) -> Mapping[str, Any]:
        return self.gateway.settings()

    def save_settings(self, values: Mapping[str, Any]) -> _payload.Outcome:
        if not values:
            raise UsageError(
                "nothing to save",
                hint="pass key=value pairs, e.g. `atf admin settings set maintenance_mode=false`",
            )
        document = _payload.mapping(self.gateway.save_settings(values))
        if document.get("success") is False:
            raise UsageError(str(document.get("message") or "the server refused the settings"))
        shown = ", ".join(
            f"{key}=[REDACTED]" if is_secret_name(key) else f"{key}={value}"
            for key, value in sorted(values.items())
        )
        return _payload.Outcome(document, f"saved {len(values)} setting(s): {shown}")

    # ------------------------------------------------------------------- admins
    def admins(self) -> list[AdminUser]:
        return self.gateway.list_admins()

    def add_admin(self, user_id: str, *, username: str = "") -> _payload.Outcome:
        problem = valid_telegram_user_id(user_id)
        if problem:
            raise UsageError(f"user id: {problem}")
        document = _payload.mapping(self.gateway.add_admin(user_id, username=username))
        if document.get("success") is False:
            raise UsageError(str(document.get("message") or f"could not promote {user_id}"))
        return _payload.Outcome(document, f"{user_id} is now an admin")

    def remove_admin(self, user_id: str, *, confirm: bool) -> _payload.Outcome:
        problem = valid_telegram_user_id(user_id)
        if problem:
            raise UsageError(f"user id: {problem}")
        if not confirm:
            raise UsageError(
                f"removing {user_id} revokes platform administration",
                hint="pass --yes to confirm",
            )
        document = _payload.mapping(self.gateway.remove_admin(user_id))
        if document.get("success") is False:
            raise UsageError(str(document.get("message") or f"could not demote {user_id}"))
        return _payload.Outcome(document, f"{user_id} is no longer an admin")

    # --------------------------------------------------------------- extensions
    def extensions(self) -> list[Mapping[str, Any]]:
        return self.gateway.extensions()

    def find_extension(self, ref: str) -> Mapping[str, Any]:
        if not ref:
            raise UsageError("an extension id is required")
        rows = self.gateway.extensions()
        wanted = ref.strip().lower()
        exact = [row for row in rows if str(row.get("id", "")).lower() == wanted]
        if not exact:
            exact = [row for row in rows if str(row.get("id", "")).lower().startswith(wanted)]
        if not exact:
            names: Sequence[str] = [str(row.get("id", "")) for row in rows]
            raise UsageError(f"unknown extension {ref!r}{suggest(wanted, names)}")
        if len(exact) > 1:
            raise UsageError(
                f"{ref!r} matches {len(exact)} extensions; use the full id",
                hint=", ".join(str(row.get("id", "")) for row in exact[:5]),
            )
        return exact[0]

    def toggle_extension(self, ref: str) -> _payload.Outcome:
        row = self.find_extension(ref)
        extension_id = str(row.get("id", ""))
        document = _payload.mapping(self.gateway.toggle_extension(extension_id))
        enabled = document.get("enabled", row.get("enabled"))
        detail = f"toggled extension {extension_id}"
        if isinstance(enabled, bool):
            detail += f" → {'enabled' if enabled else 'disabled'}"
        return _payload.Outcome(document, detail)
