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
from typing import Any, Optional, Tuple

import grpc
from grpc.aio import ServerInterceptor

from telegram_runtime import errors
from telegram_runtime.app.session_manager import SessionManager
from telegram_runtime.adapters.pyrogram.client import PyrogramAdapter
from telegram_runtime.adapters.bot_api import BotApiAdapter, BotTokenUnavailable

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

    def __init__(self, sessions: SessionManager, media_dir: str, bots: Optional[BotApiAdapter] = None) -> None:
        self._sessions = sessions
        self._media_dir = media_dir
        # The Bot Api adapter is optional so a standalone/integration runtime
        # can serve sessions without the Core's token repository attached.
        # Every bot RPC reports a typed refusal when it is absent — never a
        # crash, and never a silent USER-transport substitution.
        self._bots = bots

    # -- helpers ----------------------------------------------------------- #

    def _adapter(self, session_id: str):
        """Resolve a live adapter, or (None, typed error)."""
        client, ferr = self._sessions.client_for(session_id)
        if ferr is not None:
            return None, ferr
        return PyrogramAdapter(client), None

    def _bots_or_refusal(self) -> Tuple[Optional[BotApiAdapter], Optional[pb.TelegramOperationError]]:
        """The Bot Api adapter, or a typed refusal when none is attached.

        A runtime without the token repository must answer bot RPCs with a
        typed error — the Go Worker treats an absent capability as a
        permanent refusal of that operation and decides the fallback itself.
        """
        if self._bots is None:
            return None, self._err(errors.failed(
                errors.TELEGRAM_PERMANENT_ERROR,
                "this runtime serves no bot transport (no token repository attached)",
                error_type="BOT_TRANSPORT_UNAVAILABLE",
            ))
        return self._bots, None

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

    # -- bot transport -------------------------------------------------------- #

    async def BotAvailability(self, request, context):
        bots, refusal = self._bots_or_refusal()
        if refusal is not None:
            return pb.BotAvailabilityResponse(error=refusal)
        target = request.bot
        ok, username = await bots.availability(
            target.bot_token_id, int(target.owner_user_id or 0),
        )
        return pb.BotAvailabilityResponse(ok=bool(ok), bot_username=username or "")

    async def BotSendText(self, request, context):
        bots, refusal = self._bots_or_refusal()
        if refusal is not None:
            return pb.SendOutcome(error=refusal)
        target = request.bot
        sent, ferr = await bots.send_text(
            target.bot_token_id, int(target.owner_user_id or 0),
            request.target_chat_id, request.text,
            thread_id=int(request.message_thread_id or 0),
            inline_buttons=list(request.inline_buttons),
        )
        if ferr is not None:
            return pb.SendOutcome(error=self._err(ferr))
        return pb.SendOutcome(target_message_id=int(sent))

    async def BotCopyMessage(self, request, context):
        bots, refusal = self._bots_or_refusal()
        if refusal is not None:
            return pb.SendOutcome(error=refusal)
        target = request.bot
        sent, ferr = await bots.copy_message(
            target.bot_token_id, int(target.owner_user_id or 0),
            request.target_chat_id, request.from_chat_id, request.message_id,
            caption=request.caption or None,
            thread_id=int(request.message_thread_id or 0),
            inline_buttons=list(request.inline_buttons),
        )
        if ferr is not None:
            return pb.SendOutcome(error=self._err(ferr))
        return pb.SendOutcome(target_message_id=int(sent))

    async def BotSendMedia(self, request, context):
        bots, refusal = self._bots_or_refusal()
        if refusal is not None:
            return pb.SendOutcome(error=refusal)
        target = request.bot
        sent, ferr = await bots.send_media(
            target.bot_token_id, int(target.owner_user_id or 0),
            request.target_chat_id, request.file_reference_token,
            request.caption, request.media_type,
            thread_id=int(request.message_thread_id or 0),
            inline_buttons=list(request.inline_buttons),
        )
        if ferr is not None:
            return pb.SendOutcome(error=self._err(ferr))
        return pb.SendOutcome(target_message_id=int(sent))

    async def BotDeleteMessages(self, request, context):
        bots, refusal = self._bots_or_refusal()
        if refusal is not None:
            return pb.DeleteMessagesResponse(error=refusal)
        target = request.bot
        deleted, ferr = await bots.delete_messages(
            target.bot_token_id, int(target.owner_user_id or 0),
            request.chat_id, list(request.message_ids),
        )
        if ferr is not None:
            return pb.DeleteMessagesResponse(error=self._err(ferr))
        return pb.DeleteMessagesResponse(deleted=bool(deleted))

    # -- deletion (both transports) ------------------------------------------- //

    async def DeleteMessages(self, request, context):
        transport = str(request.transport or "user").strip().lower()
        ids = [int(i) for i in request.message_ids]
        if not ids:
            return pb.DeleteMessagesResponse(
                deleted=True,
                error=self._err(errors.failed(
                    errors.INTERNAL_ERROR, "no message ids supplied",
                    error_type="EMPTY_MESSAGE_IDS",
                )),
            )

        if transport == "bot":
            bots, refusal = self._bots_or_refusal()
            if refusal is not None:
                return pb.DeleteMessagesResponse(error=refusal)
            deleted, ferr = await bots.delete_messages(
                request.bot_token_id, int(request.owner_user_id or 0),
                request.chat_id, ids,
            )
            if ferr is not None:
                return pb.DeleteMessagesResponse(error=self._err(ferr))
            return pb.DeleteMessagesResponse(deleted=bool(deleted))

        # USER transport: through the session's MTProto client.
        adapter, ferr = self._adapter(request.session_id)
        if ferr is not None:
            return pb.DeleteMessagesResponse(error=self._err(ferr))
        ok, f = await adapter.delete_messages(request.chat_id, ids)
        if f is not None:
            return pb.DeleteMessagesResponse(error=self._err(f))
        return pb.DeleteMessagesResponse(deleted=bool(ok))

    # -- branding stage 1 ------------------------------------------------------ #

    async def BrandingStage1(self, request, context):
        """post the processed deliverable into channel C.

        The content decision (text / copy / media_group) was made by Go;
        this only executes the Telegram side effect and reports the ids that
        landed. The transformed text/caption travels verbatim — the runtime
        never re-runs a transform.
        """
        transport = str(request.transport or "user").strip().lower()
        operation = str(request.operation or "").strip().lower()
        ids: list = []

        if operation == "text":
            body = str(request.text or "").strip()
            if not body:
                return pb.BrandingStage1Response(error=self._err(errors.failed(
                    errors.INTERNAL_ERROR, "branding stage 1 text is empty",
                    error_type="EMPTY_TEXT",
                )))
            if transport == "bot":
                bots, refusal = self._bots_or_refusal()
                if refusal is not None:
                    return pb.BrandingStage1Response(error=refusal)
                sent, ferr = await bots.send_text(
                    request.bot_token_id, int(request.owner_user_id or 0),
                    request.target_chat_id, body,
                    thread_id=int(request.message_thread_id or 0),
                    inline_buttons=list(request.inline_buttons),
                )
                if ferr is not None:
                    return pb.BrandingStage1Response(error=self._err(ferr))
                if sent:
                    ids = [int(sent)]
            else:
                adapter, ferr = self._adapter(request.session_id)
                if ferr is not None:
                    return pb.BrandingStage1Response(error=self._err(ferr))
                sent, f = await adapter.send_message(
                    request.target_chat_id, body, int(request.message_thread_id or 0),
                )
                if f is not None:
                    return pb.BrandingStage1Response(error=self._err(f))
                if sent:
                    ids = [int(sent)]

        elif operation == "copy":
            if transport == "bot":
                bots, refusal = self._bots_or_refusal()
                if refusal is not None:
                    return pb.BrandingStage1Response(error=refusal)
                sent, ferr = await bots.copy_message(
                    request.bot_token_id, int(request.owner_user_id or 0),
                    request.target_chat_id, request.source_chat_id,
                    request.source_message_id,
                    caption=request.caption or None,
                    thread_id=int(request.message_thread_id or 0),
                    inline_buttons=list(request.inline_buttons),
                )
                if ferr is not None:
                    return pb.BrandingStage1Response(error=self._err(ferr))
                if sent:
                    ids = [int(sent)]
            else:
                adapter, ferr = self._adapter(request.session_id)
                if ferr is not None:
                    return pb.BrandingStage1Response(error=self._err(ferr))
                sent, f = await adapter.copy_message(
                    request.target_chat_id, request.source_chat_id,
                    request.source_message_id,
                    request.caption or None, int(request.message_thread_id or 0),
                )
                if f is not None:
                    return pb.BrandingStage1Response(error=self._err(f))
                if sent:
                    ids = [int(sent)]

        elif operation == "media_group":
            # The album must stay grouped: copying only its first message
            # would turn a grouped post into a single-item post for the
            # target. The Bot Api has no album-preserving equivalent here,
            # so the bot transport reports the typed refusal and Go decides
            # the fallback (its own policy, never a hidden one).
            if transport == "bot":
                return pb.BrandingStage1Response(error=self._err(errors.failed(
                    errors.TELEGRAM_PERMANENT_ERROR,
                    "media group delivery is not available on the bot route",
                    error_type="BOT_METHOD_INVALID",
                )))
            members = [int(i) for i in request.source_message_ids]
            if not members:
                members = [int(request.source_message_id)]
            adapter, ferr = self._adapter(request.session_id)
            if ferr is not None:
                return pb.BrandingStage1Response(error=self._err(ferr))
            sent_ids, f = await adapter.copy_media_group(
                request.target_chat_id, request.source_chat_id, members[0],
                request.caption or None, int(request.message_thread_id or 0),
            )
            if f is not None:
                return pb.BrandingStage1Response(error=self._err(f))
            ids = [int(i) for i in sent_ids or []]

        else:
            return pb.BrandingStage1Response(error=self._err(errors.failed(
                errors.INTERNAL_ERROR,
                f"unknown branding stage 1 operation: {request.operation!r}",
                error_type="UNKNOWN_OPERATION",
            )))

        return pb.BrandingStage1Response(target_message_ids=ids)

    # -- health -------------------------------------------------------------- //

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

def build_server(
    sessions: SessionManager,
    addr: str,
    token: str,
    media_dir: str,
    bots: Optional[BotApiAdapter] = None,
) -> grpc.aio.Server:
    """Construct the runtime's gRPC server.

    ``token`` must be non-empty; the caller (main) refuses to start otherwise.
    ``bots`` is optional: a runtime without the Core's token repository serves
    sessions and answers bot RPCs with a typed refusal — the Go Worker treats
    that as a permanent refusal of the bot transport and decides its own
    fallback, never a silent identity switch.
    """
    interceptors = [_AuthInterceptor(token)] if token else []
    server = grpc.aio.server(interceptors=interceptors)
    pb_grpc.add_TelegramGatewayServicer_to_server(
        TelegramGatewayServicer(sessions, media_dir, bots=bots),
        server,
    )
    bound = server.add_insecure_port(addr)
    if bound == 0:
        raise RuntimeError(f"telegram runtime could not bind {addr}")
    logger.info("telegram runtime grpc listening on %s (contract %s)", addr, CONTRACT_TAG)
    return server


async def serve(
    sessions: SessionManager,
    addr: str,
    token: str,
    media_dir: str,
    bots: Optional[BotApiAdapter] = None,
) -> grpc.aio.Server:
    """Start the runtime server. Returns the started server (await
    ``server.wait_for_termination()``)."""
    server = build_server(sessions, addr, token, media_dir, bots=bots)
    await server.start()
    return server
