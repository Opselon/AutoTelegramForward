"""Server-side authentication for the core's internal gRPC surface.

Every servicer in this package takes the acting tenant as a request field
(``owner_user_id``). That is the right shape for the *contract* — the Go API is
the only tenant-facing entry point and it stamps the identity it verified from
the caller's token, never a value from the request body — but it also means the
gRPC surface itself has no way to tell a legitimate caller from any other
process that can open a socket to it. Anyone on the compose network (a
compromised sidecar, a `docker run --network` debugging session, a future
service with an over-broad dependency) can act as any tenant simply by filling
in a different ``owner_user_id``.

This module closes that gap without changing the ownership contract: when a
shared credential is configured, a call without it never reaches a servicer, so
impersonation requires the secret *and* the network position, instead of the
network position alone.

Configuration
-------------
``ATF_CORE_RPC_TOKEN`` (config: ``core_rpc_token``). When empty — the default,
and what every deployment was before this existed — the interceptor is not
installed at all and the server behaves exactly as before; ``main`` logs a
warning that says so. When set, every RPC must carry::

    authorization: Bearer <token>

A missing, malformed or wrong credential is refused with ``UNAUTHENTICATED``
before the servicer runs. The token is compared with :func:`hmac.compare_digest`
and is never logged, echoed, or included in an error detail.

Fail closed, loudly
-------------------
The token is not required to be set, but when it *is* set it is required on
every call — there is no per-method or per-peer exemption, and no "trusted
subnet" allowance. A half-configured deployment (token on the API, not on the
core, or vice versa) fails every RPC with UNAUTHENTICATED rather than silently
downgrading to the unauthenticated behaviour, so a mismatch surfaces as a health
failure during deployment instead of as a quiet loss of the boundary.
"""

from __future__ import annotations

import hmac
import logging

import grpc

logger = logging.getLogger(__name__)

#: The metadata key the credential travels in. HTTP-style so it is obvious what
#: it is in a packet dump or a metadata listing, and so proxying tools (Envoy,
#: grpc-gateway) forward it without configuration.
AUTH_METADATA_KEY = "authorization"

#: Only this scheme is accepted. A bare token, `Basic ...`, or `Bearer` with no
#: value is refused — accepting several spellings of one credential is how
#: "authenticated" ends up meaning "something was present".
_SCHEME = "bearer"


def configured_token(value: str | None) -> str:
    """Normalize the configured credential, treating blanks as "not set".

    A whitespace-only value is a configuration mistake, not a credential: it
    would install a boundary whose secret is public knowledge. Treat it as unset
    so the startup warning fires.
    """
    return (value or "").strip()


def _credential_matches(token: str, metadata) -> bool:
    """True when ``metadata`` carries this token as a Bearer credential."""
    for key, value in metadata or ():
        if key.lower() != AUTH_METADATA_KEY:
            continue
        candidate = value or ""
        scheme, _, presented = candidate.partition(" ")
        if scheme.lower() != _SCHEME:
            continue
        # compare_digest, not ==: the comparison should not leak how many
        # leading characters of the secret were right.
        if hmac.compare_digest(presented.strip(), token):
            return True
    return False


def _unauthenticated(handler: grpc.RpcMethodHandler) -> grpc.RpcMethodHandler:
    """Wrap ``handler`` in a refusal that keeps its call cardinality.

    gRPC dispatches on the handler's cardinality, so a refusal must be published
    with the same shape as the method it replaces — otherwise a unary call
    against a stream method (or the reverse) fails with an internal error that
    looks like a bug in the servicer rather than an authentication refusal.
    """

    async def abort(request_or_iterator, context, *args, **kwargs):
        await context.abort(
            grpc.StatusCode.UNAUTHENTICATED,
            "core rpc credential required",
        )

    if handler.unary_unary:
        return grpc.unary_unary_rpc_method_handler(abort)
    if handler.unary_stream:
        return grpc.unary_stream_rpc_method_handler(abort)
    if handler.stream_unary:
        return grpc.stream_unary_rpc_method_handler(abort)
    if handler.stream_stream:
        return grpc.stream_stream_rpc_method_handler(abort)
    # Unreachable for a real registration; returning the original handler would
    # silently restore the unauthenticated surface, so refuse instead.
    return grpc.unary_unary_rpc_method_handler(abort)


class InternalAuthInterceptor(grpc.aio.ServerInterceptor):
    """Require a shared credential on every inbound RPC."""

    def __init__(self, token: str):
        self._token = token

    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        if handler is None:
            # No such method: let gRPC answer UNIMPLEMENTED. Refusing here would
            # also work, but it would tell an unauthenticated caller which
            # methods exist by the shape of the error.
            return None
        if _credential_matches(self._token, handler_call_details.invocation_metadata):
            return handler
        metadata = handler_call_details.invocation_metadata or ()
        presented = [key for key, _ in metadata if key.lower() == AUTH_METADATA_KEY]
        # Never log the value: a caller that presented the *wrong* credential
        # may well have presented a real one for some other system, and this
        # line is read on the failure path. Presence is what the operator needs.
        logger.warning(
            "refused unauthenticated core rpc %s (%d metadata keys, %s credential header)",
            handler_call_details.method,
            len(metadata),
            "a" if presented else "no",
        )
        return _unauthenticated(handler)


def build_interceptors(token: str | None) -> list:
    """Interceptors to install for ``token``; empty when unset.

    Callers must log the "unset" case themselves — this function deliberately
    does not warn, so a library import cannot spam every test run.
    """
    normalized = configured_token(token)
    if not normalized:
        return []
    return [InternalAuthInterceptor(normalized)]
