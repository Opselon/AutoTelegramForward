"""`/forward` management: list, find, edit, delete and bulk actions.

`handlers/forwarding.py` owns what a task *does* (one-line creation, the live
status console). This module owns what a user does *to* tasks once they exist,
and it exists as its own file so that neither surface keeps growing into the
other: both are mounted by the handler registry, both answer from the same
application services, and neither one owns a private copy of "find the task the
user meant".

Three rules shape the surface:

* **An id, never a position.** Every rule is addressed by its stable id (full,
  `#prefix`, or its name). Nothing here reads "2" as "the second task": list
  order follows execution priority and changes whenever a rule is edited.
* **One resolver.** :mod:`core.application.rule_selectors` turns a token into a
  rule and explains refusals with a stable code; the command, the list search
  and the bulk screen all go through it.
* **Bulk work is per item.** A batch reports each rule separately — applied,
  already in that state, gone, or failed — so one bad row never hides the rest.

Ownership is never re-implemented here: rules are read with
``list_by_owner``/``_owned_rule`` and written through ``set_status`` /
``delete_owned``, all of which re-check the owner against the stored row. A
foreign id is simply absent from the owner's list, so it answers ``not_found``
and never leaks another tenant's task name.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from core.application import bulk_ops
from core.application.rule_selectors import (
    AMBIGUOUS,
    EMPTY,
    NOT_FOUND,
    SelectorError,
    match_rule,
    resolve_rule,
    rule_label,
)
from core.infrastructure.telegram.route_lists import short_id

logger = logging.getLogger(__name__)

#: `/forward` subcommands this module answers. `add`/`status` stay in
#: `handlers/forwarding.py`; anything else falls through to the task list.
LIST_VERBS = ("list", "ls", "find", "search")
EDIT_VERBS = ("edit", "open")
DELETE_VERBS = ("delete", "del", "rm")
BULK_VERBS = ("bulk", "batch")

#: Reply rows per page. Five rows of two buttons keep the message under the
#: height a phone shows without scrolling.
PAGE_SIZE = 5

#: Where the batch selection lives inside the user's UI-state buffer.
BULK_KEY = "forward_bulk"

SELECTOR_ERROR_KEYS = {
    EMPTY: "ui_sel_empty",
    NOT_FOUND: "ui_sel_not_found",
    AMBIGUOUS: "ui_sel_ambiguous",
}

REPORT_ROW_KEYS = {
    bulk_ops.OK: "ui_bulk_row_ok",
    bulk_ops.UNCHANGED: "ui_bulk_row_unchanged",
    bulk_ops.MISSING: "ui_bulk_row_missing",
    bulk_ops.FAILED: "ui_bulk_row_failed",
}


class ForwardManageHandlers:
    """Task management commands and the bulk-selection screen."""

    forward_manage_registration = "register_forward_manage_handlers"

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def _fwd_bulk(self, uid: int) -> bulk_ops.Selection:
        """The caller's pending selection, pruned against live rules later."""
        st = self._state(uid)
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        return bulk_ops.Selection.from_buffer(st.buffer.get(BULK_KEY))

    async def _fwd_bulk_save(self, uid: int, selection: bulk_ops.Selection) -> None:
        st = self._state(uid)
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        st.buffer[BULK_KEY] = selection.to_buffer()
        persist = getattr(self, "_persist", None)
        if persist is not None:
            await persist(uid, st)

    # ------------------------------------------------------------------ #
    # Errors
    # ------------------------------------------------------------------ #
    def _selector_error(self, exc: SelectorError, lang: str) -> str:
        """Explain a refusal in the caller's language, never as a bare code."""
        key = SELECTOR_ERROR_KEYS.get(exc.code, "ui_sel_not_found")
        if exc.code == AMBIGUOUS and exc.candidates:
            return self._t(key, lang=lang, q=exc.detail,
                           candidates="\n".join(f"• {c}" for c in exc.candidates))
        return self._t(key, lang=lang, q=exc.detail or "", candidates="")

    async def _resolve_owned_task(self, uid: int, token: str, lang: str):
        """Owner-scoped resolution shared by edit, delete and bulk.

        Returns ``(rule, None)`` or ``(None, error_text)`` — no exceptions reach
        a handler, and a foreign id is indistinguishable from a missing one.
        """
        rules = await self._rules.list_by_owner(uid) if uid else []
        try:
            return resolve_rule(rules, token), None
        except SelectorError as exc:
            return None, self._selector_error(exc, lang)

    # ------------------------------------------------------------------ #
    # /forward list | find
    # ------------------------------------------------------------------ #
    async def _handle_forward_list(self, message: Message, uid: int, lang: str,
                                   query: str = "", page: int = 0) -> None:
        rules = await self._rules.list_by_owner(uid) if uid else []
        needle = (query or "").strip()
        if needle:
            rules = [rule for rule in rules if match_rule(rule, needle)]
        if not rules:
            await message.reply_text(
                self._t("ui_fwd_list_empty", lang=lang, q=needle) if needle
                else self._t("ui_forward_cmd_no_rules", lang=lang),
                reply_markup=self._kbd([[(self._t("ui_rule_add_new", lang=lang), "r")]]),
            )
            return
        text, kbd = self._render_forward_tasks(rules, lang=lang, page=page, query=needle)
        await message.reply_text(text, reply_markup=kbd)

    def _render_forward_tasks(self, rules: List[Any], *, lang: str, page: int = 0,
                              query: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        """One page of tasks, every row labelled with its stable short id."""
        total = len(rules)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        current = max(0, min(int(page), pages - 1))
        start = current * PAGE_SIZE
        lines = [self._t("ui_fwd_list_title", lang=lang, total=total)]
        if query:
            lines.append(self._t("ui_fwd_list_query", lang=lang, q=query, n=total))
        lines.append("")

        rows: List[List[Tuple[str, str]]] = []
        for rule in rules[start:start + PAGE_SIZE]:
            icon = "🟢" if getattr(rule, "is_active", False) else "🔴"
            name = (getattr(rule, "name", "") or getattr(rule, "source_chat_name", "") or "—")[:30]
            lines.append(f"• {icon} {name} · `#{short_id(rule.id)}`")
            rows.append([
                (f"{icon} {name}"[:40], f"rd:{rule.id}"),
                ("📊", f"fwd_st:{rule.id}"),
                ("🗑", f"rdel:{rule.id}"),
            ])

        if pages > 1:
            nav: List[Tuple[str, str]] = []
            if current > 0:
                nav.append((self._t("ui_prev", lang=lang), f"fml:p:{current - 1}"))
            nav.append((f"{current + 1}/{pages}", "noop"))
            if current < pages - 1:
                nav.append((self._t("ui_next", lang=lang), f"fml:p:{current + 1}"))
            rows.append(nav)
        rows.append([(self._t("ui_bulk_open", lang=lang), "fml:open")])
        lines.append("")
        lines.append(self._t("ui_fwd_list_hint", lang=lang))
        return "\n".join(lines), self._kbd(rows)

    # ------------------------------------------------------------------ #
    # /forward edit | delete
    # ------------------------------------------------------------------ #
    async def _handle_forward_edit(self, message: Message, uid: int, lang: str,
                                   token: str = "") -> None:
        if not (token or "").strip():
            await message.reply_text(self._t("ui_fwd_edit_usage", lang=lang))
            return await self._handle_forward_list(message, uid, lang)
        rule, error = await self._resolve_owned_task(uid, token, lang)
        if error:
            await message.reply_text(error)
            return await self._handle_forward_list(message, uid, lang)
        self._cmd_remember(uid, rule)
        state = self._t(
            "ui_rule_status_on" if getattr(rule, "is_active", False) else "ui_rule_status_off",
            lang=lang,
        )
        text = "\n".join([
            self._t("ui_fwd_edit_title", lang=lang,
                    name=getattr(rule, "name", "") or "—", rid=short_id(rule.id)),
            "",
            self._t("ui_fwd_edit_body", lang=lang,
                    srcs=", ".join(getattr(rule, "source_chat_ids", []) or []),
                    tgts=", ".join(getattr(rule, "target_chat_ids", []) or []),
                    state=state),
        ])
        await message.reply_text(
            text,
            reply_markup=self._kbd([
                [(self._t("ui_fwd_open", lang=lang), f"rd:{rule.id}")],
                [(self._t("ui_rule_edit_btn", lang=lang), f"re:{rule.id}"),
                 ("📊", f"fwd_st:{rule.id}")],
                [(self._t("ui_rule_btn_delete_task", lang=lang), f"rdel:{rule.id}"),
                 (self._t("ui_back", lang=lang), "r")],
            ]),
        )

    async def _handle_forward_delete(self, message: Message, uid: int, lang: str,
                                     token: str = "") -> None:
        if not (token or "").strip():
            await message.reply_text(self._t("ui_fwd_delete_usage", lang=lang))
            return await self._handle_forward_list(message, uid, lang)
        rule, error = await self._resolve_owned_task(uid, token, lang)
        if error:
            await message.reply_text(error)
            return await self._handle_forward_list(message, uid, lang)
        # The confirmation and the write stay with the panel's `rdel`/`rdelyes`
        # callbacks: one place performs deletion, with one ownership guard.
        text = self._t("ui_fwd_delete_confirm", lang=lang,
                       name=getattr(rule, "name", "") or "—", rid=short_id(rule.id))
        await message.reply_text(
            text,
            reply_markup=self._kbd([
                [(self._t("ui_confirm_yes_delete", lang=lang), f"rdel:{rule.id}")],
                [(self._t("ui_cancel_back", lang=lang), f"rd:{rule.id}")],
            ]),
        )

    # ------------------------------------------------------------------ #
    # Bulk
    # ------------------------------------------------------------------ #
    def _bulk_labels(self, rules: List[Any]) -> Dict[str, str]:
        return {str(getattr(rule, "id", "")): rule_label(rule) for rule in rules}

    def _bulk_selection(self, stored: bulk_ops.Selection, rules: List[Any]) -> bulk_ops.Selection:
        """Prune a stored selection against the owner's current rules."""
        live = self._bulk_labels(rules)
        if stored.expired():
            return stored.clear()
        return stored.prune(live.keys())

    async def _render_forward_bulk(self, uid: int, page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._resolve_lang(uid)
        rules = await self._rules.list_by_owner(uid) if uid else []
        selection = self._bulk_selection(self._fwd_bulk(uid), rules)
        await self._fwd_bulk_save(uid, selection)

        total = len(rules)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        current = max(0, min(int(page), pages - 1))
        start = current * PAGE_SIZE

        lines = [self._t("ui_bulk_title", lang=lang, sel=len(selection), total=total)]
        if total:
            lines.append(self._t("ui_bulk_hint", lang=lang, max=bulk_ops.MAX_SELECTION))
        lines.append("")

        rows: List[List[Tuple[str, str]]] = []
        for rule in rules[start:start + PAGE_SIZE]:
            rid = str(getattr(rule, "id", ""))
            mark = "✅" if rid in selection else "⬜"
            name = (getattr(rule, "name", "") or getattr(rule, "source_chat_name", "") or "—")[:28]
            label = f"{mark} {name} · #{short_id(rid)}"
            lines.append(label)
            rows.append([(label[:60], f"fml:t:{rid}")])

        if not total:
            lines.append(self._t("ui_forward_cmd_no_rules", lang=lang))

        if pages > 1:
            nav: List[Tuple[str, str]] = []
            if current > 0:
                nav.append((self._t("ui_prev", lang=lang), f"fml:p:{current - 1}"))
            nav.append((f"{current + 1}/{pages}", "noop"))
            if current < pages - 1:
                nav.append((self._t("ui_next", lang=lang), f"fml:p:{current + 1}"))
            rows.append(nav)

        if total:
            rows.append([
                (self._t("ui_bulk_sel_all", lang=lang), "fml:a"),
                (self._t("ui_bulk_none", lang=lang), "fml:n"),
            ])
            rows.append([
                (self._t("ui_bulk_enable", lang=lang), "fml:op:enable"),
                (self._t("ui_bulk_disable", lang=lang), "fml:op:disable"),
            ])
            rows.append([(self._t("ui_bulk_delete", lang=lang), "fml:conf:delete")])
        rows.append([(self._t("ui_back", lang=lang), "fml:back")])
        return "\n".join(lines), self._kbd(rows)

    async def _apply_forward_bulk(self, uid: int, op: str) -> List[bulk_ops.Outcome]:
        """Run one action per selected task, isolating each failure."""
        rules = await self._rules.list_by_owner(uid) if uid else []
        selection = self._bulk_selection(self._fwd_bulk(uid), rules)
        if not selection:
            return []
        want_active = op == bulk_ops.ENABLE
        owner_scope = None if self._is_admin(uid) else uid

        async def act(rule_id: str) -> str:
            rule = await self._owned_rule(rule_id, uid)
            if rule is None:
                return bulk_ops.MISSING
            if op == bulk_ops.DELETE:
                deleted = await self._rules.delete_owned(rule_id, owner_scope)
                return bulk_ops.OK if deleted else bulk_ops.MISSING
            if bool(getattr(rule, "is_active", False)) == want_active:
                return bulk_ops.UNCHANGED
            outcome, _fresh = await self._rules.set_status(
                rule_id, rule.status_state(), owner_scope
            )
            return bulk_ops.OK if outcome == self._rules.STATUS_OK else bulk_ops.FAILED

        outcomes = await bulk_ops.apply_each(selection.ids, act)
        log = getattr(self, "_log", None)
        if log is not None:
            try:
                summary = bulk_ops.summarize(outcomes)
                log.info("bot", "rule",
                         f"bulk {op} by {uid}: ok={summary.ok} unchanged={summary.unchanged} "
                         f"missing={summary.missing} failed={summary.failed}")
            except Exception:  # noqa: BLE001 - logging must never fail a batch
                pass
        logger.info("[PIPELINE:FORWARD_BULK] uid=%s op=%s selected=%d", uid, op, len(selection))
        await self._fwd_bulk_save(uid, selection.clear())
        return outcomes

    def _bulk_report(self, outcomes: List[bulk_ops.Outcome], lang: str,
                     labels: Dict[str, str]) -> str:
        summary = bulk_ops.summarize(outcomes)
        lines = [self._t("ui_bulk_report", lang=lang, ok=summary.ok,
                         unchanged=summary.unchanged, missing=summary.missing,
                         failed=summary.failed), ""]
        for outcome in outcomes:
            key = REPORT_ROW_KEYS.get(outcome.code, "ui_bulk_row_failed")
            lines.append(self._t(key, lang=lang, label=labels.get(outcome.rule_id, outcome.rule_id),
                                 detail=outcome.detail or ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register_forward_manage_handlers(self, b) -> None:
        @b.on_callback_query(filters.regex(r"^fml:"))
        @self._localized_handler
        async def _fwd_manage(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer()
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            verb = parts[1] if len(parts) > 1 else ""
            arg = parts[2] if len(parts) > 2 else ""
            lang = self._resolve_lang(uid)
            async with self._user_lock(uid):
                await self._fwd_manage_dispatch(cq, uid, lang, verb, arg)

    async def _fwd_manage_dispatch(self, cq: CallbackQuery, uid: int, lang: str,
                                   verb: str, arg: str) -> None:
        """Route one bulk-screen press. Every branch answers the callback."""
        text, kbd = await self._render_forward_bulk(uid)
        if verb == "back":
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_forward_tasks(rules, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)
            return await cq.answer()
        if verb == "p":
            try:
                page = int(arg)
            except (TypeError, ValueError):
                page = 0
            text, kbd = await self._render_forward_bulk(uid, page)
            await self._safe_edit(cq, text, reply_markup=kbd)
            return await cq.answer()

        rules = await self._rules.list_by_owner(uid) if uid else []
        selection = self._bulk_selection(self._fwd_bulk(uid), rules)

        if verb == "t":
            try:
                selection = selection.toggle(arg)
            except ValueError:
                return await cq.answer(
                    self._t("ui_bulk_limit", lang=lang, max=bulk_ops.MAX_SELECTION),
                    show_alert=True,
                )
            await self._fwd_bulk_save(uid, selection)
            text, kbd = await self._render_forward_bulk(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)
            return await cq.answer()

        if verb == "a":
            await self._fwd_bulk_save(uid, selection.select_all(self._bulk_labels(rules)))
            text, kbd = await self._render_forward_bulk(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)
            return await cq.answer()

        if verb == "n":
            await self._fwd_bulk_save(uid, selection.clear())
            text, kbd = await self._render_forward_bulk(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)
            return await cq.answer()

        if verb == "open":
            text, kbd = await self._render_forward_bulk(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)
            return await cq.answer()

        if not selection:
            return await cq.answer(self._t("ui_bulk_none_selected", lang=lang), show_alert=True)

        if verb == "conf" and arg == bulk_ops.DELETE:
            await self._safe_edit(
                cq,
                self._t("ui_bulk_confirm", lang=lang, n=len(selection)),
                reply_markup=self._kbd([
                    [(self._t("ui_bulk_yes", lang=lang, n=len(selection)), "fml:go:delete")],
                    [(self._t("ui_cancel_back", lang=lang), "fml:open")],
                ]),
            )
            return await cq.answer()

        if verb == "op" and arg in (bulk_ops.ENABLE, bulk_ops.DISABLE):
            labels = self._bulk_labels(rules)
            outcomes = await self._apply_forward_bulk(uid, arg)
            text = self._bulk_report(outcomes, lang, labels)
            rows = [[(self._t("ui_bulk_open", lang=lang), "fml:open")],
                    [(self._t("ui_back", lang=lang), "fml:back")]]
            await self._safe_edit(cq, text, reply_markup=self._kbd(rows))
            return await cq.answer(self._t("ui_done", lang=lang))

        if verb == "go" and arg == bulk_ops.DELETE:
            labels = self._bulk_labels(rules)
            outcomes = await self._apply_forward_bulk(uid, bulk_ops.DELETE)
            text = self._bulk_report(outcomes, lang, labels)
            rows = [[(self._t("ui_bulk_open", lang=lang), "fml:open")],
                    [(self._t("ui_back", lang=lang), "fml:back")]]
            await self._safe_edit(cq, text, reply_markup=self._kbd(rows))
            return await cq.answer(self._t("ui_done", lang=lang))

        return await cq.answer()
