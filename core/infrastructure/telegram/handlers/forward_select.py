"""The route picker: pick many sources and many targets, then create once.

This module owns *only* the screens and the wiring. The rules it follows live
one layer down:

* the selection is the value object from
  :mod:`core.application.chat_selection` (toggle, counts, merge, prune,
  serialization) — the handlers never keep their own list;
* access is proven by :mod:`core.application.chat_access` before anything is
  written, and the plan (one rule per source, every target attached) is built
  by the application layer, including duplicate detection;
* creating the rules is one application call returning a per-source outcome,
  so a failure on one source cannot hide the others.

Callback data stays short: a role letter, a page number or **one** chat id —
never a list of ids. Every payload is checked by the server against the
caller's own catalog before it changes anything, so a forged or stale press
can select nothing it could not have selected by hand.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from core.application.chat_access import (
    CODE_CREATED,
    CODE_DUPLICATE,
    CODE_FAILED,
    Probe,
    ValidationReport,
    apply_forward_plan,
    plan_forward_rules,
    summarize,
    validate_selection,
)
from core.application.chat_selection import (
    MAX_BATCH,
    ROLE_SOURCE,
    ROLE_TARGET,
    STATE_OK,
    STATE_READ_ONLY,
    STATE_UNCHECKED,
    STATE_UNKNOWN,
    PickedChat,
    Selection,
    parse_id_batch,
    role_key,
)
from core.domain.peer_ids import unique_peer_key

logger = logging.getLogger(__name__)

CB_PREFIX = "sel"
#: Where `Back` leads: the task screen the picker was opened from.
CB_RULES = "r"
PAGE_SIZE = 6

#: Buffer keys owned by this flow. Everything else in the buffer belongs to
#: whoever else was using it; `sel_cancel` drops only these.
STATE_KEY = "sel"
ROLE_KEY = "sel_role"
PAGE_KEY = "sel_page"
QUERY_KEY = "sel_query"
CHATS_KEY = "sel_chats"
NOTE_KEY = "sel_note"

STEP_IDS_SOURCE = "sel_ids_s"
STEP_IDS_TARGET = "sel_ids_t"
STEP_SEARCH_SOURCE = "sel_query_s"
STEP_SEARCH_TARGET = "sel_query_t"
STEPS = (STEP_IDS_SOURCE, STEP_IDS_TARGET, STEP_SEARCH_SOURCE, STEP_SEARCH_TARGET)

def is_cancel_word(text: Any) -> bool:
    """True when the text is the user's own cancel word.

    Resolved from the catalog (`cancel_aliases`) so a Russian, Chinese or
    Indonesian speaker can escape the ids step with the same word the screen
    offers — a hardcoded tuple can only ever know some of the ten locales.
    Imported lazily: the panel imports this module through the handler
    registry, so a module-level import would be circular.
    """
    from core.infrastructure.telegram.pro_bot_ui import cancel_aliases

    return str(text or "").strip().lower() in cancel_aliases()


def _plain(text: Any) -> str:
    """Strip Markdown that a chat title may carry, so the card stays valid."""
    return (
        str(text or "")
        .replace("*", "").replace("_", " ").replace("`", "'")
        .replace("[", "(").replace("]", ")")
    )


class ForwardSelectHandlers:
    """Mixin: the 📥 Sources / 📤 Targets picker and its confirmation."""

    # ------------------------------------------------------------------ #
    # State helpers
    # ------------------------------------------------------------------ #
    def _sel_buffer(self, st: Any) -> Dict[str, Any]:
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        return st.buffer

    def _sel_selection(self, st: Any) -> Selection:
        return Selection.from_state(self._sel_buffer(st).get(STATE_KEY))

    def _sel_save(self, st: Any, selection: Selection) -> None:
        buf = self._sel_buffer(st)
        buf[STATE_KEY] = selection.to_state()

    def _sel_role(self, st: Any, default: str = ROLE_SOURCE) -> str:
        role = str(self._sel_buffer(st).get(ROLE_KEY) or default)
        return role_key(role)

    def _sel_page(self, st: Any) -> int:
        try:
            return max(0, int(self._sel_buffer(st).get(PAGE_KEY) or 0))
        except (TypeError, ValueError):
            return 0

    def _sel_query(self, st: Any, role: str) -> str:
        queries = self._sel_buffer(st).get(QUERY_KEY)
        if isinstance(queries, dict):
            return str(queries.get(role) or "")
        return ""

    def _sel_set_query(self, st: Any, role: str, text: str) -> None:
        buf = self._sel_buffer(st)
        queries = buf.get(QUERY_KEY)
        if not isinstance(queries, dict):
            queries = {}
        queries[role] = str(text or "")
        buf[QUERY_KEY] = queries

    def _sel_note(self, st: Any) -> str:
        return str(self._sel_buffer(st).get(NOTE_KEY) or "")

    def _sel_reset(self, st: Any) -> None:
        """Drop only this flow's keys, leaving other wizards' state alone."""
        buf = self._sel_buffer(st)
        for key in (STATE_KEY, ROLE_KEY, PAGE_KEY, QUERY_KEY, CHATS_KEY, NOTE_KEY):
            buf.pop(key, None)
        if st.step in STEPS:
            st.step = ""

    # ------------------------------------------------------------------ #
    # Catalog
    # ------------------------------------------------------------------ #
    async def _selector_chats(self, uid: int, refresh: bool = False) -> List[Dict[str, Any]]:
        """The user's own chats, cached in the flow state.

        Only chats this catalog lists can be toggled: a payload naming anything
        else is answered as "not in your list", which is what makes a forged
        callback harmless.
        """
        if not refresh:
            cached = self._selector_cached(uid)
            if cached:
                return cached
        try:
            chats = await self._fetch_dialogs(uid)
        except Exception as exc:  # noqa: BLE001 - the screen must still render
            logger.warning("[PIPELINE:SELECT] catalog failed uid=%s: %s", uid, exc)
            return self._selector_cached(uid)
        return list(chats or [])

    def _selector_cached(self, uid: int) -> List[Dict[str, Any]]:
        st = self._state(uid)
        chats = self._sel_buffer(st).get(CHATS_KEY)
        return list(chats) if isinstance(chats, list) else []

    def _selector_store(self, uid: int, chats: Sequence[Dict[str, Any]]) -> None:
        st = self._state(uid)
        self._sel_buffer(st)[CHATS_KEY] = [dict(chat) for chat in chats]

    def _selector_probe(self) -> Any:
        """The access probe; tests assign ``ui.chat_probe`` directly."""
        probe = getattr(self, "chat_probe", None)
        if probe is not None:
            return probe
        from core.infrastructure.telegram.chat_probe import SessionChatProbe

        return SessionChatProbe(self._sessions, self._pool)

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def _sel_counts(self, st: Any, lang: str) -> str:
        selection = self._sel_selection(st)
        return self._t(
            "ui_sel_counts", lang=lang,
            sources=selection.count(ROLE_SOURCE), targets=selection.count(ROLE_TARGET),
        )

    def _sel_filtered(self, chats: Sequence[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        needle = (query or "").strip().lower()
        if not needle:
            return list(chats)
        out: List[Dict[str, Any]] = []
        for chat in chats:
            haystack = " ".join(
                str(chat.get(field) or "") for field in ("title", "username", "id")
            ).lower()
            if needle in haystack:
                out.append(chat)
        return out

    def _sel_mark(self, selection: Selection, role: str, chat_id: str) -> str:
        return "✅" if selection.has(role, chat_id) else "☑️"

    def render_selector(self, st: Any, role: str, lang: str,
                        note: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        """The list screen for one role: marked rows, counts, paging, actions."""
        selection = self._sel_selection(st)
        chats = self._sel_filtered(
            list(self._sel_buffer(st).get(CHATS_KEY) or []), self._sel_query(st, role)
        )
        total = len(chats)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(self._sel_page(st), total_pages - 1)
        start = page * PAGE_SIZE
        page_items = chats[start:start + PAGE_SIZE]

        header_key = "ui_sel_title_sources" if role == ROLE_SOURCE else "ui_sel_title_targets"
        lines: List[str] = [
            self._t(header_key, lang=lang),
            self._sel_counts(st, lang),
        ]
        query = self._sel_query(st, role)
        if query:
            lines.append(self._t("ui_sel_search_active", lang=lang, q=query))
        lines.append("────────────────────")
        if page_items:
            for offset, chat in enumerate(page_items, start=start + 1):
                chat_id = str(chat.get("id") or "")
                title = _plain(chat.get("title") or chat_id)
                emoji = str(chat.get("emoji") or "")
                lines.append(f"{self._sel_mark(selection, role, chat_id)} {emoji} {offset}. {title}")
                lines.append(f"   🆔 `{chat_id}`")
        else:
            lines.append(self._t("ui_sel_empty_list", lang=lang))
        lines.append("────────────────────")
        lines.append(self._t("ui_sel_page", lang=lang, page=page + 1, total=total_pages, count=total))
        lines.append(self._t("ui_sel_hint", lang=lang))
        if note:
            lines.extend(["", note])

        rows: List[List[Tuple[str, str]]] = []
        for chat in page_items:
            chat_id = str(chat.get("id") or "")
            title = _plain(chat.get("title") or chat_id)
            short = title[:26] + "…" if len(title) > 26 else title
            rows.append([(f"{self._sel_mark(selection, role, chat_id)} {short}",
                          self._cb("t", role, chat_id))])
        nav: List[Tuple[str, str]] = []
        if page > 0:
            nav.append((self._btn("ui_prev", lang=lang), self._cb("pg", role, str(page - 1))))
        nav.append((f"{page + 1} / {total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append((self._t("ui_next", lang=lang) + " ▶️", self._cb("pg", role, str(page + 1))))
        rows.append(nav)
        rows.append([
            (self._t("ui_sel_all", lang=lang), self._cb("all", role)),
            (self._t("ui_sel_none", lang=lang), self._cb("none", role)),
        ])
        rows.append([
            (self._t("ui_sel_search", lang=lang), self._cb("q", role)),
            (self._t("ui_sel_ids", lang=lang), self._cb("ids", role)),
        ])
        other = ROLE_TARGET if role == ROLE_SOURCE else ROLE_SOURCE
        other_key = "ui_sel_sources_btn" if other == ROLE_SOURCE else "ui_sel_targets_btn"
        rows.append([
            (self._t(other_key, lang=lang, count=selection.count(other)), self._cb("r", other)),
            (self._t("ui_sel_done", lang=lang), self._cb("done")),
        ])
        # Back leaves the picker for the screen that opened it (the task list);
        # Cancel empties the picks first, so they are two different exits.
        rows.append([
            (self._t("ui_sel_back", lang=lang), CB_RULES),
            (self._t("ui_sel_cancel", lang=lang), self._cb("cancel")),
        ])
        return "\n".join(lines), self._kbd(rows)

    def _probe_line(self, probe: Probe, lang: str) -> str:
        if probe.state == STATE_OK:
            mark = self._t("ui_sel_state_ok", lang=lang)
        elif probe.state == STATE_READ_ONLY:
            mark = self._t("ui_sel_state_read_only", lang=lang)
        elif probe.state == STATE_UNCHECKED:
            mark = self._t("ui_sel_state_unchecked", lang=lang)
        else:
            mark = self._t("ui_sel_state_unknown", lang=lang)
        return f"{mark} `{probe.chat_id}` {_plain(probe.title)}".rstrip()

    def render_confirm(self, st: Any, lang: str, validation: ValidationReport,
                       plan: Any, note: str = "") -> Tuple[str, InlineKeyboardMarkup]:
        """The pre-create card: who is reachable, what will be written."""
        selection = self._sel_selection(st)
        lines: List[str] = [
            self._t("ui_sel_confirm_title", lang=lang),
            self._sel_counts(st, lang),
            "────────────────────",
        ]
        for role, title_key in ((ROLE_SOURCE, "ui_sel_title_sources"),
                                (ROLE_TARGET, "ui_sel_title_targets")):
            lines.append(self._t(title_key, lang=lang))
            picks = selection.of(role)
            if not picks:
                lines.append(self._t("ui_sel_empty_list", lang=lang))
            for chat in picks:
                probe = validation.probe_for(chat.chat_id) or Probe(
                    chat_id=chat.chat_id, state=chat.state, title=chat.title
                )
                lines.append(self._probe_line(probe, lang))
        lines.append("────────────────────")
        lines.append(self._t(
            "ui_sel_plan", lang=lang,
            rules=len(plan.plans), links=plan.links, targets=len(plan.plans[0].targets) if plan.plans else 0,
        ))
        if plan.duplicates:
            lines.append(self._t("ui_sel_plan_duplicates", lang=lang,
                                 count=len(plan.duplicates)))
        if plan.skipped_sources:
            lines.append(self._t("ui_sel_plan_skipped", lang=lang,
                                 ids=", ".join(plan.skipped_sources[:5])))
        if validation.blocked:
            lines.append(self._t("ui_sel_plan_blocked", lang=lang,
                                 count=len(validation.blocked)))
        if validation.unchecked:
            lines.append(self._t("ui_sel_plan_unchecked", lang=lang,
                                 count=len(validation.unchecked)))
        if validation.overlap:
            lines.append(self._t("ui_sel_plan_overlap", lang=lang,
                                 ids=", ".join(validation.overlap[:5])))
        if note:
            lines.extend(["", note])

        creates = plan.creates
        rows: List[List[Tuple[str, str]]] = []
        if creates and (validation.ok):
            rows.append([(self._t("ui_sel_create", lang=lang, count=len(creates)),
                          self._cb("go"))])
        rows.append([
            (self._t("ui_sel_back", lang=lang), self._cb("back")),
            (self._t("ui_sel_cancel", lang=lang), self._cb("cancel")),
        ])
        return "\n".join(lines), self._kbd(rows)

    def render_outcome(self, outcomes: Sequence[Any], lang: str,
                       links: int) -> Tuple[str, InlineKeyboardMarkup]:
        """The result card: one line per source, each with its own verdict."""
        summary = summarize(outcomes)
        lines: List[str] = [
            self._t("ui_sel_result_title", lang=lang),
            self._t(
                "ui_sel_result_counts", lang=lang,
                created=summary.get(CODE_CREATED, 0),
                duplicate=summary.get(CODE_DUPLICATE, 0),
                failed=summary.get(CODE_FAILED, 0),
                links=links,
            ),
            "────────────────────",
        ]
        for outcome in outcomes:
            label = _plain(outcome.source_label or outcome.source_id)
            if outcome.code == CODE_CREATED:
                lines.append(self._t("ui_sel_result_created", lang=lang,
                                     source=label, links=outcome.links))
            elif outcome.code == CODE_DUPLICATE:
                lines.append(self._t("ui_sel_result_duplicate", lang=lang, source=label))
            else:
                lines.append(self._t("ui_sel_result_failed", lang=lang,
                                     source=label, error=_plain(outcome.detail)))
        rows = [
            [(self._t("ui_sel_again", lang=lang), self._cb("open")),
             (self._t("ui_rule_view_all", lang=lang), "r")],
            [(self._btn("ui_main_title", lang=lang), "m")],
        ]
        return "\n".join(lines), self._kbd(rows)

    # ------------------------------------------------------------------ #
    # Callback payloads
    # ------------------------------------------------------------------ #
    def _cb(self, *parts: Any) -> str:
        return ":".join([CB_PREFIX] + [str(part) for part in parts])

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    async def open_selector(self, event: Any, uid: int, role: str = ROLE_SOURCE,
                            refresh: bool = True) -> None:
        """Start (or resume) the picker on one role."""
        lang = self._resolve_lang(uid)
        st = self._state(uid)
        st.step = ""
        buf = self._sel_buffer(st)
        buf[ROLE_KEY] = role_key(role)
        buf[PAGE_KEY] = 0
        if refresh or not buf.get(CHATS_KEY):
            try:
                await event.answer(self._t("ui_loading_dialogs", lang=lang))
            except Exception:
                pass
            chats = await self._selector_chats(uid, refresh=True)
        else:
            # A resume without a fetch still owes Telegram an answer: a
            # callback left unanswered holds the presser's client for the
            # whole ask-and-never-answer duration (end to end this showed
            # up as a ten-second spinner on the real bot).
            await self._safe_answer(event)
            chats = await self._selector_chats(uid, refresh=False)
        self._selector_store(uid, chats)
        selection = self._sel_selection(st)
        known = [str(chat.get("id") or "") for chat in chats]
        selection.prune(ROLE_SOURCE, known or None)
        selection.prune(ROLE_TARGET, known or None)
        self._sel_save(st, selection)
        await self._persist(uid, st)
        text, markup = self.render_selector(st, role_key(role), lang, self._sel_note(st))
        await self._safe_edit(event, text, reply_markup=markup)

    async def _sel_show(self, event: Any, uid: int, role: str, note: str = "") -> None:
        st = self._state(uid)
        self._sel_buffer(st)[NOTE_KEY] = note
        await self._persist(uid, st)
        text, markup = self.render_selector(st, role, self._resolve_lang(uid), note)
        await self._safe_edit(event, text, reply_markup=markup)

    @staticmethod
    async def _safe_answer(event: Any, **kw) -> None:
        """Terminate the inline query, errors and old queries included."""
        try:
            await event.answer(**kw)
        except Exception:
            pass

    def _sel_lookup(self, uid: int, chat_id: str) -> Optional[Dict[str, Any]]:
        """The caller's own row for this id, or None when it is not theirs."""
        key = unique_peer_key(chat_id)
        for chat in self._selector_cached(uid):
            if unique_peer_key(str(chat.get("id") or "")) == key:
                return chat
            username = str(chat.get("username") or "")
            if username and unique_peer_key(username) == key:
                return chat
        return None

    async def toggle_chat(self, uid: int, role: str, chat_id: str) -> Tuple[bool, str]:
        """Toggle one chat; returns (now_selected, label) or refuses."""
        row = self._sel_lookup(uid, chat_id)
        if row is None:
            return False, ""
        st = self._state(uid)
        selection = self._sel_selection(st)
        pick = PickedChat(
            chat_id=str(row.get("id") or chat_id),
            title=f"{row.get('emoji') or ''} {row.get('title') or ''}".strip(),
            state=STATE_UNCHECKED,
        )
        # Re-picking re-arms the access check: the earlier verdict belonged to
        # a chat that had been unpicked, and a stale "ok" is not a permission.
        if selection.get(role, pick.chat_id) is not None:
            selection.remove(role, pick.chat_id)
            selected = False
        else:
            selection.add(role, PickedChat(pick.chat_id, pick.title, STATE_UNCHECKED))
            selected = True
        self._sel_save(st, selection)
        await self._persist(uid, st)
        return selected, pick.label

    async def toggle_many(self, uid: int, role: str, chat_ids: Sequence[str],
                          select: bool) -> int:
        """Select or clear a list of the user's chats; returns how many changed."""
        st = self._state(uid)
        selection = self._sel_selection(st)
        changed = 0
        for chat_id in chat_ids:
            row = self._sel_lookup(uid, chat_id)
            if row is None:
                continue
            pick = PickedChat(
                str(row.get("id") or chat_id),
                f"{row.get('emoji') or ''} {row.get('title') or ''}".strip(),
            )
            if select and not selection.has(role, pick.chat_id):
                if selection.add(role, pick):
                    changed += 1
            elif not select and selection.remove(role, pick.chat_id):
                changed += 1
        self._sel_save(st, selection)
        await self._persist(uid, st)
        return changed

    async def merge_ids(self, uid: int, role: str, raw: str) -> Tuple[int, int, int]:
        """Parse and merge a pasted batch; returns (added, duplicates, rejected)."""
        parsed = parse_id_batch(raw, limit=MAX_BATCH)
        st = self._state(uid)
        selection = self._sel_selection(st)
        picks = [PickedChat(chat_id=item) for item in parsed.ids]
        result = selection.merge(role, picks)
        self._sel_save(st, selection)
        await self._persist(uid, st)
        # A duplicate is a duplicate however the user produced it: twice in one
        # paste, or once in the paste and already picked on a screen. Overflow
        # is "not added", same bucket as a rejected token.
        duplicates = len(parsed.duplicates) + len(result.duplicates)
        rejected = len(parsed.rejected) + len(result.overflow)
        return len(result.added), duplicates, rejected

    async def confirm_selection(self, event: Any, uid: int) -> None:
        """Probe the picks, then show the plan for approval."""
        lang = self._resolve_lang(uid)
        st = self._state(uid)
        selection = self._sel_selection(st)
        if not selection.count(ROLE_SOURCE) or not selection.count(ROLE_TARGET):
            await event.answer(self._t("ui_sel_need_both", lang=lang), show_alert=True)
            role = ROLE_SOURCE if not selection.count(ROLE_SOURCE) else ROLE_TARGET
            return await self._sel_show(event, uid, role, self._t("ui_sel_need_both", lang=lang))

        try:
            await event.answer(self._t("ui_sel_checking", lang=lang))
        except Exception:
            pass
        validation = await validate_selection(selection, self._selector_probe(), uid)
        self._sel_save(st, selection)
        plan = plan_forward_rules(selection, await self._owner_rules(uid),
                                  overlap=validation.overlap)
        st.step = ""
        await self._persist(uid, st)
        text, markup = self.render_confirm(st, lang, validation, plan, self._sel_note(st))
        await self._safe_edit(event, text, reply_markup=markup)

    async def _owner_rules(self, uid: int) -> List[Any]:
        try:
            return list(await self._rules.list_by_owner(uid) or [])
        except Exception as exc:  # noqa: BLE001 - duplicate check is best effort
            logger.warning("[PIPELINE:SELECT] rule list failed uid=%s: %s", uid, exc)
            return []

    async def create_from_selection(self, event: Any, uid: int) -> None:
        """Create every planned rule and report one outcome per source."""
        await self._safe_answer(event)
        lang = self._resolve_lang(uid)
        st = self._state(uid)
        selection = self._sel_selection(st)
        sessions = await self._sessions.list_by_owner(uid) if self._sessions else []
        if not sessions:
            return await self._safe_edit(
                event, self._t("ui_no_sessions", lang=lang),
                reply_markup=self._sessions_menu())
        validation = await validate_selection(selection, self._selector_probe(), uid)
        self._sel_save(st, selection)
        plan = plan_forward_rules(selection, await self._owner_rules(uid),
                                  overlap=validation.overlap)
        if not plan.plans:
            text, markup = self.render_confirm(st, lang, validation, plan,
                                               self._t("ui_sel_nothing_create", lang=lang))
            return await self._safe_edit(event, text, reply_markup=markup)

        outcomes = await apply_forward_plan(
            plan, self._rules, session_id=sessions[0].id, owner_user_id=uid)
        links = sum(item.links for item in outcomes)
        try:
            self._log.info("bot", "rule", f"route created uid={uid} rules={len(outcomes)} links={links}")
        except Exception:
            pass
        self._sel_reset(st)
        await self._persist(uid, st)
        text, markup = self.render_outcome(outcomes, lang, links)
        await self._safe_edit(event, text, reply_markup=markup)

    # ------------------------------------------------------------------ #
    # Text steps
    # ------------------------------------------------------------------ #
    def _sel_step_role(self, step: str) -> str:
        return ROLE_TARGET if step.endswith("_t") else ROLE_SOURCE

    async def _step_sel_ids(self, message: Message, st: Any, text: str,
                            role: str) -> None:
        """Direct id entry: only this step reads ids, never a list screen."""
        lang = self._resolve_lang(int(message.from_user.id if message.from_user else 0))
        if is_cancel_word(text):
            st.step = ""
            await self._persist(int(message.from_user.id if message.from_user else 0), st)
            return await message.reply_text(self._t("ui_cancel_back", lang=lang),
                                            reply_markup=self._main_menu())
        uid = int(message.from_user.id if message.from_user else 0)
        added, duplicates, rejected = await self.merge_ids(uid, role, text)
        if added == 0 and rejected == 0 and duplicates == 0:
            note = self._t("ui_sel_ids_empty", lang=lang)
        else:
            note = self._t("ui_sel_ids_merged", lang=lang, added=added,
                           duplicate=duplicates, rejected=rejected)
        if rejected:
            parsed = parse_id_batch(text, limit=MAX_BATCH)
            note = note + "\n" + self._t(
                "ui_sel_ids_rejected", lang=lang, ids=", ".join(parsed.rejected[:5])
            )
        st = self._state(uid)
        st.step = ""
        self._sel_buffer(st)[ROLE_KEY] = role
        await self._persist(uid, st)
        text_out, markup = self.render_selector(st, role, lang, note)
        await message.reply_text(text_out, reply_markup=markup)

    async def _step_sel_ids_s(self, message: Message, st: Any, text: str) -> None:
        await self._step_sel_ids(message, st, text, ROLE_SOURCE)

    async def _step_sel_ids_t(self, message: Message, st: Any, text: str) -> None:
        await self._step_sel_ids(message, st, text, ROLE_TARGET)

    async def _step_sel_query(self, message: Message, st: Any, text: str,
                              role: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        lang = self._resolve_lang(uid)
        raw = (text or "").strip()
        self._sel_set_query(st, role, "" if raw.lower() in ("*", "-", "clear") else raw)
        st.step = ""
        self._sel_buffer(st)[ROLE_KEY] = role
        self._sel_buffer(st)[PAGE_KEY] = 0
        await self._persist(uid, st)
        note = self._t("ui_sel_search_applied", lang=lang, q=self._sel_query(st, role) or "*")
        text_out, markup = self.render_selector(st, role, lang, note)
        await message.reply_text(text_out, reply_markup=markup)

    async def _step_sel_query_s(self, message: Message, st: Any, text: str) -> None:
        await self._step_sel_query(message, st, text, ROLE_SOURCE)

    async def _step_sel_query_t(self, message: Message, st: Any, text: str) -> None:
        await self._step_sel_query(message, st, text, ROLE_TARGET)

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register_forward_select_handlers(self, b: Any) -> None:
        """Mount the picker's callbacks; text steps are found by `_route_text`."""
        prefix = CB_PREFIX
        handler = self

        @b.on_callback_query(filters.regex(rf"^{prefix}:open(?::([st]))?$"))
        async def _cb_open(_client, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            match = cq.matches[0] if getattr(cq, "matches", None) else None
            role = role_key(match.group(1) if match and match.group(1) else ROLE_SOURCE)
            await handler.open_selector(cq, uid, role)

        @b.on_callback_query(filters.regex(rf"^{prefix}:r:([st])$"))
        async def _cb_role(_client, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            role = role_key(cq.matches[0].group(1))
            st = handler._state(uid)
            handler._sel_buffer(st)[ROLE_KEY] = role
            handler._sel_buffer(st)[PAGE_KEY] = 0
            await handler._persist(uid, st)
            await handler._safe_answer(cq)
            await handler._sel_show(cq, uid, role)

        @b.on_callback_query(filters.regex(rf"^{prefix}:pg:([st]):(\d{{1,4}})$"))
        async def _cb_page(_client, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            match = cq.matches[0]
            role, page = role_key(match.group(1)), int(match.group(2))
            st = handler._state(uid)
            handler._sel_buffer(st)[ROLE_KEY] = role
            handler._sel_buffer(st)[PAGE_KEY] = page
            await handler._persist(uid, st)
            await handler._safe_answer(cq)
            await handler._sel_show(cq, uid, role)

        @b.on_callback_query(filters.regex(rf"^{prefix}:t:([st]):(.+)$"))
        async def _cb_toggle(_client, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            match = cq.matches[0]
            role, chat_id = role_key(match.group(1)), match.group(2)
            lang = handler._resolve_lang(uid)
            if not handler._selector_cached(uid):
                return await cq.answer(handler._t("ui_sel_stale", lang=lang), show_alert=True)
            selected, label = await handler.toggle_chat(uid, role, chat_id)
            if not label:
                return await cq.answer(handler._t("ui_sel_not_in_list", lang=lang),
                                       show_alert=True)
            note = handler._t(
                "ui_sel_picked" if selected else "ui_sel_unpicked", lang=lang, name=_plain(label)
            )
            await cq.answer(note, show_alert=False)
            await handler._sel_show(cq, uid, role)

        @b.on_callback_query(filters.regex(rf"^{prefix}:(all|none):([st])$"))
        async def _cb_mass(_client, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            match = cq.matches[0]
            verb, role = match.group(1), role_key(match.group(2))
            st = handler._state(uid)
            chats = handler._sel_filtered(
                handler._selector_cached(uid), handler._sel_query(st, role)
            )
            ids = [str(chat.get("id") or "") for chat in chats]
            changed = await handler.toggle_many(uid, role, ids, select=(verb == "all"))
            lang = handler._resolve_lang(uid)
            await cq.answer(handler._t("ui_sel_changed", lang=lang, count=changed))
            await handler._sel_show(cq, uid, role)

        @b.on_callback_query(filters.regex(rf"^{prefix}:(q|ids):([st])$"))
        async def _cb_prompt(_client, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            match = cq.matches[0]
            verb, role = match.group(1), role_key(match.group(2))
            st = handler._state(uid)
            handler._sel_buffer(st)[ROLE_KEY] = role
            lang = handler._resolve_lang(uid)
            if verb == "q":
                st.step = STEP_SEARCH_TARGET if role == ROLE_TARGET else STEP_SEARCH_SOURCE
                prompt = handler._t("ui_sel_search_prompt", lang=lang)
            else:
                st.step = STEP_IDS_TARGET if role == ROLE_TARGET else STEP_IDS_SOURCE
                prompt = handler._t("ui_sel_ids_prompt", lang=lang, max=MAX_BATCH)
            await handler._persist(uid, st)
            await handler._safe_edit(cq, prompt, reply_markup=handler._kbd([
                [(handler._t("ui_sel_cancel", lang=lang), handler._cb("cancel"))]
            ]))
            await cq.answer()

        @b.on_callback_query(filters.regex(rf"^{prefix}:done$"))
        async def _cb_done(_client, cq: CallbackQuery):
            await handler.confirm_selection(cq, cq.from_user.id if cq.from_user else 0)

        @b.on_callback_query(filters.regex(rf"^{prefix}:go$"))
        async def _cb_go(_client, cq: CallbackQuery):
            await handler.create_from_selection(cq, cq.from_user.id if cq.from_user else 0)

        @b.on_callback_query(filters.regex(rf"^{prefix}:back$"))
        async def _cb_back(_client, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = handler._state(uid)
            await handler._safe_answer(cq)
            await handler._sel_show(cq, uid, handler._sel_role(st))

        @b.on_callback_query(filters.regex(rf"^{prefix}:cancel$"))
        async def _cb_cancel(_client, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = handler._state(uid)
            handler._sel_reset(st)
            await handler._persist(uid, st)
            lang = handler._resolve_lang(uid)
            await cq.answer(handler._t("ui_op_cancelled", lang=lang))
            await handler._safe_edit(
                cq, handler._t("ui_op_cancelled", lang=lang),
                reply_markup=handler._main_menu())
