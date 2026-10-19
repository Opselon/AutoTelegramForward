"""One module per bounded context. Each exposes a small application service.

The classes here are the *only* place a CLI action is defined. A command module
parses flags and renders; a use case decides what the action means, refuses what
cannot work, and returns an :class:`~atfcli.application.results.Outcome`.
"""

from __future__ import annotations

from . import admin, audit, backup, delivery, logs, ops, resources, sessions, tasks

__all__ = [
    "admin",
    "audit",
    "backup",
    "delivery",
    "logs",
    "ops",
    "resources",
    "sessions",
    "tasks",
]
