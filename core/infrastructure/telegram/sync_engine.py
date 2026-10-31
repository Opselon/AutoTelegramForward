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
import re
import time
from typing import Any, List, Optional

from ...application.repositories import IFilterRuleRepository, IForwardRuleRepository, IMessageMapRepository
from ...domain.entities import DeliveryStatus, ForwardRule, MessageMapping, MessagePayload
from ...domain.services import FilterAction, FilterEngine

logger = logging.getLogger("atf.sync_engine")


class SyncEngine:
    """Propagates message edits and deletions to destination chats via message_map."""

    # Class-level defaults so an engine built field-by-field (``SyncEngine.__new__``
    # in tests, or a partially-initialised subclass) still has the retry bounds
    # instead of raising AttributeError on first use.
    _jobs: Any = None
    _edit_max_attempts: int = 3
    _filter_engine: Any = None
    _sleep: Any = None
    _flood_wait_max: float = 10.0
    _flood_attempts: int = 2

    def __init__(
        self,
        message_map_repo: IMessageMapRepository,
        rule_repo: IForwardRuleRepository,
        client_pool: Any,
        filter_repo: Optional[IFilterRuleRepository] = None,
        flood_wait_max: float = 10.0,
        flood_wait_attempts: int = 2,
        jobs_repo: Any = None,
        edit_max_attempts: int = 3,
    ) -> None:
        self._map = message_map_repo
        self._rules = rule_repo
        self._pool = client_pool
        self._filters = filter_repo
        self._filter_engine = FilterEngine()
        # Telegram throttles edits, deletes and sends per chat. One bounded
        # retry keeps a burst from dropping the copy silently; a wait longer
        # than the cap is reported instead of blocking the handler.
        self._flood_wait_max = float(flood_wait_max)
        self._flood_attempts = max(1, int(flood_wait_attempts))
        self._sleep = asyncio.sleep
        # The delivery queue, used to fold an edit into a job that has not run
        # yet (E2E-3). Optional and None by default so older constructors keep
        # working; the fold is simply skipped then.
        self._jobs = jobs_repo
        # Bounded in-call retry for a transient edit failure (E2E-4).
        self._edit_max_attempts = max(1, int(edit_max_attempts))

    @staticmethod
    def flood_wait_seconds(exc: BaseException) -> Optional[float]:
        """Seconds Telegram asked us to wait, or None when it is not a flood wait."""
        for attr in ("value", "retry_after", "x"):
            raw = getattr(exc, attr, None)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return float(raw)
        match = re.search(r"FLOOD_WAIT_?(\d+)", str(exc))
        return float(match.group(1)) if match else None

    async def _throttled(self, factory, *, what: str, rule_id: str, target_msg_id: Any):
        """Run one Telegram call, retrying once inside the flood-wait cap."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await factory()
            except Exception as exc:
                wait = self.flood_wait_seconds(exc)
                if wait is None or wait > self._flood_wait_max or attempt >= self._flood_attempts:
                    raise
                logger.info(
                    "[PIPELINE:SYNC_FLOOD_RETRY] %s rule=%s tgt=%s waiting %.0fs (attempt %d)",
                    what, str(rule_id)[:8], target_msg_id, wait, attempt,
                )
                await self._sleep(wait)

    async def has_mapping(self, source_chat_id: Any, source_message_id: int) -> bool:
        """Whether this message was already delivered by any rule.

        The dispatcher asks this to decide what an edit means: a mapped
        message is updated in place and must never be posted again; an unmapped
        one was never delivered, so forwarding it is a first delivery rather
        than a duplicate.

        Fails **closed**: when the mapping lookup itself errors, the edit is
        claimed rather than released. Releasing it let the pipeline re-post an
        edit as a brand new message, which is the one outcome the feature
        promises never to produce; a claimed edit that also fails to update
        leaves the copy stale and is visible in the log instead.
        """
        try:
            mappings = await self._map.get_by_source(str(source_chat_id), int(source_message_id))
        except Exception:
            logger.warning(
                "[PIPELINE:SYNC_PROBE_FAILED] message_map lookup failed for %s:%s; "
                "treating it as already delivered so the edit cannot be re-posted",
                source_chat_id, source_message_id,
            )
            return True
        return bool(mappings)

    async def handle_edit(self, payload: MessagePayload, formatted_text: str) -> int:
        """Propagate an edit from source message to all mapped target messages.

        Returns the number of target messages successfully updated.

        A mapping still ``PENDING`` (its delivery job has not run yet) is not
        skipped: the edit is folded into the job's payload so the *first* post
        already carries the new text. Skipping it made the destination keep the
        stale text forever — audit finding E2E-3.
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
            if m.delivery_status == DeliveryStatus.SENT and m.target_message_id:
                synced_count += await self._sync_sent_edit(m, payload, formatted_text, now)
                continue

            # Not yet delivered: fold the edit into the pending job instead of
            # dropping it (E2E-3). The job then posts the edited text on its
            # first attempt; the mapping is not touched, so the delivery
            # lifecycle still owns it.
            await self._fold_edit_into_pending_job(m, payload)

        return synced_count

    async def _fold_edit_into_pending_job(self, mapping, payload: MessagePayload) -> bool:
        """Write an edit's text into the pending delivery job for a mapping.

        Audit finding E2E-3: an edit arriving while the initial delivery was
        still PENDING was dropped by ``handle_edit`` (only SENT mappings are
        updated in place), so the destination posted the *old* text a moment
        later and no further update could ever reach it.

        The edit is spliced into the job payload: the worker sends from the
        payload, so the first post carries the new text. Only the job's own
        delivery fields are replaced — routing and identity keys are left alone
        so the fence, the mapping and retries still key on the original event.
        """
        try:
            updated = await self._jobs.fold_edit(
                rule_id=str(mapping.rule_id),
                source_chat_id=str(mapping.source_chat_id),
                source_message_id=int(mapping.source_message_id),
                target_chat_id=str(mapping.target_chat_id),
                text=payload.effective_text or "",
                caption=payload.effective_text or "",
                is_edit=True,
                edit_date=int(getattr(payload, "edit_date", 0) or 0),
            )
        except Exception as exc:
            logger.warning(
                "[PIPELINE:SYNC_EDIT_FOLD_FAILED] rule=%s src=%s:%s tgt=%s: %s",
                str(mapping.rule_id)[:8], mapping.source_chat_id, mapping.source_message_id,
                mapping.target_chat_id, exc,
            )
            return False
        if updated:
            logger.info(
                "[PIPELINE:SYNC_EDIT_FOLDED] rule=%s src=%s:%s tgt=%s edit folded into the "
                "pending delivery job so the first post carries the new text",
                str(mapping.rule_id)[:8], mapping.source_chat_id, mapping.source_message_id,
                mapping.target_chat_id,
            )
        return bool(updated)

    async def _sync_sent_edit(self, mapping, payload: MessagePayload, formatted_text: str, now: int) -> int:
        """Update one already-delivered mapping in place, with bounded retry.

        Audit finding E2E-4: a failed edit was only logged and never retried,
        so a transient Telegram error permanently drifted the source and the
        destination. A short bounded retry inside the sync call recovers the
        common transient case without introducing a new queue.
        """
        m = mapping
        rule = await self._rules.get_by_id(m.rule_id)
        if not rule or not rule.is_active:
            return 0

        if rule.ignore_edits:
            logger.debug("Rule %s has ignore_edits=True, skipping edit sync", rule.id[:8])
            return 0

        # One authority: the panel's "Message Edit" switch (ignore_edits)
        # with an explicit legacy sync_edits override. Consulting the bare
        # sync_edits flag made every panel "Sync" task skip the in-place
        # update and fall through to a duplicate re-post.
        if not getattr(rule, "sync_edits_effective", getattr(rule, "sync_edits", False)):
            logger.debug("Rule %s has edit sync disabled, skipping edit", rule.id[:8])
            return 0

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
                return 0

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
                    return 0

        # 3. Rule-level link filter
        if getattr(rule, "block_links", False):
            eff = payload.effective_text or ""
            if "http://" in eff.lower() or "https://" in eff.lower() or "t.me/" in eff.lower():
                logger.info(
                    "[PIPELINE:SYNC_EDIT_FILTER_DROP] Rule %s dropped edited msg %s: link_blocked",
                    rule.id[:8], payload.message_id,
                )
                return 0

        client = self._pool.get(rule.session_id)
        if client is None:
            logger.warning("[PIPELINE:SYNC_EDIT_NO_CLIENT] No live client for session %s to sync edit", rule.session_id)
            return 0

        target = int(m.target_chat_id) if m.target_chat_id.lstrip("-").isdigit() else m.target_chat_id
        target_msg_id = int(m.target_message_id)

        final_text = self._filter_engine.transform_text(
            formatted_text or payload.effective_text or "", rule, payload=payload
        )

        # E2E-4: a transient failure here used to be logged once and never
        # retried, permanently drifting source and destination. Two extra
        # attempts with a short backoff recover the common case (a momentary
        # FloodWait or a connection blip) without a new queue.
        for attempt in range(1, self._edit_max_attempts + 1):
            try:
                if payload.has_media:
                    await self._throttled(
                        lambda: client.edit_message_caption(
                            chat_id=target,
                            message_id=target_msg_id,
                            caption=final_text or None,
                        ),
                        what="edit", rule_id=rule.id, target_msg_id=target_msg_id,
                    )
                else:
                    await self._throttled(
                        lambda: client.edit_message_text(
                            chat_id=target,
                            message_id=target_msg_id,
                            text=final_text or "...",
                        ),
                        what="edit", rule_id=rule.id, target_msg_id=target_msg_id,
                    )
                logger.info(
                    "[PIPELINE:SYNC_EDIT] Synced edit rule=%s src=%s:%s -> tgt=%s:%s",
                    rule.id[:8],
                    payload.chat_id,
                    payload.message_id,
                    target,
                    target_msg_id,
                )
                return 1
            except Exception as exc:
                err_str = str(exc)
                if "MessageNotModified" in err_str:
                    # Idempotent: text is already identical
                    return 1
                if "MessageIdInvalid" in err_str or "MESSAGE_ID_INVALID" in err_str:
                    logger.debug("[PIPELINE:SYNC_EDIT_DELETED] Target message %s:%s was deleted, cannot edit", target, target_msg_id)
                    return 0
                if attempt < self._edit_max_attempts and self._retryable_edit_error(exc):
                    logger.warning(
                        "[PIPELINE:SYNC_EDIT_RETRY] Edit sync failed (attempt %d/%d) rule=%s tgt=%s:%s: %s",
                        attempt, self._edit_max_attempts, rule.id[:8], target, target_msg_id, exc,
                    )
                    await self._sleep(self._edit_backoff(attempt))
                    continue
                logger.warning(
                    "[PIPELINE:SYNC_EDIT_FAIL] Edit sync failed rule=%s tgt=%s:%s: %s",
                    rule.id[:8],
                    target,
                    target_msg_id,
                    exc,
                )
                return 0
        return 0

    @staticmethod
    def _retryable_edit_error(exc: Exception) -> bool:
        """Transient edit failures are worth another attempt; permanent ones are not.

        FloodWait and network errors pass. A permission error, an invalid
        message, or content that Telegram rejects will not fix itself, so
        retrying only burns time before the same failure.
        """
        text = str(exc)
        if "ChatAdminRequired" in text or "CHAT_ADMIN_REQUIRED" in text:
            return False
        if "MessageIdInvalid" in text or "MESSAGE_ID_INVALID" in text:
            return False
        if "MessageNotModified" in text:
            return False
        if "USER_BANNED_IN_CHANNEL" in text or "until" in text.lower() and "flood" not in text.lower():
            return False
        return True

    @staticmethod
    def _edit_backoff(attempt: int) -> float:
        """Bounded backoff with jitter: 1s, 3s, then capped at 5s."""
        import random
        base = min(1.0 * (2 ** (attempt - 1)), 5.0)
        return base + random.uniform(0, 0.5)

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
                return 0
            chats = {str(m.source_chat_id) for m in mappings}
            if len(chats) > 1:
                logger.warning(
                    "[PIPELINE:SYNC_DELETE_AMBIGUOUS] msg id %s maps to %d source chats (%s); "
                    "skipping delete sync",
                    msg_id,
                    len(chats),
                    ", ".join(sorted(chats)[:4]),
                )
                return 0
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
                await self._throttled(
                    lambda: client.delete_messages(
                        chat_id=target,
                        message_ids=[target_msg_id],
                    ),
                    what="delete", rule_id=rule.id, target_msg_id=target_msg_id,
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

        Returns the number of target copies this reply is responsible for
        (posted now, or delivered by an earlier delivery of the same reply).
        The dispatcher uses a non-zero result as a claim on the event: the
        reply has been delivered, so it must not also be forwarded as a plain
        message, and a redelivery must not post it twice. The delivery is
        recorded in the message map, which is what makes the second delivery
        and the retry safe, and makes the reply itself editable/deletable like
        any other delivered copy.
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

        # Idempotency: a mapping keyed on this reply's own message id means an
        # earlier delivery (or a Telegram redelivery) already posted it.
        already: set = set()
        try:
            own = await self._map.get_by_source(str(payload.chat_id), int(payload.message_id))
            already = {(str(m.rule_id), str(m.target_chat_id)) for m in own}
        except Exception:
            logger.debug("No reply delivery record readable for %s:%s", payload.chat_id, payload.message_id)

        replied_count = 0
        for m in mappings:
            if m.delivery_status != DeliveryStatus.SENT or not m.target_message_id:
                continue

            rule = await self._rules.get_by_id(m.rule_id)
            if not rule or not rule.is_active:
                continue
            if not getattr(rule, "sync_replies", False):
                continue

            if (str(rule.id), str(m.target_chat_id)) in already:
                # Already delivered: this is a redelivery of the reply itself.
                # It counts as handled (so the dispatcher will not forward it
                # again) even though nothing is sent now — the claim is the
                # point, not the send.
                replied_count += 1
                logger.info(
                    "[PIPELINE:SYNC_REPLY_DUPLICATE] rule=%s reply %s:%s already delivered to %s",
                    rule.id[:8], payload.chat_id, payload.message_id, m.target_chat_id,
                )
                continue

            client = self._pool.get(rule.session_id)
            if client is None:
                return 0

            target = int(m.target_chat_id) if m.target_chat_id.lstrip("-").isdigit() else m.target_chat_id

            try:
                sent = await self._throttled(
                    lambda: client.send_message(
                        chat_id=target,
                        text=text,
                        reply_to_message_id=int(m.target_message_id),
                    ),
                    what="reply", rule_id=rule.id, target_msg_id=m.target_message_id,
                )
                sent_id = getattr(sent, "id", None) or getattr(sent, "message_id", None)
                if sent:
                    replied_count += 1
                    await self._record_reply(rule, payload, m, sent_id)
                    logger.info(
                        "[PIPELINE:SYNC_REPLY] rule=%s src=%s:%s -> tgt=%s replied to %s",
                        rule.id[:8], payload.chat_id, payload.message_id, target,
                        sent_id or m.target_message_id,
                    )
            except Exception as exc:
                logger.warning(
                    "[PIPELINE:SYNC_REPLY_FAIL] Reply sync failed rule=%s tgt=%s:%s: %s",
                    rule.id[:8], target, m.target_message_id, exc,
                )

        return replied_count

    async def _record_reply(self, rule: ForwardRule, payload: MessagePayload, parent: MessageMapping,
                            sent_message_id: Any) -> None:
        """Remember that this reply reached that target, so a retry cannot repeat it."""
        target_message_id: Optional[int] = None
        if sent_message_id is not None:
            try:
                target_message_id = int(sent_message_id)
            except (TypeError, ValueError):
                target_message_id = None
        try:
            await self._map.add_or_update(MessageMapping(
                rule_id=rule.id,
                source_chat_id=str(payload.chat_id),
                source_message_id=int(payload.message_id),
                target_chat_id=str(parent.target_chat_id),
                target_message_id=target_message_id,
                delivery_status=DeliveryStatus.SENT,
            ))
        except Exception:
            logger.warning(
                "[PIPELINE:SYNC_REPLY_MAP_FAIL] rule=%s could not record reply %s:%s (a redelivery may repeat it)",
                rule.id[:8], payload.chat_id, payload.message_id,
            )
