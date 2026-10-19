"""Compatibility alias: the error taxonomy now lives in :mod:`atfcli.domain.errors`.

The taxonomy is imported by name all over the tree (commands, adapters, tests),
so the old path keeps working; new code should import from ``atfcli.domain``.
The names are re-exported rather than copied, so ``isinstance`` and ``except``
behave identically on both paths.
"""

from __future__ import annotations

from .domain.errors import (  # noqa: F401  (re-export for the legacy path)
    EXIT_CONFLICT,
    EXIT_FAILURE,
    EXIT_NOT_FOUND,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_UNAUTHORIZED,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
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
    "EXIT_CONFLICT",
    "EXIT_FAILURE",
    "EXIT_NOT_FOUND",
    "EXIT_NOT_IMPLEMENTED",
    "EXIT_OK",
    "EXIT_TIMEOUT",
    "EXIT_UNAUTHORIZED",
    "EXIT_UNREACHABLE",
    "EXIT_USAGE",
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
