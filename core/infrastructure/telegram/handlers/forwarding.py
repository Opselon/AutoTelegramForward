"""`/forward` and `/rules`: task creation, the task list and forwarding controls.

`/forward` answers with the owner-scoped task list; an optional integer argument
keeps the historical "set every task's run window" behaviour, and the `add`
sub-command creates one task in a single line. `/rules` is the same list without
the prompt.

The one-line form is presentation only: this module parses the arguments,
resolves the peers with the same resolver the wizard uses, and then hands a
fully resolved request to :class:`core.application.quick_add.QuickAddService`.
Whether a task may exist — label, ownership, duplicates, filters — is decided
there, so an API endpoint or Mini App creates the same task without copying any
of this module.

Mounting is one method so the whole surface can be moved, tested and audited as
a unit instead of hiding in the panel's `_register`.
"""

from __future__ import annotations

import logging

from pyrogram import filters
from pyrogram.types import Message

from core.application.quick_add import (
    QuickAddError,
    QuickAddRequest,
    QuickAddService,
)
from core.application.repositories import ConcurrencyError
from core.domain.value_objects import ChannelReference
from core.infrastructure.telegram.errors import tg_detail
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
    """Task-list commands, one-line creation and (later) the status screen."""

    #: The registry calls this to mount the module's handlers.
    forwarding_registration = "register_forwarding_handlers"

    def register_forwarding_handlers(self, b) -> None:
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
    # /forward: list, edit window, or add                                 #
    # ------------------------------------------------------------------ #

    def _quick_add_service(self) -> QuickAddService:
        """The application service, built lazily (tests skip __init__)."""
        service = self.__dict__.get("_quick_add")
        if service is None:
            service = QuickAddService(self._rules, self._sessions)
            self._quick_add = service
        return service

    async def _handle_forward_command(self, message: Message, uid: int) -> None:
        """Own every `/forward` spelling: `<seconds>`, `add ...`, or no args."""
        lang = self._resolve_lang(uid)
        parts = (message.text or "").strip().split(None, 1)
        args = parts[1].strip() if len(parts) > 1 else ""
        first_arg = (args.split(None, 1) or [""])[0].lower()
        if first_arg == "add":
            await self._handle_forward_add(message, uid, lang)
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
