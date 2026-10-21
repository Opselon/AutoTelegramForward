"""`/forward`, `/rules` and the per-task forwarding status screen.

`/forward` answers with the owner-scoped task list, or — with an integer
argument — keeps the historical "set every task's run window" behaviour. Each
task row opens a status screen that folds the rule state, its counters and the
durable queue counts into one view (`ForwardingStatusUseCase`), with enable /
disable acting on the same ownership guard every other surface uses.

Slash commands are registered by the module so the surface moves as a unit; the
status callback id (`fwd_st:`) is short for Telegram's 64-byte callback limit.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from core.application.forward_status import ForwardingStatus, last_forward_text

logger = logging.getLogger(__name__)


class ForwardingHandlers:
    """Task-list commands and the forwarding status surface."""

    #: The registry calls this to mount the module's handlers.
    forwarding_registration = "register_forwarding_handlers"

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    def register_forwarding_handlers(self, b) -> None:
        # The status screen is part of this surface; mount its callbacks with
        # the commands so one module is one registration.
        self.register_forwarding_status_handlers(b)

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

                if len(parts) > 1 and parts[1].lower() in {"status", "st"}:
                    lang = self._resolve_lang(uid)
                    if len(parts) > 2:
                        rule = await self._cmd_rule_for(uid, parts[2])
                        if rule is None:
                            return await message.reply_text(
                                self._t("ui_fwd_not_found", lang=lang)
                            )
                        status = await self._forward_status_use_case().for_rule(
                            rule.id, None if self._is_admin(uid) else uid
                        )
                        if status is None:
                            return await message.reply_text(
                                self._t("ui_fwd_not_found", lang=lang)
                            )
                        text, kbd = self._render_forwarding_status(status, lang=lang)
                        return await message.reply_text(text, reply_markup=kbd)

                    rules = await self._rules.list_by_owner(uid) if uid else []
                    if not rules:
                        return await message.reply_text(self._t("ui_forward_cmd_no_rules"))
                    text, kbd = self._render_rules_list(rules, page=0)
                    return await message.reply_text(
                        self._t("ui_fwd_pick", lang=lang), reply_markup=kbd
                    )

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

    # ------------------------------------------------------------------ #
    # Forwarding status
    # ------------------------------------------------------------------ #
    def _forward_status_use_case(self):
        """Build the status use case from the panel's owned dependencies."""
        from core.application.forward_status import ForwardingStatusUseCase

        return ForwardingStatusUseCase(
            rules=self._rules,
            rule_stats=self._rule_stats,
            delivery_queue=self._delivery_queue,
        )

    #: A status screen reuses the rule-list labels rather than duplicating them:
    #: there is one translation of "enabled/disabled/paused" in the catalog.
    _STATE_KEYS = {
        "running": "ui_rule_status_on",
        "disabled": "ui_rule_status_off",
        "paused": "ui_rule_status_paused",
        "paused_until": "ui_rule_status_paused_until",
    }

    def _state_label(self, status: ForwardingStatus, lang: str) -> str:
        key = self._STATE_KEYS.get(status.state_key, "ui_rule_status_paused")
        if status.state == "paused_until":
            return self._t(key, lang=lang, until=last_forward_text(status.paused_until))
        return self._t(key, lang=lang)

    def _render_forwarding_status(
        self,
        status: ForwardingStatus,
        lang: Optional[str] = None,
        can_modify: bool = True,
    ) -> Tuple[str, InlineKeyboardMarkup]:
        cur_lang = lang or "en"
        name = status.rule_id[:8]
        lines: List[str] = [
            self._t("ui_fwd_status_title", lang=cur_lang, name=name),
            self._t("ui_fwd_state", lang=cur_lang, state=self._state_label(status, cur_lang)),
            "",
            self._t(
                "ui_fwd_counters",
                lang=cur_lang,
                forwarded=status.forwarded,
                filtered=status.filtered,
                errors=status.errors,
            ),
        ]
        if status.queue:
            lines.append(
                self._t(
                    "ui_fwd_queue",
                    lang=cur_lang,
                    pending=status.pending,
                    retrying=status.retrying,
                    sent=status.sent,
                    failed=status.failed,
                )
            )
        if status.last_forward_ts:
            lines.append(
                self._t("ui_fwd_last", lang=cur_lang, when=last_forward_text(status.last_forward_ts))
            )
        else:
            lines.append(self._t("ui_fwd_never", lang=cur_lang))

        rows: List[List[Tuple[str, str]]] = []
        if can_modify:
            switch = "fwd_off" if status.is_running else "fwd_on"
            label = self._t("ui_fwd_disable", lang=cur_lang) if status.is_running \
                else self._t("ui_fwd_enable", lang=cur_lang)
            rows.append([(label, f"{switch}:{status.rule_id}")])
        rows.append([
            (self._t("ui_fwd_open", lang=cur_lang), f"rd:{status.rule_id}"),
            (self._t("ui_fwd_refresh", lang=cur_lang), f"fwd_st:{status.rule_id}"),
        ])
        rows.append([(self._btn("ui_back", lang=cur_lang), "r")])
        return "\n".join(lines), self._kbd(rows)

    def register_forwarding_status_handlers(self, b) -> None:
        @b.on_callback_query(filters.regex(r"^fwd_st:(.+)$"))
        @self._localized_handler
        async def _fwd_status(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            rule_id = cq.matches[0].group(1)
            lang = self._resolve_lang(uid)
            status = await self._forward_status_use_case().for_rule(
                rule_id, None if self._is_admin(uid) else uid
            )
            if status is None:
                return await cq.answer(self._t("ui_fwd_not_found", lang=lang), show_alert=True)
            text, kbd = self._render_forwarding_status(status, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^fwd_(on|off):(.+)$"))
        @self._localized_handler
        async def _fwd_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            which = cq.matches[0].group(1)
            rule_id = cq.matches[0].group(2)
            lang = self._resolve_lang(uid)
            # "on"/"off" are the button's intent, never a language tag.
            want_active = which == "on"
            scope = None if self._is_admin(uid) else uid
            rule = await self._owned_rule(rule_id, uid)
            if rule is None:
                return await cq.answer(self._t("ui_fwd_not_found", lang=lang), show_alert=True)

            if bool(getattr(rule, "is_active", False)) != want_active:
                # One enforcement point: the state machine, not a full-row write.
                # A stale screen (another device already moved the task) is
                # refused and answered with the current task, never a blind flip.
                outcome, fresh = await self._rules.set_status(
                    rule_id, rule.status_state(), self._scope_uid(uid)
                )
                if outcome == self._rules.STATUS_STALE:
                    current = await self._forward_status_use_case().for_rule(rule_id, scope)
                    if current is not None:
                        text, kbd = self._render_forwarding_status(current, lang=lang)
                        await self._safe_edit(cq, text, reply_markup=kbd)
                    return await cq.answer(
                        self._t("ui_concurrency_error", lang=lang), show_alert=True
                    )
                if outcome != self._rules.STATUS_OK:
                    return await cq.answer(self._t("ui_fwd_not_found", lang=lang), show_alert=True)
                try:
                    self._log.info("bot", "rule",
                                   f"rule {rule_id} {'enabled' if want_active else 'disabled'} by {uid}")
                except Exception:
                    pass

            status = await self._forward_status_use_case().for_rule(rule_id, scope)
            if status is None:
                return await cq.answer(self._t("ui_fwd_not_found", lang=lang), show_alert=True)
            text, kbd = self._render_forwarding_status(status, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_done", lang=lang))
