"""Message dispatcher: bridges pyrogram events into the forwarding pipeline."""

import logging
from typing import Awaitable, Callable

from ...application.use_cases import MessageForwardingUseCase
from ...domain.entities import MessagePayload
from ...domain.value_objects import ForwardMode
from .client_pool import ClientPool, payload_from_pyrogram

logger = logging.getLogger(__name__)


class MessageDispatcher:
    """Registers handlers on live clients and executes sends per rule."""

    def __init__(self, pool: ClientPool, use_case: MessageForwardingUseCase) -> None:
        self._pool = pool
        self._use_case = use_case
        # The pipeline's sender port is this class's `_send` coroutine.
        self._use_case.sender = self._send

    def register_handler(self, filter_cb=None) -> Callable:
        """Returns a pyrogram-compatible async handler."""

        async def _handler(client, message):
            try:
                payload: MessagePayload = payload_from_pyrogram(message)
                await self._use_case.process_message(payload)
            except Exception:
                logger.exception("Failed to process incoming message")

        return _handler

    async def _send(self, payload: MessagePayload, rule, text: str, evaluation) -> bool:
        client = self._pool.get(rule.session_id)
        if client is None:
            logger.warning("No live client for session %s", rule.session_id)
            return False
        target = rule.target_chat_id
        try:
            if rule.forward_mode == ForwardMode.DIRECT_FORWARD:
                await client.forward_messages(
                    chat_id=target, from_chat_id=int(payload.chat_id),
                    message_ids=[payload.message_id],
                )
            else:
                if payload.has_media:
                    await client.copy_message(
                        chat_id=target, from_chat_id=int(payload.chat_id),
                        message_id=payload.message_id,
                        caption=text or rule.custom_caption_template or None,
                    )
                else:
                    await client.send_message(chat_id=target, text=text)
            return True
        except Exception:
            logger.exception("Send to %s failed", target)
            return False
