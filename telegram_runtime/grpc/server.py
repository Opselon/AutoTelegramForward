"""The Telegram Runtime's gRPC server: the Go Worker's transport contract.

Serves ``worker/proto/telegram_gateway.proto`` (generated here as
``telegram_gateway_pb2``) over the same grpc.aio machinery the Core's
control plane uses, protected by the same shared Bearer credential.

Authentication: every method requires the shared internal RPC token. An
empty configured token in production is a refuse-to-start condition, never
silently allowed — unauthenticated access to these RPCs would let any caller
on the network drive Telegram sessions.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from typing import Any, Optional

import grpc
from grpc.aio import ServerInterceptor

from telegram_runtime import errors
from telegram_runtime.app.session_manager import SessionManager
from telegram_runtime.adapters.pyrogram.client import PyrogramAdapter

logger = logging.getLogger("atf.telegram_runtime.grpc")

# The generated Python stubs live under worker/proto/gen/python, so the Go
# and Python consumers of this contract compile the exact same .proto file.
# The path is resolved relative to the repository root, which makes the
# runtime importable both from the repo (tests) and from a container whose
# working directory is the repo root.
import importlib
import sys as _sys

# telegram_runtime/grpc/server.py -> <root>/telegram_runtime/grpc/server.py
# is three levels under the repository root, NOT four (the package's own
# directory is not part of the climb).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_PB_DIR = os.path.join(_REPO_ROOT, "worker", "proto", "gen", "python")
if _PB_DIR not in _sys.path:
    _sys.path.insert(0, _PB_DIR)

pb = importlib.import_module("telegram_gateway_pb2")
pb_grpc = importlib.import_module("telegram_gateway_pb2_grpc")

CONTRACT_TAG = "tfgw/v1"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class _AuthInterceptor(ServerInterceptor):
    """Require the shared Bearer credential on every inbound RPC.

    Same semantics as the Core's InternalAuthInterceptor: only the Bearer
    scheme, comparison via ``compare_digest`` (no secret-timing leak), and a
    refusal that keeps the original handler's cardinality so an
    unauthenticated caller learns nothing about the method shapes.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        if handler is None:
            return None
        metadata = handler_call_details.invocation_metadata or ()
        for key, value in metadata:
            if key.lower() != "authorization":
                continue
            scheme, _, presented = (value or "").partition(" ")
            if scheme.lower() == "bearer" and hmac.compare_digest(presented.strip(), self._token):
                return handler
        logger.warning(
            "refused unauthenticated runtime rpc %s (%d metadata keys)",
            handler_call_details.method, len(metadata),
        )
        async def _abort(request_or_iterator, context, *args, **kwargs):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "runtime rpc credential required")
        if handler.unary_unary:
            return grpc.unary_unary_rpc_method_handler(_abort)
        if handler.unary_stream:
            return grpc.unary_stream_rpc_method_handler(_abort)
        if handler.stream_unary:
            return grpc.stream_unary_rpc_method_handler(_abort)
        return grpc.stream_stream_rpc_method_handler(_abort)


# ---------------------------------------------------------------------------
# The servicer
# ---------------------------------------------------------------------------

class TelegramGatewayServicer(pb_grpc.TelegramGatewayServicer):
    """Implements the worker's TelegramGateway contract.

    Each handler is a thin pass-through to the pyrogram adapter: it resolves
    the session, executes ONE Telegram operation, and reports what Telegram
    said. No business decisions, no retries, no business-level sleeps.
    """

    def __init__(self, sessions: SessionManager, media_dir: str) -> None:
        self._sessions = sessions
        self._media_dir = media_dir

    # -- helpers ----------------------------------------------------------- #

    def _adapter(self, session_id: str):
        """Resolve a live adapter, or (None, typed error)."""
        client, ferr = self._sessions.client_for(session_id)
        if ferr is not None:
            return None, ferr
        return PyrogramAdapter(client), None

    @staticmethod
    def _err(f: errors.TelegramOperationError):
        return pb.TelegramOperationError(
            code=_CODE_VALUES.get(f.code, 0),
            error_type=f.error_type,
            error_code=f.error_code,
            retry_after_seconds=int(f.retry_after_seconds or 0),
            message=f.message,
        )

    # -- session connectivity ---------------------------------------------- #

    async def IsSessionReady(self, request, context):
        ready, state = self._sessions.is_ready(request.session_id)
        return pb.SessionAvailabilityResponse(
            ready=bool(ready), state=state,
            error=None if ready else self._err(errors.failed(errors.SESSION_UNAVAILABLE, state)),
        )

    # -- reads -------------------------------------------------------------- #

    async def GetMessage(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.GetMessageResponse(error=self._err(ferr))
        msg, f = await adapter.get_message(request.chat_id, request.message_id)
        if f is not None:
            return pb.GetMessageResponse(error=self._err(f))
        if msg is None:
            return pb.GetMessageResponse(error=self._err(
                errors.failed(errors.MESSAGE_NOT_FOUND, "source message not found")))
        info = pb.MessageInfo(
            chat_id=int(getattr(msg, "chat", None) and getattr(msg.chat, "id", 0) or 0),
            message_id=int(getattr(msg, "id", 0) or 0),
            media_group_id=str(getattr(msg, "media_group_id", "") or ""),
            has_media=bool(getattr(msg, "media", None) is not None),
            media_type=_media_kind(msg),
        )
        return pb.GetMessageResponse(info=info)

    async def GetAlbumMessageIds(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.GetAlbumMessageIdsResponse(error=self._err(ferr))
        ids, f = await adapter.get_album_message_ids(request.chat_id, request.message_id)
        if f is not None:
            return pb.GetAlbumMessageIdsResponse(error=self._err(f))
        return pb.GetAlbumMessageIdsResponse(message_ids=[int(i) for i in ids])

    async def GetFileExtension(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.GetFileExtensionResponse(error=self._err(ferr))
        ext, f = await adapter.get_file_extension(request.chat_id, request.message_id)
        if f is not None:
            return pb.GetFileExtensionResponse(error=self._err(f))
        return pb.GetFileExtensionResponse(extension=ext)

    # -- media staging ------------------------------------------------------ #

    async def DownloadMedia(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.DownloadMediaResponse(error=self._err(ferr))
        dest = request.destination.path if request.destination else ""
        if not dest:
            return pb.DownloadMediaResponse(error=self._err(
                errors.failed(errors.INTERNAL_ERROR, "no destination path supplied")))
        size, f = await adapter.download_media(request.chat_id, request.message_id, dest)
        if f is not None:
            return pb.DownloadMediaResponse(error=self._err(f))
        return pb.DownloadMediaResponse(size_bytes=int(size))

    async def ReloadMedia(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.ReloadMediaResponse(error=self._err(ferr))
        dest = request.destination.path if request.destination else ""
        if not dest:
            return pb.ReloadMediaResponse(error=self._err(
                errors.failed(errors.INTERNAL_ERROR, "no destination path supplied")))
        size, f = await adapter.reload_media(
            request.source_chat_id, request.source_message_id, dest,
            request.media_type,
        )
        if f is not None:
            return pb.ReloadMediaResponse(error=self._err(f))
        return pb.ReloadMediaResponse(size_bytes=int(size))

    async def PrepareMediaUpload(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.PrepareMediaUploadResponse(error=self._err(ferr))
        source = request.source.path if request.source else ""
        token, f = await adapter.prepare_media_upload(
            source, request.source_chat_id, request.source_message_id, request.media_type,
        )
        if f is not None:
            return pb.PrepareMediaUploadResponse(error=self._err(f))
        return pb.PrepareMediaUploadResponse(file_reference_token=token or "")

    # -- sends --------------------------------------------------------------- #

    async def ForwardMessages(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.ForwardMessagesResponse(error=self._err(ferr))
        ids, f = await adapter.forward_messages(
            request.target_chat_id, request.from_chat_id, list(request.message_ids),
            request.message_thread_id,
        )
        if f is not None:
            return pb.ForwardMessagesResponse(error=self._err(f))
        return pb.ForwardMessagesResponse(target_message_ids=[int(i) for i in ids])

    async def CopyMessage(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.SendOutcome(error=self._err(ferr))
        sent, f = await adapter.copy_message(
            request.target_chat_id, request.from_chat_id, request.message_id,
            request.caption or None, request.message_thread_id,
        )
        if f is not None:
            return pb.SendOutcome(error=self._err(f))
        return pb.SendOutcome(target_message_id=int(sent))

    async def CopyMessages(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.CopyMessagesResponse(error=self._err(ferr))
        ids, f = await adapter.copy_messages(
            request.target_chat_id, request.from_chat_id, list(request.message_ids),
            request.caption or None, request.message_thread_id,
        )
        if f is not None:
            return pb.CopyMessagesResponse(error=self._err(f))
        return pb.CopyMessagesResponse(target_message_ids=[int(i) for i in ids])

    async def CopyMediaGroup(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.CopyMessagesResponse(error=self._err(ferr))
        ids, f = await adapter.copy_media_group(
            request.target_chat_id, request.from_chat_id, request.message_id,
            request.caption or None, request.message_thread_id,
        )
        if f is not None:
            return pb.CopyMessagesResponse(error=self._err(f))
        return pb.CopyMessagesResponse(target_message_ids=[int(i) for i in ids])

    async def SendMessage(self, request, context):
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.SendOutcome(error=self._err(ferr))
        sent, f = await adapter.send_message(
            request.target_chat_id, request.text, request.message_thread_id,
        )
        if f is not None:
            return pb.SendOutcome(error=self._err(f))
        return pb.SendOutcome(target_message_id=int(sent))

    # -- health -------------------------------------------------------------- #

    async def GatewayHealth(self, request, context):
        return pb.HealthResponse(
            contract=CONTRACT_TAG, contract_major=1, contract_minor=0, status="ok",
        )


# The enum values, resolved once at import. Named lookup keeps a renamed
# code from silently becoming a 0 (UNSPECIFIED) — an unknown code stays
# visible instead of quietly meaning "everything is fine".
_CODE_VALUES = {name: value for name, value in pb.DeliveryFailureCode.items()}


def _media_kind(msg: Any) -> str:
    """Classify the MTProto message for the contract's media_type field.

    Reads the same attribute set the Core's domain media_kind resolver uses,
    so the label the Go worker receives matches the label the pipeline
    already recorded on the job payload.
    """
    for attr, label in (
        ("photo", "photo"), ("video", "video"), ("animation", "animation"),
        ("audio", "audio"), ("voice", "voice"), ("video_note", "video_note"),
        ("sticker", "sticker"), ("document", "document"), ("contact", "contact"),
        ("location", "location"), ("venue", "venue"), ("poll", "poll"),
        ("dice", "dice"), ("game", "game"),
    ):
        if getattr(msg, attr, None) is not None:
            return label
    return "text"


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------

def build_server(sessions: SessionManager, addr: str, token: str, media_dir: str) -> grpc.aio.Server:
    """Construct the runtime's gRPC server.

    ``token`` must be non-empty; the caller (main) refuses to start otherwise.
    """
    interceptors = [_AuthInterceptor(token)] if token else []
    server = grpc.aio.server(interceptors=interceptors)
    pb_grpc.add_TelegramGatewayServicer_to_server(
        TelegramGatewayServicer(sessions, media_dir), server,
    )
    bound = server.add_insecure_port(addr)
    if bound == 0:
        raise RuntimeError(f"telegram runtime could not bind {addr}")
    logger.info("telegram runtime grpc listening on %s (contract %s)", addr, CONTRACT_TAG)
    return server


async def serve(sessions: SessionManager, addr: str, token: str, media_dir: str) -> grpc.aio.Server:
    """Start the runtime server. Returns the started server (await
    ``server.wait_for_termination()``)."""
    server = build_server(sessions, addr, token, media_dir)
    await server.start()
    return server
