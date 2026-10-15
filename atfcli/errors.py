"""Errors and process exit codes for the ATF CLI.

Exit codes are part of the CLI's contract: a wrapper script must be able to tell
"the service is down" from "you typed the wrong task id" without parsing prose.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_UNAUTHORIZED = 3
EXIT_NOT_FOUND = 4
EXIT_UNREACHABLE = 5
EXIT_TIMEOUT = 6
EXIT_CONFLICT = 7
EXIT_NOT_IMPLEMENTED = 8


class AtfError(Exception):
    """Base class for every failure the CLI expects to handle."""

    exit_code = EXIT_FAILURE

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(AtfError):
    exit_code = EXIT_USAGE


class AuthError(AtfError):
    """No usable credential, or the server rejected it."""

    exit_code = EXIT_UNAUTHORIZED


class NotFoundError(AtfError):
    exit_code = EXIT_NOT_FOUND


class UnreachableError(AtfError):
    """The API, a container, or a dependency could not be contacted."""

    exit_code = EXIT_UNREACHABLE


class TimeoutError_(AtfError):
    exit_code = EXIT_TIMEOUT


class ConflictError(AtfError):
    """The request was valid but the current state refuses it."""

    exit_code = EXIT_CONFLICT


class NotSupportedError(AtfError):
    """The running server/container is older than this CLI expects."""

    exit_code = EXIT_NOT_IMPLEMENTED


class CommandFailed(AtfError):
    """A shelled-out command returned non-zero."""

    def __init__(self, message: str, *, hint: str = "", output: str = "") -> None:
        super().__init__(message, hint=hint)
        self.output = output


def http_status_error(status: int, message: str, *, hint: str = "") -> AtfError:
    """Map an HTTP status onto the CLI's error taxonomy."""
    if status in (401, 403):
        return AuthError(message, hint=hint)
    if status == 404:
        return NotFoundError(message, hint=hint)
    if status == 409:
        return ConflictError(message, hint=hint)
    if status == 501:
        return NotSupportedError(message, hint=hint)
    if status in (502, 503, 504):
        return UnreachableError(message, hint=hint)
    return AtfError(message, hint=hint)
