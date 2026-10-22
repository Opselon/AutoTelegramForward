"""The guided administration console: contracts, actions and the session loop.

This is presentation logic that lives in the application ring because it is
*policy* (what an administrator may do, in what order, behind which
confirmation) rather than formatting. The terminal itself arrives as the
:class:`~atfcli.application.console.contracts.SessionIO` port, implemented in
``atfcli.commands.console``; nothing here imports ``argparse``, a stream, or an
adapter.
"""

from __future__ import annotations

from .actions import GROUP_ORDER, actions, by_id
from .contracts import (
    DESTRUCTIVE,
    LEGEND,
    READ,
    WRITE,
    Action,
    ActionContext,
    Confirmation,
    Prompt,
    SessionIO,
)
from .session import AdminConsole
from .values import coerce

__all__ = [
    "DESTRUCTIVE",
    "GROUP_ORDER",
    "LEGEND",
    "READ",
    "WRITE",
    "Action",
    "ActionContext",
    "AdminConsole",
    "Confirmation",
    "Prompt",
    "SessionIO",
    "actions",
    "by_id",
    "coerce",
]
