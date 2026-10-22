"""`/forward`, `/rules`, one-line creation and the per-task status screen.

`/forward` answers with the owner-scoped task list; an optional integer argument
keeps the historical "set every task's run window" behaviour, `add` creates one
task in a single line, and `status [rule]` opens the forwarding console. Each
task row opens a status screen that folds the rule state, its counters and the
durable queue counts into one view (:class:`core.application.forward_status`),
with enable / disable acting on the same ownership guard every surface uses.

The one-line form is presentation only: this module parses the arguments,
resolves the peers with the same resolver the wizard uses, and then hands a
fully resolved request to :class:`core.application.quick_add.QuickAddService`.
Whether a task may exist — label, ownership, duplicates, filters — is decided
there, so an API endpoint or Mini App creates the same task without copying any
of this module.

Mounting is one method so the whole surface can be moved, tested and audited as
a unit instead of hiding in the panel's `_register`. The status callback ids
(`fwd_st:`, `fwd_on:`, `fwd_off:`) are short for Telegram's 64-byte limit.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from core.application.forward_status import ForwardingStatus, last_forward_text
from core.application.quick_add import (
    QuickAddError,
    QuickAddRequest,
    QuickAddService,
)
from core.application.repositories import ConcurrencyError
from core.domain.value_objects import ChannelReference
from core.infrastructure.telegram.errors import tg_detail
from core.infrastructure.telegram.handlers.forward_manage import (
    BULK_VERBS,
    DELETE_VERBS,
    EDIT_VERBS,
    LIST_VERBS,
)
from core.infrastructure.telegram.quick_add import QuickAddParseError, parse_quick_add
from core.infrastructure.telegram.route_lists import (
    STATUS_RESOLVED,
    join_ids,
    short_id,
    validate_resolved_uniqueness,
)
from core.infrastructure.telegram.ui_state import UiState

logger = logging.getLogger(__name__)

#: `/forward add` parse/service failure -> the catalog key that explains it.
#: One table, so a new failure code cannot ship without a message.
QUICK_ADD_ERROR_KEYS = {
    "missing_arguments": "ui_forward_add_usage",
    "not_add": "ui_forward_add_usage",
    "missing_arrow": "ui_forward_add_need_arrow",
    "missing_label": "ui_forward_add_bad_label",
    "bad_label": "ui_forward_add_bad_label",
    "empty_sources": "ui_route_err_empty_sources",
    "empty_targets": "ui_route_err_empty_targets",
    "unknown_filter": "ui_forward_add_bad_filter",
    "bad_filter_value": "ui_forward_add_bad_filter",
    "duplicate_filter": "ui_forward_add_bad_filter",
    "bad_peer": "ui_forward_add_bad_peer",
    "duplicate_label": "ui_forward_add_duplicate",
    "overlap": "ui_forward_add_overlap",
    "no_session": "ui_no_sessions",
    "no_owner": "cmd_failed",
}


class ForwardingHandlers:
    """Task-list commands, one-line creation and the forwarding status screen."""

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
                await self._handle_forward_command(message, uid)
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
    # /forward: list, edit window, add, or status                        #
    # ------------------------------------------------------------------ #

    def _quick_add_service(self) -> QuickAddService:
        """The application service, built lazily (tests skip __init__)."""
        service = self.__dict__.get("_quick_add")
        if service is None:
            service = QuickAddService(self._rules, self._sessions)
            self._quick_add = service
        return service

    async def _handle_forward_command(self, message: Message, uid: int) -> None:
        """Own every `/forward` spelling: `<seconds>`, `add ...`, `status`, or none."""
        lang = self._resolve_lang(uid)
        parts = (message.text or "").strip().split(None, 1)
        args = parts[1].strip() if len(parts) > 1 else ""
        first_arg = (args.split(None, 1) or [""])[0].lower()
        if first_arg == "add":
            await self._handle_forward_add(message, uid, lang)
            return
        # Management verbs live in `forward_manage.py`; routing them here keeps
        # one `/forward` entry point and one help text.
        if first_arg in LIST_VERBS:
            rest = args.split(None, 1)
            query = rest[1].strip() if len(rest) > 1 else ""
            return await self._handle_forward_list(message, uid, lang, query)
        if first_arg in EDIT_VERBS:
            rest = args.split(None, 1)
            return await self._handle_forward_edit(
                message, uid, lang, rest[1].strip() if len(rest) > 1 else "")
        if first_arg in DELETE_VERBS:
            rest = args.split(None, 1)
            return await self._handle_forward_delete(
                message, uid, lang, rest[1].strip() if len(rest) > 1 else "")
        if first_arg in BULK_VERBS:
            text, kbd = await self._render_forward_bulk(uid)
            return await message.reply_text(text, reply_markup=kbd)
        if first_arg in {"status", "st"}:
            rest = args.split(None, 1)
            await self._handle_forward_status(message, uid, lang, rest[1].strip() if len(rest) > 1 else "")
            return
        if args.isdigit():
            secs = int(args)
            rules = await self._rules.list_by_owner(uid) if uid else []
            for rule in rules:
                rule.max_time_edit = secs
                await self._save_owned_rule(rule, uid)
            await message.reply_text(self._t("ui_mte_global_set", lang=lang, secs=secs))
            return

        rules = await self._rules.list_by_owner(uid) if uid else []
        if not rules:
            await message.reply_text(
                self._t("ui_forward_cmd_no_rules", lang=lang),
                reply_markup=self._kbd([
                    [(self._t("ui_rule_add_new", lang=lang), "r")],
                ]),
            )
            return
        text = self._t("ui_forward_cmd_prompt", lang=lang)
        hint = self._t("ui_forward_add_hint", lang=lang)
        _, kbd = self._render_rules_list(rules, page=0)
        await message.reply_text(text + "\n\n" + hint, reply_markup=kbd)

    async def _handle_forward_status(self, message: Message, uid: int, lang: str, token: str = "") -> None:
        """/forward status [task] — one task, or the list to pick one from."""
        if token:
            rule = await self._cmd_rule_for(uid, token)
            if rule is None:
                await message.reply_text(self._t("ui_fwd_not_found", lang=lang))
                return
            status = await self._forward_status_use_case().for_rule(
                rule.id, None if self._is_admin(uid) else uid
            )
            if status is None:
                await message.reply_text(self._t("ui_fwd_not_found", lang=lang))
                return
            text, kbd = self._render_forwarding_status(status, lang=lang)
            await message.reply_text(text, reply_markup=kbd)
            return

        rules = await self._rules.list_by_owner(uid) if uid else []
        if not rules:
            await message.reply_text(self._t("ui_forward_cmd_no_rules", lang=lang))
            return
        text, kbd = self._render_rules_list(rules, page=0)
        await message.reply_text(self._t("ui_fwd_pick", lang=lang), reply_markup=kbd)

    async def _handle_forward_add(self, message: Message, uid: int, lang: str = "") -> None:
        """`/forward add LABEL SRC -> DST [--flags]` — resolve, create, report."""
        lang = lang or self._resolve_lang(uid)
        try:
            cmd = parse_quick_add(message.text or "")
        except QuickAddParseError as exc:
            key = QUICK_ADD_ERROR_KEYS.get(exc.code, "ui_forward_add_usage")
            await message.reply_text(
                self._t(key, lang=lang, flag=exc.detail or "",
                        label=exc.detail or "", id=exc.detail or ""),
            )
            return

        st = self._state(uid) if uid else UiState()
        src_ids, src_names, src_status = await self._route_resolve_all(uid, st, cmd.sources)
        tgt_ids, tgt_names, tgt_status = await self._route_resolve_all(uid, st, cmd.targets)
        pairs = list(zip(cmd.sources, src_status)) + list(zip(cmd.targets, tgt_status))
        failed = [token for token, status in pairs if status != STATUS_RESOLVED]
        if failed:
            if len(failed) == 1:
                note = self._t("ui_route_resolve_failed", lang=lang, id=failed[0])
            else:
                note = self._t("ui_route_summary_fail", lang=lang, ok=len(pairs) - len(failed),
                               total=len(pairs), id=failed[0])
            await message.reply_text(note)
            return
        problems = validate_resolved_uniqueness(src_ids, tgt_ids)
        if problems:
            await message.reply_text(self._route_problem_text(problems, lang))
            return

        request = QuickAddRequest(
            label=cmd.label,
            sources=[ChannelReference(chat_id=cid, title=title)
                     for cid, title in zip(src_ids, src_names)],
            targets=[ChannelReference(chat_id=cid, title=title)
                     for cid, title in zip(tgt_ids, tgt_names)],
            filters=cmd.filters,
        )
        try:
            rule = await self._quick_add_service().create(uid, request)
        except QuickAddError as exc:
            key = QUICK_ADD_ERROR_KEYS.get(exc.code, "ui_forward_add_usage")
            await message.reply_text(
                self._t(key, lang=lang, flag=exc.detail or "",
                        label=exc.detail or cmd.label, id=exc.detail or ""),
            )
            return
        except ConcurrencyError:
            await message.reply_text(self._t("ui_concurrency_error", lang=lang))
            return
        except Exception as exc:
            await self._record_error("bot", exc, user_id=uid)
            await message.reply_text(
                self._t("ui_route_failed", lang=lang, error=tg_detail(exc)),
            )
            return

        logger.info(
            "[PIPELINE:QUICK_ADD] uid=%s task=%s sources=%s targets=%s filters=%s",
            uid, short_id(rule.id), len(rule.source_chat_ids),
            len(rule.target_chat_ids), len(cmd.filters.tokens()),
        )
        filters_text = ", ".join(cmd.filters.tokens()) or self._t(
            "ui_forward_add_no_filters", lang=lang)
        body = self._t(
            "ui_forward_add_created",
            lang=lang,
            task=rule.name,
            rid=short_id(rule.id),
            srcs=join_ids(rule.source_chat_ids),
            tgts=join_ids(rule.target_chat_ids),
            filters=filters_text,
        )
        await message.reply_text(
            body,
            reply_markup=self._kbd([
                [(self._t("ui_rule_adv_settings", lang=lang), f"rd:{rule.id}")],
                [(self._t("ui_rule_view_all", lang=lang), "r")],
            ]),
        )

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
