"""The bot's text-command surface: one router, one guide, no duplicates.

Everything here is presentation: it resolves the caller's task through the
owner-scoped use cases and then either renders an existing screen or applies a
small, validated change (a replace entry, an allow/block word, a clone run).

Design rules:

* The command table lives in :mod:`core.application.command_registry`; this
  module answers exactly the commands that no earlier handler owns, so a
  command is never handled twice and the guide can never drift from reality.
* Every task id a command carries is re-resolved with the authenticated user id
  (``_owned_rule``), never trusted: a command argument is a selector, not proof
  of ownership.
* Every user-visible string comes from the catalog. No copy is authored here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional, Tuple
from urllib.parse import quote

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.application.command_registry import ROUTED_TOKENS, guide_text, resolve
from core.domain.text_replace import (
    MAX_ENTRIES,
    ReplaceError,
    describe_entry,
    error_key,
    find_key,
    merge_entry,
    parse_add_arguments,
)

logger = logging.getLogger(__name__)

#: Clone bounds: a user-triggered history copy is a bounded operation, not a
#: migration tool. Both ends are enforced before the first message is sent.
_CLONE_DEFAULT = 20
_CLONE_MAX = 100
_CLONE_SLEEP = 0.15


class TextCommandMixin:
    """Slash-command handlers mounted onto :class:`ProBotUI`."""

    # ------------------------------------------------------------------ #
    # Mounting
    # ------------------------------------------------------------------ #
    def _mount_text_commands(self, b) -> None:
        """Register the commands the registry says this router owns."""

        @b.on_message(filters.command(list(ROUTED_TOKENS)) & filters.private, group=-2)
        @self._localized_handler
        async def _text_command(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            try:
                parts = (message.text or "").split(None, 1)
                name = (parts[0][1:] if parts else "").split("@", 1)[0].lower()
                args = parts[1].strip() if len(parts) > 1 else ""
                spec = resolve(name)
                if spec is None or not uid:
                    return
                logger.info("[PIPELINE:CMD] uid=%s cmd=%s args=%r", uid, spec.name, args[:60])
                await self._dispatch_text_command(message, spec.name, args)
            except Exception as exc:  # a command must never kill the dispatcher
                logger.exception("[PIPELINE:CMD] uid=%s failed: %s", uid, exc)
                try:
                    await message.reply_text(
                        self._t("cmd_failed", lang=self._resolve_lang(uid))
                    )
                except Exception:
                    pass
            finally:
                # The command is ours: keep it away from the free-text FSM
                # router in group -1. Never inside a try/except — the signal
                # travels as an exception.
                message.stop_propagation()

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    async def _dispatch_text_command(self, message: Message, name: str, args: str) -> None:
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None:
            # Registry and router disagree: a code defect, never silent.
            logger.error("[PIPELINE:CMD] no handler for registered command %r", name)
            uid = message.from_user.id if message.from_user else 0
            return await message.reply_text(self._t("cmd_failed", lang=self._resolve_lang(uid)))
        await handler(message, args)

    # ------------------------------------------------------------------ #
    # Shared resolution helpers
    # ------------------------------------------------------------------ #
    async def _cmd_rules(self, uid: int) -> List[Any]:
        try:
            return await self._rules.list_by_owner(uid) if (self._rules and uid) else []
        except Exception as exc:
            logger.warning("[PIPELINE:CMD] list_by_owner(%s) failed: %s", uid, exc)
            return []

    async def _cmd_rule_for(self, uid: int, token: str = "") -> Optional[Any]:
        """Resolve a task from a command argument (id, id prefix, name, chat)."""
        rules = await self._cmd_rules(uid)
        if not rules:
            return None
        wanted = (token or "").strip().strip("`'\"")
        if not wanted:
            st = self._state(uid)
            sticky = st.buffer.get("last_rule_id") if isinstance(st.buffer, dict) else ""
            if sticky:
                for rule in rules:
                    if str(getattr(rule, "id", "")) == str(sticky):
                        return rule
            return rules[0] if len(rules) == 1 else None
        low = wanted.lower()
        for rule in rules:
            if str(getattr(rule, "id", "")) == wanted:
                return rule
        for rule in rules:
            rid = str(getattr(rule, "id", ""))
            if len(wanted) >= 4 and rid.lower().startswith(low):
                return rule
        for rule in rules:
            name_val = str(getattr(rule, "name", "") or "")
            if name_val and name_val.lower() == low:
                return rule
        for rule in rules:
            if str(getattr(rule, "source_chat_id", "")) == wanted:
                return rule
            src_name = str(getattr(rule, "source_chat_name", "") or "").lstrip("@").lower()
            if src_name and src_name == low.lstrip("@"):
                return rule
        return None

    def _kind_label(self, kind: str, lang: str) -> str:
        """The localized word for a list kind.

        Spelled out instead of `f"cmd_kind_{kind}"`: an assembled key hides the
        catalog dependency from the i18n audit, which then cannot tell a real
        label from a typo that would render as a raw key.
        """
        if kind == "whitelist":
            key = "cmd_kind_whitelist"
        elif kind == "blacklist":
            key = "cmd_kind_blacklist"
        elif kind == "channel":
            key = "cmd_kind_channel"
        elif kind == "group":
            key = "cmd_kind_group"
        elif kind == "user":
            key = "cmd_kind_user"
        elif kind == "bot":
            key = "cmd_kind_bot"
        else:
            key = "cmd_kind_channel"
        return self._t(key, lang=lang)

    async def _cmd_rule_or_pick(self, message: Message, uid: int, token: str = "") -> Optional[Any]:
        """The task for this command, or a picker message and ``None``."""
        lang = self._resolve_lang(uid)
        rules = await self._cmd_rules(uid)
        if not rules:
            await message.reply_text(self._t("ui_forward_cmd_no_rules", lang=lang),
                                     reply_markup=self._main_menu(uid))
            return None
        rule = await self._cmd_rule_for(uid, token)
        if rule is not None:
            self._cmd_remember(uid, rule)
            return rule
        await message.reply_text(
            self._t("cmd_task_picker", lang=lang),
            reply_markup=self._rules_menu(rules, page=0, lang=lang),
        )
        return None

    def _cmd_remember(self, uid: int, rule: Any) -> None:
        """Remember the task a command addressed, so the next one can omit it."""
        try:
            st = self._state(uid)
            if isinstance(st.buffer, dict):
                st.buffer["last_rule_id"] = str(getattr(rule, "id", ""))
        except Exception:
            pass

    async def _cmd_split_task(self, uid: int, raw: str) -> Tuple[Optional[Any], str]:
        """Consume an optional leading task selector from ``raw``.

        Only consumed when it resolves to a task the caller owns AND something
        follows it — otherwise ``/replace add promo -"a" -"b"`` would read
        "promo" as a task name and lose the entry.
        """
        parts = (raw or "").strip().split(None, 1)
        if len(parts) < 2:
            return None, (raw or "").strip()
        rule = await self._cmd_rule_for(uid, parts[0])
        if rule is None:
            return None, (raw or "").strip()
        return rule, parts[1].strip()

    # ------------------------------------------------------------------ #
    # Core
    # ------------------------------------------------------------------ #
    async def _cmd_commands(self, message: Message, args: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        await message.reply_text(
            guide_text(self._t, lang=lang),
            reply_markup=self._kbd([[(self._btn("ui_main_title", lang=lang), "m")]]),
        )

    # ------------------------------------------------------------------ #
    # Forwarding
    # ------------------------------------------------------------------ #
    async def _cmd_spam(self, message: Message, args: str) -> None:
        """Mass-send tasks: one source fanned out to more than one target."""
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        rules = await self._cmd_rules(uid)
        mass = [r for r in rules if len(getattr(r, "target_chat_ids", []) or []) > 1]
        if not mass:
            return await message.reply_text(
                self._t("cmd_spam_empty", lang=lang),
                reply_markup=self._kbd([[(self._btn("ui_main_menu_forward", lang=lang), "r")]]),
            )
        lines = [self._t("cmd_spam_title", lang=lang, n=len(mass)), ""]
        rows: List[List[Tuple[str, str]]] = []
        for rule in mass[:20]:
            title = str(getattr(rule, "name", "") or getattr(rule, "source_chat_name", "") or rule.id)[:28]
            count = len(getattr(rule, "target_chat_ids", []) or [])
            lines.append(f"• {title} — {count}")
            rows.append([(f"⚙️ {title}", f"rd:{rule.id}")])
        rows.append([(self._btn("ui_main_title", lang=lang), "m")])
        await message.reply_text("\n".join(lines), reply_markup=self._kbd(rows))

    async def _cmd_publish(self, message: Message, args: str) -> None:
        """Publishing tasks: rules whose auto-post scheduler is on."""
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        rules = await self._cmd_rules(uid)
        publishing = [r for r in rules if bool(getattr(r, "auto_post_enabled", False))]
        if not publishing:
            return await message.reply_text(
                self._t("cmd_publish_empty", lang=lang),
                reply_markup=self._kbd([[(self._btn("ui_main_menu_forward", lang=lang), "r")]]),
            )
        lines = [self._t("cmd_publish_title", lang=lang, n=len(publishing)), ""]
        rows: List[List[Tuple[str, str]]] = []
        for rule in publishing[:20]:
            title = str(getattr(rule, "name", "") or getattr(rule, "source_chat_name", "") or rule.id)[:28]
            lines.append(f"• {title}")
            rows.append([(f"⚙️ {title}", f"rd:{rule.id}")])
        rows.append([(self._btn("ui_main_title", lang=lang), "m")])
        await message.reply_text("\n".join(lines), reply_markup=self._kbd(rows))

    # ------------------------------------------------------------------ #
    # Replace rules (the reason this command surface exists)
    # ------------------------------------------------------------------ #
    async def _cmd_replace(self, message: Message, args: str) -> None:
        """``/replace`` — the replace-rule manager, one list across all tasks.

        Bare ``/replace`` opens the unified manager panel: every rule the caller
        owns, however many tasks run it, with the search, the status filter, the
        paging and the whole detail screen the dashboard and the mobile client
        offer. It goes through :class:`~core.application.replace_rules.
        ReplaceRuleService`, the surface every other client uses.

        The per-task CLI verbs are kept for people who type::

            /replace add [task] NAME -"OLD" -"NEW" [--regex] [-i] [-w] [-c]
            /replace del [task] NAME|OLD
            /replace <task>        open that task's replace menu

        and they still write through the same engine, so a rule added here is the
        one `/replace` lists and one the pipeline applies.
        """
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        raw = (args or "").strip()
        sub, _, rest = raw.partition(" ")
        sub = sub.lower()
        rest = rest.strip()

        if sub in ("", "list", "show"):
            return await self._cmd_replace_panel(message, uid, lang)

        if sub in ("add", "set", "new"):
            return await self._cmd_replace_add(message, uid, lang, rest)

        if sub in ("del", "delete", "remove", "rm"):
            return await self._cmd_replace_del(message, uid, lang, rest)

        if sub in ("clear", "reset"):
            rule = await self._cmd_rule_or_pick(message, uid, rest)
            if rule is None:
                return
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["replacements"] = {}
            await self._save_owned_rule(rule, uid)
            await message.reply_text(self._t("ui_rep_all_cleared_alert", lang=lang))
            text, kbd = self._render_replace_menu(rule)
            return await message.reply_text(text, reply_markup=kbd)

        # Not a verb: treat the whole argument as a task selector.
        rule = await self._cmd_rule_or_pick(message, uid, raw)
        if rule is None:
            return
        text, kbd = self._render_replace_menu(rule)
        await message.reply_text(text, reply_markup=kbd)

    async def _cmd_replace_panel(self, message: Message, uid: int, lang: str) -> None:
        """Open the unified manager list, scoping to the caller's own rules."""
        service = getattr(self, "_replace_rules", None)
        if service is None:
            # The manager is unavailable (an older core, a wiring hole): fall
            # back to the per-task menu rather than showing an empty panel that
            # would look like "you own nothing".
            rule = await self._cmd_rule_or_pick(message, uid, "")
            if rule is None:
                return
            text, kbd = self._render_replace_menu(rule)
            return await message.reply_text(text, reply_markup=kbd)
        body, kbd = await self._render_rpl_list(uid)
        await message.reply_text(body, reply_markup=kbd)

    async def _cmd_replace_add(self, message: Message, uid: int, lang: str, rest: str) -> None:
        rule, payload = await self._cmd_split_task(uid, rest)
        if rule is None:
            rule = await self._cmd_rule_or_pick(message, uid, "")
            if rule is None:
                return
        try:
            entry, named = parse_add_arguments(payload)
            replacements = merge_entry(rule.replacements, entry)
        except ReplaceError as exc:
            return await message.reply_text(
                self._t(error_key(exc.code), lang=lang, max=MAX_ENTRIES),
                reply_markup=self._kbd([[(self._t("ui_cancel_back", lang=lang), f"rtx:{rule.id}")]]),
            )
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["replacements"] = replacements
        await self._save_owned_rule(rule, uid)
        self._cmd_remember(uid, rule)

        added = describe_entry(entry, self._t("ui_rep_word_deleted", lang=lang))
        await message.reply_text(
            self._t("ui_replace_added", lang=lang, added_list="• " + added)
        )
        text, kbd = self._render_replace_menu(rule)
        await message.reply_text(text, reply_markup=kbd)

    async def _cmd_replace_del(self, message: Message, uid: int, lang: str, rest: str) -> None:
        rule, payload = await self._cmd_split_task(uid, rest)
        if rule is None:
            rule = await self._cmd_rule_or_pick(message, uid, "")
            if rule is None:
                return
        needle = payload.strip()
        key = find_key(rule.replacements, needle)
        if not key:
            return await message.reply_text(
                self._t("ui_rep_none_found_alert", lang=lang),
                reply_markup=self._kbd([[(self._t("ui_cancel_back", lang=lang), f"rtx:{rule.id}")]]),
            )
        replacements = dict(rule.replacements)
        replacements.pop(key, None)
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["replacements"] = replacements
        await self._save_owned_rule(rule, uid)
        await message.reply_text(self._t("cmd_replace_removed", lang=lang, source=key))
        text, kbd = self._render_replace_menu(rule)
        await message.reply_text(text, reply_markup=kbd)

    # ------------------------------------------------------------------ #
    # Allow / block lists
    # ------------------------------------------------------------------ #
    async def _cmd_whitelist(self, message: Message, args: str) -> None:
        await self._cmd_word_list(message, args, kind="whitelist")

    async def _cmd_blacklist(self, message: Message, args: str) -> None:
        await self._cmd_word_list(message, args, kind="blacklist")

    async def _cmd_word_list(self, message: Message, args: str, kind: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        raw = (args or "").strip()
        sub, _, rest = raw.partition(" ")
        sub = sub.lower()
        rest = rest.strip()
        label = self._kind_label(kind, lang)
        attr = "whitelist_words" if kind == "whitelist" else "blacklist_words"
        renderer = self._render_whitelist_menu if kind == "whitelist" else self._render_blacklist_menu
        verbs = ("list", "show", "add", "set", "del", "delete", "remove", "rm", "clear", "reset")

        if sub not in verbs:
            # No verb: the whole argument is a task selector.
            rule = await self._cmd_rule_or_pick(message, uid, raw)
            if rule is None:
                return
            text, kbd = renderer(rule)
            return await message.reply_text(text, reply_markup=kbd)

        rule, payload = await self._cmd_split_task(uid, rest) if rest else (None, "")
        if rule is None:
            rule = await self._cmd_rule_or_pick(message, uid, "")
        if rule is None:
            return
        self._cmd_remember(uid, rule)

        if sub in ("list", "show"):
            text, kbd = renderer(rule)
            return await message.reply_text(text, reply_markup=kbd)

        if sub in ("add", "set"):
            words = [w.strip() for w in payload.replace("\n", ",").split(",") if w.strip()]
            if not words:
                return await message.reply_text(
                    self._t("cmd_words_empty", lang=lang, kind=label)
                )
            current = list(getattr(rule, attr) or [])
            added = 0
            for word in words:
                if word not in current:
                    current.append(word)
                    added += 1
            setattr(rule, attr, current)
            await self._save_owned_rule(rule, uid)
            await message.reply_text(self._t("cmd_words_added", lang=lang, n=added, kind=label))
            text, kbd = renderer(rule)
            return await message.reply_text(text, reply_markup=kbd)

        if sub in ("del", "delete", "remove", "rm"):
            words = [w.strip() for w in payload.replace("\n", ",").split(",") if w.strip()]
            current = list(getattr(rule, attr) or [])
            kept = [w for w in current if w not in words]
            removed = len(current) - len(kept)
            setattr(rule, attr, kept)
            await self._save_owned_rule(rule, uid)
            await message.reply_text(self._t("cmd_words_removed", lang=lang, n=removed, kind=label))
            text, kbd = renderer(rule)
            return await message.reply_text(text, reply_markup=kbd)

        # clear / reset
        setattr(rule, attr, [])
        await self._save_owned_rule(rule, uid)
        await message.reply_text(self._t("cmd_words_cleared", lang=lang, kind=label))
        text, kbd = renderer(rule)
        await message.reply_text(text, reply_markup=kbd)

    async def _cmd_filter_users(self, message: Message, args: str) -> None:
        """The task's allow/block filter set (keywords + media types)."""
        uid = message.from_user.id if message.from_user else 0
        rule = await self._cmd_rule_or_pick(message, uid, (args or "").strip())
        if rule is None:
            return
        text, kbd = self._render_filters_menu(rule)
        await message.reply_text(text, reply_markup=kbd)

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #
    async def _cmd_language(self, message: Message, args: str) -> None:
        await message.reply_text(self._t("ui_lang_choose"), reply_markup=self._lang_menu())

    async def _cmd_upgrade(self, message: Message, args: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(self._t("ui_buy_btn", lang=lang), url="https://t.me/Capxi")],
            [InlineKeyboardButton(self._btn("ui_back", lang=lang), callback_data="m")],
        ])
        await message.reply_text(self._upgrade_text(uid), reply_markup=kbd)

    async def _cmd_make_money(self, message: Message, args: str) -> None:
        """Referral link + the real number of accounts that joined through it."""
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        username = ""
        try:
            me = getattr(self.bot, "me", None)
            username = str(getattr(me, "username", "") or "")
        except Exception:
            username = ""
        if not username:
            return await message.reply_text(self._t("cmd_failed", lang=lang))
        link = f"https://t.me/{username}?start=ref_{uid}"
        count = 0
        if self._db is not None:
            try:
                count = int(self._db.count_referrals(uid))
            except Exception as exc:
                logger.warning("[PIPELINE:CMD] referral count failed for %s: %s", uid, exc)
        share = f"https://t.me/share/url?url={quote(link, safe='')}"
        await message.reply_text(
            self._t("cmd_make_money_body", lang=lang, link=link, count=count),
            reply_markup=self._kbd([
                [InlineKeyboardButton(self._btn("cmd_make_money_btn", lang=lang), url=share)],
                [(self._btn("ui_main_title", lang=lang), "m")],
            ]),
        )

    async def _cmd_get_app(self, message: Message, args: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        web = self._web_base_url()
        android = "https://github.com/Opselon/AutoTelegramForward/releases/latest"
        await message.reply_text(
            self._t("cmd_get_app_body", lang=lang, web=web),
            reply_markup=self._kbd([
                [InlineKeyboardButton(self._btn("cmd_app_web", lang=lang), url=web)],
                [InlineKeyboardButton(self._btn("cmd_app_android", lang=lang), url=android)],
                [(self._btn("ui_main_title", lang=lang), "m")],
            ]),
        )

    async def _cmd_disconnect(self, message: Message, args: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
        lines = [self._t("ui_sessions_title", lang=lang), ""]
        if not sessions:
            lines.append(self._t("ui_none", lang=lang))
        for session in sessions:
            try:
                live = self._pool.get(session.id) is not None
            except Exception:
                live = False
            mark = "🟢" if live else ("🟡" if session.is_active else "🔴")
            lines.append(f"{mark} `{session.id[:8]}` +{session.phone_number}")
        await message.reply_text("\n".join(lines), reply_markup=self._sessions_menu())

    # ------------------------------------------------------------------ #
    # Clone
    # ------------------------------------------------------------------ #
    async def _cmd_clone(self, message: Message, args: str) -> None:
        """``/clone <source> <target> <count>`` — copy recent history between chats."""
        uid = message.from_user.id if message.from_user else 0
        lang = self._resolve_lang(uid)
        parts = (args or "").split()
        if len(parts) < 3:
            return await message.reply_text(self._t("cmd_clone_usage", lang=lang))
        source, target, raw_count = parts[0], parts[1], parts[2]
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return await message.reply_text(self._t("cmd_clone_usage", lang=lang))
        count = max(1, min(count, _CLONE_MAX))

        client = await self._cmd_client(uid)
        if client is None:
            return await message.reply_text(self._t("ui_no_sessions", lang=lang))

        status = await message.reply_text(self._t("cmd_clone_running", lang=lang, n=count))
        copied = failed = 0
        try:
            ids: List[int] = []
            async for msg in client.get_chat_history(source, limit=count):
                ids.append(int(msg.id))
            ids.sort()
            target_peer = int(target) if str(target).lstrip("-").isdigit() else target
            for msg_id in ids:
                try:
                    await client.copy_message(target_peer, source, msg_id)
                    copied += 1
                except Exception as exc:
                    failed += 1
                    logger.warning("[PIPELINE:CLONE] uid=%s msg=%s failed: %s", uid, msg_id, exc)
                await asyncio.sleep(_CLONE_SLEEP)
        except Exception as exc:
            logger.warning("[PIPELINE:CLONE] uid=%s aborted: %s", uid, exc)
            return await message.reply_text(
                self._t("cmd_clone_failed", lang=lang, error=str(exc)[:120])
            )
        text = self._t("cmd_clone_done", lang=lang, copied=copied, total=copied + failed)
        try:
            await status.edit_text(text)
        except Exception:
            await message.reply_text(text)

    async def _cmd_client(self, uid: int) -> Optional[Any]:
        """The caller's first live client, reconnecting a stored session once."""
        sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
        for session in sessions:
            if not getattr(session, "is_active", True):
                continue
            client = self._pool.get(session.id)
            if client is not None and getattr(client, "is_connected", False):
                return client
            # Only the plaintext session string can be restored: the encrypted
            # column is ciphertext and `start_with_session_string` would fail
            # silently on it (see the dialog-fetch path for the same trap).
            sess_str = getattr(session, "session_string", "") or ""
            if not sess_str:
                continue
            try:
                await self._pool.start_with_session_string(
                    session.id, sess_str, credential_id=getattr(session, "credential_id", "default")
                )
            except Exception as exc:
                logger.warning("[PIPELINE:CLONE] session %s reconnect failed: %s", session.id[:8], exc)
                continue
            client = self._pool.get(session.id)
            if client is not None and getattr(client, "is_connected", False):
                return client
        return None

    # ------------------------------------------------------------------ #
    # Referral capture (called from /start)
    # ------------------------------------------------------------------ #
    async def _record_referral(self, uid: int, text: str) -> None:
        """Attribute ``/start ref_<id>`` once, best effort, never raising."""
        try:
            parts = (text or "").split()
            if len(parts) < 2 or self._db is None:
                return
            payload = parts[1].strip()
            if not payload.lower().startswith("ref"):
                return
            inviter = payload[3:].lstrip("_") if payload.lower().startswith("ref_") else payload[3:]
            inviter_id = int(inviter)
            if inviter_id == int(uid or 0):
                return
            inserted = self._db.record_referral(int(uid or 0), inviter_id)
            if inserted:
                logger.info("[PIPELINE:REFERRAL] invitee=%s inviter=%s", uid, inviter_id)
        except Exception as exc:
            logger.debug("[PIPELINE:REFERRAL] ignored (%s)", exc)
