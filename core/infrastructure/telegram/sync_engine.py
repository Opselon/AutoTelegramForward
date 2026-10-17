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
import time
from typing import Any, List, Optional

from ...application.repositories import IFilterRuleRepository, IForwardRuleRepository, IMessageMapRepository
from ...domain.entities import DeliveryStatus, ForwardRule, MessageMapping, MessagePayload
from ...domain.services import FilterAction, FilterEngine

logger = logging.getLogger("atf.sync_engine")


class SyncEngine:
    """Propagates message edits and deletions to destination chats via message_map."""

    def __init__(
        self,
        message_map_repo: IMessageMapRepository,
        rule_repo: IForwardRuleRepository,
        client_pool: Any,
        filter_repo: Optional[IFilterRuleRepository] = None,
    ) -> None:
        self._map = message_map_repo
        self._rules = rule_repo
        self._pool = client_pool
        self._filters = filter_repo
        self._filter_engine = FilterEngine()

    async def has_mapping(self, source_chat_id: Any, source_message_id: int) -> bool:
        """Whether this message was already delivered by any rule.

        The dispatcher asks this to decide what an edit means: a mapped
        message is updated in place and must never be posted again; an unmapped
        one was never delivered, so forwarding it is a first delivery rather
        than a duplicate.
        """
        try:
            mappings = await self._map.get_by_source(str(source_chat_id), int(source_message_id))
        except Exception:
            logger.debug("message_map lookup failed for %s:%s", source_chat_id, source_message_id)
            return False
        return bool(mappings)

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
        now = int(time.time())

        for m in mappings:
            if m.delivery_status != DeliveryStatus.SENT or not m.target_message_id:
                continue

            rule = await self._rules.get_by_id(m.rule_id)
            if not rule or not rule.is_active:
                continue

            if rule.ignore_edits:
                logger.debug("Rule %s has ignore_edits=True, skipping edit sync", rule.id[:8])
                continue

            # One authority: the panel's "Message Edit" switch (ignore_edits)
            # with an explicit legacy sync_edits override. Consulting the bare
            # sync_edits flag made every panel "Sync" task skip the in-place
            # update and fall through to a duplicate re-post.
            if not getattr(rule, "sync_edits_effective", getattr(rule, "sync_edits", False)):
                logger.debug("Rule %s has edit sync disabled, skipping edit", rule.id[:8])
                continue

            # 1. max_time_edit check: ignore edits if older than max_time_edit
            msg_date = getattr(payload, "date", 0) or getattr(payload, "edit_date", 0)
            max_edit = getattr(rule, "max_time_edit", 86400)
            if msg_date and max_edit > 0:
                age = now - int(msg_date)
                if age > max_edit:
                    logger.info(
                        "[PIPELINE:SYNC_EDIT_TIME_EXCEEDED] Rule %s edit age %ds exceeds max_time_edit %ds",
                        rule.id[:8], age, max_edit,
                    )
                    continue

            # 2. Whitelist / Blacklist & Filters check on edit
            if self._filters is not None and getattr(rule, "filter_rule_id", None):
                f_rule = await self._filters.get_by_id(str(rule.filter_rule_id))
                if f_rule is not None:
                    eval_res = self._filter_engine.evaluate(payload, f_rule)
                    if eval_res.action == FilterAction.DROP:
                        logger.info(
                            "[PIPELINE:SYNC_EDIT_FILTER_DROP] Rule %s dropped edited msg %s: %s",
                            rule.id[:8], payload.message_id, eval_res.reason,
                        )
                        continue

            # 3. Rule-level link filter
            if getattr(rule, "block_links", False):
                eff = payload.effective_text or ""
                if "http://" in eff.lower() or "https://" in eff.lower() or "t.me/" in eff.lower():
                    logger.info(
                        "[PIPELINE:SYNC_EDIT_FILTER_DROP] Rule %s dropped edited msg %s: link_blocked",
                        rule.id[:8], payload.message_id,
                    )
                    continue

            client = self._pool.get(rule.session_id)
            if client is None:
                logger.warning("[PIPELINE:SYNC_EDIT_NO_CLIENT] No live client for session %s to sync edit", rule.session_id)
                continue

            target = int(m.target_chat_id) if m.target_chat_id.lstrip("-").isdigit() else m.target_chat_id
            target_msg_id = int(m.target_message_id)

            final_text = self._filter_engine.transform_text(
                formatted_text or payload.effective_text or "", rule, payload=payload
            )

            try:
                if payload.has_media:
                    await client.edit_message_caption(
                        chat_id=target,
                        message_id=target_msg_id,
                        caption=final_text or None,
                    )
                else:
                    await client.edit_message_text(
                        chat_id=target,
                        message_id=target_msg_id,
                        text=final_text or "...",
                    )
                synced_count += 1
                logger.info(
                    "[PIPELINE:SYNC_EDIT] Synced edit rule=%s src=%s:%s -> tgt=%s:%s",
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
                    logger.debug("[PIPELINE:SYNC_EDIT_DELETED] Target message %s:%s was deleted, cannot edit", target, target_msg_id)
                else:
                    logger.warning(
                        "[PIPELINE:SYNC_EDIT_FAIL] Edit sync failed rule=%s tgt=%s:%s: %s",
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
            if mappings:
                deleted_count += await self._delete_mapped(mappings, str(source_chat_id), int(msg_id))
        return deleted_count

    async def handle_delete_without_chat(self, deleted_message_ids: List[int]) -> int:
        """Delete-sync when Telegram did not say which chat the message was in.

        ``updateDeleteMessages`` (private chats and basic groups) carries message
        ids only, so pyrogram hands the handler ``Message(chat=None)``. Dropping
        that update left the task panel's "Delete Sync" switch dead for every
        non-channel source. The message map still knows the id, so resolve it
        there — but only when the id maps to a single source chat. Two monitored
        chats can hold the same message id, and removing the wrong copy is worse
        than missing a delete, so an ambiguous id is skipped and logged.
        """
        deleted_count = 0
        for msg_id in deleted_message_ids:
            mappings = await self._map.list_by_source_message_id(int(msg_id))
            if not mappings:
                continue
            chats = {str(m.source_chat_id) for m in mappings}
            if len(chats) > 1:
                logger.warning(
                    "[PIPELINE:SYNC_DELETE_AMBIGUOUS] msg id %s maps to %d source chats (%s); "
                    "skipping delete sync",
                    msg_id,
                    len(chats),
                    ", ".join(sorted(chats)[:4]),
                )
                continue
            deleted_count += await self._delete_mapped(mappings, chats.pop(), int(msg_id))
        return deleted_count

    async def _delete_mapped(self, mappings: List[MessageMapping], source_chat_id: str, msg_id: int) -> int:
        """Remove the delivered copies of one source message. Returns the count.

        Shared by both delete paths so chat-known and chat-less updates can never
        drift apart on the rule gate (active + ``sync_deletes``) or on the
        already-deleted case.
        """
        deleted_count = 0
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
                logger.warning(
                    "[PIPELINE:SYNC_DELETE_NO_CLIENT] rule=%s has no live client; copy %s:%s left behind",
                    rule.id[:8],
                    m.target_chat_id,
                    m.target_message_id,
                )
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
                    "[PIPELINE:SYNC_DELETE] Synced delete rule=%s src=%s:%s -> tgt=%s:%s",
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
                        "[PIPELINE:SYNC_DELETE_FAIL] Delete sync failed rule=%s tgt=%s:%s: %s",
                        rule.id[:8],
                        target,
                        target_msg_id,
                        exc,
                    )

        return deleted_count

    async def handle_reply(self, payload: MessagePayload) -> int:
        """Propagate a reply made in the source chat onto the mapped target.

        v2.0 app update — "Reply" feature: when a user replies to a forwarded
        message in the source chat, the same reply text is posted in the target
        chat as a reply *to the mapped target message*, so the conversation
        context survives the crossing.

        Returns the number of target messages replied to.
        """
        replied_id = getattr(payload, "reply_to_message_id", None)
        if not replied_id:
            return 0

        mappings = await self._map.get_by_source(str(payload.chat_id), int(replied_id))
        if not mappings:
            return 0

        text = (payload.text or payload.caption or "").strip()
        if not text:
            return 0

        replied_count = 0
        for m in mappings:
            if m.delivery_status != DeliveryStatus.SENT or not m.target_message_id:
                continue

            rule = await self._rules.get_by_id(m.rule_id)
            if not rule or not rule.is_active:
                continue
            if not getattr(rule, "sync_replies", False):
                continue

            client = self._pool.get(rule.session_id)
            if client is None:
                continue

            target = int(m.target_chat_id) if m.target_chat_id.lstrip("-").isdigit() else m.target_chat_id

            try:
                sent = await client.send_message(
                    chat_id=target,
                    text=text,
                    reply_to_message_id=int(m.target_message_id),
                )
                if sent:
                    replied_count += 1
                    logger.info(
                        "[PIPELINE:SYNC_REPLY] rule=%s src=%s:%s -> tgt=%s replied to %s",
                        rule.id[:8], payload.chat_id, payload.message_id, target,
                        getattr(sent, "id", None) or getattr(sent, "message_id", None),
                    )
            except Exception as exc:
                logger.warning(
                    "[PIPELINE:SYNC_REPLY_FAIL] Reply sync failed rule=%s tgt=%s:%s: %s",
                    rule.id[:8], target, m.target_message_id, exc,
                )

        return replied_count
