"""`/forward` and `/rules`: the task list and the per-task forwarding controls.

`/forward` answers with the owner-scoped task list; an optional integer argument
keeps the historical "set every task's run window" behaviour. `/rules` is the
same list without the prompt. Mounting is one method so the whole surface can be
moved, tested and audited as a unit instead of hiding in the panel's `_register`.
"""

from __future__ import annotations

import logging

from pyrogram import filters
from pyrogram.types import Message

logger = logging.getLogger(__name__)


class ForwardingHandlers:
    """Task-list commands and (later) the forwarding status screen."""

    #: The registry calls this to mount the module's handlers.
    forwarding_registration = "register_forwarding_handlers"

    def register_forwarding_handlers(self, b) -> None:
        @b.on_message(filters.command("forward") & filters.private)
        @self._localized_handler
        async def _forward_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("[PIPELINE:CMD] /forward uid=%s", uid)
            try:
                parts = (message.text or "").strip().split()
                if len(parts) > 1 and parts[1].isdigit():
                    secs = int(parts[1])
                    rules = await self._rules.list_by_owner(uid) if uid else []
                    for r in rules:
                        r.max_time_edit = secs
                        await self._save_owned_rule(r, uid)
                    await message.reply_text(self._t("ui_mte_global_set", secs=secs))
                    return

                rules = await self._rules.list_by_owner(uid) if uid else []
                if not rules:
                    await message.reply_text(self._t("ui_forward_cmd_no_rules"))
                    return

                _, kbd = self._render_rules_list(rules, page=0)
                await message.reply_text(self._t("ui_forward_cmd_prompt"), reply_markup=kbd)
            except Exception as exc:
                logger.exception("[PIPELINE:CMD] /forward failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("rules") & filters.private)
        @self._localized_handler
        async def _rules_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("[PIPELINE:CMD] /rules uid=%s", uid)
            try:
                rules = await self._rules.list_by_owner(uid) if uid else []
                text, kbd = self._render_rules_list(rules, page=0)
                await message.reply_text(text, reply_markup=kbd)
            except Exception as exc:
                logger.exception("[PIPELINE:CMD] /rules failed for uid=%s: %s", uid, exc)
