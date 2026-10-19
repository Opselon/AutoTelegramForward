"""The domain layer: what the CLI talks about, and the rules those things obey.

Nothing in this package performs I/O, imports argparse, or knows that the stack
is reachable over HTTP. That is the point: a rule such as "a task must keep one
source" or "a port is 1-65535" is decided once, here, and every surface —
wizard prompt, argparse flag, use case — asks the same code.

Modules
-------
``errors``      the failure taxonomy and the process exit codes it maps to
``validators``  one rule per human-enterable value, plus argparse adapters
``models``      typed views over the API payloads (tasks, sessions, logs, ...)
``logs``        the log-level vocabulary and the log query value object
``host``        the machine model: services, ports, revisions, upgrade runs
``resources``   the CRUD resource catalogue (AI configs, prompts, filters)
``secrets``     which names hold a credential (masking + echo checks)
"""

from __future__ import annotations

from .errors import (
    AtfError,
    AuthError,
    CommandFailed,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    TimeoutError_,
    UnreachableError,
    UsageError,
    http_status_error,
)

__all__ = [
    "AtfError",
    "AuthError",
    "CommandFailed",
    "ConflictError",
    "NotFoundError",
    "NotSupportedError",
    "TimeoutError_",
    "UnreachableError",
    "UsageError",
    "http_status_error",
]
