"""Sync Engine for edit and delete propagation using persistent message_map.

Invariants:
- Edit propagation applies ONLY to previously delivered messages mapped to active rules with sync_edits=True.
- Delete propagation applies ONLY to previously delivered messages mapped to active rules with sync_deletes=True.
- Safe idempotent operations: MessageNotModified and already-deleted messages are caught cleanly without crashing.
- Multi-target and album messages are individually resolved per destination chat.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

from ...application.repositories import IForwardRuleRepository, IMessageMapRepository
from ...domain.entities import DeliveryStatus, ForwardRule, MessagePayload

logger = logging.getLogger("atf.sync_engine")


class SyncEngine:
    """Propagates message edits and deletions to destination chats via message_map."""

    def __init__(
        self,
        message_map_repo: IMessageMapRepository,
        rule_repo: IForwardRuleRepository,
        client_pool: Any,
    ) -> None:
        self._map = message_map_repo
        self._rules = rule_repo
        self._pool = client_pool

    async def handle_edit(self, payload: MessagePayload, formatted_text: str) -> int:
        """Propagate an edit from source message to all mapped target messages.

        Returns the number of target messages successfully updated.
        """
        mappings = await self._map.get_by_source(str(payload.chat_id), int(payload.message_id))
        if not mappings:
            logger.debug(
                "No message_map entry found for edited message %s:%s",
                payload.chat_id,
                payload.message_id,
            )
            return 0

        synced_count = 0
        for m in mappings:
            if m.delivery_status != DeliveryStatus.SENT or not m.target_message_id:
                continue

            rule = await self._rules.get_by_id(m.rule_id)
            if not rule or not rule.is_active:
                continue

            if rule.ignore_edits:
                logger.debug("Rule %s has ignore_edits=True, skipping edit sync", rule.id[:8])
                continue

            if not getattr(rule, "sync_edits", True):
                continue

            client = self._pool.get(rule.session_id)
            if client is None:
                logger.warning("No live client for session %s to sync edit", rule.session_id)
                continue

            target = int(m.target_chat_id) if m.target_chat_id.lstrip("-").isdigit() else m.target_chat_id
            target_msg_id = int(m.target_message_id)

            try:
                if payload.has_media:
                    await client.edit_message_caption(
                        chat_id=target,
                        message_id=target_msg_id,
                        caption=formatted_text or None,
                    )
                else:
                    await client.edit_message_text(
                        chat_id=target,
                        message_id=target_msg_id,
                        text=formatted_text or "...",
                    )
                synced_count += 1
                logger.info(
                    "Synced edit rule=%s src=%s:%s -> tgt=%s:%s",
                    rule.id[:8],
                    payload.chat_id,
                    payload.message_id,
                    target,
                    target_msg_id,
                )
            except Exception as exc:
                err_str = str(exc)
                if "MessageNotModified" in err_str:
                    # Idempotent: text is already identical
                    synced_count += 1
                elif "MessageIdInvalid" in err_str or "MESSAGE_ID_INVALID" in err_str:
                    logger.debug("Target message %s:%s was deleted, cannot edit", target, target_msg_id)
                else:
                    logger.warning(
                        "Edit sync failed rule=%s tgt=%s:%s: %s",
                        rule.id[:8],
                        target,
                        target_msg_id,
                        exc,
                    )

        return synced_count

    async def handle_delete(self, source_chat_id: str, deleted_message_ids: List[int]) -> int:
        """Propagate deletions from source chat to all mapped target messages.

        Returns the number of target messages successfully deleted.
        """
        deleted_count = 0
        for msg_id in deleted_message_ids:
            mappings = await self._map.get_by_source(str(source_chat_id), int(msg_id))
            if not mappings:
                continue

            for m in mappings:
                if not m.target_message_id:
                    continue

                rule = await self._rules.get_by_id(m.rule_id)
                if not rule or not rule.is_active:
                    continue

                if not getattr(rule, "sync_deletes", False):
                    continue

                client = self._pool.get(rule.session_id)
                if client is None:
                    continue

                target = int(m.target_chat_id) if m.target_chat_id.lstrip("-").isdigit() else m.target_chat_id
                target_msg_id = int(m.target_message_id)

                try:
                    await client.delete_messages(
                        chat_id=target,
                        message_ids=[target_msg_id],
                    )
                    deleted_count += 1
                    logger.info(
                        "Synced delete rule=%s src=%s:%s -> tgt=%s:%s",
                        rule.id[:8],
                        source_chat_id,
                        msg_id,
                        target,
                        target_msg_id,
                    )
                except Exception as exc:
                    err_str = str(exc)
                    if "MessageIdInvalid" in err_str or "MESSAGE_ID_INVALID" in err_str:
                        # Already deleted
                        deleted_count += 1
                    else:
                        logger.warning(
                            "Delete sync failed rule=%s tgt=%s:%s: %s",
                            rule.id[:8],
                            target,
                            target_msg_id,
                            exc,
                        )

        return deleted_count
