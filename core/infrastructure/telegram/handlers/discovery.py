"""`/getchannel`, `/getgroup`, `/getuser`, `/getbot`: the chat catalog + actions.

Presentation only: classification, ordering, freshness and paging come from
:class:`core.application.discovery.DialogCatalog` and
:class:`core.application.discovery.BrowseDialogsUseCase`; the scan belongs to
the infrastructure gateway. This module decides what a page says, which buttons
it carries, and how a chosen chat enters the staged task builder.

The four commands answer with a paged list (``/getchannel 2`` jumps to the
second page) and the same list can be navigated, switched by kind and refreshed
from the buttons underneath it. A page turn reuses the scan the application
cached; only the refresh button rescans. Buttons carry ``gch_pg:<kind>:<page>``
and are answered through the user who pressed them — never through an id stored
in the payload — so one tenant cannot page through another tenant's chats.

The catalog is the entry point for task creation: a chat's action sheet can set
it as the source (then pick a target) or as the target (then pick a source), and
either path lands in the same ``rule_source`` / ``rule_target`` wizard that the
menu's "new task" button drives. No second creation path, no second state.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from core.application.discovery import (
    KIND_BOT,
    KIND_CHANNEL,
    KIND_GROUP,
    KIND_SAVED,
    KIND_USER,
    PRIORITY,
    BrowseDialogsUseCase,
    ChatListQuery,
    ChatQueryError,
    DialogCatalog,
    parse_chat_list_query,
)

logger = logging.getLogger(__name__)

#: Chats per page in the discovery browser.
PAGE_SIZE = 6

#: Chats per page of the copy-ready `-id` list. The list is one line per chat
#: and the message must stay far under Telegram's 4096-char limit even when
#: titles are long, so the page is deliberately generous but bounded.
PLAIN_PAGE_SIZE = 20

#: The kinds a page offers a switch to, in the catalog's display order. "saved"
#: is deliberately absent: Saved Messages is a session artefact, not a task
#: target anyone browses for.
BROWSABLE_KINDS: Tuple[str, ...] = (KIND_CHANNEL, KIND_GROUP, KIND_USER, KIND_BOT)


class DiscoveryHandlers:
    """Discovery commands and the navigation buttons of their lists."""

    #: Declared to the handler registry; each name needs a ``_cmd_<name>`` here.
    discovery_commands: tuple = ("getchannel", "getgroup", "getuser", "getbot")
    #: The registry calls this to mount the callbacks.
    discovery_registration = "register_discovery_handlers"

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def _cmd_getchannel(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, KIND_CHANNEL, args)

    async def _cmd_getgroup(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, KIND_GROUP, args)

    async def _cmd_getuser(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, KIND_USER, args)

    async def _cmd_getbot(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, KIND_BOT, args)

    async def _cmd_discovery(self, message: Message, kind: str, args: str = "") -> None:
        """Answer one `/get…` command, flag-aware.

        Bare `/getchannel` (or `/getchannel 2`) keeps the interactive paged
        sheet. `-id` answers the copy-ready one-line list (name → id, the
        exact spelling `/forward add` accepts); `-all` widens a kind command
        to every kind; a page number pages the plain list. The grammar lives
        in :func:`core.application.discovery.parse_chat_list_query`; unknown
        tokens are refused with a usage line, never guessed.
        """
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        try:
            query = parse_chat_list_query(args)
        except ChatQueryError as exc:
            return await message.reply_text(
                self._t("ui_gch_bad_query", lang=lang, detail=exc.detail),
            )
        # The plain `-id` list is the user asking for the truth now: it backs
        # itself with a live rescan, which also keeps its freshness line honest.
        catalog = await self._discovery_catalog(uid, force=query.with_ids)
        if query.with_ids:
            return await self._reply_plain_catalog(
                message, catalog, kind=kind, query=query, lang=lang,
            )
        if not catalog.of_kind(kind):
            return await message.reply_text(
                self._t("ui_gch_empty", lang=lang, kind=self._kind_label(kind, lang)),
                reply_markup=self._main_menu(uid),
            )
        text, kbd = self._render_discovery_page(catalog, kind=kind, page=query.page, lang=lang)
        await message.reply_text(text, reply_markup=kbd)

    def _query_kinds(self, kind: str, query: ChatListQuery) -> Tuple[str, ...]:
        """The kinds one answer covers: the command's kind, or all of them.

        `-all` walks the catalog's own display priority and skips Saved
        Messages: it is a session artefact, not a chat a task routes through.
        """
        if query.all_kinds:
            by_priority = tuple(
                k for k, _rank in sorted(PRIORITY.items(), key=lambda item: item[1])
            )
            return tuple(k for k in by_priority if k != KIND_SAVED)
        return (kind,)

    async def _reply_plain_catalog(
        self,
        message: Message,
        catalog: DialogCatalog,
        kind: str,
        query: ChatListQuery,
        lang: str,
    ) -> None:
        """The copy-ready one-line list: `N. Name → `<id>``.

        No buttons by design — this answer exists to be copied from, and one
        id per line pastes straight into `/forward add`. A live rescan backs
        it (counts as an explicit refresh), so the freshness line is honest.
        """
        kinds = self._query_kinds(kind, query)
        entries = [entry for entry in catalog.entries if entry.kind in kinds]
        if not entries:
            return await message.reply_text(
                self._t("ui_gch_empty", lang=lang, kind=self._kind_label(kind, lang)),
            )
        total = len(entries)
        total_pages = max(1, -(-total // PLAIN_PAGE_SIZE))
        index = max(0, min(query.page, total_pages - 1))
        chunk = entries[index * PLAIN_PAGE_SIZE:(index + 1) * PLAIN_PAGE_SIZE]

        header = self._t("cmd_kind_all", lang=lang) if query.all_kinds else self._kind_label(kind, lang)
        lines: List[str] = [self._t("ui_gch_title", lang=lang, kind=header, n=total)]
        lines.append(f"*{self._t('ui_gch_refreshed', lang=lang)}*")
        lines.append("────────────────────")
        last_kind: Optional[str] = None
        for offset, entry in enumerate(chunk):
            number = index * PLAIN_PAGE_SIZE + offset + 1
            if query.all_kinds and entry.kind != last_kind:
                last_kind = entry.kind
                lines.append(f"{entry.emoji} **{self._kind_label(entry.kind, lang)}**")
            handle = f" • {entry.handle}" if entry.handle else ""
            lines.append(f"`{number}.` **{entry.title}** \u2192 `{entry.id}`{handle}")
        lines.append("────────────────────")
        if total_pages > 1:
            lines.append(
                self._t(
                    "ui_picker_page_info",
                    lang=lang, page=index + 1, total=total_pages, count=total,
                )
            )
            lines.append(self._t(
                "ui_gch_plain_pages", lang=lang,
                next=min(index + 2, total_pages),
            ))
        lines.append(self._t("ui_gch_plain_footer", lang=lang))
        await message.reply_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Application integration
    # ------------------------------------------------------------------ #
    def _browse_discovery(self) -> BrowseDialogsUseCase:
        """The browse use case over the session-backed catalog.

        Built once per panel, so its per-owner cache survives between commands
        and between page turns — that is the whole point of paging without a
        rescan.
        """
        if getattr(self, "_browse_use_case", None) is None:
            self._browse_use_case = BrowseDialogsUseCase(
                self._dialog_catalog(), page_size=PAGE_SIZE
            )
        return self._browse_use_case

    async def _discovery_catalog(self, uid: int, force: bool = False) -> DialogCatalog:
        """The application catalog for this user, or an empty one.

        A dead session is a message, not a crash: the caller renders the empty
        state and the gateway has already logged why it found nothing. Freshness
        is the use case's policy (per owner, bounded); ``force`` is the refresh
        button asking for the truth now.
        """
        try:
            return await self._browse_discovery().catalog(
                uid,
                refresh=force,
                saved_label=self._t("ui_saved_messages", lang=self._resolve_lang(uid)),
            )
        except Exception as exc:
            logger.exception("[PIPELINE:DISCOVERY] catalog failed for uid=%s: %s", uid, exc)
            return DialogCatalog()

    async def _fetch_dialogs(self, owner_user_id: Any = None) -> List[Dict[str, Any]]:
        """Dict rows for the wizard buffers (compatibility with the FSM)."""
        uid = int(owner_user_id or 0)
        catalog = await self._dialog_catalog().execute(
            uid,
            saved_label=self._t("ui_saved_messages", lang=self._resolve_lang(uid)),
        )
        return catalog.as_dicts()

    async def _catalog_entry(self, uid: int, token: str):
        """Resolve a button token — a chat id or @username — against the catalog."""
        return (await self._discovery_catalog(uid)).find(token)

    # ------------------------------------------------------------------ #
    # Renderers
    # ------------------------------------------------------------------ #
    def _render_discovery_page(
        self,
        catalog: DialogCatalog,
        kind: str,
        page: int = 0,
        lang: Optional[str] = None,
    ) -> Tuple[str, InlineKeyboardMarkup]:
        cur_lang = lang or "en"
        label = self._kind_label(kind, cur_lang)
        p = catalog.of_kind(kind).page(page, page_size=PAGE_SIZE)
        totals = catalog.kinds()

        lines: List[str] = [
            self._t("ui_gch_title", lang=cur_lang, kind=label, n=p.total),
            f"*{self._t('ui_gch_hint', lang=cur_lang)}*",
            "────────────────────",
        ]
        for idx, entry in enumerate(p.entries, start=p.start + 1):
            handle = f" • {entry.handle}" if entry.handle else ""
            pin = "📌 " if entry.pinned else ""
            lines.append(f"{entry.emoji} {pin}**{idx}. {entry.title}**\n   🆔 `{entry.id}`{handle}")
        lines.append("────────────────────")
        lines.append(
            self._t(
                "ui_picker_page_info",
                lang=cur_lang,
                page=p.page + 1,
                total=p.total_pages,
                count=p.total,
            )
        )

        rows: List[List[Tuple[str, str]]] = []
        for local_idx, entry in enumerate(p.entries):
            idx = p.start + local_idx
            name = entry.title[:24] + "…" if len(entry.title) > 24 else entry.title
            rows.append([(f"{entry.emoji} {idx + 1}. {name}", f"gch_v:{kind}:{idx}")])

        nav: List[Tuple[str, str]] = []
        if p.has_prev:
            nav.append((self._btn("ui_prev", lang=cur_lang), f"gch_pg:{kind}:{p.page - 1}"))
        nav.append((f"{p.page + 1} / {p.total_pages}", "noop"))
        if p.has_next:
            nav.append((self._t("ui_next", lang=cur_lang) + " ▶️", f"gch_pg:{kind}:{p.page + 1}"))
        rows.append(nav)

        # Switching kind keeps the list, not the page: another kind's pages have
        # their own numbering, so its first page is the only safe target.
        switch: List[Tuple[str, str]] = []
        for other in BROWSABLE_KINDS:
            if other == kind or not totals.get(other):
                continue
            switch.append(
                (f"{self._kind_label(other, cur_lang)} ({totals[other]})", f"gch_pg:{other}:0")
            )
        if switch:
            rows.append(switch)

        rows.append([
            (self._t("ui_refresh_list", lang=cur_lang), f"gch_rf:{kind}:{p.page}"),
            (self._btn("ui_back", lang=cur_lang), "m"),
        ])
        return "\n".join(lines), self._kbd(rows)

    def _render_chat_actions(
        self,
        entry: Any,
        kind: str,
        page: int,
        lang: str,
    ) -> Tuple[str, InlineKeyboardMarkup]:
        extras: List[str] = []
        if entry.username:
            extras.append(self._t("ui_gch_username", lang=lang, username=entry.username))
        extras.append(self._t("ui_gch_kind", lang=lang, kind=self._kind_label(entry.kind, lang)))
        text = self._t(
            "ui_gch_detail",
            lang=lang,
            title=f"{entry.emoji} {entry.title}",
            id=entry.id,
            extra="\n".join(extras),
        )

        # Buttons carry the chat id, not its page index: an index is only
        # meaningful against one snapshot of the list, an id survives a refresh.
        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_gch_as_source", lang=lang), f"gch_src:{entry.id}"),
                (self._t("ui_gch_as_target", lang=lang), f"gch_tgt:{entry.id}"),
            ],
            [(self._t("ui_gch_tasks", lang=lang), f"gch_tsk:{entry.id}")],
            [(self._t("ui_gch_back", lang=lang), f"gch_pg:{kind}:{page}")],
        ]
        return text, self._kbd(rows)

    # ------------------------------------------------------------------ #
    # Callbacks
    # ------------------------------------------------------------------ #
    def register_discovery_handlers(self, b) -> None:
        @b.on_callback_query(filters.regex(r"^gch_pg:([a-z_]+):(\d+)$"))
        @self._localized_handler
        async def _gch_page(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            kind = cq.matches[0].group(1)
            page = int(cq.matches[0].group(2))
            lang = self._resolve_lang(uid)
            catalog = await self._discovery_catalog(uid)
            if not catalog.of_kind(kind):
                return await cq.answer(
                    self._t("ui_gch_empty", lang=lang, kind=self._kind_label(kind, lang)),
                    show_alert=True,
                )
            text, kbd = self._render_discovery_page(catalog, kind=kind, page=page, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^gch_rf:([a-z_]+):(\d+)$"))
        @self._localized_handler
        async def _gch_refresh(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            kind = cq.matches[0].group(1)
            page = int(cq.matches[0].group(2))
            lang = self._resolve_lang(uid)
            catalog = await self._discovery_catalog(uid, force=True)
            if not catalog.of_kind(kind):
                return await cq.answer(
                    self._t("ui_gch_empty", lang=lang, kind=self._kind_label(kind, lang)),
                    show_alert=True,
                )
            # Refresh rescans but keeps the page: the user asked for the truth,
            # not to be thrown back to the top of the list.
            text, kbd = self._render_discovery_page(catalog, kind=kind, page=page, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^gch_v:([a-z_]+):(\d+)$"))
        @self._localized_handler
        async def _gch_view(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            kind = cq.matches[0].group(1)
            index = int(cq.matches[0].group(2))
            lang = self._resolve_lang(uid)
            entry = (await self._discovery_catalog(uid)).of_kind(kind).at(index)
            if not entry:
                return await cq.answer(self._t("ui_none", lang=lang), show_alert=True)
            if await self._settle_flow(uid, reason="discovery:view"):
                # A tap is a navigation decision: it disarms any pending prompt
                # instead of leaving the next message to answer an abandoned one.
                pass
            text, kbd = self._render_chat_actions(entry, kind=kind, page=index // PAGE_SIZE, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^gch_src:(.+)$"))
        @self._localized_handler
        async def _gch_use_source(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            token = cq.matches[0].group(1)
            lang = self._resolve_lang(uid)
            entry = await self._catalog_entry(uid, token)
            if not entry:
                return await cq.answer(self._t("ui_none", lang=lang), show_alert=True)

            st = self._state(uid)
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            st.rule_source = entry.id
            st.rule_target = ""
            st.buffer["source_name"] = entry.title
            st.step = "rule_target"
            st.buffer["page"] = 0
            st.buffer["chats"] = (await self._discovery_catalog(uid)).as_dicts()
            st.buffer.pop("pending_target", None)
            await self._persist(uid, st)

            await cq.answer(self._t("ui_src_selected", lang=lang, title=entry.title))
            text, kbd = self._render_chat_picker(st, role="target", page=0, uid=uid)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^gch_tgt:(.+)$"))
        @self._localized_handler
        async def _gch_use_target(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            token = cq.matches[0].group(1)
            lang = self._resolve_lang(uid)
            entry = await self._catalog_entry(uid, token)
            if not entry:
                return await cq.answer(self._t("ui_none", lang=lang), show_alert=True)

            st = self._state(uid)
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            st.rule_target = entry.id
            st.buffer["target_name"] = entry.title
            if getattr(st, "rule_source", ""):
                # Source was already chosen: this completes the staged build.
                source_title = st.buffer.get("source_name") or st.rule_source
                await cq.answer(self._t("ui_creating_rule", lang=lang))
                return await self._finish_rule_creation(cq, st, source_title, entry.title)

            # Target first: arm the source picker, which will complete the rule.
            st.buffer["pending_target"] = {"id": entry.id, "title": entry.title}
            st.step = "rule_source"
            st.buffer["page"] = 0
            st.buffer["chats"] = (await self._discovery_catalog(uid)).as_dicts()
            await self._persist(uid, st)
            await cq.answer(self._t("ui_tgt_selected", lang=lang, title=entry.title))
            text, kbd = self._render_chat_picker(st, role="source", page=0, uid=uid)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^gch_tsk:(.+)$"))
        @self._localized_handler
        async def _gch_tasks(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            token = cq.matches[0].group(1)
            lang = self._resolve_lang(uid)
            entry = await self._catalog_entry(uid, token)
            if not entry:
                return await cq.answer(self._t("ui_none", lang=lang), show_alert=True)

            rules = await self._rules.list_by_owner(uid) if (self._rules and uid) else []
            related = [
                r for r in rules
                if str(getattr(r, "source_chat_id", "")) == str(entry.id)
                or str(getattr(r, "target_chat_id", "")) == str(entry.id)
            ]

            lines: List[str] = [self._t("ui_gch_tasks_title", lang=lang, title=entry.title), ""]
            rows: List[List[Tuple[str, str]]] = []
            if not related:
                lines.append(self._t("ui_gch_tasks_empty", lang=lang))
            for pos, rule in enumerate(related[:PAGE_SIZE], start=1):
                status = "🟢" if rule.is_active else "🔴"
                src = rule.source_chat_name or rule.source_chat_id
                tgt = rule.target_chat_name or rule.target_chat_id
                lines.append(f"{status} **{pos}.** `{rule.id[:8]}` {src} ➔ {tgt}")
                rows.append([
                    (f"{status} {src[:14]} ➔ {tgt[:14]}", f"rd:{rule.id}"),
                    (self._t("ui_fwd_open", lang=lang), f"fwd_st:{rule.id}"),
                ])
            rows.append([(self._t("ui_gch_back", lang=lang), f"gch_v:{entry.kind}:{entry.id}")])
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._kbd(rows))
            await cq.answer()