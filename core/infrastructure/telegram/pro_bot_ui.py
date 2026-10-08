"""Pro Bot UI — button-driven control panel.

Every operation (login, rules, filters, AI, logs, stats, backup) is driven by
inline keyboards so the user never has to type a command. A per-user session
state machine (`UiState`) tracks multi-step flows.
"""

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ...application.use_cases import (
    AIConfigUseCases,
    FilterRuleUseCases,
    ForwardRuleUseCases,
    MessageForwardingUseCase,
    SessionUseCases,
)
from ...domain.entities import AIConfig, FilterRule, ForwardRule
from ...domain.value_objects import AIProviderType, ForwardMode, RoutingType
from .i18n import I18n, SUPPORTED_LANGUAGES
from .login_flow import LoginFlowManager
from .client_pool import ClientPool, payload_from_pyrogram

logger = logging.getLogger("atf.botui")

LANG_FLAG = {"en": "English 🇬🇧", "fa": "فارسی 🇮🇷", "ru": "Русский 🇷🇺", "zh": "中文 🇨🇳"}

# Callback data must stay <= 64 bytes for Telegram.
CB = {
    "main": "m",
    "sessions": "s",
    "rules": "r",
    "filters": "f",
    "ai": "a",
    "logs": "l",
    "stats": "st",
    "lang": "lg",
    "login": "sl",
    "backup": "sb",
    "help": "h",
    "back": "b",
    "rule_add": "ra",
    "rule_del": "rd",
    "rule_toggle": "rt",
    "filter_add": "fa",
    "filter_del": "fd",
    "ai_add": "aa",
    "ai_test": "at",
    "ai_del": "ad",
    "logs_recent": "lr",
    "logs_stats": "ls",
    "lang_set": "lgs",
}


@dataclass
class UiState:
    """Per-user UI flow state (FSM)."""

    step: str = ""
    rule_source: str = ""
    rule_target: str = ""
    filter_id: Optional[str] = None
    ai_field: str = ""
    ai_name: str = ""
    ai_provider: str = ""
    ai_model: str = ""
    ai_api_key: str = ""
    login_phone: str = ""
    buffer: Dict[str, Any] = field(default_factory=dict)


class ProBotUI:
    """Renders and routes the entire button-based control panel."""

    def __init__(
        self,
        bot: Client,
        i18n: I18n,
        sessions: SessionUseCases,
        rules: ForwardRuleUseCases,
        filter_rules: FilterRuleUseCases,
        ai_configs: AIConfigUseCases,
        pipeline: MessageForwardingUseCase,
        pool: ClientPool,
        login: LoginFlowManager,
        log_client,
        admin_ids,
    ) -> None:
        self.bot = bot
        self.i18n = i18n
        self._sessions = sessions
        self._rules = rules
        self._filters = filter_rules
        self._ai = ai_configs
        self._pipeline = pipeline
        self._pool = pool
        self._login = login
        self._log = log_client
        self._admin_ids = set(admin_ids or [])
        self._states: Dict[int, UiState] = {}
        self._register()

    # ------------------------------------------------------------------ #
    def _is_admin(self, user_id: int) -> bool:
        return not self._admin_ids or user_id in self._admin_ids

    def _state(self, uid: int) -> UiState:
        return self._states.setdefault(uid, UiState())

    def _t(self, key: str, **kw) -> str:
        return self.i18n.t(key, **kw)

    def _kbd(self, rows) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
        )

    # ------------------------------------------------------------------ #
    # Menus
    # ------------------------------------------------------------------ #
    def _main_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("🔗 " + self._t("ui_sessions"), CB["sessions"]), ("📡 " + self._t("ui_rules"), CB["rules"])],
            [("🧹 " + self._t("ui_filters"), CB["filters"]), ("🤖 " + self._t("ui_ai"), CB["ai"])],
            [("📊 " + self._t("ui_stats"), CB["stats"]), ("🐛 " + self._t("ui_logs"), CB["logs"])],
            [("🌐 " + self._t("ui_lang"), CB["lang"]), ("ℹ️ " + self._t("ui_help"), CB["help"])],
        ])

    def _sessions_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("➕ " + self._t("ui_login"), CB["login"])],
            [("💾 " + self._t("ui_backup"), CB["backup"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _rules_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("➕ " + self._t("ui_rule_add"), CB["rule_add"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _filters_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("➕ " + self._t("ui_filter_add"), CB["filter_add"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _ai_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("➕ " + self._t("ui_ai_add"), CB["ai_add"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _logs_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("🕘 " + self._t("ui_logs_recent"), CB["logs_recent"])],
            [("📈 " + self._t("ui_logs_stats"), CB["logs_stats"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _lang_menu(self) -> InlineKeyboardMarkup:
        rows = [[(name, f'{CB["lang_set"]}:{code}')] for code, name in LANG_FLAG.items()]
        rows.append([("⬅️ " + self._t("ui_back"), CB["main"])])
        return self._kbd(rows)

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #
    def _register(self) -> None:
        b = self.bot

        @b.on_message(filters.command("start") & filters.private)
        async def _start(_, message: Message):
            await message.reply_text(
                self._t("ui_welcome"), reply_markup=self._main_menu()
            )
            self._log.info("bot", "system", f"/start by {message.from_user.id}")

        @b.on_message(filters.command("menu") & filters.private)
        async def _menu(_, message: Message):
            await message.reply_text(self._t("ui_main_title"), reply_markup=self._main_menu())

        @b.on_callback_query(filters.regex("^" + CB["main"] + "$"))
        async def _cb_main(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_main_title"), reply_markup=self._main_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["sessions"] + "$"))
        async def _cb_sessions(_, cq: CallbackQuery):
            await cq.edit_message_text(
                self._t("ui_sessions_title"), reply_markup=self._sessions_menu()
            )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["rules"] + "$"))
        async def _cb_rules(_, cq: CallbackQuery):
            rules = await self._rules.list_all()
            lines = [self._t("ui_rules_title"), ""]
            if not rules:
                lines.append(self._t("ui_none"))
            for r in rules:
                status = "🟢" if r.is_active else "🔴"
                lines.append(
                    f"{status} `{r.id[:8]}` {r.source_chat_name or r.source_chat_id}"
                    f" → {r.target_chat_name or r.target_chat_id}"
                )
            await cq.edit_message_text("\n".join(lines), reply_markup=self._rules_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["filters"] + "$"))
        async def _cb_filters(_, cq: CallbackQuery):
            filters_ = await self._filters.list_all()
            lines = [self._t("ui_filters_title"), ""]
            if not filters_:
                lines.append(self._t("ui_none"))
            for f in filters_:
                lines.append(f"🧹 `{f.id[:8]}` {f.name}")
            await cq.edit_message_text("\n".join(lines), reply_markup=self._filters_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai"] + "$"))
        async def _cb_ai(_, cq: CallbackQuery):
            cfgs = await self._ai.list_all()
            lines = [self._t("ui_ai_title"), ""]
            if not cfgs:
                lines.append(self._t("ui_none"))
            for c in cfgs:
                status = "🟢" if c.is_enabled else "🔴"
                lines.append(f"{status} `{c.id[:8]}` {c.name} [{c.provider.value}/{c.model}]")
            await cq.edit_message_text("\n".join(lines), reply_markup=self._ai_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["stats"] + "$"))
        async def _cb_stats(_, cq: CallbackQuery):
            s = self._pipeline.stats
            text = self._t(
                "ui_stats_body",
                processed=s.processed, forwarded=s.forwarded, filtered=s.filtered,
                rewritten=s.rewritten, errors=s.errors,
                uptime=int(time.time()) - s.started_at,
            )
            await cq.edit_message_text(text, reply_markup=self._main_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["lang"] + "$"))
        async def _cb_lang(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_lang_choose"), reply_markup=self._lang_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["lang_set"] + ":"))
        async def _cb_lang_set(_, cq: CallbackQuery):
            code = cq.data.split(":", 1)[1]
            if self.i18n.set_language(code):
                await cq.edit_message_text(
                    self._t("ui_lang_done"), reply_markup=self._main_menu()
                )
                self._log.info("bot", "system", f"language set to {code}")
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["help"] + "$"))
        async def _cb_help(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_help_body"), reply_markup=self._main_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["back"] + "$"))
        async def _cb_back(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_main_title"), reply_markup=self._main_menu())
            await cq.answer()

        # ---------------- login flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["login"] + "$"))
        async def _cb_login(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "login_phone"
            await cq.edit_message_text(self._t("ui_login_phone"))
            await cq.answer()

        # ---------------- rule add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["rule_add"] + "$"))
        async def _cb_rule_add(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            st.step = "rule_source"
            await cq.edit_message_text(self._t("ui_rule_source"))
            await cq.answer()

        # ---------------- filter add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["filter_add"] + "$"))
        async def _cb_filter_add(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            st.step = "filter_name"
            await cq.edit_message_text(self._t("ui_filter_name"))
            await cq.answer()

        # ---------------- ai add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["ai_add"] + "$"))
        async def _cb_ai_add(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            st.step = "ai_name"
            await cq.edit_message_text(self._t("ui_ai_name"))
            await cq.answer()

        # ---------------- rule delete / toggle ----------------
        @b.on_callback_query(filters.regex("^" + CB["rule_del"] + ":"))
        async def _cb_rule_del(_, cq: CallbackQuery):
            rid = cq.data.split(":", 1)[1]
            await self._rules.delete(rid)
            self._log.info("bot", "rule", f"rule {rid} deleted")
            await cq.edit_message_text(self._t("ui_rule_deleted"), reply_markup=self._rules_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["rule_toggle"] + ":"))
        async def _cb_rule_toggle(_, cq: CallbackQuery):
            rid = cq.data.split(":", 1)[1]
            await self._rules.toggle(rid)
            self._log.info("bot", "rule", f"rule {rid} toggled")
            await cq.answer(self._t("ui_done"))

        # ---------------- backup ----------------
        @b.on_callback_query(filters.regex("^" + CB["backup"] + "$"))
        async def _cb_backup(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            sessions = await self._sessions.list_all()
            if not sessions:
                await cq.edit_message_text(self._t("ui_no_sessions"), reply_markup=self._sessions_menu())
                return await cq.answer()
            target = sessions[0]
            try:
                bundle = {
                    "phone": target.phone_number,
                    "session_string": target.session_string_encrypted,
                    "user_id": target.user_id,
                    "username": target.username,
                    "first_name": target.first_name,
                }
                blob = base64.urlsafe_b64encode(
                    json.dumps(bundle).encode("utf-8")
                ).decode("ascii")
                path = f"backup_{target.id[:8]}.atf"
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(blob)
                await self.bot.send_document(
                    chat_id=cq.message.chat.id, document=path,
                    caption=self._t("ui_backup_done", phone=target.phone_number),
                )
                os.remove(path)
                self._log.info("bot", "session", f"backup exported for {target.phone_number}")
            except Exception as exc:
                logger.exception("backup failed")
                await cq.message.reply_text(self._t("ui_backup_fail", error=str(exc)))
            await cq.answer()

        # ---------------- logs panel ----------------
        @b.on_callback_query(filters.regex("^" + CB["logs"] + "$"))
        async def _cb_logs(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_logs_title"), reply_markup=self._logs_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["logs_recent"] + "$"))
        async def _cb_logs_recent(_, cq: CallbackQuery):
            try:
                resp = await self._log.query(limit=20)
                lines = [self._t("ui_logs_recent_title"), ""]
                if not resp.logs:
                    lines.append(self._t("ui_none"))
                for e in resp.logs:
                    lines.append(f"`{e.level}` {e.service}/{e.category}: {e.message}")
                await cq.edit_message_text("\n".join(lines), reply_markup=self._logs_menu())
            except Exception as exc:
                await cq.edit_message_text(
                    self._t("ui_logs_fail", error=str(exc)), reply_markup=self._logs_menu()
                )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["logs_stats"] + "$"))
        async def _cb_logs_stats(_, cq: CallbackQuery):
            try:
                resp = await self._log.stats()
                lines = [self._t("ui_logs_stats_title"), "", f"Σ total: {resp.total}", ""]
                for level, count in resp.by_level.items():
                    lines.append(f"{level}: {count}")
                await cq.edit_message_text("\n".join(lines), reply_markup=self._logs_menu())
            except Exception as exc:
                await cq.edit_message_text(
                    self._t("ui_logs_fail", error=str(exc)), reply_markup=self._logs_menu()
                )
            await cq.answer()

        # ---------------- ai test / delete ----------------
        @b.on_callback_query(filters.regex("^" + CB["ai_test"] + ":"))
        async def _cb_ai_test(_, cq: CallbackQuery):
            aid = cq.data.split(":", 1)[1]
            await cq.answer(self._t("ui_ai_testing"), show_alert=False)
            ok, text = await self._ai.test_rewrite(aid, self._t("ui_ai_sample"))
            body = text if ok else f"❌ {text}"
            await cq.message.reply_text(self._t("ui_ai_test_result", result=body[:2000]))

        @b.on_callback_query(filters.regex("^" + CB["ai_del"] + ":"))
        async def _cb_ai_del(_, cq: CallbackQuery):
            aid = cq.data.split(":", 1)[1]
            await self._ai.delete(aid)
            await cq.edit_message_text(self._t("ui_done"), reply_markup=self._ai_menu())
            await cq.answer()

        # ---------------- free-text FSM router ----------------
        @b.on_message(filters.private & ~filters.command(["start", "menu"]))
        async def _text(_, message: Message):
            await self._route_text(message)

    # ------------------------------------------------------------------ #
    async def _route_text(self, message: Message) -> None:
        uid = message.from_user.id
        st = self._state(uid)
        text = (message.text or "").strip()
        if not st.step:
            return await message.reply_text(
                self._t("ui_use_menu"), reply_markup=self._main_menu()
            )

        handler = getattr(self, f"_step_{st.step}", None)
        if handler is None:
            st.step = ""
            return await message.reply_text(
                self._t("ui_use_menu"), reply_markup=self._main_menu()
            )
        await handler(message, st, text)

    # ------------------------------------------------------------------ #
    # Step handlers (FSM)
    # ------------------------------------------------------------------ #
    async def _step_login_phone(self, message: Message, st: UiState, text: str) -> None:
        if not text.startswith("+"):
            return await message.reply_text(self._t("ui_login_phone_bad"))
        st.step = "login_code"
        st.login_phone = text
        await self._login.start(uid := message.from_user.id, text)
        self._log.info("bot", "login", f"login started for {text}")
        await message.reply_text(self._t("ui_login_code"))

    async def _step_login_code(self, message: Message, st: UiState, text: str) -> None:
        result = await self._login.submit_code(message.from_user.id, text.strip())
        if result == "send_password":
            st.step = "login_password"
            return await message.reply_text(self._t("ui_login_password"))
        await self._finish_login(message, st, result)

    async def _step_login_password(self, message: Message, st: UiState, text: str) -> None:
        result = await self._login.submit_password(message.from_user.id, text)
        await self._finish_login(message, st, result)

    async def _finish_login(self, message: Message, st: UiState, result: str) -> None:
        st.step = ""
        if result.startswith("login_success"):
            self._log.info("bot", "login", f"login success for {st.login_phone}")
            await message.reply_text(
                self._t("ui_login_done", phone=st.login_phone),
                reply_markup=self._main_menu(),
            )
        else:
            key, _, detail = result.partition(":")
            await message.reply_text(self._t("ui_login_fail", error=detail or key))

    async def _step_rule_source(self, message: Message, st: UiState, text: str) -> None:
        st.rule_source = text.strip()
        st.step = "rule_target"
        await message.reply_text(self._t("ui_rule_target"))

    async def _step_rule_target(self, message: Message, st: UiState, text: str) -> None:
        st.rule_target = text.strip()
        st.step = ""
        sessions = await self._sessions.list_all()
        if not sessions:
            return await message.reply_text(self._t("ui_no_sessions"), reply_markup=self._sessions_menu())
        session = sessions[0]
        rule = ForwardRule(
            session_id=session.id,
            source_chat_id=st.rule_source,
            source_chat_name=st.rule_source,
            target_chat_id=st.rule_target,
            target_chat_name=st.rule_target,
            routing_type=RoutingType.CHANNEL_TO_CHANNEL,
            forward_mode=ForwardMode.COPY_MESSAGE,
        )
        await self._rules.create(rule)
        self._log.info("bot", "rule", f"rule created {st.rule_source} -> {st.rule_target}")
        await message.reply_text(
            self._t("ui_rule_done", source=st.rule_source, target=st.rule_target),
            reply_markup=self._rules_menu(),
        )

    async def _step_filter_name(self, message: Message, st: UiState, text: str) -> None:
        st.step = "filter_blacklist"
        st.buffer["name"] = text.strip()
        await message.reply_text(self._t("ui_filter_blacklist"))

    async def _step_filter_blacklist(self, message: Message, st: UiState, text: str) -> None:
        st.step = ""
        blacklist = [w.strip() for w in text.replace("،", ",").split(",") if w.strip()]
        fr = FilterRule(name=st.buffer.get("name", "filter"), blacklist_keywords=blacklist)
        await self._filters.create(fr)
        self._log.info("bot", "filter", f"filter {fr.name} created with {len(blacklist)} blacklist words")
        await message.reply_text(self._t("ui_filter_done", name=fr.name), reply_markup=self._filters_menu())

    async def _step_ai_name(self, message: Message, st: UiState, text: str) -> None:
        st.step = "ai_provider"
        st.ai_name = text.strip()
        await message.reply_text(self._t("ui_ai_provider"))

    async def _step_ai_provider(self, message: Message, st: UiState, text: str) -> None:
        provider = text.strip().lower()
        if provider not in {p.value for p in AIProviderType}:
            return await message.reply_text(self._t("ui_ai_provider_bad"))
        st.ai_provider = provider
        st.step = "ai_model"
        await message.reply_text(self._t("ui_ai_model"))

    async def _step_ai_model(self, message: Message, st: UiState, text: str) -> None:
        st.ai_model = text.strip()
        st.step = "ai_api_key"
        await message.reply_text(self._t("ui_ai_key"))

    async def _step_ai_api_key(self, message: Message, st: UiState, text: str) -> None:
        st.step = ""
        cfg = AIConfig(
            name=st.ai_name,
            provider=AIProviderType(st.ai_provider),
            model=st.ai_model,
            api_key=text.strip(),
        )
        await self._ai.create(cfg)
        self._log.info("bot", "ai", f"AI config {cfg.name} created ({cfg.provider.value})")
        await message.reply_text(
            self._t("ui_ai_done", name=cfg.name), reply_markup=self._ai_menu()
        )
