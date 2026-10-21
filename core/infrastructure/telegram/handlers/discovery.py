"""`/getchannel`, `/getgroup`, `/getuser`, `/getbot`: what the account can see.

Presentation only: the catalog itself comes from the application use case, and
the scan belongs to the infrastructure gateway. This module decides how a page
of that catalog is worded and which buttons it carries.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from pyrogram.types import Message

logger = logging.getLogger(__name__)

#: How many chats one discovery reply prints before it summarises the rest.
DISCOVERY_LIMIT = 40


class DiscoveryHandlers:
    """Discovery commands, mounted through the text-command router."""

    #: Declared to the handler registry; each name needs a ``_cmd_<name>`` here.
    discovery_commands: tuple = ("getchannel", "getgroup", "getuser", "getbot")

    async def _cmd_getchannel(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, "channel")

    async def _cmd_getgroup(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, "group")

    async def _cmd_getuser(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, "user")

    async def _cmd_getbot(self, message: Message, args: str) -> None:
        await self._cmd_discovery(message, "bot")

    async def _discovery_catalog(self, uid: int):
        """The application catalog for this user, or an empty one."""
        from core.application.discovery import DialogCatalog

        try:
            catalog = await self._dialog_catalog().execute(
                uid,
                saved_label=self._t("ui_saved_messages", lang=self._resolve_lang(uid)),
            )
        except Exception as exc:  # a dead session is a message, not a crash
            logger.exception("[PIPELINE:DISCOVERY] catalog failed for uid=%s: %s", uid, exc)
            return DialogCatalog()
        return catalog

    async def _cmd_discovery(self, message: Message, kind: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        label = self._kind_label(kind, lang)
        catalog = (await self._discovery_catalog(uid)).of_kind(kind)
        if not catalog:
            return await message.reply_text(
                self._t("cmd_list_empty", lang=lang, kind=label),
                reply_markup=self._main_menu(uid),
            )
        lines: List[str] = [
            self._t("cmd_list_title", lang=lang, kind=label, n=len(catalog)),
            "",
        ]
        for chat in list(catalog)[:DISCOVERY_LIMIT]:
            lines.append(f"{chat.emoji} {chat.title}{(' (' + chat.handle + ')') if chat.handle else ''} — `{chat.id}`")
        if len(catalog) > DISCOVERY_LIMIT:
            lines.append(self._t("ui_bl_more", lang=lang, more=len(catalog) - DISCOVERY_LIMIT))
        await message.reply_text(
            "\n".join(lines),
            reply_markup=self._kbd([[(self._btn("ui_main_menu_forward", lang=lang), "r")]]),
        )

    # Kept for the modules that only need dict rows (the rule wizard buffer).
    async def _fetch_dialogs(self, owner_user_id: Any = None) -> List[Dict[str, Any]]:
        catalog = await self._dialog_catalog().execute(
            int(owner_user_id or 0),
            saved_label=self._t(
                "ui_saved_messages", lang=self._resolve_lang(int(owner_user_id or 0))
            ),
        )
        return catalog.as_dicts()
