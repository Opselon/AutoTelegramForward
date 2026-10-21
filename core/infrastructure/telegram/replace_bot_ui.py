"""The `/replace` manager panel — one list, every task, three surfaces deep.

What this file is
-----------------
The Telegram face of :class:`core.application.replace_rules.ReplaceRuleService`.
It renders and routes; every decision it makes is a call to that service, which
the dashboard and the mobile app also call through gRPC. There is no second
implementation of "what a valid rule is" here.

The panel
---------
``/replace`` opens a paginated list of the caller's rules:

    🔤 Replace rules — 12 total · 🟢 9 · 🔴 3
    page 1/2                    [⬅️] [1/2] [➡️]

Every row is a button that opens the rule's detail screen, where the whole set of
actions lives: enable/disable, edit, tasks, test, duplicate, delete. Delete asks
for confirmation, and every write re-renders the list with fresh counts — the
counts on screen are read from the service after the write, never adjusted
locally, because a count that is guessed will eventually be wrong.

Callback data
-------------
``rpl:<verb>[:<rule id>[:<arg>]]``. Rule ids are 16 hex characters, so the
longest payload (``rpl:en:<id>:0``) is 27 bytes — far inside Telegram's 64-byte
limit, and short enough that no index-into-a-list indirection is needed.

Panel state (the search query, the status filter, the page, and a rule being
created) lives in the caller's ``UiState.buffer``, not in callback data: it is
per-user, it must survive a restart, and it must never be trusted from the wire.
A callback therefore re-renders from state, and a rule id in a callback is only
ever a selector — the service refuses one the caller does not own.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...application.replace_rules import (
    DEFAULT_PAGE_SIZE,
    ReplaceRuleError,
    STATUS_ALL,
    STATUS_DISABLED,
    STATUS_ENABLED,
    error_key,
)
from ...domain.text_replace import (
    DEFAULT_PRIORITY,
    MAX_NAME_LEN,
    MODE_LITERAL,
    MODE_REGEX,
    clamp_priority,
)

logger = logging.getLogger("atf.replace.botui")

CB_PREFIX = "rpl"

#: Steps this panel owns. They are also listed in ``ui_state_manager.UI_STEPS``:
#: a step that is not there is coerced to "" on persist, and the user's next
#: message then falls through to the main menu — the flow looks broken even
#: though its handler exists.
STEP_SEARCH = "replace_search"
STEP_FIELD = "replace_field_input"
STEP_TEST = "replace_test_sample"

STATUS_ORDER = (STATUS_ALL, STATUS_ENABLED, STATUS_DISABLED)


def _cb(*parts: Any) -> str:
    return ":".join([CB_PREFIX, *(str(part) for part in parts)])


class ReplaceBotMixin:
    """Renderers, callbacks and FSM steps for the replace-rule manager."""

    # -- helpers ---------------------------------------------------------
    @property
    def _rpl(self) -> Any:
        return getattr(self, "_replace_rules", None)

    def _rpl_lang(self, uid: int) -> str:
        try:
            return self._lang_for(uid=uid) or "en"
        except Exception:
            return "en"

    def _rpl_state(self, uid: int) -> Dict[str, Any]:
        """The panel's own slice of the caller's UI state.

        Kept as a plain dict in ``st.buffer["replace"]`` so it persists with the
        rest of the flow state and cannot be supplied by a callback.
        """
        st = self._state(uid)
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        panel = st.buffer.get("replace")
        if not isinstance(panel, dict):
            panel = {}
            st.buffer["replace"] = panel
        panel.setdefault("status", STATUS_ALL)
        panel.setdefault("query", "")
        panel.setdefault("page", 1)
        return panel

    async def _rpl_store(self, uid: int) -> None:
        await self._persist(uid, self._state(uid))

    def _rpl_filters(self, uid: int) -> Tuple[str, str, int]:
        panel = self._rpl_state(uid)
        status = str(panel.get("status") or STATUS_ALL)
        if status not in STATUS_ORDER:
            status = STATUS_ALL
        query = str(panel.get("query") or "")
        try:
            page = int(panel.get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        return status, query, max(1, page)

    async def _rpl_refuse(self, cq: CallbackQuery, exc: ReplaceRuleError, uid: int) -> None:
        """Answer a refused action in the caller's language.

        The service names a catalog key for every refusal, so a new refusal
        cannot reach a user as an untranslated code.
        """
        await cq.answer(self._t(error_key(exc.code), lang=self._rpl_lang(uid)), show_alert=True)

    # -- list ------------------------------------------------------------
    async def _render_rpl_list(self, uid: int, page: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._rpl
        if service is None:
            return _t("ui_rpl_unavailable"), self._kbd([[(self._btn("ui_back_main_menu"), _cb("list"))]])

        status, query, current = self._rpl_filters(uid)
        if page is not None:
            current = max(1, int(page))
        try:
            data = await service.list(
                uid, query=query, status=status, page=current, per_page=DEFAULT_PAGE_SIZE,
            )
        except Exception as exc:
            logger.warning("[PIPELINE:REPLACE] list failed for %s: %s", uid, exc)
            return _t("ui_rpl_unavailable"), self._kbd([[(self._btn("ui_back_main_menu"), _cb("list"))]])

        panel = self._rpl_state(uid)
        panel["page"] = int(data.get("page") or 1)
        counts = data.get("counts") or {}

        lines = [
            _t("ui_rpl_title"),
            "",
            _t(
                "ui_rpl_counts",
                total=counts.get("total", 0),
                on=counts.get("enabled", 0),
                off=counts.get("disabled", 0),
            ),
        ]
        filter_line = _t(f"ui_rpl_filter_{status}")
        if query:
            filter_line = _t("ui_rpl_filter_query", q=query, f=filter_line)
        lines.append(filter_line)
        if data.get("pages", 1) > 1:
            lines.append(_t("ui_rpl_page", page=data.get("page", 1), pages=data.get("pages", 1)))
        lines.append("")

        items = data.get("items") or []
        if not items:
            lines.append(_t("ui_rpl_empty_query") if (query or status != STATUS_ALL) else _t("ui_rpl_empty"))
        rows: List[List[tuple]] = []
        for item in items:
            icon = "🟢" if item.get("enabled") else "🔴"
            name = str(item.get("name") or item.get("source") or "")[:32]
            target = str(item.get("to") or "")
            preview = f"→ {target[:22]}" if target else _t("ui_rpl_deletes")
            label = f"{icon} {name} · {preview}"
            rows.append([(label[:64], _cb("v", item.get("id")))])

        # Navigation: filters first, then paging, then the two create actions.
        rows.append([
            (_t("ui_rpl_btn_all"), _cb("st", STATUS_ALL)),
            (_t("ui_rpl_btn_on"), _cb("st", STATUS_ENABLED)),
            (_t("ui_rpl_btn_off"), _cb("st", STATUS_DISABLED)),
        ])
        rows.append([(_t("ui_rpl_btn_search"), _cb("q"))])
        pager: List[tuple] = []
        if data.get("page", 1) > 1:
            pager.append((_t("ui_rpl_btn_prev"), _cb("p", int(data["page"]) - 1)))
        if data.get("pages", 1) > 1:
            pager.append((_t("ui_rpl_page_short", page=data["page"], pages=data["pages"]), _cb("noop")))
        if data.get("page", 1) < data.get("pages", 1):
            pager.append((_t("ui_rpl_btn_next"), _cb("p", int(data["page"]) + 1)))
        if pager:
            rows.append(pager)
        rows.append([(_t("ui_rpl_btn_new"), _cb("new"))])
        rows.append([(self._btn("ui_back_main_menu"), "m")])
        await self._rpl_store(uid)
        return "\n".join(lines), self._kbd(rows)

    # -- detail ----------------------------------------------------------
    async def _render_rpl_detail(self, uid: int, rule_id: str) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._rpl
        if service is None:
            return _t("ui_rpl_unavailable"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        try:
            rule = await service.get(uid, rule_id)
        except ReplaceRuleError:
            return (
                _t("ui_rpl_err_not_found"),
                self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]]),
            )

        status = _t("ui_rpl_status_on") if rule.enabled else _t("ui_rpl_status_off")
        mode = _t("ui_rpl_mode_regex" if rule.is_regex else "ui_rpl_mode_literal")
        target = rule.to if rule.to else _t("ui_rpl_deletes")
        tasks = ", ".join(rule.task_names) if rule.task_names else _t("ui_rpl_no_tasks")
        lines = [
            _t("ui_rpl_detail_title", name=(rule.name or rule.source)[:40]),
            "",
            _t("ui_rpl_detail_status", status=status, mode=mode, badge=rule.badge),
            _t("ui_rpl_detail_source", source=rule.source),
            _t("ui_rpl_detail_target", target=target),
            _t("ui_rpl_detail_priority", priority=rule.priority),
            _t("ui_rpl_detail_tasks", n=len(rule.task_ids), names=tasks),
        ]
        if rule.flags:
            lines.append(_t("ui_rpl_detail_flags", flags=rule.flags))

        toggle_label = _t("ui_rpl_btn_disable") if rule.enabled else _t("ui_rpl_btn_enable")
        rows: List[List[tuple]] = [
            [(toggle_label, _cb("en", rule.id, "1" if not rule.enabled else "0"))],
            [(_t("ui_rpl_btn_edit"), _cb("ed", rule.id)), (_t("ui_rpl_btn_tasks"), _cb("ts", rule.id))],
            [(_t("ui_rpl_btn_test"), _cb("tr", rule.id)), (_t("ui_rpl_btn_dup"), _cb("dup", rule.id))],
            [(_t("ui_rpl_btn_delete"), _cb("del", rule.id))],
            [(_t("ui_rpl_btn_back"), _cb("list"))],
        ]
        return "\n".join(lines), self._kbd(rows)

    # -- delete confirmation ---------------------------------------------
    async def _render_rpl_confirm_delete(self, uid: int, rule_id: str) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._rpl
        if service is None:
            return _t("ui_rpl_unavailable"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        try:
            rule = await service.get(uid, rule_id)
        except ReplaceRuleError:
            return _t("ui_rpl_err_not_found"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        text = _t(
            "ui_rpl_confirm_delete",
            name=(rule.name or rule.source)[:40],
            n=len(rule.task_ids),
        )
        rows = [
            [(_t("ui_rpl_btn_delete_yes"), _cb("delok", rule.id))],
            [(_t("ui_rpl_btn_cancel"), _cb("v", rule.id))],
        ]
        return text, self._kbd(rows)

    # -- tasks -----------------------------------------------------------
    async def _render_rpl_tasks(self, uid: int, rule_id: str) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._rpl
        if service is None:
            return _t("ui_rpl_unavailable"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        try:
            rule = await service.get(uid, rule_id)
            tasks = await service.tasks(uid)
        except ReplaceRuleError:
            return _t("ui_rpl_err_not_found"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        if not tasks:
            return _t("ui_rpl_err_no_tasks"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("v", rule_id))]])
        assigned = set(rule.task_ids)
        lines = [
            _t("ui_rpl_tasks_title", name=(rule.name or rule.source)[:40]),
            _t("ui_rpl_tasks_hint"),
            "",
        ]
        rows: List[List[tuple]] = []
        for task in tasks[:12]:
            mark = "✅" if task["id"] in assigned else "⬜️"
            label = f"{mark} {str(task['name'])[:36]}"
            rows.append([(label[:64], _cb("tt", rule_id, task["id"]))])
        if len(assigned) <= 0:
            lines.append(_t("ui_rpl_need_one_task"))
        rows.append([(_t("ui_rpl_btn_back"), _cb("v", rule_id))])
        return "\n".join(lines), self._kbd(rows)

    # -- edit ------------------------------------------------------------
    async def _render_rpl_edit(self, uid: int, rule_id: str) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._rpl
        if service is None:
            return _t("ui_rpl_unavailable"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        try:
            rule = await service.get(uid, rule_id)
        except ReplaceRuleError:
            return _t("ui_rpl_err_not_found"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        mode = _t("ui_rpl_mode_regex" if rule.is_regex else "ui_rpl_mode_literal")
        lines = [
            _t("ui_rpl_edit_title", name=(rule.name or rule.source)[:40]),
            "",
            _t("ui_rpl_edit_body", source=rule.source, target=(rule.to or _t("ui_rpl_deletes")),
               mode=mode, priority=rule.priority),
            _t("ui_rpl_edit_hint"),
        ]
        rows = [
            [(_t("ui_rpl_btn_edit_name"), _cb("ef", rule_id, "name")),
             (_t("ui_rpl_btn_edit_source"), _cb("ef", rule_id, "source"))],
            [(_t("ui_rpl_btn_edit_target"), _cb("ef", rule_id, "to")),
             (_t("ui_rpl_btn_edit_priority"), _cb("ef", rule_id, "priority"))],
            [(_t("ui_rpl_btn_mode", mode=mode), _cb("md", rule_id, MODE_REGEX if not rule.is_regex else MODE_LITERAL))],
            [(_t("ui_rpl_btn_back"), _cb("v", rule_id))],
        ]
        return "\n".join(lines), self._kbd(rows)

    # -- create wizard ---------------------------------------------------
    def _rpl_draft(self, uid: int) -> Dict[str, Any]:
        panel = self._rpl_state(uid)
        draft = panel.get("draft")
        if not isinstance(draft, dict):
            draft = {
                "name": "", "source": "", "to": "", "mode": MODE_LITERAL,
                "flags": "", "priority": DEFAULT_PRIORITY, "task_ids": [],
            }
            panel["draft"] = draft
        return draft

    async def _render_rpl_draft(self, uid: int) -> Tuple[str, InlineKeyboardMarkup]:
        """The create screen: everything the rule needs, and what is missing."""
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        draft = self._rpl_draft(uid)
        mode = _t("ui_rpl_mode_regex" if draft.get("mode") == MODE_REGEX else "ui_rpl_mode_literal")
        source = str(draft.get("source") or "")
        target = str(draft.get("to") or "")
        task_ids = draft.get("task_ids") or []
        lines = [
            _t("ui_rpl_new_title"),
            "",
            _t("ui_rpl_new_name", name=(draft.get("name") or _t("ui_rpl_none"))),
            _t("ui_rpl_new_source", source=(source or _t("ui_rpl_none"))),
            _t("ui_rpl_new_target", target=(target or _t("ui_rpl_deletes"))),
            _t("ui_rpl_new_mode", mode=mode),
            _t("ui_rpl_new_priority", priority=draft.get("priority", DEFAULT_PRIORITY)),
            _t("ui_rpl_new_tasks", n=len(task_ids)),
        ]
        ready = bool(source.strip()) and bool(task_ids)
        if not ready:
            lines += ["", _t("ui_rpl_new_missing")]
        rows: List[List[tuple]] = [
            [(_t("ui_rpl_btn_edit_name"), _cb("df", "name")), (_t("ui_rpl_btn_edit_source"), _cb("df", "source"))],
            [(_t("ui_rpl_btn_edit_target"), _cb("df", "to")), (_t("ui_rpl_btn_edit_priority"), _cb("df", "priority"))],
            [(_t("ui_rpl_btn_mode", mode=mode), _cb("dm"))],
            [(_t("ui_rpl_btn_tasks"), _cb("dt"))],
        ]
        if ready:
            rows.append([(_t("ui_rpl_btn_create"), _cb("dcreate"))])
        rows.append([(_t("ui_rpl_btn_cancel"), _cb("list"))])
        return "\n".join(lines), self._kbd(rows)

    async def _render_rpl_draft_tasks(self, uid: int) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._rpl
        if service is None:
            return _t("ui_rpl_unavailable"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        draft = self._rpl_draft(uid)
        chosen = set(draft.get("task_ids") or [])
        try:
            tasks = await service.tasks(uid)
        except Exception as exc:
            logger.warning("[PIPELINE:REPLACE] task list failed for %s: %s", uid, exc)
            tasks = []
        if not tasks:
            return (
                _t("ui_rpl_err_no_tasks"),
                self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("new"))]]),
            )
        rows: List[List[tuple]] = []
        for task in tasks[:12]:
            mark = "✅" if task["id"] in chosen else "⬜️"
            rows.append([(f"{mark} {str(task['name'])[:36]}"[:64], _cb("dtg", task["id"]))])
        rows.append([(_t("ui_rpl_btn_done"), _cb("new"))])
        lines = [_t("ui_rpl_pick_tasks_title"), _t("ui_rpl_pick_tasks_hint"), ""]
        return "\n".join(lines), self._kbd(rows)

    # -- test ------------------------------------------------------------
    async def _render_rpl_test(self, uid: int, rule_id: str, sample: str) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        service = self._rpl
        if service is None:
            return _t("ui_rpl_unavailable"), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]])
        try:
            result = await service.test(uid, {"rule_id": rule_id, "sample": sample})
        except ReplaceRuleError as exc:
            return _t(error_key(exc.code)), self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("v", rule_id))]])
        output = str(result.get("output") or "")
        lines = [
            _t("ui_rpl_test_title"),
            "",
            _t("ui_rpl_test_input", text=sample[:200]),
            _t("ui_rpl_test_output", text=output[:200]),
            _t("ui_rpl_test_matches", n=result.get("matches", 0)),
        ]
        if not result.get("changed"):
            lines.append(_t("ui_rpl_test_unchanged"))
        rows = [
            [(_t("ui_rpl_btn_test_again"), _cb("tr", rule_id))],
            [(_t("ui_rpl_btn_back"), _cb("v", rule_id))],
        ]
        return "\n".join(lines), self._kbd(rows)

    # -- FSM steps -------------------------------------------------------
    async def _step_replace_search(self, message: Message, st: Any, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        lang = self._lang_for(st)
        panel = self._rpl_state(uid)
        panel["query"] = text.strip()[:80]
        panel["page"] = 1
        st.step = ""
        await self._rpl_store(uid)
        body, kbd = await self._render_rpl_list(uid)
        await message.reply_text(body, reply_markup=kbd)

    async def _step_replace_test_sample(self, message: Message, st: Any, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = ""
        if isinstance(st.buffer, dict):
            panel = st.buffer.get("replace") or {}
            if isinstance(panel, dict):
                rule_id = str(panel.get("test_rule") or "")
        st.step = ""
        await self._rpl_store(uid)
        if not rule_id:
            body, kbd = await self._render_rpl_list(uid)
            return await message.reply_text(body, reply_markup=kbd)
        body, kbd = await self._render_rpl_test(uid, rule_id, text)
        await message.reply_text(body, reply_markup=kbd)

    async def _step_replace_field_input(self, message: Message, st: Any, text: str) -> None:
        """One prompt handler for every field, drafted or saved.

        ``buffer["replace"]["editing"]`` says which rule is being edited ("" for
        the create wizard) and which field the caller is answering, so the eight
        prompts do not become eight near-identical steps.
        """
        uid = int(message.from_user.id if message.from_user else 0)
        lang = self._lang_for(st)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        panel = self._rpl_state(uid)
        editing = panel.get("editing") if isinstance(panel.get("editing"), dict) else {}
        rule_id = str(editing.get("rule_id") or "")
        field = str(editing.get("field") or "")
        value = (text or "").strip()

        st.step = ""
        if field == "priority":
            value = str(clamp_priority(value))

        if not rule_id:
            # Create wizard: write into the draft, then re-render it.
            draft = self._rpl_draft(uid)
            if field in ("name", "source", "to"):
                draft[field] = value if field != "name" else value[:MAX_NAME_LEN]
            elif field == "priority":
                draft["priority"] = clamp_priority(value)
            panel["editing"] = {}
            await self._rpl_store(uid)
            body, kbd = await self._render_rpl_draft(uid)
            return await message.reply_text(body, reply_markup=kbd)

        payload: Dict[str, Any] = {}
        if field in ("name", "source", "to"):
            payload[field] = value if field != "name" else value[:MAX_NAME_LEN]
        elif field == "priority":
            payload["priority"] = clamp_priority(value)
        else:
            panel["editing"] = {}
            await self._rpl_store(uid)
            body, kbd = await self._render_rpl_detail(uid, rule_id)
            return await message.reply_text(body, reply_markup=kbd)

        try:
            await self._rpl.update(uid, rule_id, payload)
        except ReplaceRuleError as exc:
            panel["editing"] = {}
            await self._rpl_store(uid)
            await message.reply_text(_t(error_key(exc.code)))
            body, kbd = await self._render_rpl_edit(uid, rule_id)
            return await message.reply_text(body, reply_markup=kbd)
        except Exception as exc:
            logger.warning("[PIPELINE:REPLACE] update failed for %s: %s", uid, exc)
            panel["editing"] = {}
            await self._rpl_store(uid)
            await message.reply_text(_t("ui_internal_error"))
            body, kbd = await self._render_rpl_edit(uid, rule_id)
            return await message.reply_text(body, reply_markup=kbd)

        panel["editing"] = {}
        await self._rpl_store(uid)
        await message.reply_text(_t("ui_rpl_saved"))
        body, kbd = await self._render_rpl_detail(uid, rule_id)
        await message.reply_text(body, reply_markup=kbd)

    # -- registration ----------------------------------------------------
    def register_replace_handlers(self) -> None:
        """Mount the panel's callbacks. Called once from ``_register``."""
        b = self.bot

        async def _render(cq: CallbackQuery, uid: int, renderer, *args) -> None:
            body, kbd = await renderer(uid, *args)
            await self._safe_edit(cq, body, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rpl:"))  # noqa: F821
        @self._localized_handler
        async def _cb_rpl(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer()
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            verb = parts[1] if len(parts) > 1 else "list"
            arg1 = parts[2] if len(parts) > 2 else ""
            arg2 = parts[3] if len(parts) > 3 else ""
            await self._rpl_dispatch(cq, uid, verb, arg1, arg2, _render)

    async def _rpl_dispatch(self, cq: CallbackQuery, uid: int, verb: str, arg1: str,
                            arg2: str, render) -> None:
        """Route one callback. Kept out of the closure so tests can drive it.

        Every branch answers the callback query, even the read-only ones: a
        button Telegram considers unanswered spins for 30 seconds and then makes
        the whole panel look unresponsive.
        """
        service = self._rpl
        lang = self._rpl_lang(uid)
        _t = lambda key, **kw: self._t(key, lang=lang, **kw)  # noqa: E731
        panel = self._rpl_state(uid)

        if verb in ("noop",):
            return await cq.answer()

        if verb == "list":
            panel["editing"] = {}
            panel["draft"] = None
            await self._rpl_store(uid)
            await cq.answer()
            return await render(cq, uid, self._render_rpl_list)

        if verb == "p":
            try:
                page = int(arg1)
            except (TypeError, ValueError):
                page = 1
            await cq.answer()
            return await render(cq, uid, self._render_rpl_list, page)

        if verb == "st":
            panel["status"] = arg1 if arg1 in STATUS_ORDER else STATUS_ALL
            panel["page"] = 1
            await self._rpl_store(uid)
            await cq.answer()
            return await render(cq, uid, self._render_rpl_list)

        if verb == "q":
            self._state(uid).step = STEP_SEARCH
            await self._rpl_store(uid)
            await cq.answer()
            return await self._safe_edit(
                cq, _t("ui_rpl_search_prompt"),
                reply_markup=self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("list"))]]),
            )

        if service is None:
            return await cq.answer(_t("ui_rpl_unavailable"), show_alert=True)

        if verb == "v":
            await cq.answer()
            return await render(cq, uid, self._render_rpl_detail, arg1)

        if verb == "en":
            want = arg2 in ("1", "true", "on")
            try:
                rule = await service.update(uid, arg1, {"enabled": want})
            except ReplaceRuleError as exc:
                return await self._rpl_refuse(cq, exc, uid)
            except Exception as exc:
                logger.warning("[PIPELINE:REPLACE] enable failed for %s: %s", uid, exc)
                return await cq.answer(_t("ui_internal_error"), show_alert=True)
            await cq.answer(_t("ui_rpl_enabled_alert") if rule.enabled else _t("ui_rpl_disabled_alert"))
            return await render(cq, uid, self._render_rpl_detail, arg1)

        if verb == "del":
            await cq.answer()
            return await render(cq, uid, self._render_rpl_confirm_delete, arg1)

        if verb == "delok":
            try:
                result = await service.delete(uid, arg1)
            except ReplaceRuleError as exc:
                return await self._rpl_refuse(cq, exc, uid)
            except Exception as exc:
                logger.warning("[PIPELINE:REPLACE] delete failed for %s: %s", uid, exc)
                return await cq.answer(_t("ui_internal_error"), show_alert=True)
            await cq.answer(_t("ui_rpl_deleted_alert", n=result.get("removed", 0)))
            return await render(cq, uid, self._render_rpl_list)

        if verb == "dup":
            try:
                clone = await service.duplicate(uid, arg1)
            except ReplaceRuleError as exc:
                return await self._rpl_refuse(cq, exc, uid)
            except Exception as exc:
                logger.warning("[PIPELINE:REPLACE] duplicate failed for %s: %s", uid, exc)
                return await cq.answer(_t("ui_internal_error"), show_alert=True)
            await cq.answer(_t("ui_rpl_duplicated_alert"))
            return await render(cq, uid, self._render_rpl_detail, clone.id)

        if verb == "ts":
            await cq.answer()
            return await render(cq, uid, self._render_rpl_tasks, arg1)

        if verb == "tt":
            # A tap toggles one task's membership. Re-reading the rule first
            # keeps the write relative to what is stored now, not to what the
            # screen showed when it was rendered.
            try:
                rule = await service.get(uid, arg1)
            except ReplaceRuleError as exc:
                return await self._rpl_refuse(cq, exc, uid)
            chosen = list(rule.task_ids)
            if arg2 in chosen:
                chosen.remove(arg2)
            else:
                chosen.append(arg2)
            if not chosen:
                return await cq.answer(_t("ui_rpl_err_task_required"), show_alert=True)
            try:
                await service.update(uid, arg1, {"task_ids": chosen})
            except ReplaceRuleError as exc:
                return await self._rpl_refuse(cq, exc, uid)
            except Exception as exc:
                logger.warning("[PIPELINE:REPLACE] task assignment failed for %s: %s", uid, exc)
                return await cq.answer(_t("ui_internal_error"), show_alert=True)
            await cq.answer(_t("ui_rpl_tasks_saved"))
            return await render(cq, uid, self._render_rpl_tasks, arg1)

        if verb == "tr":
            panel["test_rule"] = arg1
            self._state(uid).step = STEP_TEST
            await self._rpl_store(uid)
            await cq.answer()
            return await self._safe_edit(
                cq, _t("ui_rpl_test_prompt"),
                reply_markup=self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("v", arg1))]]),
            )

        if verb == "ed":
            await cq.answer()
            return await render(cq, uid, self._render_rpl_edit, arg1)

        if verb == "ef":
            self._state(uid).step = STEP_FIELD
            panel["editing"] = {"rule_id": arg1, "field": arg2}
            await self._rpl_store(uid)
            await cq.answer()
            return await self._safe_edit(
                cq, _t(f"ui_rpl_prompt_{arg2}" if arg2 in ("name", "source", "to", "priority")
                       else "ui_rpl_prompt_source"),
                reply_markup=self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("ed", arg1))]]),
            )

        if verb == "md":
            mode = MODE_REGEX if arg2 == MODE_REGEX else MODE_LITERAL
            try:
                await service.update(uid, arg1, {"mode": mode})
            except ReplaceRuleError as exc:
                return await self._rpl_refuse(cq, exc, uid)
            except Exception as exc:
                logger.warning("[PIPELINE:REPLACE] mode change failed for %s: %s", uid, exc)
                return await cq.answer(_t("ui_internal_error"), show_alert=True)
            await cq.answer(_t("ui_rpl_saved"))
            return await render(cq, uid, self._render_rpl_edit, arg1)

        # ---- create wizard ----
        if verb == "new":
            panel["draft"] = None
            panel["editing"] = {}
            await self._rpl_store(uid)
            await cq.answer()
            return await render(cq, uid, self._render_rpl_draft)

        if verb == "df":
            self._state(uid).step = STEP_FIELD
            panel["editing"] = {"rule_id": "", "field": arg1}
            await self._rpl_store(uid)
            await cq.answer()
            return await self._safe_edit(
                cq, _t(f"ui_rpl_prompt_{arg1}" if arg1 in ("name", "source", "to", "priority")
                       else "ui_rpl_prompt_source"),
                reply_markup=self._kbd([[(self._btn("ui_rpl_btn_back"), _cb("new"))]]),
            )

        if verb == "dm":
            draft = self._rpl_draft(uid)
            draft["mode"] = MODE_REGEX if draft.get("mode") != MODE_REGEX else MODE_LITERAL
            await self._rpl_store(uid)
            await cq.answer()
            return await render(cq, uid, self._render_rpl_draft)

        if verb == "dt":
            await cq.answer()
            return await render(cq, uid, self._render_rpl_draft_tasks)

        if verb == "dtg":
            draft = self._rpl_draft(uid)
            chosen = list(draft.get("task_ids") or [])
            if arg1 in chosen:
                chosen.remove(arg1)
            elif len(chosen) < 20:
                chosen.append(arg1)
            draft["task_ids"] = chosen
            await self._rpl_store(uid)
            await cq.answer()
            return await render(cq, uid, self._render_rpl_draft_tasks)

        if verb == "dcreate":
            draft = self._rpl_draft(uid)
            payload = {
                "name": draft.get("name", ""),
                "source": draft.get("source", ""),
                "to": draft.get("to", ""),
                "mode": draft.get("mode", MODE_LITERAL),
                "priority": draft.get("priority", DEFAULT_PRIORITY),
                "task_ids": list(draft.get("task_ids") or []),
            }
            try:
                rule = await service.create(uid, payload)
            except ReplaceRuleError as exc:
                return await self._rpl_refuse(cq, exc, uid)
            except Exception as exc:
                logger.warning("[PIPELINE:REPLACE] create failed for %s: %s", uid, exc)
                return await cq.answer(_t("ui_internal_error"), show_alert=True)
            panel["draft"] = None
            panel["editing"] = {}
            await self._rpl_store(uid)
            await cq.answer(_t("ui_rpl_created_alert"))
            return await render(cq, uid, self._render_rpl_detail, rule.id)

        await cq.answer()
        return await render(cq, uid, self._render_rpl_list)


__all__ = [
    "ReplaceBotMixin",
    "STEP_FIELD",
    "STEP_SEARCH",
    "STEP_TEST",
]
