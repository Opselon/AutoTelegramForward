"""Pro Bot UI — button-driven control panel.

Every operation (login, rules, filters, AI, logs, stats, backup) is driven by
inline keyboards so the user never has to type a command. A per-user session
state machine (`UiState`) tracks multi-step flows and is persisted to the
`ui_states` table, so a restart mid-login resumes the user where they stopped.

Error policy: no handler returns a raw traceback or a bare internal key. Every
pyrogram `RPCError` is translated by `errors.tg_error` into a localized message
that carries the exact reason Telegram reported, and logged to `error_log` so
the debug panel can show it.
"""

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
from .client_pool import ClientPool
from .errors import SEV_ERROR, SEV_FATAL, tg_detail, tg_error
from .i18n import I18n
from .login_flow import LoginFlowManager

logger = logging.getLogger("atf.botui")

LANG_FLAG = {"en": "English 🇬🇧", "fa": "فارسی 🇮🇷", "ru": "Русский 🇷🇺", "zh": "中文 🇨🇳"}

# Callback data must stay <= 64 bytes for Telegram. Keep keys short.
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
    "ai_provider_pick": "ap",
    "ai_model_pick": "am",
    "logs_recent": "lr",
    "logs_stats": "ls",
    "logs_errors": "le",
    "lang_set": "lgs",
    "creds": "cv",
    "cred_add": "ca",
    "cred_del": "cd",
    "cred_default": "cdef",
    "login_retry": "slr",
    "cancel": "cx",
    "debug": "dbg",
    "refresh": "rf",
    "perf": "pf",
}

SEV_ICON = {"info": "ℹ️", "warn": "⚠️", "error": "❌", "auth": "🔐", "fatal": "💀"}


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
    cred_label: str = ""
    cred_api_id: str = ""
    cred_api_hash: str = ""
    buffer: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize the FSM for the durable ui_states table."""
        return {
            "step": self.step,
            "rule_source": self.rule_source,
            "rule_target": self.rule_target,
            "ai_name": self.ai_name,
            "ai_provider": self.ai_provider,
            "ai_model": self.ai_model,
            "login_phone": self.login_phone,
            "cred_label": self.cred_label,
            "cred_api_id": self.cred_api_id,
            "buffer": {k: v for k, v in self.buffer.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UiState":
        if not data:
            return cls()
        return cls(
            step=data.get("step", ""),
            rule_source=data.get("rule_source", ""),
            rule_target=data.get("rule_target", ""),
            filter_id=data.get("filter_id"),
            ai_name=data.get("ai_name", ""),
            ai_provider=data.get("ai_provider", ""),
            ai_model=data.get("ai_model", ""),
            login_phone=data.get("login_phone", ""),
            cred_label=data.get("cred_label", ""),
            cred_api_id=str(data.get("cred_api_id", "")),
            buffer=data.get("buffer", {}) or {},
        )


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
        credentials=None,
        bot_tokens=None,
        dispatcher=None,
        boot_manager=None,
        ui_state_repo=None,
        error_log=None,
        metrics=None,
        rule_stats=None,
        users=None,
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
        self._credentials = credentials
        self._bot_tokens = bot_tokens
        self._dispatcher = dispatcher
        self._boot_manager = boot_manager
        self._ui_state_repo = ui_state_repo
        self._error_log = error_log
        self._metrics = metrics
        self._rule_stats = rule_stats
        self._users = users
        self._states: Dict[int, UiState] = {}
        self._register()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _is_admin(self, user_id: int) -> bool:
        return not self._admin_ids or user_id in self._admin_ids

    def _state(self, uid: int) -> UiState:
        st = self._states.get(uid)
        if st is None:
            st = UiState()
            resumed = self._load_state_sync(uid)
            if resumed:
                st = UiState.from_dict(resumed)
                logger.debug("resumed ui state for %s → %s", uid, st.step)
            self._states[uid] = st
        return st

    def _load_state_sync(self, uid: int) -> Optional[dict]:
        """Best-effort synchronous read of persisted state (sqlite is sync)."""
        repo = self._ui_state_repo
        if repo is None:
            return None
        db = getattr(repo, "_db", None)
        if db is None:
            return None
        try:
            row = db.query_one(
                "SELECT step, buffer FROM ui_states WHERE user_id=?", (int(uid),)
            )
        except Exception:
            return None
        if not row:
            return None
        return {"step": row["step"] or "", "buffer": db.loads(row["buffer"], {})}

    async def _persist(self, uid: int, st: UiState) -> None:
        if self._ui_state_repo is None:
            return
        try:
            if st.step:
                await self._ui_state_repo.save(uid, st.step, st.to_dict())
            else:
                await self._ui_state_repo.clear(uid)
        except Exception:
            logger.debug("ui state persist failed", exc_info=True)

    def _t(self, key: str, **kw) -> str:
        return self.i18n.t(key, **kw)

    def _kbd(self, rows) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
        )

    def _cancel_kbd(self) -> InlineKeyboardMarkup:
        return self._kbd([[("❌ " + self._t("ui_cancel"), CB["cancel"])]])

    # ------------------------------------------------------------------ #
    # Menus
    # ------------------------------------------------------------------ #
    def _main_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("📱 " + self._t("ui_sessions"), CB["sessions"]), ("⚡ " + self._t("ui_rules"), CB["rules"])],
            [("🧹 " + self._t("ui_filters"), CB["filters"]), ("🤖 " + self._t("ui_ai"), CB["ai"])],
            [("📊 " + self._t("ui_stats"), CB["stats"]), ("🔑 " + self._t("ui_creds"), CB["creds"])],
            [("🛠 " + self._t("ui_debug"), CB["debug"]), ("🌐 " + self._t("ui_lang"), CB["lang"])],
            [("📚 " + self._t("ui_help"), CB["help"])],
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
            [("🕘 " + self._t("ui_logs_recent"), CB["logs_recent"]),
             ("⚠️ " + self._t("ui_logs_errors"), CB["logs_errors"])],
            [("📈 " + self._t("ui_logs_stats"), CB["logs_stats"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _debug_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("🕘 " + self._t("ui_logs_recent"), CB["logs_recent"]),
             ("⚠️ " + self._t("ui_logs_errors"), CB["logs_errors"])],
            [("📈 " + self._t("ui_perf"), CB["perf"])],
            [("🔄 " + self._t("ui_refresh"), CB["refresh"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _lang_menu(self) -> InlineKeyboardMarkup:
        rows = [[(name, f'{CB["lang_set"]}:{code}')] for code, name in LANG_FLAG.items()]
        rows.append([(self._t("ui_back"), CB["main"])])
        return self._kbd(rows)

    # ------------------------------------------------------------------ #
    # API credentials vault
    # ------------------------------------------------------------------ #
    def _creds_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [(self._t("ui_cred_add"), CB["cred_add"])],
            [(self._t("ui_back"), CB["main"])],
        ])

    def _provider_label(self, key: str) -> str:
        return {
            "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google Gemini",
            "groq": "Groq", "deepseek": "DeepSeek", "openrouter": "OpenRouter",
            "9router": "9Router", "ninerouter": "9Router", "xai": "xAI",
            "mistral": "Mistral", "together": "Together AI", "fireworks": "Fireworks",
            "custom": "Custom",
        }.get(key, key or "—")

    def _provider_picker(self) -> InlineKeyboardMarkup:
        names = self._ai.provider_names() if hasattr(self._ai, "provider_names") else []
        if not names:
            names = [p.value for p in AIProviderType]
        rows: list = []
        row: list = []
        for name in names:
            row.append((self._provider_label(name), f'{CB["ai_provider_pick"]}:{name}'))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([("❌ " + self._t("ui_cancel"), CB["cancel"])])
        return self._kbd(rows)

    def _model_picker(self, st: UiState, provider: str) -> InlineKeyboardMarkup | None:
        models = self._ai.models_for(provider) if hasattr(self._ai, "models_for") else []
        models = list(models or [])[:10]
        if not models:
            return None
        # Index-based callback data keeps payloads far under the 64-byte limit.
        st.buffer["models"] = models
        rows: list = []
        for i, model in enumerate(models):
            rows.append([(model, f'{CB["ai_model_pick"]}:{i}')])
        rows.append([("❌ " + self._t("ui_cancel"), CB["cancel"])])
        return self._kbd(rows)

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #
    def _register(self) -> None:
        b = self.bot

        @b.on_message(filters.command("start") & filters.private)
        async def _start(_, message: Message):
            await message.reply_text(self._t("ui_welcome"), reply_markup=self._main_menu())
            try:
                self._log.info("bot", "system", f"/start by {message.from_user.id}")
            except Exception:
                pass

        @b.on_message(filters.command("menu") & filters.private)
        async def _menu(_, message: Message):
            await message.reply_text(self._t("ui_main_title"), reply_markup=self._main_menu())

        @b.on_callback_query(filters.regex("^" + CB["main"] + "$"))
        async def _cb_main(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_main_title"), reply_markup=self._main_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["sessions"] + "$"))
        async def _cb_sessions(_, cq: CallbackQuery):
            sessions = await self._sessions.list_all()
            lines = [self._t("ui_sessions_title"), ""]
            if not sessions:
                lines.append(self._t("ui_none"))
            for s in sessions:
                try:
                    live = self._pool.get(s.id) is not None
                except Exception:
                    live = False
                mark = "🟢" if live else ("🟡" if s.is_active else "🔴")
                lines.append(f"{mark} `{s.id[:8]}` +{s.phone_number}")
            await cq.edit_message_text("\n".join(lines), reply_markup=self._sessions_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["rules"] + "$"))
        async def _cb_rules(_, cq: CallbackQuery):
            rules = await self._rules.list_all()
            lines = [self._t("ui_rules_title"), ""]
            if not rules:
                lines.append(self._t("ui_none"))
            for r in rules:
                status = "🟢" if r.is_active else "🔴"
                target = getattr(r, "target_label", None) or (
                    r.target_chat_name or r.target_chat_id)
                lines.append(
                    f"{status} `{r.id[:8]}` {r.source_chat_name or r.source_chat_id} → {target}")
                mode = getattr(r.forward_mode, "value", r.forward_mode)
                delay = float(getattr(r, "delay_seconds", 0) or 0)
                lines.append(f"     mode={mode} delay={delay:.0f}s")
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
                lines.append(
                    f"{status} `{c.id[:8]}` {c.name} "
                    f"[{self._provider_label(c.provider.value)}/{c.model}]")
            await cq.edit_message_text("\n".join(lines), reply_markup=self._ai_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["stats"] + "$"))
        async def _cb_stats(_, cq: CallbackQuery):
            await cq.edit_message_text(
                await self._dashboard_body(), reply_markup=self._main_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["perf"] + "$"))
        async def _cb_perf(_, cq: CallbackQuery):
            await cq.edit_message_text(await self._perf_body(), reply_markup=self._debug_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["refresh"] + "$"))
        async def _cb_refresh(_, cq: CallbackQuery):
            await cq.edit_message_text(
                await self._dashboard_body(), reply_markup=self._debug_menu())
            await cq.answer(self._t("ui_done"), show_alert=False)

        @b.on_callback_query(filters.regex("^" + CB["lang"] + "$"))
        async def _cb_lang(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_lang_choose"), reply_markup=self._lang_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["lang_set"] + ":"))
        async def _cb_lang_set(_, cq: CallbackQuery):
            code = cq.data.split(":", 1)[1]
            if self.i18n.set_language(code):
                await cq.edit_message_text(
                    self._t("ui_lang_done"), reply_markup=self._main_menu())
                if self._users is not None:
                    try:
                        await self._users.set_language(cq.from_user.id, code)
                    except Exception:
                        pass
                try:
                    self._log.info("bot", "system", f"language set to {code}")
                except Exception:
                    pass
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["help"] + "$"))
        async def _cb_help(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_help_body"), reply_markup=self._main_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["back"] + "$"))
        async def _cb_back(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_main_title"), reply_markup=self._main_menu())
            await cq.answer()

        # ---------------- cancel any active flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["cancel"] + "$"))
        async def _cb_cancel(_, cq: CallbackQuery):
            uid = cq.from_user.id
            st = self._state(uid)
            st.step = ""
            await self._persist(uid, st)
            self._login.cancel(uid)
            await cq.edit_message_text(self._t("ui_cancelled"), reply_markup=self._main_menu())
            await cq.answer()

        # ---------------- login flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["login_retry"] + "$"))
        async def _cb_login_retry(_, cq: CallbackQuery):
            # One-tap restart of a login that expired in a restart.
            # Reuses the persisted phone, so the user never retypes it.
            uid = cq.from_user.id
            st = self._state(uid)
            phone = (getattr(st, "login_phone", "") or "").strip()
            if not LoginFlowManager.valid_phone(phone):
                st.step = "login_phone"
                await self._persist(uid, st)
                await cq.answer()
                return await cq.message.edit_text(
                    self._t("ui_login_phone"), reply_markup=self._cancel_kbd())
            result = await self._login.start(uid, LoginFlowManager.normalize_phone(phone))
            await cq.answer()
            if result != "send_code":
                key, _, detail = result.partition(":")
                return await cq.message.edit_text(
                    self._t(f"ui_login_fail_{key}", error=detail),
                    reply_markup=self._cancel_kbd())
            st.step = "login_code"
            await self._persist(uid, st)
            await cq.message.edit_text(
                self._t("ui_login_code"), reply_markup=self._cancel_kbd())

        @b.on_callback_query(filters.regex("^" + CB["login"] + "$"))
        async def _cb_login(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "login_phone"
            await self._persist(cq.from_user.id, st)
            await cq.edit_message_text(
                self._t("ui_login_phone"), reply_markup=self._cancel_kbd())
            await cq.answer()

        # ---------------- api credentials vault ----------------
        @b.on_callback_query(filters.regex("^" + CB["creds"] + "$"))
        async def _cb_creds(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            lines = [self._t("ui_creds_title"), ""]
            if self._credentials is not None:
                try:
                    creds = await self._credentials.list_all()
                    if not creds:
                        lines.append(self._t("ui_none"))
                    for c in creds:
                        mark = "★" if c.is_default else "·"
                        lines.append(f"{mark} `{c.id[:8]}` {c.label or ''} ({c.api_id})")
                except Exception as exc:
                    await self._record_error("bot", exc, user_id=cq.from_user.id)
                    lines.append("⚠️ " + tg_detail(exc))
            else:
                lines.append(self._t("ui_none"))
            await cq.edit_message_text("\n".join(lines), reply_markup=self._creds_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["cred_add"] + "$"))
        async def _cb_cred_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "cred_label"
            await self._persist(cq.from_user.id, st)
            await cq.edit_message_text(
                self._t("ui_cred_label"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["cred_default"] + ":"))
        async def _cb_cred_default(_, cq: CallbackQuery):
            cid = cq.data.split(":", 1)[1]
            if self._credentials is not None:
                try:
                    await self._credentials.set_default(cid)
                    try:
                        self._log.info("bot", "cred", f"credential {cid[:8]} set default")
                    except Exception:
                        pass
                except Exception as exc:
                    await self._record_error("bot", exc, user_id=cq.from_user.id)
            await cq.answer(self._t("ui_cred_set_default", id=cid[:8]), show_alert=True)

        # ---------------- rule add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["rule_add"] + "$"))
        async def _cb_rule_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            sessions = await self._sessions.list_all()
            if not sessions:
                await cq.edit_message_text(
                    self._t("ui_no_sessions"), reply_markup=self._sessions_menu())
                return await cq.answer()
            st = self._state(cq.from_user.id)
            st.step = "rule_source"
            await self._persist(cq.from_user.id, st)
            await cq.edit_message_text(
                self._t("ui_rule_source"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["rule_del"] + ":"))
        async def _cb_rule_del(_, cq: CallbackQuery):
            rid = cq.data.split(":", 1)[1]
            await self._rules.delete(rid)
            try:
                self._log.info("bot", "rule", f"rule {rid} deleted")
            except Exception:
                pass
            await cq.edit_message_text(
                self._t("ui_rule_deleted"), reply_markup=self._rules_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["rule_toggle"] + ":"))
        async def _cb_rule_toggle(_, cq: CallbackQuery):
            rid = cq.data.split(":", 1)[1]
            await self._rules.toggle(rid)
            try:
                self._log.info("bot", "rule", f"rule {rid} toggled")
            except Exception:
                pass
            await cq.answer(self._t("ui_done"))

        # ---------------- filter add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["filter_add"] + "$"))
        async def _cb_filter_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "filter_name"
            await self._persist(cq.from_user.id, st)
            await cq.edit_message_text(
                self._t("ui_filter_name"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["filter_del"] + ":"))
        async def _cb_filter_del(_, cq: CallbackQuery):
            fid = cq.data.split(":", 1)[1]
            await self._filters.delete(fid)
            await cq.edit_message_text(self._t("ui_done"), reply_markup=self._filters_menu())
            await cq.answer()

        # ---------------- ai add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["ai_add"] + "$"))
        async def _cb_ai_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_name"
            await self._persist(cq.from_user.id, st)
            await cq.edit_message_text(self._t("ui_ai_name"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai_provider_pick"] + ":"))
        async def _cb_ai_provider_pick(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            if st.step != "ai_provider":
                return await cq.answer()
            provider = cq.data.split(":", 1)[1]
            if provider not in {p.value for p in AIProviderType}:
                return await cq.answer(self._t("ui_ai_provider_bad"), show_alert=True)
            st.ai_provider = provider
            st.step = "ai_model"
            kb = self._model_picker(st, provider)
            await self._persist(cq.from_user.id, st)
            if kb is not None:
                await cq.edit_message_text(self._t("ui_ai_model"), reply_markup=kb)
            else:
                await cq.edit_message_text(
                    self._t("ui_ai_model"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai_model_pick"] + ":"))
        async def _cb_ai_model_pick(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            if st.step != "ai_model":
                return await cq.answer()
            try:
                idx = int(cq.data.split(":", 1)[1])
                model = st.buffer.get("models", [])[idx]
            except (ValueError, IndexError):
                return await cq.answer(self._t("ui_ai_provider_bad"), show_alert=True)
            st.ai_model = model
            st.step = "ai_api_key"
            await self._persist(cq.from_user.id, st)
            await cq.edit_message_text(self._t("ui_ai_key"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai_del"] + ":"))
        async def _cb_ai_del(_, cq: CallbackQuery):
            aid = cq.data.split(":", 1)[1]
            await self._ai.delete(aid)
            await cq.edit_message_text(self._t("ui_done"), reply_markup=self._ai_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai_test"] + ":"))
        async def _cb_ai_test(_, cq: CallbackQuery):
            aid = cq.data.split(":", 1)[1]
            await cq.answer(self._t("ui_ai_testing"), show_alert=False)
            try:
                ok, text = await self._ai.test_rewrite(aid, self._t("ui_ai_sample"))
                body = text if ok else f"❌ {text}"
                await cq.message.reply_text(self._t("ui_ai_test_result", result=body[:2000]))
            except Exception as exc:
                await self._record_error("ai", exc, user_id=cq.from_user.id)
                await cq.message.reply_text(self._t("ui_ai_fail", error=tg_detail(exc)))

        # ---------------- backup ----------------
        @b.on_callback_query(filters.regex("^" + CB["backup"] + "$"))
        async def _cb_backup(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            sessions = await self._sessions.list_all()
            if not sessions:
                await cq.edit_message_text(
                    self._t("ui_no_sessions"), reply_markup=self._sessions_menu())
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
                try:
                    self._log.info(
                        "bot", "session", f"backup exported for {target.phone_number}")
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("backup failed")
                await self._record_error("bot", exc, user_id=cq.from_user.id)
                await cq.message.reply_text(
                    self._t("ui_backup_fail", error=tg_detail(exc)))
            await cq.answer()

        # ---------------- logs panel ----------------
        @b.on_callback_query(filters.regex("^" + CB["logs"] + "$"))
        async def _cb_logs(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_logs_title"), reply_markup=self._logs_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["logs_recent"] + "$"))
        async def _cb_logs_recent(_, cq: CallbackQuery):
            lines = [self._t("ui_logs_recent"), ""]
            if self._error_log is not None:
                try:
                    rows = await self._error_log.recent(10)
                    if not rows:
                        lines.append(self._t("ui_none"))
                    for r in rows:
                        sev = SEV_ICON.get(r.get("severity", ""), "❓")
                        lines.append(
                            f"{sev} `{r.get('error_name', '?')}` [{r.get('category', '?')}]")
                        det = (r.get("detail") or "")[:90]
                        if det:
                            lines.append(f"     {det}")
                except Exception as exc:
                    lines.append("⚠️ " + tg_detail(exc))
            else:
                lines.append(self._t("ui_none"))
            await cq.edit_message_text("\n".join(lines), reply_markup=self._logs_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["logs_errors"] + "$"))
        async def _cb_logs_errors(_, cq: CallbackQuery):
            lines = [self._t("ui_logs_errors"), ""]
            if self._error_log is not None:
                try:
                    rows = await self._error_log.recent(8, severity=SEV_FATAL)
                    rows += await self._error_log.recent(8, severity=SEV_ERROR)
                    if not rows:
                        lines.append(self._t("ui_none"))
                    for r in rows:
                        sev = SEV_ICON.get(r.get("severity", ""), "❓")
                        lines.append(
                            f"{sev} `{r.get('error_name', '?')}` [{r.get('category', '?')}]"
                            f" recoverable={bool(r.get('recoverable'))}")
                        det = (r.get("detail") or "")[:90]
                        if det:
                            lines.append(f"     {det}")
                except Exception as exc:
                    lines.append("⚠️ " + tg_detail(exc))
            else:
                lines.append(self._t("ui_none"))
            await cq.edit_message_text("\n".join(lines), reply_markup=self._logs_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["logs_stats"] + "$"))
        async def _cb_logs_stats(_, cq: CallbackQuery):
            lines = [self._t("ui_logs_stats"), ""]
            if self._error_log is not None:
                try:
                    since = int(time.time()) - 24 * 3600
                    counts = await self._error_log.counts_by_severity(since)
                    if not counts:
                        lines.append(self._t("ui_none"))
                    for sev, n in sorted(counts.items()):
                        lines.append(f"{SEV_ICON.get(sev, '❓')} {sev}: {n}")
                except Exception as exc:
                    lines.append("⚠️ " + tg_detail(exc))
            else:
                lines.append(self._t("ui_none"))
            await cq.edit_message_text("\n".join(lines), reply_markup=self._logs_menu())
            await cq.answer()

        # ---------------- debug ----------------
        @b.on_callback_query(filters.regex("^" + CB["debug"] + "$"))
        async def _cb_debug(_, cq: CallbackQuery):
            await cq.edit_message_text(
                await self._debug_body(), reply_markup=self._debug_menu())
            await cq.answer()

        # ---------------- free-text FSM router ----------------
        @b.on_message(filters.private & filters.text & ~filters.command(["start", "menu"]))
        async def _text(_, message: Message):
            # Every step of every flow is traced: if the bot ever goes quiet
            # again, the log shows exactly which step died and why.
            uid = message.from_user.id if message.from_user else 0
            preview = (message.text or "")[:40].replace("\n", " ")
            step = ""
            try:
                step = self._state(uid).step or "<none>"
            except Exception:
                pass
            self._log.info(
                "bot", "fsm",
                f"text in from {uid} step={step}: {preview!r}")
            try:
                await self._route_text(message)
            except Exception as exc:
                self._log.error(
                    "bot", "fsm",
                    f"route_text failed for {uid} step={step}: {type(exc).__name__}: {exc}",
                )
                try:
                    await message.reply_text(
                        self._t("ui_internal_error"),
                        reply_markup=self._cancel_kbd())
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # error recording
    # ------------------------------------------------------------------ #
    async def _record_error(
        self, category: str, exc: BaseException, user_id: Optional[int] = None,
        session_id: Optional[str] = None, rule_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        """Store the *exact* Telegram error and broadcast it to the log service."""
        key, sev, recoverable = tg_error(exc)
        detail = tg_detail(exc)
        if self._error_log is not None:
            try:
                await self._error_log.record(
                    category=category, error_name=type(exc).__name__,
                    severity=sev, detail=detail, recoverable=recoverable,
                    user_id=user_id, session_id=session_id, rule_id=rule_id,
                    chat_id=chat_id,
                )
            except Exception:
                logger.debug("error_log write failed", exc_info=True)
        if self._metrics is not None and sev in (SEV_ERROR, SEV_FATAL):
            try:
                await self._metrics.bump(errors=1)
            except Exception:
                pass
        try:
            self._log.error("bot", category, f"{type(exc).__name__}: {detail}")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # dashboard bodies
    # ------------------------------------------------------------------ #
    async def _dashboard_body(self) -> str:
        s = self._pipeline.stats
        lines = [self._t("ui_stats_title"), ""]
        lines.append(f"📥 {self._t('ui_stat_processed')}: {s.processed}")
        lines.append(f"⚡ {self._t('ui_stat_forwarded')}: {s.forwarded}")
        lines.append(f"🧹 {self._t('ui_stat_filtered')}: {s.filtered}")
        lines.append(f"🤖 {self._t('ui_stat_rewritten')}: {s.rewritten}")
        lines.append(f"❌ {self._t('ui_stat_errors')}: {s.errors}")
        lines.append(f"⏱ {self._t('ui_stat_uptime')}: {int(time.time()) - s.started_at}s")
        if self._metrics is not None:
            try:
                lines.append("")
                lines.append(f"🗂 {self._t('ui_stat_total')}: {await self._metrics.total_str()}")
            except Exception:
                pass
        return "\n".join(lines)

    async def _perf_body(self) -> str:
        s = self._pipeline.stats
        uptime = max(int(time.time()) - s.started_at, 1)
        rate = s.processed / uptime * 60
        lines = [self._t("ui_perf_title"), ""]
        lines.append(f"⚡ {self._t('ui_perf_rate')}: {rate:.1f}/min")
        lines.append(f"📥 {self._t('ui_stat_processed')}: {s.processed}")
        lines.append(f"❌ {self._t('ui_stat_errors')}: {s.errors}")
        try:
            live = len(self._pool.all_clients())
        except Exception:
            live = 0
        lines.append(f"📱 {self._t('ui_perf_sessions')}: {live}")
        if self._metrics is not None:
            try:
                hourly = await self._metrics.hourly(24)
                if hourly:
                    total = sum(h["forwarded"] for h in hourly)
                    lines.append(f"📈 24h: {total} {self._t('ui_stat_forwarded').lower()}")
            except Exception:
                pass
        return "\n".join(lines)

    async def _debug_body(self) -> str:
        lines = [self._t("ui_debug_title"), ""]
        try:
            live = len(self._pool.all_clients())
        except Exception:
            live = 0
        lines.append(f"📱 {self._t('ui_perf_sessions')}: {live}")
        if self._dispatcher is not None:
            q = getattr(self._dispatcher, "queue_size", None)
            if q is not None:
                try:
                    lines.append(f"📦 queue depth: {q()}")
                except Exception:
                    pass
        if self._error_log is not None:
            try:
                since = int(time.time()) - 24 * 3600
                counts = await self._error_log.counts_by_severity(since)
                lines.append(f"⚠️ errors 24h: {sum(counts.values())}")
            except Exception:
                pass
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # text routing (FSM)
    # ------------------------------------------------------------------ #
    async def _route_text(self, message: Message) -> None:
        uid = message.from_user.id
        st = self._state(uid)
        text = (message.text or "").strip()
        if not st.step:
            # Command-driven flows (e.g. /login via BotManager) own this user
            # right now — stay silent instead of spamming "use the menu".
            if self._login.has_pending(uid):
                return
            return await message.reply_text(
                self._t("ui_use_menu"), reply_markup=self._main_menu()
            )

        handler = getattr(self, f"_step_{st.step}", None)
        if handler is None:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_use_menu"), reply_markup=self._main_menu()
            )
        await handler(message, st, text)

    # ------------------------------------------------------------------ #
    # Step handlers (FSM)
    # ------------------------------------------------------------------ #
    async def _step_login_phone(self, message: Message, st: UiState, text: str) -> None:
        uid = message.from_user.id
        phone = self._login.normalize_phone(text)
        if not self._login.valid_phone(text):
            return await message.reply_text(
                self._t("ui_login_phone_bad"), reply_markup=self._cancel_kbd())
        result = await self._login.start(uid, phone)
        if result != "send_code":
            # Stay on the phone step so the user can simply send it again
            # (this also survives a cooldown or a transient network error).
            key, _, detail = result.partition(":")
            try:
                self._log.error(
                    "bot", "login",
                    f"send_code failed for {phone}: {type(self._login).__name__} → {result}")
            except Exception:
                pass
            return await message.reply_text(
                self._t(f"ui_login_fail_{key}", error=detail),
                reply_markup=self._cancel_kbd())
        st.step = "login_code"
        st.login_phone = phone
        await self._persist(uid, st)
        try:
            self._log.info("bot", "login", f"login started for {phone}")
        except Exception:
            pass
        await message.reply_text(self._t("ui_login_code"), reply_markup=self._cancel_kbd())

    async def _step_login_code(self, message: Message, st: UiState, text: str) -> None:
        result = await self._login.submit_code(message.from_user.id, text.strip())
        if result == "send_password":
            st.step = "login_password"
            await self._persist(message.from_user.id, st)
            return await message.reply_text(
                self._t("ui_login_password"), reply_markup=self._cancel_kbd())
        await self._finish_login(message, st, result)

    async def _step_login_password(self, message: Message, st: UiState, text: str) -> None:
        result = await self._login.submit_password(message.from_user.id, text)
        await self._finish_login(message, st, result)

    async def _finish_login(self, message: Message, st: UiState, result: str) -> None:
        if result == "auth_expired":
            # The login client died in a restart. No code can ever succeed
            # against the old phone_code_hash, so offer a one-tap restart
            # with the same phone instead of letting the user retype codes.
            st.step = "login_phone"
            await self._persist(message.from_user.id, st)
            phone = getattr(st, "login_phone", "") or ""
            return await message.reply_text(
                self._t("ui_login_expired", phone=phone),
                reply_markup=self._kbd(
                    [[("📱 " + self._t("ui_login_retry"), CB["login_retry"])]]
                ) if phone else self._cancel_kbd())
        st.step = ""
        await self._persist(message.from_user.id, st)
        if result.startswith("login_success"):
            try:
                self._log.info("bot", "login", f"login success for {st.login_phone}")
            except Exception:
                pass
            await message.reply_text(
                self._t("ui_login_done", phone=st.login_phone),
                reply_markup=self._main_menu(),
            )
        else:
            key, _, detail = result.partition(":")
            await message.reply_text(
                self._t("ui_login_fail", error=detail or key),
                reply_markup=self._main_menu(),
            )

    # ---------------- credential FSM ----------------
    async def _step_cred_label(self, message: Message, st: UiState, text: str) -> None:
        st.cred_label = text[:64]
        st.step = "cred_api_id"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_cred_api_id"), reply_markup=self._cancel_kbd())

    async def _step_cred_api_id(self, message: Message, st: UiState, text: str) -> None:
        raw = text.strip()
        if not raw.isdigit():
            return await message.reply_text(
                self._t("ui_cred_api_id_bad"), reply_markup=self._cancel_kbd())
        st.cred_api_id = raw
        st.step = "cred_api_hash"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_cred_api_hash"), reply_markup=self._cancel_kbd())

    async def _step_cred_api_hash(self, message: Message, st: UiState, text: str) -> None:
        st.cred_api_hash = text.strip()
        st.step = ""
        if self._credentials is None:
            await self._persist(message.from_user.id, st)
            return await message.reply_text(
                self._t("ui_cred_fail", error="vault unavailable"),
                reply_markup=self._creds_menu(),
            )
        try:
            cred = await self._credentials.create(
                label=st.cred_label or "default",
                api_id=int(st.cred_api_id),
                api_hash=st.cred_api_hash,
                is_default=True,
            )
            try:
                self._pool.register_credentials(cred.id, cred.api_id, cred.api_hash)
                self._pool.register_credentials("default", cred.api_id, cred.api_hash)
            except Exception:
                pass
            try:
                self._log.info("bot", "cred", f"credential {cred.id[:8]} added")
            except Exception:
                pass
            await self._persist(message.from_user.id, st)
            await message.reply_text(
                self._t("ui_cred_done", id=cred.id[:8]),
                reply_markup=self._creds_menu(),
            )
        except Exception as exc:
            await self._record_error("bot", exc, user_id=message.from_user.id)
            await message.reply_text(
                self._t("ui_cred_fail", error=tg_detail(exc)),
                reply_markup=self._creds_menu(),
            )
        finally:
            try:
                await message.delete()
            except Exception:
                pass  # api_hash is sensitive — best-effort cleanup only

    # ---------------- rule FSM ----------------
    async def _step_rule_source(self, message: Message, st: UiState, text: str) -> None:
        st.rule_source = text.strip()
        st.step = "rule_target"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_rule_target"), reply_markup=self._cancel_kbd())

    async def _step_rule_target(self, message: Message, st: UiState, text: str) -> None:
        st.rule_target = text.strip()
        st.step = ""
        sessions = await self._sessions.list_all()
        if not sessions:
            await self._persist(message.from_user.id, st)
            return await message.reply_text(
                self._t("ui_no_sessions"), reply_markup=self._sessions_menu())
        session = sessions[0]
        try:
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
            try:
                self._log.info(
                    "bot", "rule", f"rule created {st.rule_source} -> {st.rule_target}")
            except Exception:
                pass
            await self._persist(message.from_user.id, st)
            await message.reply_text(
                self._t("ui_rule_done", source=st.rule_source, target=st.rule_target),
                reply_markup=self._rules_menu(),
            )
        except Exception as exc:
            await self._record_error("bot", exc, user_id=message.from_user.id)
            await message.reply_text(
                self._t("ui_rule_fail", error=tg_detail(exc)),
                reply_markup=self._rules_menu(),
            )

    # ---------------- filter FSM ----------------
    async def _step_filter_name(self, message: Message, st: UiState, text: str) -> None:
        st.step = "filter_blacklist"
        st.buffer["name"] = text.strip()
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_filter_blacklist"), reply_markup=self._cancel_kbd())

    async def _step_filter_blacklist(self, message: Message, st: UiState, text: str) -> None:
        st.step = ""
        blacklist = [w.strip() for w in text.replace("،", ",").split(",") if w.strip()]
        try:
            fr = FilterRule(
                name=st.buffer.get("name", "filter"), blacklist_keywords=blacklist)
            await self._filters.create(fr)
            try:
                self._log.info(
                    "bot", "filter",
                    f"filter {fr.name} created with {len(blacklist)} blacklist words")
            except Exception:
                pass
            await self._persist(message.from_user.id, st)
            await message.reply_text(
                self._t("ui_filter_done", name=fr.name), reply_markup=self._filters_menu())
        except Exception as exc:
            await self._record_error("bot", exc, user_id=message.from_user.id)
            await message.reply_text(
                self._t("ui_filter_fail", error=tg_detail(exc)),
                reply_markup=self._filters_menu(),
            )

    # ---------------- AI config FSM ----------------
    async def _step_ai_name(self, message: Message, st: UiState, text: str) -> None:
        st.step = "ai_provider"
        st.ai_name = text.strip()
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_ai_provider"), reply_markup=self._provider_picker())

    async def _step_ai_provider(self, message: Message, st: UiState, text: str) -> None:
        provider = text.strip().lower()
        if provider not in {p.value for p in AIProviderType}:
            return await message.reply_text(
                self._t("ui_ai_provider_bad"), reply_markup=self._cancel_kbd())
        st.ai_provider = provider
        st.step = "ai_model"
        kb = self._model_picker(st, provider)
        await self._persist(message.from_user.id, st)
        if kb is not None:
            await message.reply_text(self._t("ui_ai_model"), reply_markup=kb)
        else:
            await message.reply_text(
                self._t("ui_ai_model"), reply_markup=self._cancel_kbd())

    async def _step_ai_model(self, message: Message, st: UiState, text: str) -> None:
        st.ai_model = text.strip()
        st.step = "ai_api_key"
        await self._persist(message.from_user.id, st)
        await message.reply_text(self._t("ui_ai_key"), reply_markup=self._cancel_kbd())

    async def _step_ai_api_key(self, message: Message, st: UiState, text: str) -> None:
        st.step = ""
        try:
            cfg = AIConfig(
                name=st.ai_name,
                provider=AIProviderType(st.ai_provider),
                model=st.ai_model,
                api_key=text.strip(),
            )
            await self._ai.create(cfg)
            try:
                self._log.info(
                    "bot", "ai", f"AI config {cfg.name} created ({cfg.provider.value})")
            except Exception:
                pass
            await self._persist(message.from_user.id, st)
            await message.reply_text(
                self._t("ui_ai_done", name=cfg.name), reply_markup=self._ai_menu())
        except Exception as exc:
            await self._record_error("ai", exc, user_id=message.from_user.id)
            await message.reply_text(
                self._t("ui_ai_fail", error=tg_detail(exc)),
                reply_markup=self._ai_menu())
        finally:
            try:
                await message.delete()
            except Exception:
                pass  # api key is sensitive — best-effort cleanup
