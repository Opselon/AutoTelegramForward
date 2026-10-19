"""Compatibility alias: the API client now lives in
:mod:`atfcli.infrastructure.http.transport`.

The HTTP client is the adapter for the API port; keeping the old module path
means existing importers (and the CLI's own history) keep working. New code
should either import from ``atfcli.infrastructure`` or, better, depend on the
use cases in ``atfcli.application`` instead of talking HTTP at all.
"""

from __future__ import annotations

from .infrastructure.http.transport import (  # noqa: F401  (legacy path)
    DEFAULT_URLS,
    TOKEN_TTL_SECONDS,
    ApiClient,
    Credential,
    TokenStore,
    jwt_payload,
)

__all__ = [
    "DEFAULT_URLS",
    "TOKEN_TTL_SECONDS",
    "ApiClient",
    "Credential",
    "TokenStore",
    "jwt_payload",
]
