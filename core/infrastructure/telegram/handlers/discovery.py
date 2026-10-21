"""`/getchannel`, `/getgroup`, `/getuser`, `/getbot`: the chat catalog + actions.

Presentation only: classification, ordering and paging come from
:class:`core.application.discovery.DialogCatalog`; the scan belongs to the
infrastructure gateway. This module decides what a page says, which buttons it
carries, and how a chosen chat enters the staged task builder.

The catalog is the entry point for task creation: a chat's action sheet can set
it as the source (then pick a target) or as the target (then pick a source), and
either path lands in the same ``rule_source`` / ``rule_target`` wizard that the
menu's "new task" button drives. No second creation path, no second state.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from core.application.discovery import DialogCatalog

logger = logging.getLogger(__name__)

#: Chats per page in the discovery browser.
PAGE_SIZE = 6
#: How long a fetched catalog backs the rendered buttons. A scan hits Telegram
#: (up to 12s); a tap must not repeat it, and the index in a button resolves
#: against the same list the user is looking at.
CATALOG_TTL = 600.0


class DiscoveryHandlers:
    """Discovery commands and their callback surface."""

    #: Declared to the handler registry; each name needs a ``_cmd_<name>`` here.
    discovery_commands: tuple = ("getchannel", "getgroup", "getuser", "getbot")
    #: The registry calls this to mount the callbacks.
    discovery_registration = "register_discovery_handlers"

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def _cmd_getchannel(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, "channel")

    async def _cmd_getgroup(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, "group")

    async def _cmd_getuser(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, "user")

    async def _cmd_getbot(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, "bot")

    async def _cmd_discovery(self, message: Message, kind: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        catalog = await self._discovery_catalog(uid)
        picked = catalog.of_kind(kind)
        if not picked:
            return await message.reply_text(
                self._t("ui_gch_empty", lang=lang, kind=self._kind_label(kind, lang)),
                reply_markup=self._main_menu(uid),
            )
        text, kbd = self._render_discovery_page(picked, kind=kind, page=0, lang=lang)
        await message.reply_text(text, reply_markup=kbd)

    # ------------------------------------------------------------------ #
    # Application integration
    # ------------------------------------------------------------------ #
    async def _discovery_catalog(self, uid: int, force: bool = False) -> DialogCatalog:
        """The application catalog for this user, or an empty one.

        A dead session is a message, not a crash: the caller renders the empty
        state and the gateway has already logged why it found nothing. The
        result is cached per user for :data:`CATALOG_TTL`, because a scan hits
        Telegram and a button index must resolve against the list the user saw.
        """
        cache = getattr(self, "_discovery_cache", None)
        if cache is None:
            cache = self._discovery_cache = {}
        hit = cache.get(int(uid))
        if hit and not force and (time.monotonic() - hit[0]) < CATALOG_TTL:
            return hit[1]
        try:
            catalog = await self._dialog_catalog().execute(
                uid,
                saved_label=self._t("ui_saved_messages", lang=self._resolve_lang(uid)),
            )
        except Exception as exc:
            logger.exception("[PIPELINE:DISCOVERY] catalog failed for uid=%s: %s", uid, exc)
            return DialogCatalog()
        cache[int(uid)] = (time.monotonic(), catalog)
        return catalog

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
        p = catalog.page(page, page_size=PAGE_SIZE)

        lines: List[str] = [
            self._t("ui_gch_title", lang=cur_lang, kind=label, n=p.total),
            f"*{self._t('ui_gch_hint', lang=cur_lang)}*",
            "────────────────────",
        ]
        for idx, entry in enumerate(p.entries, start=p.start + 1):
            handle = f" • {entry.handle}" if entry.handle else ""
            lines.append(f"{entry.emoji} **{idx}. {entry.title}**\n   🆔 `{entry.id}`{handle}")
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
        rows.append([
            (self._t("ui_refresh_list", lang=cur_lang), f"gch_rf:{kind}"),
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
            catalog = (await self._discovery_catalog(uid)).of_kind(kind)
            if not catalog:
                return await cq.answer(
                    self._t("ui_gch_empty", lang=lang, kind=self._kind_label(kind, lang)),
                    show_alert=True,
                )
            text, kbd = self._render_discovery_page(catalog, kind=kind, page=page, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^gch_rf:([a-z_]+)$"))
        @self._localized_handler
        async def _gch_refresh(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            kind = cq.matches[0].group(1)
            lang = self._resolve_lang(uid)
            catalog = (await self._discovery_catalog(uid, force=True)).of_kind(kind)
            if not catalog:
                return await cq.answer(
                    self._t("ui_gch_empty", lang=lang, kind=self._kind_label(kind, lang)),
                    show_alert=True,
                )
            text, kbd = self._render_discovery_page(catalog, kind=kind, page=0, lang=lang)
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