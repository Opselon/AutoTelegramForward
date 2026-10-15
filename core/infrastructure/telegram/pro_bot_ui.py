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

import asyncio
import base64
import copy
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from ...application.backup_service import (
    decrypt_bundle,
    deserialize_bundle,
    encrypt_bundle,
    serialize_bundle,
)
from ...application.repositories import ConcurrencyError
from ...application.use_cases import (
    AIConfigUseCases,
    FilterRuleUseCases,
    ForwardRuleUseCases,
    MessageForwardingUseCase,
    SessionUseCases,
)
from ...domain.entities import AIConfig, FilterRule, ForwardRule
from ...domain.value_objects import AIProviderType, ForwardMode, RoutingType, RoutingDecision
from .client_pool import ClientPool
from .errors import SEV_ERROR, SEV_FATAL, tg_detail, tg_error
from .i18n import I18n, SUPPORTED_LANGUAGES
from .login_flow import LoginFlowManager

logger = logging.getLogger("atf.botui")

LANG_FLAG = {
    "en": "English 🇬🇧",
    "fa": "فارسی 🇮🇷",
    "pt": "Português 🇵🇹",
    "uz": "O'zbek 🇺🇿",
    "ru": "Русский 🇷🇺",
    "id": "Bahasa Indonesia 🇮🇩",
    "es": "Español 🇪🇸",
    "fr": "Français 🇫🇷",
    "it": "Italiano 🇮🇹",
    "zh": "中文 🇨🇳",
}

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
    "rule_del": "rdel",
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
    "upgrade": "upg",
    "creds": "cv",
    "cred_add": "ca",
    "cred_del": "cd",
    "cred_default": "cdef",
    "login_retry": "slr",
    "cancel": "cx",
    "debug": "dbg",
    "refresh": "rf",
    "perf": "pf",
    "dashboard": "dsh",
    "webpass": "wp",
    "webpass_reset": "wpr",
    # Web dashboard URL configuration (operator sets public URL / domain)
    "weburl": "wurl",
    "weburl_set": "wurls",
    # AI Rewrite (per-rule): pick AI config / prompt / fallback policy / test
    "r_ai": "rai",
    "r_ai_cfg": "raic",
    "r_ai_prompt": "raip",
    "r_ai_policy": "raif",
    "r_ai_test": "rait",
    "r_ai_ttxt": "raix",
    # Global prompt management (bot side)
    "p_list": "pl",
    "p_new": "pn",
    "p_edit": "pe",
    "p_del": "pd",
    "p_test": "pt",
    # Full-installation backup & restore (server migration)
    "migrate": "mg",
    "migrate_export": "mgx",
    "migrate_export_enc": "mgxe",
    "migrate_import": "mgi",
    "migrate_overwrite": "mgo",
    "migrate_help": "mgh",
    # Client settings & user credentials
    "settings": "stg",
    "client_creds": "clc",
    "client_cred_reset": "clcr",
    # Administrator Control Plane
    "admin": "adm",
    "adm_stats": "admst",
    "adm_users": "admus",
    "adm_user_view": "admuv",
    "adm_user_suspend": "admssp",
    "adm_user_activate": "admact",
    "adm_user_plan": "admpln",
    "adm_bcast": "admbc",
    "adm_bcast_confirm": "admbcc",
    "adm_bcast_cancel": "admbcx",
    "adm_forcesub": "admfs",
    "adm_forcesub_toggle": "admfst",
    "adm_forcesub_set": "admfss",
    "adm_admins": "admadm",
    "adm_admin_add": "admaadd",
    "adm_admin_del": "admadel",
    "adm_config": "admcfg",
    "check_sub": "chksub",
}

SEV_ICON = {"info": "ℹ️", "warn": "⚠️", "error": "❌", "auth": "🔐", "fatal": "💀"}


@dataclass
class UiState:
    """Per-user UI flow state (FSM)."""

    step: str = ""
    lang: str = ""
    user_id: int = 0
    rule_source: str = ""
    rule_target: str = ""
    filter_id: Optional[str] = None
    ai_field: str = ""
    ai_name: str = ""
    ai_provider: str = ""
    ai_model: str = ""
    ai_api_key: str = ""
    ai_base_url: str = ""
    login_phone: str = ""
    cred_label: str = ""
    cred_api_id: str = ""
    cred_api_hash: str = ""
    buffer: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize the FSM for the durable ui_states table."""
        return {
            "step": self.step,
            "lang": getattr(self, "lang", ""),
            "user_id": getattr(self, "user_id", 0),
            "rule_source": self.rule_source,
            "rule_target": self.rule_target,
            "ai_name": self.ai_name,
            "ai_provider": self.ai_provider,
            "ai_model": self.ai_model,
            "ai_base_url": self.ai_base_url,
            "login_phone": self.login_phone,
            "cred_label": self.cred_label,
            "cred_api_id": self.cred_api_id,
            "buffer": {k: v for k, v in self.buffer.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UiState":
        if not data:
            return cls()
        # Unwrap any recursive wrapping from past persistence anomalies
        cur = dict(data)
        while isinstance(cur.get("buffer"), dict) and any(
            k in cur["buffer"] for k in ("step", "login_phone", "cred_label", "rule_source")
        ):
            inner = cur.pop("buffer")
            for k, v in inner.items():
                if k != "buffer" and (not cur.get(k) or cur.get(k) == ""):
                    cur[k] = v
            cur["buffer"] = inner.get("buffer", {})

        return cls(
            step=cur.get("step", ""),
            lang=cur.get("lang", ""),
            user_id=int(cur.get("user_id", 0) or 0),
            rule_source=cur.get("rule_source", ""),
            rule_target=cur.get("rule_target", ""),
            filter_id=cur.get("filter_id"),
            ai_name=cur.get("ai_name", ""),
            ai_provider=cur.get("ai_provider", ""),
            ai_model=cur.get("ai_model", ""),
            ai_base_url=cur.get("ai_base_url", ""),
            login_phone=cur.get("login_phone", ""),
            cred_label=cur.get("cred_label", ""),
            cred_api_id=str(cur.get("cred_api_id", "")),
            buffer=cur.get("buffer", {}) or {},
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
        prompt_service=None,
        ai_transformer=None,
        pv_responder=None,
        web_url: str = "",
        account_servicer=None,
        backup_service=None,
        settings=None,
        admin_service=None,
        db=None,
    ) -> None:
        self.bot = bot
        self.i18n = i18n
        # NOTE: intentionally NO shared default language. _t() with no
        # explicit lang renders English — the only safe cross-user default.
        # Per-user renderers resolve via _lang_for(rule|st|uid) and pass
        # lang= explicitly, so one user's choice can never leak into
        # another user's render.
        self._sessions = sessions
        self._rules = rules
        self._filters = filter_rules
        self._ai = ai_configs
        self._pipeline = pipeline
        self._pool = pool
        self._login = login
        self._log = log_client
        self._admin_ids = set(admin_ids or [])
        self._admin_service = admin_service
        self._db = db
        self._credentials = credentials
        self._bot_tokens = bot_tokens
        self._dispatcher = dispatcher
        self._boot_manager = boot_manager
        self._ui_state_repo = ui_state_repo
        self._error_log = error_log
        self._metrics = metrics
        self._rule_stats = rule_stats
        self._users = users
        self._prompts = prompt_service
        self._ai_transformer = ai_transformer
        self._pv_responder = pv_responder
        self._web_url = (web_url or "").rstrip("/")
        self._accounts = account_servicer
        self._backup = backup_service
        self._settings = settings  # ISettingsRepository, optional
        self._states: Dict[int, UiState] = {}
        self._register()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _is_admin(self, user_id: int) -> bool:
        if getattr(self, "_admin_service", None) is not None:
            return self._admin_service.is_admin(user_id)
        admin_ids = getattr(self, "_admin_ids", None)
        try:
            return bool(admin_ids and int(user_id or 0) in admin_ids)
        except Exception:
            return False

    def _scope_uid(self, user_id: int) -> Optional[int]:
        if not getattr(self, "_admin_ids", None) and getattr(self, "_admin_service", None) is None:
            return None
        if self._is_admin(user_id):
            return None
        return int(user_id) if user_id else 0

    async def _owned_rule(self, rule_id: str, user_id: int):
        # v12.2: every rule read in the bot funnels through the use-case
        # ownership guard. rule_id comes from conversation state or callback
        # data, which is user-controlled — never trust it unsccoped.
        if not rule_id:
            return None
        return await self._rules.get_owned(rule_id, self._scope_uid(user_id))

    async def _save_owned_rule(self, rule, user_id: int, expected_version=None):
        # v12.2: writes go through update_owned so a rule whose id was
        # transplanted into another user's conversation state is refused.
        return await self._rules.update_owned(rule, self._scope_uid(user_id), expected_version)

    def _resolve_lang(self, uid: Optional[int] = None, user_obj: Any = None) -> str:
        """Resolve user's chosen language with fail-closed English fallback.

        Resolution order: in-memory UiState → persisted DB row → the user's
        own Telegram client language_code → 'en' (never the process-wide
        i18n.current, which is shared across all users and would leak one
        user's language choice into every other tenant's renders)."""
        if uid:
            states = getattr(self, "_states", None)
            if states and isinstance(states, dict):
                st = states.get(int(uid))
                if st and getattr(st, "lang", None) and st.lang in SUPPORTED_LANGUAGES:
                    return st.lang

            users = getattr(self, "_users", None)
            if users is not None and getattr(users, "_db", None) is not None:
                try:
                    row = users._db.query_one("SELECT language FROM users WHERE user_id=?", (int(uid),))
                    if row and row.get("language") and row["language"].lower() in SUPPORTED_LANGUAGES:
                        resolved = row["language"].lower()
                        if states and isinstance(states, dict) and int(uid) in states:
                            states[int(uid)].lang = resolved
                        return resolved
                except Exception:
                    pass

        if user_obj and getattr(user_obj, "language_code", None):
            code = str(user_obj.language_code).lower().split("-")[0].split("_")[0]
            if code in SUPPORTED_LANGUAGES:
                return code

        return "en"

    def _lang_for(self, *sources: Any, uid: Optional[int] = None) -> str:
        """Resolve the render language from whatever identity is at hand.

        Renderers that cannot take an explicit ``lang`` argument (rule
        menus, keypads, step handlers) call this with whatever identity
        they hold — ``rule.owner_user_id``, ``st`` (UiState) or a
        message's ``from_user`` — so they never fall back to a shared
        process-wide language.

        Resolution: UiState → persisted preference → the user's own
        Telegram client language_code → ``en``. Never reads
        ``i18n.current`` (process-global; would leak between users).
        """
        user_obj = None
        resolved_uid = uid
        for src in sources:
            if src is None:
                continue
            if isinstance(src, int):
                resolved_uid = resolved_uid or src
            elif isinstance(src, str) and src.isdigit():
                resolved_uid = resolved_uid or int(src)
            elif hasattr(src, "language_code"):
                user_obj = user_obj or src
            else:
                # Rule-like object exposing owner_user_id (ForwardRule,
                # drafts) or UiState exposing user_id.
                owner = getattr(src, "owner_user_id", None)
                if owner is None:
                    owner = getattr(src, "user_id", None)
                if isinstance(owner, int) and owner > 0:
                    resolved_uid = resolved_uid or owner
                elif isinstance(owner, str) and owner.isdigit():
                    resolved_uid = resolved_uid or int(owner)
                if hasattr(src, "lang") and not user_obj:
                    # UiState.lang is the in-memory per-user preference;
                    # _resolve_lang reads it from self._states directly.
                    pass
        return self._resolve_lang(resolved_uid, user_obj)

    def _state(self, uid: int, user: Any = None) -> UiState:
        st = self._states.get(uid)
        if st is None:
            st = UiState()
            resumed = self._load_state_sync(uid)
            if resumed:
                st = UiState.from_dict(resumed)
                logger.debug("resumed ui state for %s → %s", uid, st.step)
            self._states[uid] = st
        st.user_id = int(uid)
        if not getattr(st, "lang", ""):
            st.lang = self._resolve_lang(uid, user)
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

    def _t(self, key: str, lang: Optional[str] = None, **kw) -> str:
        # Never read the process-wide i18n.current singleton here: one user's
        # language choice must not leak into another user's render. An
        # explicit lang always wins; without one, English is the only safe
        # default because self._lang would be shared across all users.
        cur_lang = lang or "en"
        return self.i18n.t(key, lang=cur_lang, **kw)

    @staticmethod
    async def _safe_edit(target, text: str, reply_markup=None, **kw) -> None:
        """edit_message_text without the MESSAGE_NOT_MODIFIED traceback spam.

        Telegram rejects a no-op edit (same text + same keyboard) with 400
        MESSAGE_NOT_MODIFIED — normal on double-clicks / re-renders. Swallow
        only that; everything else propagates to the dispatcher's logger.
        """
        try:
            await target.edit_message_text(text, reply_markup=reply_markup, **kw)
        except MessageNotModified:
            pass

    def _kbd(self, rows) -> InlineKeyboardMarkup:
        built_rows = []
        for row in rows:
            built_row = []
            for item in row:
                if isinstance(item, InlineKeyboardButton):
                    built_row.append(item)
                    continue
                text, data = item
                sdata = str(data)
                if sdata.startswith("webapp:"):
                    built_row.append(InlineKeyboardButton(text, web_app=WebAppInfo(url=sdata[7:])))
                elif sdata.startswith("http://") or sdata.startswith("https://"):
                    built_row.append(InlineKeyboardButton(text, url=data))
                else:
                    built_row.append(InlineKeyboardButton(text, callback_data=data))
            built_rows.append(built_row)
        return InlineKeyboardMarkup(built_rows)

    def _cancel_kbd(self) -> InlineKeyboardMarkup:
        return self._kbd([[("❌ " + self._t("ui_cancel"), CB["cancel"])]])

    def _code_keypad(self, st: UiState) -> InlineKeyboardMarkup:
        lang = self._lang_for(st)
        return self._kbd([
            [("1", "k:1"), ("2", "k:2"), ("3", "k:3")],
            [("4", "k:4"), ("5", "k:5"), ("6", "k:6")],
            [("7", "k:7"), ("8", "k:8"), ("9", "k:9")],
            [("⌫", "k:del"), ("0", "k:0"), ("❌ " + self._t("ui_cancel", lang=lang), CB["cancel"])],
        ])

    def _login_code_text(self, st: UiState) -> str:
        lang = self._lang_for(st)
        digits = ""
        if isinstance(st.buffer, dict):
            digits = str(st.buffer.get("code_digits", "") or "")
        slots = []
        for i in range(5):
            if i < len(digits):
                slots.append(digits[i])
            else:
                slots.append("·")
        code_box = " ".join(slots)

        return (
            f"{self._t('ui_login_code')}\n\n"
            f"🔢 کد وارد شده: `[ {code_box} ]`"
        )

    def _render_welcome_onboarding(self, uid: int, user: Optional[Any] = None) -> Tuple[str, InlineKeyboardMarkup]:
        """First-run onboarding welcome screen for users without a connected Telegram session."""
        display_name = ""
        if user:
            parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
            display_name = " ".join(p for p in parts if p).strip()
            if not display_name and getattr(user, "username", None):
                display_name = f"@{user.username}"
        if not display_name:
            display_name = f"User {uid}" if uid else "Valued User"

        lang = self._resolve_lang(uid, user)

        text = self._t("ui_onboard_greeting", lang=lang, display_name=display_name) + self._t("ui_onboard_body", lang=lang)
        connect_btn = self._t("ui_onboard_connect_btn", lang=lang)
        support_btn = self._t("ui_onboard_support_btn", lang=lang)

        rows = [
            [(connect_btn, CB["login"])],
            [(support_btn, "https://t.me/Capxi")],
        ]
        return text, self._kbd(rows)

    async def _connect_prompt_text(self, uid: int) -> str:
        """Detailed connect guide text."""
        lang = await self._get_user_lang(uid)
        return (
            self._t("ui_connect_title", lang=lang) + "\n\n"
            + self._t("ui_connect_send_as_one", lang=lang) + "\n"
            + self._t("ui_connect_example_cmd", lang=lang) + "\n\n"
            + self._t("ui_connect_replace_note", lang=lang) + "\n"
            + self._t("ui_connect_bullet_space", lang=lang) + "\n"
            + self._t("ui_connect_bullet_no_token", lang=lang) + "\n"
            + self._t("ui_connect_bullet_two_step", lang=lang) + "\n\n"
            + self._t("ui_connect_country_example", lang=lang)
        )

    # ------------------------------------------------------------------ #
    # Menus
    # ------------------------------------------------------------------ #
    async def _get_user_lang(self, uid: int, user: Any = None) -> str:
        return self._resolve_lang(uid, user)

    async def _main_text(self, uid: int, user: Optional[Any] = None) -> str:
        """Main-menu dashboard: matches the Pro Telegram control panel design.

        Displays user details, connected sessions, applied rules, filters,
        server status, web token, service plan, forwarding metrics, and support links.
        Strictly scoped to the requesting tenant (uid).
        """
        # User details
        display_name = ""
        if user:
            parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
            display_name = " ".join(p for p in parts if p).strip()
            if not display_name and getattr(user, "username", None):
                display_name = f"@{user.username}"
        if not display_name:
            display_name = f"User {uid}" if uid else "Valued User"

        uid_str = str(uid) if uid else "N/A"

        # Rules & Active tasks
        rules: list[Any] = []
        if self._rules is not None and uid:
            try:
                rules = await self._rules.list_by_owner(uid)
            except Exception:
                logger.debug("rules lookup failed in _main_text", exc_info=True)

        active_rules = sum(1 for r in rules if getattr(r, "is_active", False))
        total_rules = len(rules)

        # Filters & Replacements
        filters_: list[Any] = []
        if self._filters is not None and uid:
            try:
                filters_ = await self._filters.list_by_owner(uid)
            except Exception:
                logger.debug("filters lookup failed in _main_text", exc_info=True)

        blocked_kw = sum(len(getattr(f, "blacklist_keywords", []) or []) for f in filters_)
        allowed_kw = sum(len(getattr(f, "whitelist_keywords", []) or []) for f in filters_)

        replace_count = 0
        for r in rules:
            try:
                if hasattr(r, "replacements") and callable(r.replacements):
                    reps = r.replacements()
                    if isinstance(reps, dict):
                        replace_count += len(reps)
                elif isinstance(getattr(r, "metadata", None), dict):
                    reps = r.metadata.get("replacements")
                    if isinstance(reps, dict):
                        replace_count += len(reps)
            except Exception:
                pass

        # AI Configurations
        ai_configs: list[Any] = []
        if self._ai is not None and uid:
            try:
                ai_configs = await self._ai.list_by_owner(uid)
            except Exception:
                logger.debug("ai configs lookup failed in _main_text", exc_info=True)

        active_ai = sum(1 for a in ai_configs if getattr(a, "is_enabled", False))
        total_ai = len(ai_configs)
        active_ai_model = "None"
        for a in ai_configs:
            if getattr(a, "is_enabled", False):
                active_ai_model = f"{getattr(a, 'provider', '')}/{getattr(a, 'model', '')}".strip("/")
                break

        # PV Responder Config
        pv_cfg = self._pv_responder.get_config_for_user(uid) if (self._pv_responder and uid) else None
        pv_enabled = bool(pv_cfg and pv_cfg.enabled)
        pv_delays = f"{pv_cfg.typing_delay_min:.1f}-{pv_cfg.typing_delay_max:.1f}s" if pv_cfg else "N/A"

        # Sessions & Connected Phone
        sessions: list[Any] = []
        if self._sessions is not None and uid:
            try:
                sessions = await self._sessions.list_by_owner(uid)
            except Exception:
                logger.debug("sessions lookup failed in _main_text", exc_info=True)

        live_sessions = sum(1 for s in sessions if getattr(s, "is_active", False))
        total_sessions = len(sessions)

        connected_phone = "None"
        if sessions:
            p = getattr(sessions[0], "phone_number", None) or getattr(sessions[0], "phone", None)
            if p:
                clean_phone = str(p).lstrip("+")
                connected_phone = f"+{clean_phone}"

        # Forwarded metrics strictly scoped to user's rules
        fwd_24h = await self._forwarded_24h(uid, rules=rules)

        # Server info
        server_name = "VIP 995"

        # Web Token
        token_str = "None"
        if self._accounts is not None:
            try:
                _, _, tok = await self._accounts.get_or_create_credentials(
                    uid, display_name=display_name, force_reset=False
                )
                if tok:
                    token_str = tok[:24] if len(tok) > 24 else tok
            except Exception:
                pass
        if token_str == "None":
            try:
                minted = await self._mint_web_token(uid)
                if minted:
                    token_str = minted[:24] if len(minted) > 24 else minted
            except Exception:
                pass

        # Service Plan
        plan = "VIP Pro" if (active_rules > 0 or live_sessions > 0) else "Free"
        remaining = "Active" if plan == "VIP Pro" else "0 days"

        # Resolve per-user WITH the user object so the user's own Telegram
        # client language wins (never the process-wide i18n.current).
        lang = await self._get_user_lang(uid, user)

        session_status = (
            self._t("ui_main_sess_active", lang=lang) if live_sessions > 0
            else (self._t("ui_main_sess_registered", lang=lang) if total_sessions > 0
                  else self._t("ui_main_sess_disconnected", lang=lang))
        )
        pv_status = self._t("ui_main_pv_on", lang=lang) if pv_enabled else self._t("ui_main_pv_off", lang=lang)
        sep = self._t("ui_sep", lang=lang)
        heavy = self._t("ui_main_sep_heavy", lang=lang)

        def _t(key, **kw):
            return self._t(key, lang=lang, **kw)

        lines = [
            _t("ui_main_welcome", name=display_name),
            "",
            _t("ui_main_service_status"),
            _t("ui_main_active_tasks", n=active_rules),
            "",
            _t("ui_main_rules_header"),
            _t("ui_main_blocked_kw", n=blocked_kw),
            _t("ui_main_allowed_kw", n=allowed_kw),
            _t("ui_main_replace_rules", n=replace_count),
            _t("ui_main_ai_configs", a=active_ai, t=total_ai, model=active_ai_model),
            _t("ui_main_pv_line", s=pv_status, d=pv_delays),
            "",
            _t("ui_main_support_1"),
            _t("ui_main_support_2"),
            "",
            heavy,
            _t("ui_main_account_header"),
            sep,
            _t("ui_main_display_name", n=display_name),
            _t("ui_main_user_id", id=uid_str),
            _t("ui_main_phone", p=connected_phone),
            _t("ui_main_server", s=server_name),
            _t("ui_main_token", t=token_str),
            "",
            _t("ui_main_plan_header"),
            sep,
            _t("ui_main_plan", p=plan),
            _t("ui_main_remaining", r=remaining),
            _t("ui_main_service_running"),
            "",
            _t("ui_main_tasks_header"),
            sep,
            _t("ui_main_active_tasks2", n=active_rules),
            _t("ui_main_fwd_24h", n=fwd_24h),
            _t("ui_main_total_tasks", n=total_rules),
            "",
            _t("ui_main_sessions_header"),
            sep,
            _t("ui_main_connected_sessions", a=live_sessions, b=total_sessions),
            _t("ui_main_sess_status", s=session_status),
            "",
            _t("ui_main_credits_header"),
            sep,
            _t("ui_main_balance"),
            heavy,
        ]

        return "\n".join(lines)

    async def _forwarded_24h(self, uid: int, rules: Optional[list] = None) -> int:
        """Forwarded message count in the trailing 24h for this user's rules."""
        if not uid:
            return 0
        total = 0
        if rules and self._rule_stats is not None:
            for r in rules:
                try:
                    st = await self._rule_stats.get(r.id)
                    if st:
                        total += int(st.get("forwarded") or 0)
                except Exception:
                    pass
            return total
        return 0

    async def _dlq_count(self) -> int:
        """Failed messages sitting in the dead-letter queue."""
        if self._error_log is None:
            return 0
        try:
            counts = await self._error_log.counts_by_severity()
            return int(counts.get("error", 0) or 0)
        except Exception:
            return 0

    def _main_menu(self, uid: int = 0) -> InlineKeyboardMarkup:
        cur_lang = self._resolve_lang(uid)
        is_adm = self._is_admin(uid) if uid else False
        rows = []
        if is_adm:
            rows.append([
                InlineKeyboardButton(
                    self._t("ui_main_menu_admin", lang=cur_lang),
                    callback_data=CB["admin"],
                )
            ])

        rows.extend([
            [
                InlineKeyboardButton(self._t("ui_main_menu_forward", lang=cur_lang), callback_data=CB["rules"]),
                InlineKeyboardButton(self._t("ui_main_menu_filters", lang=cur_lang), callback_data=CB["filters"]),
            ],
            [
                InlineKeyboardButton(self._t("ui_main_menu_publish", lang=cur_lang), callback_data=CB["rule_add"]),
                InlineKeyboardButton(self._t("ui_main_menu_clone", lang=cur_lang), callback_data=CB["ai"]),
            ],
            [
                InlineKeyboardButton(self._t("ui_main_menu_settings", lang=cur_lang), callback_data=CB["settings"]),
                InlineKeyboardButton(self._t("ui_main_menu_web", lang=cur_lang), callback_data=CB["dashboard"]),
            ],
            [
                InlineKeyboardButton(self._t("ui_main_menu_upgrade", lang=cur_lang), callback_data=CB["upgrade"]),
                InlineKeyboardButton(self._t("ui_main_menu_stats", lang=cur_lang), callback_data=CB["stats"]),
            ],
            [
                InlineKeyboardButton(self._t("ui_main_menu_help", lang=cur_lang), callback_data=CB["help"]),
                InlineKeyboardButton(self._t("ui_main_menu_lang", lang=cur_lang), callback_data=CB["lang"]),
            ],
            [
                InlineKeyboardButton(self._t("ui_main_menu_support", lang=cur_lang), url="https://t.me/Capxi"),
            ],
            [
                InlineKeyboardButton(self._t("ui_main_menu_sessions", lang=cur_lang), callback_data=CB["sessions"]),
            ],
        ])
        return InlineKeyboardMarkup(rows)

    def _upgrade_text(self, uid: int = 0) -> str:
        if self._resolve_lang(uid) == "fa":
            return (
                "💎 **ارتقای سرویس و خرید اشتراک ویژه (VIP Upgrade)**\n\n"
                "با ارتقا به نسخه ویژه، به پیشرفته‌ترین امکانات سیستم بدون هیچ محدودیتی دسترسی خواهید داشت:\n\n"
                "✨ **امکانات نسخه Pro / VIP:**\n"
                "• ⚡ فوروارد آنی پیام‌ها با حداکثر سرعت و بدون تاخیر\n"
                "• 🔓 بای‌پس و کپی کامل کانال‌های محافظت‌شده و قفل‌دار (Restricted Bypass)\n"
                "• 🤖 بازنویسی هوشمند متون، ترجمه و تغییر لحن با AI (ChatGPT / DeepSeek)\n"
                "• 🧹 فیلترهای پیشرفته بلک‌لیست، وایت‌لیست، رگکس و حذف لینک‌ها\n"
                "• 🌐 دسترسی کامل به پنل تحت وب حرفه‌ای و مدیریت چند کاربره\n"
                "• 📈 بدون محدودیت در تعداد تسک‌ها، کانال‌ها و پیام‌ها\n"
                "• 🛡 پشتیبانی ۲۴ ساعته اختصاصی\n\n"
                "💳 **پلن‌های فعال:**\n"
                "• پلن ۱ ماهه حرفه‌ای (Pro)\n"
                "• پلن ۳ ماهه تجاری (Business)\n"
                "• پلن دائمی نامحدود (Lifetime VIP)\n\n"
                "جهت خرید، استعلام تعرفه‌ها (ریالی یا تتر/رمزارز) و فعال‌سازی فوری با پشتیبانی در ارتباط باشید:\n"
                "📩 ارتباط مستقیم: @Capxi"
            )
        return (
            "💎 **Service Upgrade & VIP Plans**\n\n"
            "Upgrade your subscription to unlock all professional automation features with zero restrictions:\n\n"
            "✨ **VIP Features:**\n"
            "• ⚡ Real-time multi-channel message forwarding with ultra-low latency\n"
            "• 🔓 Complete bypass and copy from protected/restricted channels\n"
            "• 🤖 Smart AI rewrites, translation, and tone matching (ChatGPT / DeepSeek)\n"
            "• 🧹 Advanced keyword blacklists, whitelists, regex & link cleaning\n"
            "• 🌐 Full access to the Web Control Dashboard\n"
            "• 📈 Unlimited active forward rules, channels & volume\n"
            "• 🛡 24/7 dedicated priority support\n\n"
            "💳 **Available Plans:**\n"
            "• 1-Month Pro Plan\n"
            "• 3-Month Business Plan\n"
            "• Lifetime VIP Unlimited\n\n"
            "To purchase via Crypto (USDT/TON) or local payment and receive instant activation, contact support:\n"
            "📩 Direct Contact: @Capxi"
        )

    def _client_settings_menu(self, uid: int = 0) -> InlineKeyboardMarkup:
        is_fa = self._resolve_lang(uid) == "fa"
        is_adm = self._is_admin(uid) if uid else False
        rows = [
            [
                InlineKeyboardButton(
                    "📱 کلیدهای اختصاصی تلگرام (API ID & Hash)" if is_fa else "📱 My Telegram API Credentials",
                    callback_data=CB["client_creds"],
                )
            ],
            [
                InlineKeyboardButton(
                    "🌐 تغییر زبان (Language)" if is_fa else "🌐 Language",
                    callback_data=CB["lang"],
                ),
                InlineKeyboardButton(
                    "💎 اشتراک و سهمیه (Plan & Quota)" if is_fa else "💎 Plan & Quota",
                    callback_data=CB["upgrade"],
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔑 ورود مستقیم به پنل وب (Web Token)" if is_fa else "🔑 Web Access Token",
                    callback_data=CB["webpass"],
                )
            ],
        ]
        if is_adm:
            rows.append([
                InlineKeyboardButton(
                    "👑 ورود به پنل مدیریت کل (Admin Panel)" if is_fa else "👑 Administrator Control Plane",
                    callback_data=CB["admin"],
                )
            ])
        rows.append([
                InlineKeyboardButton(self._t("ui_back_main_menu"), callback_data=CB["main"]),
        ])
        return InlineKeyboardMarkup(rows)

    async def _render_admin_panel(self, uid: int) -> tuple[str, InlineKeyboardMarkup]:
        is_fa = self._resolve_lang(uid) == "fa"
        total_users = 0
        active_rules = 0
        dlq_errors = await self._dlq_count()
        fs_channel = ""
        fs_enabled = False

        if self._users:
            try:
                total_users = await self._users.count_all()
            except Exception:
                pass
        if self._rules:
            try:
                all_r = await self._rules.list_all()
                active_rules = sum(1 for r in all_r if getattr(r, "enabled", True))
            except Exception:
                pass

        if self._db:
            try:
                r1 = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_enabled'")
                if r1 and r1["value"] == "1":
                    fs_enabled = True
                r2 = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_channel'")
                if r2 and r2["value"]:
                    fs_channel = str(r2["value"])
            except Exception:
                pass

        fs_status_str = f"🟢 فعال ({fs_channel})" if fs_enabled and fs_channel else "⚪ غیرفعال"

        if is_fa:
            lines = [
                "👑 **پنل مدیریت کل سامانه (Administrator Control Plane)**\n",
                "📊 **وضعیت لحظه‌ای و مانیتورینگ سیستم:**",
                f"• 👥 کل کاربران / مستاجران ثبت‌شده: `{total_users}`",
                f"• ⚡ قوانین فعال فوروارد: `{active_rules}`",
                f"• ❌ خطاهای صف / DLQ: `{dlq_errors}`",
                f"• 🔒 قفل عضویت اجباری: {fs_status_str}",
                "• 🖥 وضعیت سرویس‌ها: Core 🟢 | Logger 🟢 | Web 🟢\n",
                "👇 جهت مدیریت بخش‌های مختلف سامانه، گزینه مورد نظر را انتخاب کنید:",
            ]
            rows = [
                [
                    InlineKeyboardButton("📊 آمار و مانیتورینگ جامع", callback_data=CB["adm_stats"]),
                    InlineKeyboardButton("👥 مدیریت کاربران و مستاجران", callback_data=CB["adm_users"]),
                ],
                [
                    InlineKeyboardButton("📢 ارسال پیام همگانی (Broadcast)", callback_data=CB["adm_bcast"]),
                    InlineKeyboardButton("🔒 قفل عضویت اجباری (Force-Sub)", callback_data=CB["adm_forcesub"]),
                ],
                [
                    InlineKeyboardButton("👮‍♂️ مدیریت مدیران سامانه", callback_data=CB["adm_admins"]),
                    InlineKeyboardButton("🔐 کلیدهای API سراسری (Vault)", callback_data=CB["creds"]),
                ],
                [
                    InlineKeyboardButton("⚙️ تنظیمات عمومی و دامنه وب", callback_data=CB["adm_config"]),
                    InlineKeyboardButton("📦 پشتیبان‌گیری و مهاجرت سرور", callback_data=CB["migrate"]),
                ],
                [
                    InlineKeyboardButton(f"🔙 {self._t('ui_back_main_menu')}", callback_data=CB["main"]),
                ],
            ]
        else:
            lines = [
                "👑 **Administrator Control Plane**\n",
                "📊 **System Status & Live Metrics:**",
                f"• 👥 Registered Users / Tenants: `{total_users}`",
                f"• ⚡ Active Forward Rules: `{active_rules}`",
                f"• ❌ DLQ / System Errors: `{dlq_errors}`",
                f"• 🔒 Force-Subscription: {fs_status_str}",
                "• 🖥 Services Health: Core 🟢 | Logger 🟢 | Web 🟢\n",
                "Select an administrative module below:",
            ]
            rows = [
                [
                    InlineKeyboardButton("📊 System Overview & Metrics", callback_data=CB["adm_stats"]),
                    InlineKeyboardButton("👥 Tenant & User Management", callback_data=CB["adm_users"]),
                ],
                [
                    InlineKeyboardButton("📢 Broadcast Center", callback_data=CB["adm_bcast"]),
                    InlineKeyboardButton("🔒 Force-Subscription Channel", callback_data=CB["adm_forcesub"]),
                ],
                [
                    InlineKeyboardButton("👮‍♂️ Administrator Access Control", callback_data=CB["adm_admins"]),
                    InlineKeyboardButton("🔐 Global API Credentials Vault", callback_data=CB["creds"]),
                ],
                [
                    InlineKeyboardButton("⚙️ Global Config & Domain", callback_data=CB["adm_config"]),
                    InlineKeyboardButton("📦 Server Migration & Backup", callback_data=CB["migrate"]),
                ],
                [
                    InlineKeyboardButton("🔙 Back to Main Menu", callback_data=CB["main"]),
                ],
            ]
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def _migrate_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("📤 " + self._t("ui_migrate_export"), CB["migrate_export"])],
            [("🔒 " + self._t("ui_migrate_export_enc"), CB["migrate_export_enc"])],
            [("📥 " + self._t("ui_migrate_import"), CB["migrate_import"])],
            [("ⓘ " + self._t("ui_migrate_help"), CB["migrate_help"])],
            [("↩ " + self._t("ui_back"), CB["back"])],
        ])

    def _sessions_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("➕ " + self._t("ui_login"), CB["login"])],
            [("💾 " + self._t("ui_backup"), CB["backup"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _rules_menu(self, rules: Optional[list] = None, page: int = 0, page_size: int = 5) -> InlineKeyboardMarkup:
        rows: List[List[Tuple[str, str]]] = [
            [("➕ " + self._t("ui_rule_add"), CB["rule_add"])],
        ]
        if rules:
            total_items = len(rules)
            total_pages = max(1, (total_items + page_size - 1) // page_size)
            page = max(0, min(page, total_pages - 1))
            start = page * page_size
            end = min(start + page_size, total_items)
            page_rules = rules[start:end]

            for r in page_rules:
                status_icon = "🟢" if r.is_active else "🔴"
                src = (r.source_chat_name or r.source_chat_id)[:12]
                dst = (r.target_chat_name or r.target_chat_id)[:12]
                rows.append([
                    (f"{status_icon} {src} ➔ {dst}", f"rd:{r.id}"),
                    ("✏️ ویرایش", f"re:{r.id}"),
                    ("🗑", f'{CB["rule_del"]}:{r.id}'),
                ])

            if total_pages > 1:
                nav_row: List[Tuple[str, str]] = []
                if page > 0:
                    nav_row.append((f"◀️ {self._t('ui_prev')}", f"r_pg:{page - 1}"))
                nav_row.append((f"📄 {page + 1} / {total_pages}", "noop"))
                if page < total_pages - 1:
                    nav_row.append((f"{self._t('ui_next')} ▶️", f"r_pg:{page + 1}"))
                rows.append(nav_row)

        rows.append([("⬅️ " + self._t("ui_back"), CB["main"])])
        return self._kbd(rows)

    def _render_rules_list(
        self, rules: Optional[list] = None, page: int = 0, page_size: int = 5
    ) -> Tuple[str, InlineKeyboardMarkup]:
        rules_list = rules if rules is not None else []
        # Resolve the language from the first rule's owner — all rules in
        # this list belong to the same tenant (owner-scoped queries).
        lang = self._lang_for(rules_list[0]) if rules_list else None
        lines = [self._t("ui_rules_title", lang=lang), ""]
        if not rules_list:
            lines.append(self._t("ui_none", lang=lang))
            return "\n".join(lines), self._rules_menu([], page=0, page_size=page_size)

        total_items = len(rules_list)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        end = min(start + page_size, total_items)
        page_items = rules_list[start:end]

        for idx, r in enumerate(page_items, start=start + 1):
            status = "🟢" if r.is_active else "🔴"
            target = getattr(r, "target_label", None) or (r.target_chat_name or r.target_chat_id)
            lines.append(
                f"{status} **{idx}.** `{r.id[:8]}` **{r.source_chat_name or r.source_chat_id}** ➔ **{target}**"
            )
            mode = getattr(r.forward_mode, "value", r.forward_mode)
            delay = float(getattr(r, "delay_seconds", 0) or 0)
            lines.append(f"     ⚙️ mode={mode} | ⏳ delay={delay:.0f}s")

        if total_pages > 1:
            lines.append("")
            lines.append(self._t("ui_rules_page_info", lang=lang, page=page + 1, total=total_pages, count=total_items))

        return "\n".join(lines), self._rules_menu(rules_list, page=page, page_size=page_size)

    def _render_rule_detail(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render the complete Task Management & Routing Overview (Screens 1, 4, 5)."""
        task_name = rule.name or f"task_{rule.id[:8]}"
        src_label = rule.source_chat_name or rule.source_chat_id or "AF Route Lab Source"
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id or "AF Route Lab Target")

        status_text = self._t("ui_rule_status_running", lang=lang) if rule.is_active else self._t("ui_rule_status_paused", lang=lang)
        debug_text = self._t("ui_rule_on", lang=lang) if rule.debug_mode else self._t("ui_rule_off", lang=lang)
        sender_type = rule.sender_type or self._t("ui_rule_sender_personal", lang=lang)
        topic_str = f"#{rule.topic_id}" if rule.topic_id else self._t("ui_none_label", lang=lang)
        fwd_hdr_status = self._t("ui_rule_yes", lang=lang) if rule.show_forward_header else self._t("ui_rule_no", lang=lang)
        dup_status = self._t("ui_rule_on", lang=lang) if rule.duplicate_filters else self._t("ui_rule_off", lang=lang)
        rep_sync_status = self._t("ui_rule_on", lang=lang) if rule.sync_replies else self._t("ui_rule_off", lang=lang)
        edit_sync_status = self._t("ui_rule_off", lang=lang) if rule.ignore_edits else self._t("ui_rule_on", lang=lang)
        del_sync_status = self._t("ui_rule_on", lang=lang) if rule.sync_deletes else self._t("ui_rule_off", lang=lang)
        cleaner_status = self._t("ui_rule_on", lang=lang) if rule.cleaner_enabled else self._t("ui_rule_off", lang=lang)

        warning_line = (
            self._t("ui_rule_detail_header_warning", lang=lang)
            if rule.show_forward_header
            else ""
        )

        bl_count = len(rule.blacklist)
        wl_count = len(rule.whitelist)
        cl_count = (1 if rule.cleaner_enabled else 0) + len(rule.clean_custom_words) + (1 if rule.remove_links else 0) + (1 if rule.remove_emojis else 0)
        fl_count = (1 if rule.duplicate_filters else 0) + (1 if rule.block_links else 0) + (1 if rule.block_voice else 0) + (1 if rule.block_stickers else 0)
        rep_count = len(rule.replacements)
        delay_val = f"{rule.delay_seconds:.0f}s" if rule.delay_seconds > 0 else "0s"

        grp = rule.feature_group or "all"
        chips = {
            "all": self._t("ui_rule_chip_all", lang=lang),
            "crypto": self._t("ui_rule_chip_crypto", lang=lang),
            "mmo": self._t("ui_rule_chip_mmo", lang=lang),
            "content": self._t("ui_rule_chip_content", lang=lang),
        }

        # Header and Task Overview text matching the Telegram Mini App & Mobile screen
        text = (
            self._t("ui_rule_detail_title", lang=lang) + "\n"
            + self._t("ui_rule_detail_sep", lang=lang) + "\n"
            + self._t("ui_rule_detail_task_name", lang=lang, task_name=task_name) + "\n\n"
            + self._t("ui_rule_detail_overview", lang=lang) + "\n"
            + self._t("ui_rule_detail_fwd_status", lang=lang, status_text=status_text) + "\n"
            + self._t("ui_rule_detail_source", lang=lang, src_label=src_label, source_chat_id=rule.source_chat_id) + "\n"
            + self._t("ui_rule_detail_target", lang=lang, dst_label=dst_label, target_chat_id=rule.target_chat_id) + "\n"
            + self._t("ui_rule_detail_show_header", lang=lang, fwd_hdr_status=fwd_hdr_status) + warning_line + "\n"
            + self._t("ui_rule_detail_debug_topic", lang=lang, debug_text=debug_text, topic_str=topic_str) + "\n\n"
            + self._t("ui_rule_detail_features", lang=lang) + "\n"
            + self._t("ui_rule_detail_sync_row1", lang=lang, edit_sync_status=edit_sync_status, del_sync_status=del_sync_status, rep_sync_status=rep_sync_status) + "\n"
            + self._t("ui_rule_detail_sync_row2", lang=lang, fwd_hdr_status=fwd_hdr_status, dup_status=dup_status) + "\n"
            + self._t("ui_rule_detail_sync_row3", lang=lang, cleaner_status=cleaner_status, delay_val=delay_val) + "\n\n"
            + self._t("ui_rule_detail_sender", lang=lang, sender_type=sender_type) + "\n"
            + self._t("ui_rule_detail_paid_addon", lang=lang) + "\n"
        )

        rows: List[List[Tuple[str, str]]] = [
            # Top routing controls
            [
                (self._t("ui_rule_edit_source", lang=lang), f"res:{rule.id}"),
                (self._t("ui_rule_edit_target", lang=lang), f"ret:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_header", lang=lang, fwd_hdr_status=fwd_hdr_status), f"rtg_fwdhdr:{rule.id}"),
                (self._t("ui_rule_btn_topic", lang=lang, topic_str=topic_str), f"rtpc:{rule.id}"),
            ],
            # Features: 5 Toggle Pills (matching mobile screen)
            [
                (self._t("ui_rule_btn_edit", lang=lang, edit_sync_status=edit_sync_status), f"red:{rule.id}"),
                (self._t("ui_rule_btn_delete", lang=lang, del_sync_status=del_sync_status), f"rdl:{rule.id}"),
                (self._t("ui_rule_btn_reply", lang=lang, rep_sync_status=rep_sync_status), f"rtg_repsync:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_header", lang=lang, fwd_hdr_status=fwd_hdr_status), f"rtg_fwdhdr:{rule.id}"),
                (self._t("ui_rule_btn_dup", lang=lang, dup_status=dup_status), f"rtg_dup:{rule.id}"),
            ],
            # 6 Modular Option Cards (matching mobile screen)
            [
                (self._t("ui_rule_btn_replace", lang=lang, rep_count=rep_count), f"rtx:{rule.id}"),
                (self._t("ui_rule_btn_blacklist", lang=lang, bl_count=bl_count), f"rfl_bl:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_whitelist", lang=lang, wl_count=wl_count), f"rfl_wl:{rule.id}"),
                (self._t("ui_rule_btn_cleaner", lang=lang, cl_count=cl_count), f"rcl:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_filters", lang=lang, fl_count=fl_count), f"rfl:{rule.id}"),
                (self._t("ui_rule_btn_delay", lang=lang, delay_val=delay_val), f"rdly:{rule.id}"),
            ],
            # Task operations
            [
                (self._t("ui_rule_btn_status", lang=lang, status_text=status_text), f"rtg:{rule.id}"),
                (self._t("ui_rule_btn_duplicate", lang=lang), f"rdup:{rule.id}"),
                (self._t("ui_rule_btn_rename", lang=lang), f"rrn:{rule.id}"),
                (self._t("ui_rule_btn_draft", lang=lang), f"re:{rule.id}"),
            ],
            [
                (self._t("ui_rule_btn_advanced", lang=lang), f"radv:{rule.id}"),
                (self._t("ui_rule_btn_delete_task", lang=lang), f"rrm:{rule.id}"),
            ],
            # Feature groups tabs / chips
            [
                (f"● {chips['all']}" if grp == "all" else chips["all"], f"rgrp:{rule.id}:all"),
                (f"● {chips['crypto']}" if grp == "crypto" else chips["crypto"], f"rgrp:{rule.id}:crypto"),
                (f"● {chips['mmo']}" if grp == "mmo" else chips["mmo"], f"rgrp:{rule.id}:mmo"),
                (f"● {chips['content']}" if grp == "content" else chips["content"], f"rgrp:{rule.id}:content"),
            ],
            [
                (f"🔙 {self._t('ui_rule_back_rules')}", CB["rules"]),
            ],
        ]

        # Filtered Add-ons grid based on selected feature group
        addons = rule.paid_addons
        if grp == "crypto":
            rows.extend([
                [
                    (self._t("ui_rule_grp_crypto_mode", lang=lang, state=self._t("ui_rule_on", lang=lang) if rule.crypto_mode else self._t("ui_rule_off", lang=lang)), f"rtg_crypto:{rule.id}"),
                    (self._t("ui_rule_addon_sig_format", lang=lang), f"radon:{rule.id}:sig_format"),
                ],
                [
                    (self._t("ui_rule_addon_wallet_clean", lang=lang), f"radon:{rule.id}:wallet_clean"),
                    (self._t("ui_rule_addon_webhook", lang=lang), f"radon:{rule.id}:webhook"),
                ],
            ])
        elif grp == "mmo":
            ak_on = rule.affiliate_kit.get("enabled", False)
            rows.extend([
                [
                    ("🔗 Affiliate Kit: " + ("ON" if ak_on else "OFF"), f"rafl:{rule.id}"),
                    ("🔒 Outbound Webhooks", f"radon:{rule.id}:webhook"),
                ],
                [
                    ("🔗 Link Buttons", f"rbtn:{rule.id}"),
                    ("🔒 Auto Delete Messages", f"radon:{rule.id}:auto_del"),
                ],
            ])
        elif grp == "content":
            c_comm = addons.get("channel_comments", False)
            rows.extend([
                [
                    ("★ Channel Comments: " + ("ON" if c_comm else "OFF"), f"radon:{rule.id}:comments"),
                    ("🔒 Forward Every N", f"radon:{rule.id}:every_n"),
                ],
                [
                    ("🔒 Keyword Confirm", f"radon:{rule.id}:kw_confirm"),
                    ("🔒 Keyword Reply Thr", f"radon:{rule.id}:kw_reply"),
                ],
                [
                    ("🔒 Auto Delete Msg", f"radon:{rule.id}:auto_del"),
                    ("🖼 Watermark", f"rwm:{rule.id}"),
                ],
            ])
        else:  # "all" (matches Image 4 exactly)
            c_comm = addons.get("channel_comments", False)
            ev_n = addons.get("every_n", False)
            kw_c = addons.get("kw_confirm", False)
            kw_r = addons.get("kw_reply", False)
            a_del = addons.get("auto_del", False)
            wh = addons.get("webhook", False)

            rows.extend([
                [
                    ("★ Channel Comments: " + ("ON" if c_comm else "OFF"), f"radon:{rule.id}:comments"),
                    ("🔒 Forward Every N: " + ("ON" if ev_n else "OFF"), f"radon:{rule.id}:every_n"),
                ],
                [
                    ("🔒 Keyword Confirma... " + ("ON" if kw_c else "OFF"), f"radon:{rule.id}:kw_confirm"),
                    ("🔒 Keyword Reply Thr... " + ("ON" if kw_r else "OFF"), f"radon:{rule.id}:kw_reply"),
                ],
                [
                    ("🔒 Auto Delete Messa... " + ("ON" if a_del else "OFF"), f"radon:{rule.id}:auto_del"),
                    ("🔒 Outbound Webhoo... " + ("ON" if wh else "OFF"), f"radon:{rule.id}:webhook"),
                ],
            ])

        rows.append([
            (self._t("ui_rule_addon_all", lang=lang), f"radon_all:{rule.id}"),
        ])
        rows.append([
            (f"⬅️ {self._t('ui_rule_back_rules')}", CB["rules"]),
        ])

        return text, self._kbd(rows)

    def _render_advanced_settings(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render the complete Advanced Settings dashboard (Screens 2 & 3)."""
        task_name = rule.name or f"task_{rule.id[:8]}"
        src_label = rule.source_chat_name or rule.source_chat_id or "AF Route Lab Source"
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id or "AF Route Lab Target")

        dup_status = "on 🟢" if rule.duplicate_filters else "off ⚪"
        crypto_status = "on 🟢" if rule.crypto_mode else "off ⚪"
        bl_count = len(rule.blacklist)
        wl_count = len(rule.whitelist)
        bu_count = len(rule.blacklist_users)
        wu_count = len(rule.whitelist_users)
        cl_count = len(rule.clean_custom_words) + (1 if rule.remove_links else 0) + (1 if rule.remove_emojis else 0)
        fl_count = 1 if rule.filter_rule_id else 0

        sched_on = bool(rule.auto_post_scheduler.get("enabled", False))
        sched_status = "On 🟢" if sched_on else "Off ⚪"
        pwr_on = bool(rule.power_schedule.get("enabled", False))
        pwr_status = "On 🟢" if pwr_on else "Off ⚪"

        delay_val = f"{rule.delay_seconds:.0f} s" if rule.delay_seconds > 0 else "0 s"
        mte_val = f"{rule.max_time_edit} s" if rule.max_time_edit > 0 else "0 s"

        raw_status = "on 🟢" if rule.process_raw_text else "off ⚪"
        btn2txt_status = "on 🟢" if rule.button_to_text else "off ⚪"

        lk_badge = "🚫 حذف" if (rule.remove_links or (isinstance(rule.metadata, dict) and bool(rule.metadata.get("remove_links")))) else "🟢 مجاز"
        vc_badge = "🚫 مسدود" if rule.block_voice else "🟢 مجاز"
        st_badge = "🚫 مسدود" if rule.block_stickers else "🟢 مجاز"
        em_badge = "🧹 پاکسازی" if rule.remove_emojis else "🟢 حفظ"
        alb_badge = "1️⃣ فقط اولین مدیا" if rule.album_mode == "first" else ("🔀 تفکیک پیام‌ها" if rule.album_mode == "split" else "🖼 آلبوم کامل")
        reps_badge = f"{len(rule.replacements)} مورد فعال" if len(rule.replacements) else "غیرفعال"
        hdr_badge = "تنظیم شده" if rule.header else "خالی"
        ftr_badge = "تنظیم شده" if rule.footer else "خالی"
        ed_badge = "⏹ نادیده" if rule.ignore_edits else "🔄 سینک"
        dl_badge = "🗑 سینک" if rule.sync_deletes else "⏹ نادیده"

        rep_count = len(rule.replacements)
        tr_status = rule.translate_lang.upper() if rule.translate_lang else "Off"
        wm_status = "Configured" if rule.watermark else "Empty"
        btn_count = len(rule.link_buttons)
        ak_on = rule.affiliate_kit.get("enabled", False)
        ak_status = "Active" if ak_on else "Off"

        ocr_status = "Active 🟢" if rule.ocr_image_ai else "Inactive ⚪"
        ai_rw_status = "Active 🟢" if rule.smart_ai_rewrite else "Inactive ⚪"
        digest_status = "Active 🟢" if rule.news_digest else "Inactive ⚪"

        text = (
            f"🎚 **تنظیمات پیشرفته قانون (Advanced Settings)**\n"
            f"📋 **Route:** `{task_name}`\n"
            f"📡 **مبدأ:** `{src_label}` (`{rule.source_chat_id}`)\n"
            f"🎯 **مقصد:** `{dst_label}` (`{rule.target_chat_id}`)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🛡 **Filters (?):**\n"
            f"• Duplicate: `{dup_status}` | Crypto: `{crypto_status}`\n"
            f"• Blacklist: `Activated: {bl_count}` | Whitelist: `Activated: {wl_count}`\n"
            f"• Block Users: `Activated: {bu_count}` | Allow Users: `Activated: {wu_count}`\n"
            f"• Cleaner: `Activated: {cl_count}` | Filter Rules: `Activated: {fl_count}`\n"
            f"• لینک: {lk_badge} | ویس: {vc_badge} | استیکر: {st_badge} | ایموجی: {em_badge}\n\n"
            f"⏰ **Schedules (?):**\n"
            f"• Auto Post Scheduler: `{sched_status}`\n"
            f"• Power On/Off Schedule: `{pwr_status}`\n\n"
            f"⏱ **Time Forward (?):**\n"
            f"• Delay: `{delay_val}` | Max Edit Time: `{mte_val}`\n\n"
            f"⚙️ **Processing Messages (?):**\n"
            f"• Process Raw Text: `{raw_status}` | Button To Text: `{btn2txt_status}`\n\n"
            f"✍️ **Modify Content (?):**\n"
            f"• Header: `{hdr_badge}` | Footer: `{ftr_badge}`\n"
            f"• Replace: `Activated: {reps_badge}` | Translate: `{tr_status}`\n"
            f"• Watermark: `{wm_status}` | Link Buttons: `{btn_count} btns`\n"
            f"• Affiliate Kit: `{ak_status}` | Album Mode: `{alb_badge}`\n\n"
            f"🤖 **AI Features (Creativity {rule.ai_creativity} · Max Tokens {rule.ai_max_tokens}):**\n"
            f"• OCR Image AI: `{ocr_status}`\n"
            f"• Smart AI Rewrite: `{ai_rw_status}`\n"
            f"• News Digest: `{digest_status}`\n"
        )

        rows: List[List[Tuple[str, str]]] = [
            # Filters Section Grid
            [
                (f"🔀 Duplicate Filters: {dup_status}", f"rtg_dup:{rule.id}"),
                (f"🪙 Crypto Mode: {crypto_status}", f"rtg_crypto:{rule.id}"),
            ],
            [
                (f"🚫 Blacklist ({bl_count})", f"rfl_bl:{rule.id}"),
                (f"✅ Whitelist ({wl_count})", f"rfl_wl:{rule.id}"),
            ],
            [
                (f"🚷 Blacklist Users ({bu_count})", f"rfl_bu:{rule.id}"),
                (f"👥 Whitelist Users ({wu_count})", f"rfl_wu:{rule.id}"),
            ],
            [
                (f"🧹 Cleaner ({cl_count})", f"rcl:{rule.id}"),
                (f"🛡 Filters ({fl_count})", f"rfl:{rule.id}"),
            ],
            [
                (f"🔗 لینک: {lk_badge}", f"rlk:{rule.id}"),
                (f"🎙 ویس: {vc_badge}", f"rvc:{rule.id}"),
            ],
            [
                (f"🎭 استیکر: {st_badge}", f"rst:{rule.id}"),
                (f"😀 ایموجی: {em_badge}", f"rem:{rule.id}"),
            ],
            # Schedules Section
            [
                (f"⏰ Auto Post Scheduler: {sched_status}", f"rsch_auto:{rule.id}"),
                (f"⏻ Power On/Off Sch: {pwr_status}", f"rsch_pwr:{rule.id}"),
            ],
            # Time Forward Section
            [
                (f"⏱ Set Delay ({delay_val})", f"rdly:{rule.id}"),
                (f"⏳ Max Edit Time ({mte_val})", f"rmte:{rule.id}"),
            ],
            # Processing Messages Section
            [
                (f"📄 Process Raw Text: {raw_status}", f"rtg_raw:{rule.id}"),
                (f"🔘 Button To Text: {btn2txt_status}", f"rtg_btn2txt:{rule.id}"),
            ],
            # Modify Content Section
            [
                (f"📝 هدر و فوتر", f"rhf:{rule.id}"),
                (f"🔤 Replace ({rep_count})", f"rtx:{rule.id}"),
            ],
            [
                (f"🌐 Translate Language ({tr_status})", f"rtr:{rule.id}"),
                (f"🖼 Watermark ({wm_status})", f"rwm:{rule.id}"),
            ],
            [
                (f"🔗 Link Buttons ({btn_count})", f"rbtn:{rule.id}"),
                (f"💲 Affiliate Kit ({ak_status})", f"rafl:{rule.id}"),
            ],
            [
                (f"✏️ ادیت: {ed_badge}", f"red:{rule.id}"),
                (f"🗑 حذف: {dl_badge}", f"rdl:{rule.id}"),
            ],
            [
                (f"🖼 آلبوم: {alb_badge}", f"rmg:{rule.id}"),
                (f"🗑 حذف قانون", f"rrm:{rule.id}"),
            ],
            # AI Features Section
            [
                (f"👁 OCR Image AI: {ocr_status}", f"rtg_ocr:{rule.id}"),
                (f"✨ Smart AI Rewrite: {ai_rw_status}", f"rtg_ai:{rule.id}"),
            ],
            [
                (f"📰 News Digest: {digest_status}", f"rtg_digest:{rule.id}"),
                (f"⚙️ AI Config ({rule.ai_creativity}/{rule.ai_max_tokens})", f"rai_cfg:{rule.id}"),
            ],
            # Return button
            [
                (f"🔙 {self._t('ui_rule_back_task')}", f"rd:{rule.id}"),
            ],
        ]

        return text, self._kbd(rows)

    def _render_all_addons(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render all 18 Add-ons catalogue with toggle controls."""
        task_name = rule.name or f"task_{rule.id[:8]}"
        addons = rule.paid_addons
        
        all_18 = [
            ("comments", "★ Channel Comments"),
            ("every_n", "🔒 Forward Every N Messages"),
            ("kw_confirm", "🔒 Keyword Confirmation"),
            ("kw_reply", "🔒 Keyword Reply Thread"),
            ("auto_del", "🔒 Auto Delete Messages"),
            ("webhook", "🔒 Outbound Webhooks"),
            ("crypto", "🪙 Crypto Mode"),
            ("affiliate", "💲 Affiliate Kit"),
            ("link_buttons", "🔗 Link Buttons"),
            ("watermark", "🖼 Watermark Protection"),
            ("duplicate", "🔀 Duplicate Filters"),
            ("cleaner", "🧹 Cleaner Protocol"),
            ("scheduler", "⏰ Auto Post Scheduler"),
            ("power_schedule", "⏻ Power On/Off Schedule"),
            ("raw_text", "📄 Process Raw Text"),
            ("button_to_text", "🔘 Button To Text"),
            ("ocr_ai", "👁 OCR Image AI"),
            ("ai_rewrite", "✨ Smart AI Rewrite"),
        ]

        text = (
            f"👑 **PAID ADDON Catalogue — 18 Modular Extensions**\n"
            f"📋 **Task:** `{task_name}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"از این بخش می‌توانید افزونه‌های ماژولار و پیشرفته را برای این تسک فعال یا غیرفعال نمایید:\n"
        )

        rows: List[List[Tuple[str, str]]] = []
        for i in range(0, len(all_18), 2):
            pair = []
            k1, lbl1 = all_18[i]
            on1 = "🟢" if addons.get(k1) else "⚪"
            pair.append((f"{lbl1} {on1}", f"radon_tg:{rule.id}:{k1}"))
            if i + 1 < len(all_18):
                k2, lbl2 = all_18[i + 1]
                on2 = "🟢" if addons.get(k2) else "⚪"
                pair.append((f"{lbl2} {on2}", f"radon_tg:{rule.id}:{k2}"))
            rows.append(pair)

        rows.append([(f"🔙 {self._t('ui_rule_back_task')}", f"rd:{rule.id}")])
        return text, self._kbd(rows)

    def _render_draft_editor(self, rule: ForwardRule, draft: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render comprehensive draft editor screen where settings are staged safely."""
        src_label = draft.get("source_name") or draft.get("source_chat_id", "")
        dst_label = draft.get("target_name") or draft.get("target_chat_id", "")
        status_str = self._t("ui_draft_status_on", lang=lang) if draft.get("is_active", True) else self._t("ui_draft_status_off", lang=lang)

        f_mode = draft.get("forward_mode", ForwardMode.COPY_MESSAGE.value)
        mode_val = getattr(f_mode, "value", str(f_mode))
        mode_names = {
            "COPY_MESSAGE": self._t("ui_draft_mode_copy", lang=lang),
            "DIRECT_FORWARD": self._t("ui_draft_mode_forward", lang=lang),
            "REWRITE_AI": self._t("ui_draft_mode_ai", lang=lang),
        }
        mode_title = mode_names.get(mode_val, mode_val)

        reps = draft.get("replacements") or {}
        reps_count = len(reps) if isinstance(reps, dict) else 0
        reps_badge = self._t("ui_draft_count_n", lang=lang, n=reps_count) if reps_count else self._t("ui_draft_empty", lang=lang)

        hdr = str(draft.get("header") or "")
        ftr = str(draft.get("footer") or "")
        hdr_badge = f"`{hdr[:14]}…`" if len(hdr) > 14 else (f"`{hdr}`" if hdr else self._t("ui_draft_empty", lang=lang))
        ftr_badge = f"`{ftr[:14]}…`" if len(ftr) > 14 else (f"`{ftr}`" if ftr else self._t("ui_draft_empty", lang=lang))

        lk_badge = self._t("ui_draft_remove", lang=lang) if draft.get("remove_links") else self._t("ui_draft_allowed", lang=lang)
        vc_badge = self._t("ui_draft_blocked", lang=lang) if draft.get("block_voice") else self._t("ui_draft_allowed", lang=lang)
        st_badge = self._t("ui_draft_blocked", lang=lang) if draft.get("block_stickers") else self._t("ui_draft_allowed", lang=lang)
        em_badge = self._t("ui_draft_clean", lang=lang) if draft.get("remove_emojis") else self._t("ui_draft_keep", lang=lang)
        ed_badge = self._t("ui_draft_ignore", lang=lang) if draft.get("ignore_edits") else self._t("ui_draft_sync", lang=lang)
        dl_badge = self._t("ui_draft_delete_sync", lang=lang) if draft.get("sync_deletes") else self._t("ui_draft_ignore", lang=lang)

        album_names = {
            "album": self._t("ui_album_full", lang=lang),
            "first": self._t("ui_album_first", lang=lang),
            "split": self._t("ui_album_split", lang=lang),
        }
        alb_mode = str(draft.get("album_mode") or "album")
        alb_badge = album_names.get(alb_mode, self._t("ui_album_full", lang=lang))

        text = (
            self._t("ui_draft_title", lang=lang) + "\n\n"
            + self._t("ui_draft_rule_id", lang=lang, rid=rule.id[:8], v=rule.version) + "\n"
            + self._t("ui_sep", lang=lang) + "\n"
            + self._t("ui_draft_source", lang=lang, src=src_label, sid=draft.get('source_chat_id')) + "\n"
            + self._t("ui_draft_target", lang=lang, dst=dst_label, tid=draft.get('target_chat_id')) + "\n"
            + self._t("ui_draft_status", lang=lang, s=status_str) + "\n"
            + self._t("ui_draft_fmode", lang=lang, m=mode_title) + "\n"
            + self._t("ui_sep", lang=lang) + "\n"
            + self._t("ui_draft_replace", lang=lang, n=reps_badge) + "\n"
            + self._t("ui_draft_hf", lang=lang, h=hdr_badge, f=ftr_badge) + "\n"
            + self._t("ui_draft_pair_links_voice", lang=lang, lk=lk_badge, vc=vc_badge) + "\n"
            + self._t("ui_draft_pair_st_emoji", lang=lang, st=st_badge, em=em_badge) + "\n"
            + self._t("ui_draft_pair_ed_del", lang=lang, ed=ed_badge, dl=dl_badge) + "\n"
            + self._t("ui_draft_media", lang=lang, a=alb_badge) + "\n\n"
            + self._t("ui_draft_hint", lang=lang)
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (self._t("ui_draft_btn_src", lang=lang), f"ed_s:{rule.id}"),
                (self._t("ui_draft_btn_dst", lang=lang), f"ed_t:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_mode", lang=lang, m=mode_val), f"ed_m:{rule.id}"),
                (self._t("ui_draft_btn_status", lang=lang, s=status_str), f"ed_a:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_links", lang=lang, b=lk_badge), f"ed_lk:{rule.id}"),
                (self._t("ui_draft_btn_voice", lang=lang, b=vc_badge), f"ed_vc:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_stickers", lang=lang, b=st_badge), f"ed_st:{rule.id}"),
                (self._t("ui_draft_btn_emoji", lang=lang, b=em_badge), f"ed_em:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_edit", lang=lang, b=ed_badge), f"ed_ed:{rule.id}"),
                (self._t("ui_draft_btn_del", lang=lang, b=dl_badge), f"ed_dl:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_album", lang=lang, b=alb_badge), f"ed_mg:{rule.id}"),
                (self._t("ui_draft_btn_replace", lang=lang, n=reps_count), f"rtx:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_ai", lang=lang, b=draft.get("ai_status_badge") or self._t("ui_draft_ai_off", lang=lang)), f"r_ai:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_hf", lang=lang), f"rhf:{rule.id}"),
            ],
            [
                (self._t("ui_draft_btn_save", lang=lang), f"ed_save:{rule.id}"),
                (self._t("ui_draft_btn_cancel", lang=lang), f"ed_can:{rule.id}"),
            ],
            [
                (f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}"),
            ],
        ]
        return text, self._kbd(rows)

    def _render_replace_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render text replacement configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        reps = rule.replacements

        lines = [
            self._t("ui_rep_title", lang=lang) + "\n",
            self._t("ui_rep_rule", lang=lang, s=src_label, d=dst_label) + "\n",
            self._t("ui_sep", lang=lang),
            self._t("ui_rep_active_list", lang=lang),
        ]
        if not reps:
            lines.append(self._t("ui_rep_none", lang=lang) + "\n")
        else:
            for idx, (old, new) in enumerate(reps.items(), start=1):
                rep_display = f"`{new}`" if new else self._t("ui_rep_word_deleted", lang=lang)
                lines.append(f"{idx}. `{old}` ➔ {rep_display}")
            lines.append("")

        lines.extend([
            self._t("ui_rep_help_title", lang=lang),
            self._t("ui_rep_help_add", lang=lang),
            self._t("ui_rep_help_fmt", lang=lang),
            self._t("ui_rep_help_del", lang=lang),
        ])

        rows: List[List[Tuple[str, str]]] = [
            [(self._t("ui_rep_btn_add", lang=lang), f"rtxa:{rule.id}")],
        ]
        if reps:
            del_row: List[Tuple[str, str]] = []
            for idx, old in enumerate(list(reps.keys())[:6]):
                display_k = old[:10]
                del_row.append((f"❌ {display_k}", f"rtxd:{rule.id}:{idx}"))
                if len(del_row) == 2:
                    rows.append(del_row)
                    del_row = []
            if del_row:
                rows.append(del_row)
            rows.append([(self._t("ui_rep_btn_clear", lang=lang), f"rtxc:{rule.id}")])

        rows.append([(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")])
        return "\n".join(lines), self._kbd(rows)

    def _render_header_footer_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render header and footer configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        hdr = rule.header
        ftr = rule.footer

        lines = [
            "📝 **تنظیمات هدر و فوتر (پیشوند و پسوند پیام)**\n",
            f"قانون: **{src_label} ➔ {dst_label}**\n",
            "────────────────────",
            "📌 **هدر فعلی (ابتدای پیام):**",
            f"`{hdr}`\n" if hdr else "❌ تنظیم نشده است.\n",
            "📌 **فوتر فعلی (انتهای پیام):**",
            f"`{ftr}`\n" if ftr else "❌ تنظیم نشده است.\n",
            "💡 هدر و فوتر به صورت خودکار به متن تمام پیام‌های ارسالی این قانون اضافه خواهند شد.",
        ]

        rows: List[List[Tuple[str, str]]] = [
            [
                ("✏️ تنظیم هدر", f"rh_s:{rule.id}"),
                ("🗑 حذف هدر", f"rh_d:{rule.id}"),
            ],
            [
                ("✏️ تنظیم فوتر", f"rf_s:{rule.id}"),
                ("🗑 حذف فوتر", f"rf_d:{rule.id}"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return "\n".join(lines), self._kbd(rows)

    def _render_cleaner_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render cleaner settings menu for this task."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)

        url_b = "🚫 حذف" if rule.remove_links else "🟢 مجاز"
        usr_b = "🚫 حذف" if rule.clean_usernames else "🟢 مجاز"
        tag_b = "🚫 حذف" if rule.clean_hashtags else "🟢 مجاز"
        emo_b = "🧹 پاکسازی" if rule.remove_emojis else "🟢 حفظ"
        phn_b = "🚫 حذف" if rule.clean_phones else "🟢 مجاز"
        btn_b = "🚫 حذف" if rule.clean_buttons else "🟢 مجاز"
        spc_b = "🧹 پاکسازی" if rule.clean_empty_lines else "🟢 حفظ"
        wrd_cnt = len(rule.clean_custom_words)
        wrd_b = f"{wrd_cnt} کلمه" if wrd_cnt else "تنظیم نشده"

        text = (
            "🧹 **تنظیمات پاک‌کننده پیام (Cleaner Settings)**\n\n"
            f"قانون: **{src_label} ➔ {dst_label}**\n"
            "────────────────────\n"
            "اینجا می‌توانید مواردی که نمی‌خواهید در پیام ظاهر شوند را مشخص کنید:\n\n"
            f"• 🔗 **لینک‌ها (URLs):** {url_b}\n"
            f"• 👤 **منشن‌ها (@Usernames):** {usr_b}\n"
            f"• #️⃣ **هشتگ‌ها (#Hashtags):** {tag_b}\n"
            f"• 😀 **ایموجی‌ها (Emojis):** {emo_b}\n"
            f"• 📞 **شماره‌های تماس (Phone):** {phn_b}\n"
            f"• 🔘 **دکمه‌های شیشه‌ای (Buttons):** {btn_b}\n"
            f"• ✂️ **خطوط خالی اضافه (Empty Lines):** {spc_b}\n"
            f"• 📝 **کلمات سفارشی:** {wrd_b}\n\n"
            "👇 جهت فعال یا غیرفعال‌سازی هر گزینه کلیک کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (f"🔗 لینک: {url_b}", f"rcl_url:{rule.id}"),
                (f"👤 منشن: {usr_b}", f"rcl_usr:{rule.id}"),
            ],
            [
                (f"#️⃣ هشتگ: {tag_b}", f"rcl_tag:{rule.id}"),
                (f"😀 ایموجی: {emo_b}", f"rcl_emo:{rule.id}"),
            ],
            [
                (f"📞 شماره تماس: {phn_b}", f"rcl_phn:{rule.id}"),
                (f"🔘 دکمه شیشه‌ای: {btn_b}", f"rcl_btn:{rule.id}"),
            ],
            [
                (f"✂️ خطوط خالی: {spc_b}", f"rcl_spc:{rule.id}"),
                (f"📝 کلمات سفارشی: {wrd_b}", f"rcl_wrd:{rule.id}"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_filters_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render content filter settings menu for this task."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)

        lnk_b = "🚫 مسدود" if rule.block_links else "🟢 مجاز"
        vid_b = "🚫 مسدود" if rule.block_video else "🟢 مجاز"
        aud_b = "🚫 مسدود" if rule.block_audio else "🟢 مجاز"
        stk_b = "🚫 مسدود" if rule.block_stickers else "🟢 مجاز"
        txt_b = "🚫 مسدود" if rule.block_text else "🟢 مجاز"
        voc_b = "🚫 مسدود" if rule.block_voice else "🟢 مجاز"

        text = (
            "🛡 **فیلترهای محتوا (Content Filters)**\n\n"
            f"قانون: **{src_label} ➔ {dst_label}**\n"
            "────────────────────\n"
            "مشخص کنید کدام نوع محتواها فیلتر (مسدود) یا مجاز باشند:\n\n"
            f"• 🔗 **فیلتر لینک (Link Filter):** {lnk_b}\n"
            f"• 🎬 **ویدیو (Video Filter):** {vid_b}\n"
            f"• 🎵 **فایل صوتی (Audio Filter):** {aud_b}\n"
            f"• 🎭 **استیکر (Sticker Filter):** {stk_b}\n"
            f"• 📝 **پیام متنی (Text Filter):** {txt_b}\n"
            f"• 🎙 **وویس (Voice Filter):** {voc_b}\n\n"
            "👇 جهت تغییر وضعیت هر فیلتر کلیک کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                (f"🔗 لینک: {lnk_b}", f"rfl_lnk:{rule.id}"),
                (f"🎬 ویدیو: {vid_b}", f"rfl_vid:{rule.id}"),
            ],
            [
                (f"🎵 فایل صوتی: {aud_b}", f"rfl_aud:{rule.id}"),
                (f"🎭 استیکر: {stk_b}", f"rfl_stk:{rule.id}"),
            ],
            [
                (f"📝 متن: {txt_b}", f"rfl_txt:{rule.id}"),
                (f"🎙 وویس: {voc_b}", f"rfl_voc:{rule.id}"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_max_time_edit_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render max edit time configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        current = rule.max_time_edit
        curr_str = f"{current} ثانیه" if current > 0 else "نامحدود (0)"

        text = (
            "⏱ **محدودیت زمانی ویرایش (Max Edit Time)**\n\n"
            f"قانون: **{src_label} ➔ {dst_label}**\n"
            f"سقف مجاز فعلی: **{curr_str}**\n"
            "────────────────────\n"
            "اگر در کانال مبدأ پیامی ویرایش شود، تنها در صورتی در مقصد نیز ویرایش اعمال خواهد شد "
            "که سن پیام از زمان انتشار کمتر از این مقدار باشد.\n\n"
            "👇 یکی از مقادیر پیش‌فرض را انتخاب کنید یا عدد دلخواه وارد کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                ("10 دقیقه (600s)", f"rmte_s:{rule.id}:600"),
                ("1 ساعت (3600s)", f"rmte_s:{rule.id}:3600"),
            ],
            [
                ("24 ساعت (86400s)", f"rmte_s:{rule.id}:86400"),
                ("48 ساعت (172800s)", f"rmte_s:{rule.id}:172800"),
            ],
            [
                ("♾ بدون محدودیت (0)", f"rmte_s:{rule.id}:0"),
                ("✏️ ورود ثانیه دلخواه", f"rmte_c:{rule.id}"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_delay_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render forward delay configuration menu with quick presets."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        current = rule.delay_seconds
        curr_str = f"{current:.0f} ثانیه" if current > 0 else "بدون تأخیر (فوری 0s)"

        text = (
            "⏱ **تنظیم تأخیر در ارسال (Set Delay Forward)**\n\n"
            f"قانون: **{src_label} ➔ {dst_label}**\n"
            f"تأخیر فعلی: **{curr_str}**\n"
            "────────────────────\n"
            "با فعال‌سازی تأخیر، پیام‌ها پیش از فوروارد در صف به این میزان صبر می‌کنند تا ارسال طبیعی‌تر به نظر برسد و از لیمیت تلگرام جلوگیری شود.\n\n"
            "👇 یکی از مقادیر پیش‌فرض را انتخاب کنید یا عدد دلخواه وارد کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                ("⚡ بدون تأخیر (0s)", f"rdly_s:{rule.id}:0"),
                ("⏱ 3 ثانیه", f"rdly_s:{rule.id}:3"),
            ],
            [
                ("⏱ 5 ثانیه", f"rdly_s:{rule.id}:5"),
                ("⏱ 10 ثانیه", f"rdly_s:{rule.id}:10"),
            ],
            [
                ("⏱ 30 ثانیه", f"rdly_s:{rule.id}:30"),
                ("⏱ 60 ثانیه", f"rdly_s:{rule.id}:60"),
            ],
            [
                ("✏️ ورود ثانیه دلخواه", f"rdly_c:{rule.id}"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_blacklist_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render blacklist keyword configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        bl = list(rule.blacklist or [])

        items_str = "\n".join([f"• `{w}`" for w in bl[:20]]) if bl else "❌ هیچ کلمه‌ای در لیست سیاه نیست."
        if len(bl) > 20:
            items_str += f"\n... و {len(bl) - 20} کلمه دیگر"

        text = (
            "🚫 **لیست سیاه کلمات (Blacklist Keywords)**\n\n"
            f"قانون: **{src_label} ➔ {dst_label}**\n"
            f"تعداد کلمات مسدود: **{len(bl)}**\n"
            "────────────────────\n"
            "پیام‌هایی که شامل هریک از این کلمات باشند، فوروارد **نمی‌شوند**:\n\n"
            f"{items_str}\n\n"
            "👇 جهت مدیریت لیست سیاه انتخاب کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                ("➕ افزودن کلمه جدید", f"rfl_bl_add:{rule.id}"),
                ("🗑 پاکسازی کل لیست", f"rfl_bl_clr:{rule.id}"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_whitelist_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render whitelist keyword configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        wl = list(rule.whitelist or [])

        items_str = "\n".join([f"• `{w}`" for w in wl[:20]]) if wl else "❌ هیچ کلمه‌ای در لیست سفید نیست (تمام پیام‌ها مجازند)."
        if len(wl) > 20:
            items_str += f"\n... و {len(wl) - 20} کلمه دیگر"

        text = (
            "✅ **لیست سفید کلمات (Whitelist Keywords)**\n\n"
            f"قانون: **{src_label} ➔ {dst_label}**\n"
            f"تعداد کلمات مجاز: **{len(wl)}**\n"
            "────────────────────\n"
            "تنها پیام‌هایی فوروارد می‌شوند که حداقل شامل یکی از این کلمات باشند:\n\n"
            f"{items_str}\n\n"
            "👇 جهت مدیریت لیست سفید انتخاب کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                ("➕ افزودن کلمه جدید", f"rfl_wl_add:{rule.id}"),
                ("🗑 پاکسازی کل لیست", f"rfl_wl_clr:{rule.id}"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_watermark_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render watermark configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        wm = rule.watermark

        text = (
            "🖼 **تنظیم واتر‌مارک (Watermark Text)**\n\n"
            f"قانون: **{src_label} ➔ {dst_label}**\n"
            f"متن فعلی: **{wm or 'تنظیم نشده (خالی)'}**\n"
            "────────────────────\n"
            "این متن به انتهای کپشن تصاویر و ویدیوها یا به صورت واتر‌مارک الصاق خواهد شد.\n\n"
            "👇 جهت تغییر یا حذف انتخاب کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                ("✏️ تنظیم متن واتر‌مارک", f"rwm_set:{rule.id}"),
                ("🗑 حذف واتر‌مارک", f"rwm_del:{rule.id}"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    def _render_translate_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render target translation language picker."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        current = (rule.translate_lang or "").lower()
        curr_label = current.upper() if current else "غیرفعال (Off)"

        text = (
            "🌐 **ترجمه خودکار پیام‌ها (Auto Translate)**\n\n"
            f"قانون: **{src_label} ➔ {dst_label}**\n"
            f"زبان مقصد فعلی: **{curr_label}**\n"
            "────────────────────\n"
            "پیام‌ها پیش از فوروارد به صورت خودکار به زبان انتخابی ترجمه خواهند شد.\n\n"
            "👇 زبان مقصد موردنظر را انتخاب کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                ("❌ غیرفعال‌سازی (Off)", f"rtr_set:{rule.id}:none"),
            ],
            [
                ("🇬🇧 English (EN)", f"rtr_set:{rule.id}:en"),
                ("🇮🇷 فارسی (FA)", f"rtr_set:{rule.id}:fa"),
            ],
            [
                ("🇷🇺 Русский (RU)", f"rtr_set:{rule.id}:ru"),
                ("🇪🇸 Español (ES)", f"rtr_set:{rule.id}:es"),
            ],
            [
                ("🇵🇹 Português (PT)", f"rtr_set:{rule.id}:pt"),
                ("🇺🇿 O'zbek (UZ)", f"rtr_set:{rule.id}:uz"),
            ],
            [
                ("🇮🇩 Bahasa Indonesia (ID)", f"rtr_set:{rule.id}:id"),
                ("🇫🇷 Français (FR)", f"rtr_set:{rule.id}:fr"),
            ],
            [
                ("🇮🇹 Italiano (IT)", f"rtr_set:{rule.id}:it"),
            ],
            [(f"⬅️ {self._t('ui_rule_back_rule')}", f"rd:{rule.id}")],
        ]
        return text, self._kbd(rows)

    async def _render_profile_menu(self, uid: int, user: Any = None) -> Tuple[str, InlineKeyboardMarkup]:
        """Render user profile dashboard with reset options."""
        name = getattr(user, "first_name", "") or getattr(user, "username", "") or str(uid)
        username = f"@{user.username}" if getattr(user, "username", None) else "—"
        sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
        rules = await self._rules.list_by_owner(uid) if (self._rules and uid) else []
        active_rules = [r for r in rules if r.is_active]

        text = (
            "👤 **پروفایل کاربری و وضعیت حساب**\n\n"
            f"🆔 **شناسه عددی (User ID):** `{uid}`\n"
            f"👤 **نام کاربری:** {username} ({name})\n"
            f"📱 **سشن‌های متصل:** `{len(sessions)}` سشن فعال\n"
            f"⚙️ **تسک‌های فوروارد:** `{len(rules)}` کل (`{len(active_rules)}` فعال)\n"
            "────────────────────\n"
            "👇 گزینه‌های مدیریت و بازنشانی حساب:"
        )

        kbd = self._kbd([
            [("🔄 بازنشانی ستاپ (Restart Setup)", "pr_restart")],
            [("⚠️ ریست تنظیمات تسک‌ها (Reset Config)", "pr_reset_cfg")],
            [(f"⬅️ {self._t('ui_back_main_menu')}", CB["main"])],
        ])
        return text, kbd

    def _render_smart_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(rule)
        """Render dedicated smart routing, VIP channel, and deduplication menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)

        use_mid = bool(getattr(rule, "use_intermediate", False))
        mid_id = getattr(rule, "intermediate_channel_id", "") or ""
        mid_name = getattr(rule, "intermediate_channel_name", "") or ""
        mid_display = f"`{mid_name or mid_id}`" if mid_id else self._t("ui_smart_not_set", lang=lang)

        cat = getattr(rule, "message_category", "ALL")
        crit = getattr(rule, "detection_criteria", {}) or {}
        match_mode = (crit.get("match_mode") or "ANY").upper()
        keywords = crit.get("text_contains") or crit.get("keywords") or []
        kw_display = ", ".join(f"`{k}`" for k in keywords) if keywords else self._t("ui_smart_not_set", lang=lang)
        regex_p = crit.get("regex_pattern") or crit.get("text_regex") or ""
        regex_display = f"`{regex_p}`" if regex_p else self._t("ui_smart_not_set", lang=lang)

        f_mode = getattr(rule, "forward_mode", ForwardMode.COPY_MESSAGE)
        mode_val = getattr(f_mode, "value", str(f_mode))

        _ns = self._t("ui_smart_not_set", lang=lang)
        # Determine active routing path
        if use_mid and mid_id and cat in ("VIP", "VIP_ONLY"):
            path_desc = self._t("ui_smart_path2_desc", lang=lang)
            path_tag = self._t("ui_smart_path2_tag", lang=lang)
        elif mode_val == "DIRECT_FORWARD":
            path_desc = self._t("ui_smart_path3_desc", lang=lang)
            path_tag = self._t("ui_smart_path3_tag", lang=lang)
        else:
            path_desc = self._t("ui_smart_path1_desc", lang=lang)
            path_tag = self._t("ui_smart_path1_tag", lang=lang)

        chdr = getattr(rule, "custom_header", "") or ""
        hdr_display = f"`{chdr[:25]}…`" if len(chdr) > 25 else (f"`{chdr}`" if chdr else _ns)
        prio = int(getattr(rule, "priority", 10) or 10)
        mode_desc = (self._t("ui_smart_match_all", lang=lang) if match_mode == "ALL"
                     else self._t("ui_smart_match_any", lang=lang))

        lines = [
            self._t("ui_smart_title", lang=lang),
            self._t("ui_smart_rule_line", lang=lang, src=src_label, dst=dst_label),
            "────────────────────",
            self._t("ui_smart_active_path", lang=lang, path_desc=path_desc),
            self._t("ui_smart_mid_channel", lang=lang, mid=mid_display),
            self._t("ui_smart_match_logic", lang=lang, match_mode=match_mode, mode_desc=mode_desc),
            self._t("ui_smart_keywords", lang=lang, keywords=kw_display),
            self._t("ui_smart_regex", lang=lang, regex=regex_display),
            self._t("ui_smart_custom_header", lang=lang, header=hdr_display),
            self._t("ui_smart_priority", lang=lang, prio=prio),
            self._t("ui_smart_lock_policy", lang=lang),
            self._t("ui_smart_antiloop", lang=lang),
            self._t("ui_smart_hint", lang=lang),
        ]

        # Buttons
        rows: List[List[Tuple[str, str]]] = [
            [(self._t("ui_smart_btn_change_path", lang=lang, path_tag=path_tag[:20]), f"rsmpth:{rule.id}")],
            [
                (self._t("ui_smart_btn_logic", lang=lang, match_mode=match_mode), f"rsmmod:{rule.id}"),
                (self._t("ui_smart_btn_mid", lang=lang), f"rsmw:{rule.id}"),
            ],
            [
                (self._t("ui_smart_btn_keywords", lang=lang), f"rsmtx:{rule.id}"),
                (self._t("ui_smart_btn_regex", lang=lang), f"rsmrx:{rule.id}"),
            ],
            [
                (self._t("ui_smart_btn_header", lang=lang), f"rsmh:{rule.id}"),
                (self._t("ui_smart_btn_del_header", lang=lang), f"rsmhd:{rule.id}"),
            ],
            [
                (self._t("ui_smart_btn_prio_up", lang=lang), f"rsmpu:{rule.id}"),
                (self._t("ui_smart_btn_prio_down", lang=lang), f"rsmpd:{rule.id}"),
            ],
            [(self._t("ui_smart_btn_dryrun", lang=lang), f"rsmtst:{rule.id}")],
            [(f"⬅️ {self._t('ui_rule_back_rule', lang=lang)}", f"rd:{rule.id}")],
        ]
        return "\n".join(lines), self._kbd(rows)

    def _render_chat_picker(self, st: UiState, role: str, page: int = 0, uid: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
        lang = self._lang_for(st)
        """Render a rich, paginated dialog selection screen with inline buttons and fail-closed i18n."""
        rc = "s" if role == "source" else "t"
        page_size = 5

        effective_uid = uid or getattr(st, "user_id", None)
        cur_lang = self._resolve_lang(effective_uid) if hasattr(self, "_resolve_lang") else "en"

        def _clean_md(text: Any) -> str:
            if not text:
                return ""
            return str(text).replace("*", "").replace("_", " ").replace("`", "'").replace("[", "(").replace("]", ")")

        is_edit = str(st.step).startswith("rule_edit_")
        if is_edit and isinstance(st.buffer, dict) and st.buffer.get("rule_id"):
            rid = st.buffer.get("rule_id")
            cancel_cb = f"re:{rid}" if st.buffer.get("in_draft") else f"rd:{rid}"
        elif st.step in ("rule_source", "rule_target"):
            cancel_cb = "r_cancel"
        else:
            cancel_cb = CB["cancel"]

        chats = st.buffer.get("chats", []) if isinstance(st.buffer, dict) else []

        if chats:
            total_items = len(chats)
            total_pages = max(1, (total_items + page_size - 1) // page_size)
            page = max(0, min(page, total_pages - 1))
            start = page * page_size
            end = min(start + page_size, total_items)
            page_items = chats[start:end]

            if role == "source":
                header = (
                    f"{self._t('ui_src_sel', lang=cur_lang)}\n"
                    f"*{self._t('ui_src_desc', lang=cur_lang)}*\n"
                    "────────────────────"
                )
            else:
                src_name = st.buffer.get("source_name") or st.rule_source
                header = (
                    f"✅ **{self._t('ui_source', lang=cur_lang)}:** {src_name} (`{st.rule_source}`)\n\n"
                    f"{self._t('ui_tgt_sel', lang=cur_lang)}\n"
                    f"*{self._t('ui_tgt_desc', lang=cur_lang)}*\n"
                    "────────────────────"
                )

            lines = [header]
            for idx, item in enumerate(page_items, start=start + 1):
                clean_title = _clean_md(item["title"])
                uname = f" • @{item['username']}" if item.get("username") else ""
                lines.append(f"{item['emoji']} **{idx}. {clean_title}**\n   🆔 `{item['id']}`{uname}")

            lines.append("────────────────────")
            lines.append(self._t("ui_picker_page_info", lang=cur_lang, page=page + 1, total=total_pages, count=total_items))
            lines.append(self._t("ui_picker_prompt", lang=cur_lang))
            text = "\n".join(lines)

            rows: List[List[Tuple[str, str]]] = []
            for local_idx, item in enumerate(page_items):
                global_idx = start + local_idx
                title_disp = item["title"][:22] + "…" if len(item["title"]) > 22 else item["title"]
                btn_label = f"{item['emoji']} {start + local_idx + 1}. {title_disp}"
                rows.append([(btn_label, f"ch:{rc}:{global_idx}")])

            nav_row: List[Tuple[str, str]] = []
            if page > 0:
                nav_row.append((f"◀️ {self._t('ui_prev', lang=cur_lang)}", f"pg:{rc}:{page - 1}"))
            nav_row.append((f"📄 {page + 1} / {total_pages}", "noop"))
            if page < total_pages - 1:
                nav_row.append((f"{self._t('ui_next', lang=cur_lang)} ▶️", f"pg:{rc}:{page + 1}"))
            rows.append(nav_row)

            bottom_row: List[Tuple[str, str]] = [
                (self._t("ui_refresh_list", lang=cur_lang), f"rfc:{rc}"),
            ]
            if st.step == "rule_target":
                bottom_row.append((self._t("ui_change_src", lang=cur_lang), "r_back_source"))
            bottom_row.append((self._t("ui_cancel_back", lang=cur_lang), cancel_cb))
            rows.append(bottom_row)
            return text, self._kbd(rows)

        # Fallback when no chats could be fetched automatically
        if role == "source":
            text = (
                f"{self._t('ui_src_sel', lang=cur_lang)}\n\n"
                f"{self._t('ui_no_dialog', lang=cur_lang)}\n\n"
                f"{self._t('ui_no_dialog_bytes', lang=cur_lang)}"
            )
        else:
            src_name = st.buffer.get("source_name") or st.rule_source
            text = (
                f"✅ **{self._t('ui_source', lang=cur_lang)}:** {src_name} (`{st.rule_source}`)\n\n"
                f"{self._t('ui_tgt_sel', lang=cur_lang)}\n\n"
                f"{self._t('ui_target_no_dialog', lang=cur_lang)}\n\n"
                f"{self._t('ui_no_dialog_bytes', lang=cur_lang)}"
            )

        fallback_rows: List[List[Tuple[str, str]]] = [
            [(self._t("ui_refresh_list", lang=cur_lang), f"rfc:{rc}")],
        ]
        if st.step == "rule_target":
            fallback_rows.append([(self._t("ui_change_src", lang=cur_lang), "r_back_source")])
        fallback_rows.append([(self._t("ui_cancel_back", lang=cur_lang), cancel_cb)])
        return text, self._kbd(fallback_rows)

    async def _fetch_dialogs(self, owner_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch available channels/groups from connected session clients with structured pipeline logging."""
        _dlg_uid = int(owner_user_id or 0)
        logger.info("[PIPELINE:DIALOG_FETCH:START] Initiating dialog fetch for owner_user_id=%s", _dlg_uid)

        sessions: List[Any] = []
        if _dlg_uid:
            try:
                sessions = await self._sessions.list_by_owner(_dlg_uid)
                logger.info("[PIPELINE:DIALOG_FETCH:BY_OWNER] Found %d session(s) for owner_user_id=%s", len(sessions), _dlg_uid)
            except Exception as exc:
                logger.warning("[PIPELINE:DIALOG_FETCH:WARN] list_by_owner(%s) failed: %s", _dlg_uid, exc)

        if not sessions and (_dlg_uid == 0 or self._is_admin(_dlg_uid)):
            try:
                sessions = await self._sessions.list_all()
                logger.info("[PIPELINE:DIALOG_FETCH:ADMIN_FALLBACK] Loaded %d session(s) from global list", len(sessions))
            except Exception as exc:
                logger.warning("[PIPELINE:DIALOG_FETCH:WARN] list_all() failed: %s", exc)

        active_sessions = [s for s in sessions if getattr(s, "is_active", True) and getattr(s, "is_authorized", False)]
        if not active_sessions and sessions:
            active_sessions = [s for s in sessions if getattr(s, "is_active", True)] or sessions

        logger.info("[PIPELINE:DIALOG_FETCH:SESSIONS] Candidate active sessions count=%d (total=%d)", len(active_sessions), len(sessions))
        if not active_sessions:
            logger.warning("[PIPELINE:DIALOG_FETCH:EMPTY] No active sessions found for owner_user_id=%s", _dlg_uid)
            return []

        chats: List[Dict[str, Any]] = []
        seen_ids = set()
        cur_lang = self._resolve_lang(_dlg_uid)

        for session in active_sessions:
            client = self._pool.get(session.id)
            if not client or not getattr(client, "is_connected", False):
                sess_str = getattr(session, "session_string", "") or getattr(session, "session_string_encrypted", "")
                if sess_str:
                    try:
                        logger.info("[PIPELINE:DIALOG_FETCH:RECONNECT] Reconnecting session %s...", session.id[:8])
                        await self._pool.start_with_session_string(
                            session.id, sess_str, credential_id=getattr(session, "credential_id", "default")
                        )
                        client = self._pool.get(session.id)
                    except Exception as exc:
                        logger.warning("[PIPELINE:DIALOG_FETCH:RECONNECT_FAIL] Session %s reconnect failed: %s", session.id[:8], exc)
                        continue

            if not client or not getattr(client, "is_connected", False):
                logger.warning("[PIPELINE:DIALOG_FETCH:SESSION_DISCONNECTED] Session %s is not connected", session.id[:8])
                continue

            try:
                logger.info("[PIPELINE:DIALOG_FETCH:SCAN] Scanning dialogs on session %s...", session.id[:8])
                session_chats_count = 0
                async with asyncio.timeout(12.0):
                    async for dialog in client.get_dialogs(limit=100):
                        chat = getattr(dialog, "chat", None)
                        if not chat or chat.id in seen_ids:
                            continue
                        seen_ids.add(chat.id)

                        ctype = getattr(chat.type, "value", str(chat.type)).lower()
                        my_id = getattr(client.me, "id", None) if hasattr(client, "me") and client.me else None

                        if "channel" in ctype:
                            emoji = "📢"
                            kind = "channel"
                        elif "supergroup" in ctype or "group" in ctype:
                            emoji = "👥"
                            kind = "group"
                        elif "bot" in ctype:
                            emoji = "🤖"
                            kind = "bot"
                        elif my_id and chat.id == my_id:
                            emoji = "💾"
                            kind = "saved"
                        else:
                            emoji = "👤"
                            kind = "user"

                        title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat.id)
                        if kind == "saved":
                            title = self._t("ui_saved_messages", lang=cur_lang)

                        chats.append({
                            "id": str(chat.id),
                            "title": str(title),
                            "emoji": emoji,
                            "kind": kind,
                            "username": getattr(chat, "username", "") or "",
                        })
                        session_chats_count += 1
                logger.info("[PIPELINE:DIALOG_FETCH:SUCCESS] Session %s fetched %d chats", session.id[:8], session_chats_count)
            except Exception as exc:
                logger.warning("[PIPELINE:DIALOG_FETCH:ERROR] Exception fetching dialogs for session %s: %s", session.id[:8], exc)

        priority = {"channel": 0, "group": 1, "saved": 2, "bot": 3, "user": 4}
        chats.sort(key=lambda c: priority.get(c["kind"], 99))
        logger.info("[PIPELINE:DIALOG_FETCH:COMPLETE] Returned %d total dialogs for owner_user_id=%s", len(chats), _dlg_uid)
        return chats

    async def _finish_rule_creation(self, event: Any, st: UiState, source_title: str, target_title: str) -> None:
        lang = self._lang_for(st)
        """Persist ForwardRule and present the completion card."""
        uid: int = int(event.from_user.id if getattr(event, "from_user", None) else (event.chat.id if getattr(event, "chat", None) else 0))
        cur_lang = self._resolve_lang(uid)

        def _norm_ep(s: str) -> str:
            s = (s or "").strip().lower()
            for p in ("https://t.me/", "http://t.me/", "t.me/", "@"):
                if s.startswith(p):
                    s = s[len(p):]
            return s.strip()

        if _norm_ep(st.rule_source) and _norm_ep(st.rule_source) == _norm_ep(st.rule_target):
            st.step = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            err_text = self._t("ui_loop_detected_title", lang=cur_lang)
            markup = self._kbd([
                [(self._t("ui_retry", lang=cur_lang), CB["rule_add"])],
                [(f"🏠 {self._t('ui_main_title', lang=cur_lang)}", CB["main"])],
            ])
            if hasattr(event, "edit_message_text"):
                await self._safe_edit(event, err_text, reply_markup=markup)
            else:
                await event.reply_text(err_text, reply_markup=markup)
            return

        _fuid = self._scope_uid(uid)
        if _fuid is None and hasattr(self._sessions, "list_all"):
            _res = self._sessions.list_all()
            sessions = await _res if asyncio.iscoroutine(_res) else _res
        elif hasattr(self._sessions, "list_by_owner"):
            _res = self._sessions.list_by_owner(uid)
            sessions = await _res if asyncio.iscoroutine(_res) else _res
        elif hasattr(self._sessions, "list_all"):
            _res = self._sessions.list_all()
            sessions = await _res if asyncio.iscoroutine(_res) else _res
        else:
            sessions = []
        if not sessions:
            st.step = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            msg_text = self._t("ui_no_sessions", lang=cur_lang)
            if hasattr(event, "edit_message_text"):
                await self._safe_edit(event, msg_text, reply_markup=self._sessions_menu())
            else:
                await event.reply_text(msg_text, reply_markup=self._sessions_menu())
            return

        session = sessions[0]
        try:
            from core.domain.entities import ForwardMode, ForwardRule, RoutingType

            rule = ForwardRule(
                session_id=session.id,
                source_chat_id=st.rule_source,
                source_chat_name=source_title,
                target_chat_id=st.rule_target,
                target_chat_name=target_title,
                routing_type=RoutingType.CHANNEL_TO_CHANNEL,
                forward_mode=ForwardMode.COPY_MESSAGE,
                owner_user_id=uid,
            )
            await self._rules.create(rule)
            try:
                self._log.info("bot", "rule", f"rule created {st.rule_source} -> {st.rule_target}")
            except Exception:
                pass

            st.step = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)

            success_text = (
                f"{self._t('ui_rule_created_header', lang=cur_lang)}\n\n"
                f"📥 **{self._t('ui_source', lang=cur_lang)}:** {source_title}\n"
                f"   🆔 `{st.rule_source}`\n\n"
                f"📤 **{self._t('ui_target', lang=cur_lang)}:** {target_title}\n"
                f"   🆔 `{st.rule_target}`\n\n"
                f"{self._t('ui_rule_fwd_mode_desc', lang=cur_lang)}\n"
                f"{self._t('ui_rule_status_monitoring', lang=cur_lang)}"
            )
            markup = self._kbd([
                [(self._t("ui_rule_adv_settings", lang=cur_lang), f"rd:{rule.id}")],
                [(self._t("ui_rule_add_new", lang=cur_lang), CB["rule_add"]), (self._t("ui_rule_view_all", lang=cur_lang), CB["rules"])],
                [(f"🏠 {self._t('ui_main_title', lang=cur_lang)}", CB["main"])],
            ])

            if hasattr(event, "edit_message_text"):
                await self._safe_edit(event, success_text, reply_markup=markup)
            else:
                await event.reply_text(success_text, reply_markup=markup)

        except Exception as exc:
            await self._record_error("bot", exc, user_id=uid)
            err_text = self._t("ui_rule_fail", lang=lang, error=tg_detail(exc))
            if hasattr(event, "edit_message_text"):
                await self._safe_edit(event, err_text, reply_markup=self._rules_menu())
            else:
                await event.reply_text(err_text, reply_markup=self._rules_menu())

    def _filters_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("➕ " + self._t("ui_filter_add"), CB["filter_add"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
        ])

    def _ai_menu(self, cfgs: Optional[list] = None) -> InlineKeyboardMarkup:
        rows: list = []
        if cfgs:
            for c in cfgs:
                status = "🟢" if c.is_enabled else "🔴"
                prov_val = c.provider.value if hasattr(c.provider, "value") else str(c.provider)
                prov = self._provider_label(prov_val)
                label = f"{status} {c.name or 'AI'} [{prov} • {c.model}]"
                rows.append([(label, f"aid:{c.id}")])
        rows.append([("💬 پاسخگوی هوشمند پی‌وی (AI PV Assistant)", "pv:menu")])
        rows.append([("➕ " + self._t("ui_ai_add"), CB["ai_add"])])
        rows.append([("⬅️ " + self._t("ui_back"), CB["main"])])
        return self._kbd(rows)

    def _render_ai_detail(self, cfg: AIConfig, health_info: Optional[dict] = None) -> Tuple[str, InlineKeyboardMarkup]:
        status_emoji = "🟢 فعال" if cfg.is_enabled else "🔴 غیرفعال"
        prov_key = cfg.provider.value if hasattr(cfg.provider, "value") else str(cfg.provider)
        prov_name = self._provider_label(prov_key)

        key = cfg.api_key or ""
        if len(key) > 12:
            masked_key = f"{key[:7]}...{key[-4:]}"
        elif key:
            masked_key = "********"
        else:
            masked_key = "(تنظیم نشده)"

        from core.infrastructure.ai.providers import OpenAICompatibleProvider
        base_url = cfg.base_url or OpenAICompatibleProvider.default_base_urls.get(prov_key, "") or "(پیش‌فرض)"

        lines = [
            "🤖 **جزئیات و وضعیت پیکربندی هوش مصنوعی**",
            "────────────────────",
            f"🏷 **نام:** {cfg.name or '—'}",
            f"🆔 **شناسه:** `{cfg.id}`",
            f"🔌 **سرویس‌دهنده:** `{prov_name}` (`{prov_key}`)",
            f"🧠 **مدل فعال:** `{cfg.model}`",
            f"🌐 **هاست / Base URL:** `{base_url}`",
            f"🔑 **کلید احراز هویت (API Key):** `{masked_key}`",
            f"🌡 **دما (Temperature):** `{cfg.temperature}`",
            f"🔘 **وضعیت سیستم:** {status_emoji}",
        ]

        if health_info:
            lines.append("────────────────────")
            if health_info.get("ok"):
                lines.append("🩺 **تست سلامت (Health Check):** 🟢 **سالم و متصل**")
                lines.append(f"⚡ **زمان پاسخ (Latency):** `{health_info.get('latency_ms', 0):.0f}ms`")
                if health_info.get("reply"):
                    reply_preview = str(health_info["reply"])[:120].replace("\n", " ")
                    lines.append(f"💬 **پاسخ نمونه:** `{reply_preview}`")
            else:
                lines.append("🩺 **تست سلامت (Health Check):** 🔴 **خطا در برقراری ارتباط**")
                err_msg = str(health_info.get("error", "unknown"))[:200]
                lines.append(f"⚠️ **پیام خطا:** `{err_msg}`")

        lines.append("────────────────────")
        lines.append("👇 یکی از عملیات زیر را انتخاب کنید:")
        text = "\n".join(lines)

        tog_text = "🔴 غیرفعال‌سازی" if cfg.is_enabled else "🟢 فعال‌سازی"
        rows = [
            [
                ("🩺 تست سلامت (Ping)", f"ai_hc:{cfg.id}"),
                ("🧪 تست بازنویسی متن", f"ai_sample:{cfg.id}"),
            ],
            [
                ("✏️ تغییر هاست (Base URL)", f"ai_eh:{cfg.id}"),
                ("🧠 تغییر مدل", f"ai_em:{cfg.id}"),
            ],
            [
                ("🔑 تغییر کلید (API Key)", f"ai_ek:{cfg.id}"),
                (tog_text, f"ai_tog:{cfg.id}"),
            ],
            [
                ("🗑 حذف پیکربندی", f"ai_del_ask:{cfg.id}"),
                ("⬅️ بازگشت به لیست AI", CB["ai"]),
            ],
        ]
        return text, self._kbd(rows)

    def _render_pv_menu(self, uid: int = 0, lang: str = "fa") -> Tuple[str, InlineKeyboardMarkup]:
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            return "❌ ماژول پاسخگوی هوشمند پی‌وی بارگذاری نشده است.", self._kbd([[("⬅️ بازگشت", CB["ai"])]])

        cfg = resp.get_config_for_user(uid) if hasattr(resp, "get_config_for_user") else resp.config
        persona_preview = (cfg.persona_prompt[:140] + "...") if len(cfg.persona_prompt) > 140 else cfg.persona_prompt

        if lang == "fa":
            status_str = "🟢 فعال" if cfg.enabled else "🔴 غیرفعال"
            tog_btn = "🔴 غیرفعال‌سازی" if cfg.enabled else "🟢 فعال‌سازی"
            text = (
                "🤖 **پاسخگوی فوق‌العاده هوشمند پی‌وی (AI PV Assistant)**\n\n"
                f"📊 **وضعیت فعالیت:** {status_str}\n"
                f"⏱ **شبیه‌سازی تایپینگ انسانی:** `{cfg.typing_delay_min:.1f}` تا `{cfg.typing_delay_max:.1f}` ثانیه\n"
                f"⏳ **کول‌داون ضداسپم:** هر `{cfg.cooldown_seconds}` ثانیه\n"
                f"🤖 **نادیده‌گرفتن ربات‌ها:** {'✅ بله' if cfg.ignore_bots else '❌ خیر'}\n\n"
                f"🎭 **لحن و شخصیت فعلی:**\n"
                f"_{persona_preview}_\n\n"
                "💡 **ویژگی‌ها:** با اتصال این بخش به هوش مصنوعی، هر پیامی به پی‌وی اکانت شما بیاید با "
                "شبیه‌سازی اکشنِ Typing و لحنی کاملاً انسانی، جذاب و فارسی پاسخ داده می‌شود."
            )
            rows = [
                [(f"🔘 وضعیت: {tog_btn}", "pv:toggle")],
                [("🎭 تنظیم لحن و پرامپت شخصیت", "pv:persona")],
                [("⏱ تنظیم زمان تأخیر و تایپینگ", "pv:delays")],
                [("🧪 تست نمونه پاسخگویی", "pv:test")],
                [("⬅️ " + self._t("ui_back"), CB["ai"])],
            ]
        elif lang == "ru":
            status_str = "🟢 Активно" if cfg.enabled else "🔴 Неактивно"
            tog_btn = "🔴 Отключить" if cfg.enabled else "🟢 Включить"
            text = (
                "🤖 **Умный автоответчик в ЛС (AI PV Assistant)**\n\n"
                f"📊 **Статус:** {status_str}\n"
                f"⏱ **Имитация набора текста:** от `{cfg.typing_delay_min:.1f}` до `{cfg.typing_delay_max:.1f}` сек\n"
                f"⏳ **Антиспам кулдаун:** каждые `{cfg.cooldown_seconds}` сек\n"
                f"🤖 **Игнорировать ботов:** {'✅ Да' if cfg.ignore_bots else '❌ Нет'}\n\n"
                f"🎭 **Текущий стиль общения:**\n"
                f"_{persona_preview}_\n"
            )
            rows = [
                [(f"🔘 Статус: {tog_btn}", "pv:toggle")],
                [("🎭 Настройка стиля и промпта", "pv:persona")],
                [("⏱ Настройка задержек набора", "pv:delays")],
                [("🧪 Тестировать ответ", "pv:test")],
                [("⬅️ Назад", CB["ai"])],
            ]
        elif lang == "zh":
            status_str = "🟢 已启用" if cfg.enabled else "🔴 未启用"
            tog_btn = "🔴 关闭" if cfg.enabled else "🟢 开启"
            text = (
                "🤖 **私聊智能自动回复 (AI PV Assistant)**\n\n"
                f"📊 **运行状态：** {status_str}\n"
                f"⏱ **拟人输入模拟：** `{cfg.typing_delay_min:.1f}` 至 `{cfg.typing_delay_max:.1f}` 秒\n"
                f"⏳ **防刷冷却：** 每 `{cfg.cooldown_seconds}` 秒\n"
                f"🤖 **忽略机器人：** {'✅ 是' if cfg.ignore_bots else '❌ 否'}\n\n"
                f"🎭 **当前语气与人设：**\n"
                f"_{persona_preview}_\n"
            )
            rows = [
                [(f"🔘 状态: {tog_btn}", "pv:toggle")],
                [("🎭 设置回复语气与提示词", "pv:persona")],
                [("⏱ 设置输入延迟", "pv:delays")],
                [("🧪 测试回复", "pv:test")],
                [("⬅️ 返回", CB["ai"])],
            ]
        else:
            status_str = "🟢 Active" if cfg.enabled else "🔴 Inactive"
            tog_btn = "🔴 Disable" if cfg.enabled else "🟢 Enable"
            text = (
                "🤖 **AI Private Chat Assistant (AI PV Assistant)**\n\n"
                f"📊 **Status:** {status_str}\n"
                f"⏱ **Typing Simulation:** `{cfg.typing_delay_min:.1f}` to `{cfg.typing_delay_max:.1f}` sec\n"
                f"⏳ **Anti-spam Cooldown:** `{cfg.cooldown_seconds}` sec\n"
                f"🤖 **Ignore Bots:** {'✅ Yes' if cfg.ignore_bots else '❌ No'}\n\n"
                f"🎭 **Current Persona:**\n"
                f"_{persona_preview}_\n"
            )
            rows = [
                [(f"🔘 Status: {tog_btn}", "pv:toggle")],
                [("🎭 Configure Persona & Prompt", "pv:persona")],
                [("⏱ Configure Typing Delays", "pv:delays")],
                [("🧪 Test Sample Reply", "pv:test")],
                [("⬅️ " + self._t("ui_back"), CB["ai"])],
            ]
        return text, self._kbd(rows)

    def _render_pv_persona_menu(self, lang: str = "fa") -> Tuple[str, InlineKeyboardMarkup]:
        if lang == "fa":
            text = (
                "🎭 **تنظیم لحن و شخصیت پاسخگوی پی‌وی:**\n\n"
                "یکی از سبک‌های آماده زیر را انتخاب کنید یا پرامپت دلخواه بنویسید:"
            )
            rows = [
                [("😃 لحن خودمانی، جذاب و طبیعی (پیش‌فرض)", "pv:set_p:casual")],
                [("💼 لحن کاری، مؤدبانه و حرفه‌ای", "pv:set_p:business")],
                [("⚡ پاسخ‌های بسیار کوتاه و دوستانه", "pv:set_p:short")],
                [("✏️ نوشتن پرامپت اختصاصی و دلخواه", "pv:set_p:custom")],
                [("⬅️ بازگشت به تنظیمات پی‌وی", "pv:menu")],
            ]
        elif lang == "ru":
            text = (
                "🎭 **Настройка стиля общения:**\n\n"
                "Выберите один из готовых стилей или задайте свой промпт:"
            )
            rows = [
                [("😃 Дружелюбный и естественный (по умолчанию)", "pv:set_p:casual")],
                [("💼 Деловой и вежливый", "pv:set_p:business")],
                [("⚡ Очень краткий и быстрый", "pv:set_p:short")],
                [("✏️ Свой персональный промпт", "pv:set_p:custom")],
                [("⬅️ Назад в меню PV", "pv:menu")],
            ]
        elif lang == "zh":
            text = (
                "🎭 **设置回复语气与人设：**\n\n"
                "选择预设模板或自定义提示词："
            )
            rows = [
                [("😃 亲切自然（默认）", "pv:set_p:casual")],
                [("💼 商务专业礼貌", "pv:set_p:business")],
                [("⚡ 简短快速回复", "pv:set_p:short")],
                [("✏️ 自定义专属提示词", "pv:set_p:custom")],
                [("⬅️ 返回 PV 设置", "pv:menu")],
            ]
        else:
            text = (
                "🎭 **Configure Persona & Tone:**\n\n"
                "Choose a ready style or write your custom persona prompt:"
            )
            rows = [
                [("😃 Friendly & Casual (Default)", "pv:set_p:casual")],
                [("💼 Polite & Professional", "pv:set_p:business")],
                [("⚡ Short & Fast Replies", "pv:set_p:short")],
                [("✏️ Custom Prompt", "pv:set_p:custom")],
                [("⬅️ Back to PV Menu", "pv:menu")],
            ]
        return text, self._kbd(rows)

    def _render_pv_delays_menu(self, lang: str = "fa") -> Tuple[str, InlineKeyboardMarkup]:
        if lang == "fa":
            text = (
                "⏱ **تنظیم زمان تأخیر شبیه‌سازی تایپ انسانی:**\n\n"
                "سرعت تایپ و ارسال پاسخ در پی‌وی را مشخص کنید:"
            )
            rows = [
                [("⚡ سریع (۱.۵ تا ۳ ثانیه)", "pv:set_d:fast")],
                [("🧘 طبیعی و انسانی (۲ تا ۴.۵ ثانیه)", "pv:set_d:normal")],
                [("🐢 با تأمل و طبیعی (۳.۵ تا ۶.۵ ثانیه)", "pv:set_d:slow")],
                [("⬅️ بازگشت به تنظیمات پی‌وی", "pv:menu")],
            ]
        elif lang == "ru":
            text = (
                "⏱ **Настройка задержки набора текста:**\n\n"
                "Укажите скорость имитации набора сообщения:"
            )
            rows = [
                [("⚡ Быстро (1.5 - 3.0 сек)", "pv:set_d:fast")],
                [("🧘 Естественно (2.0 - 4.5 сек)", "pv:set_d:normal")],
                [("🐢 Неспешно (3.5 - 6.5 сек)", "pv:set_d:slow")],
                [("⬅️ Назад в меню PV", "pv:menu")],
            ]
        elif lang == "zh":
            text = (
                "⏱ **设置拟人输入延迟：**\n\n"
                "选择模拟打字与发送的间隔时长："
            )
            rows = [
                [("⚡ 快速 (1.5 - 3.0 秒)", "pv:set_d:fast")],
                [("🧘 自然拟人 (2.0 - 4.5 秒)", "pv:set_d:normal")],
                [("🐢 沉着从容 (3.5 - 6.5 秒)", "pv:set_d:slow")],
                [("⬅️ 返回 PV 设置", "pv:menu")],
            ]
        else:
            text = (
                "⏱ **Configure Typing Delay Simulation:**\n\n"
                "Select typing and response speed:"
            )
            rows = [
                [("⚡ Fast (1.5 - 3.0s)", "pv:set_d:fast")],
                [("🧘 Natural (2.0 - 4.5s)", "pv:set_d:normal")],
                [("🐢 Deliberate (3.5 - 6.5s)", "pv:set_d:slow")],
                [("⬅️ Back to PV Menu", "pv:menu")],
            ]
        return text, self._kbd(rows)

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
        items = [(name, f'{CB["lang_set"]}:{code}') for code, name in LANG_FLAG.items()]
        rows = [items[i:i + 2] for i in range(0, len(items), 2)]
        rows.append([(self._t("ui_back"), CB["main"])])
        return self._kbd(rows)

    # ------------------------------------------------------------------ #
    # API credentials vault
    # ------------------------------------------------------------------ #
    def _creds_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("➕ " + self._t("ui_cred_add"), CB["cred_add"])],
            [("🗄 " + self._t("ui_migrate"), CB["migrate"]), ("🛠 " + self._t("ui_debug"), CB["debug"])],
            [("🔗 " + self._t("ui_weburl"), CB["weburl"])],
            [("⬅️ " + self._t("ui_back"), CB["main"])],
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
        lang = self._lang_for(st)
        models = self._ai.models_for(provider) if hasattr(self._ai, "models_for") else []
        models = list(models or [])[:10]
        if not models:
            return None
        # Index-based callback data keeps payloads far under the 64-byte limit.
        st.buffer["models"] = models
        rows: list = []
        for i, model in enumerate(models):
            rows.append([(model, f'{CB["ai_model_pick"]}:{i}')])
        rows.append([("❌ " + self._t("ui_cancel", lang=lang), CB["cancel"])])
        return self._kbd(rows)

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #
    def _register(self) -> None:
        b = self.bot

        # Trace all callback queries across the UI
        @b.on_callback_query(group=-1)
        async def _trace_all_callbacks(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            data = cq.data or ""
            logger.info("Button clicked by uid=%s: callback_data=%r", uid, data)
            try:
                self._log.info("bot", "cb", f"click from {uid}: {data}")
            except Exception:
                pass
            # Do NOT swallow dispatcher flow: `continue_propagation()` raises
            # ContinuePropagation (a StopAsyncIteration subclass) and the
            # dispatcher relies on that exception propagating to reach handlers
            # in the next group. Wrapping it in `except Exception` silently ate
            # it, so no real handler ever ran and buttons appeared dead.
            # Best-effort local log only; never touch the propagation signal.
            if not data:
                await cq.continue_propagation()

        @b.on_message(filters.command("start") & filters.private)
        async def _start(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /start from uid=%s", uid)
            try:
                # Reset any in-progress FSM state on fresh /start
                st = self._state(uid)
                st.step = ""
                st.rule_source = ""
                st.rule_target = ""
                if isinstance(st.buffer, dict):
                    st.buffer.clear()
                await self._persist(uid, st)
                self._login.cancel(uid)

                # If user has no sessions connected yet, show onboarding screen
                sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
                if not sessions:
                    text, kbd = self._render_welcome_onboarding(uid, user=message.from_user)
                    await message.reply_text(text, reply_markup=kbd)
                else:
                    body = await self._main_text(uid, user=message.from_user)
                    await message.reply_text(body, reply_markup=self._main_menu())
                try:
                    self._log.info("bot", "system", f"/start by {uid}")
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("/start failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("connect") & filters.private)
        async def _connect_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /connect from uid=%s", uid)
            if not uid:
                return
            st = self._state(uid)
            st.step = "login_phone"
            await self._persist(uid, st)
            args = (message.text or "").split(None, 1)
            phone_arg = args[1].strip() if len(args) > 1 else ""
            if phone_arg:
                await self._step_login_phone(message, st, phone_arg)
            else:
                prompt = await self._connect_prompt_text(uid)
                await message.reply_text(prompt, reply_markup=self._cancel_kbd())

        @b.on_message(filters.command("cancel") & filters.private)
        async def _cancel_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /cancel from uid=%s", uid)
            if not uid:
                return
            st = self._state(uid)
            # Preserve active login flow to avoid "no_pending_login" errors
            if not getattr(st, "step", "").startswith("login_"):
                st.step = ""
                st.rule_source = ""
                st.rule_target = ""
                if isinstance(st.buffer, dict):
                    st.buffer.clear()
                await self._persist(uid, st)
                if self._login.has_pending(uid):
                    await self._login.cancel(uid)
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                lang = await self._get_user_lang(uid)
                if lang == "fa":
                    c_text = "❌ عملیات لغو شد."
                    btn_connect = "🚀 تلاش مجدد برای اتصال"
                    btn_menu = "🏚️ منوی اصلی"
                elif lang == "ru":
                    c_text = "❌ Операция отменена."
                    btn_connect = "🚀 Подключить снова"
                    btn_menu = "🏚️ Главное меню"
                elif lang == "zh":
                    c_text = "❌ 操作已取消。"
                    btn_connect = "🚀 重新连接"
                    btn_menu = "🏚️ 主菜单"
                else:
                    c_text = "❌ Operation cancelled."
                    btn_connect = "🚀 Connect Again"
                    btn_menu = "🏚️ Main Menu"
                kbd = self._kbd([
                    [(btn_connect, CB["login"])],
                    [(btn_menu, CB["main"])],
                ])
                await message.reply_text(c_text, reply_markup=kbd)
            else:
                await message.reply_text(self._t("ui_cancelled"), reply_markup=self._main_menu())

        @b.on_message(filters.command(["menu", "panel"]) & filters.private)
        async def _menu(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /menu from uid=%s", uid)
            try:
                sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
                if not sessions:
                    text, kbd = self._render_welcome_onboarding(uid, user=message.from_user)
                    await message.reply_text(text, reply_markup=kbd)
                else:
                    body = await self._main_text(uid, user=message.from_user)
                    await message.reply_text(body, reply_markup=self._main_menu())
            except Exception as exc:
                logger.exception("/menu failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("settings") & filters.private)
        async def _settings_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /settings from uid=%s", uid)
            lines = [
                "⚙️ **تنظیمات و سفارشی‌سازی (Settings)**\n",
                "از این بخش می‌توانید تنظیمات اختصاصی حساب کاربری و کلیدهای شخصی خود را مدیریت کنید.\n",
                f"👤 شناسه عددی شما (User ID): `{uid}`",
            ]
            if self._is_admin(uid):
                lines.append("👑 وضعیت دسترسی: **مدیر سیستم (Administrator)**")
            else:
                lines.append("👤 وضعیت دسترسی: **کاربر عادی (Client)**")
            kbd = self._client_settings_menu(uid)
            await message.reply_text("\n".join(lines), reply_markup=kbd)

        @b.on_message(filters.command("admin") & filters.private)
        async def _admin_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /admin from uid=%s", uid)
            if not self._is_admin(uid):
                return await message.reply_text("⛔ دسترسی غیرمجاز. این بخش مخصوص مدیران سامانه است.")
            text, kbd = await self._render_admin_panel(uid)
            await message.reply_text(text, reply_markup=kbd)

        @b.on_message(filters.command("profile") & filters.private)
        async def _profile_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /profile from uid=%s", uid)
            try:
                body, kbd = await self._render_profile_menu(uid, message.from_user)
                await message.reply_text(body, reply_markup=kbd)
            except Exception as exc:
                logger.exception("/profile failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("forward") & filters.private)
        async def _forward_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /forward from uid=%s", uid)
            try:
                parts = (message.text or "").strip().split()
                if len(parts) > 1 and parts[1].isdigit():
                    secs = int(parts[1])
                    rules = await self._rules.list_by_owner(uid) if uid else []
                    for r in rules:
                        r.max_time_edit = secs
                        await self._save_owned_rule(r, uid)
                    await message.reply_text(
                        f"✅ سقف مجاز زمان ویرایش پیام‌ها (`max_time_edit`) برای تمام تسک‌های شما به **{secs} ثانیه** تنظیم شد."
                    )
                    return

                rules = await self._rules.list_by_owner(uid) if uid else []
                if not rules:
                    await message.reply_text("❌ شما هیچ تسک فعالی برای تنظیم محدودیت زمانی ادیت ندارید.")
                    return

                text = (
                    "⏱ **تنظیم محدودیت زمان ویرایش پیام (Max Time Edit)**\n\n"
                    "می‌توانید دستور را به صورت `/forward <ثانیه>` ارسال کنید (مثلاً `/forward 3600`) "
                    "یا تسک مورد نظر خود را از لیست زیر انتخاب فرمایید:"
                )
                _, kbd = self._render_rules_list(rules, page=0)
                await message.reply_text(text, reply_markup=kbd)
            except Exception as exc:
                logger.exception("/forward failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("rules") & filters.private)
        async def _rules_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /rules from uid=%s", uid)
            try:
                rules = await self._rules.list_by_owner(uid) if uid else []
                text, kbd = self._render_rules_list(rules, page=0)
                await message.reply_text(text, reply_markup=kbd)
            except Exception as exc:
                logger.exception("/rules failed for uid=%s: %s", uid, exc)

        @b.on_callback_query(filters.regex("^" + CB["main"] + "$"))
        async def _cb_main(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
                await self._safe_edit(cq, text, reply_markup=kbd)
            else:
                await self._safe_edit(
                    cq, 
                    await self._main_text(uid, user=cq.from_user),
                    reply_markup=self._main_menu(),
                )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["sessions"] + "$"))
        async def _cb_sessions(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            logger.info("Executing _cb_sessions for uid=%s", uid)
            try:
                sessions = await self._sessions.list_by_owner(uid) if uid else []
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
                await self._safe_edit(cq, "\n".join(lines), reply_markup=self._sessions_menu())
                await cq.answer()
            except Exception as exc:
                logger.exception("_cb_sessions failed for uid=%s: %s", uid, exc)
                try:
                    await cq.answer(f"⚠️ خطا: {exc}", show_alert=True)
                except Exception:
                    pass

        @b.on_callback_query(filters.regex("^" + CB["rules"] + "$"))
        async def _cb_rules(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_rules_list(rules, page=0)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_pg:(\d+)$"))
        async def _cb_rules_page(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            page = int(raw.split(":", 1)[1])
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_rules_list(rules, page=page)
            try:
                await self._safe_edit(cq, text, reply_markup=kbd)
            except Exception:
                pass
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["filters"] + "$"))
        async def _cb_filters(_, cq: CallbackQuery):
            uid_f = cq.from_user.id if cq.from_user else 0
            filters_ = await self._filters.list_by_owner(uid_f) if uid_f else []
            lines = [self._t("ui_filters_title"), ""]
            if not filters_:
                lines.append(self._t("ui_none"))
            for f in filters_:
                lines.append(f"🧹 `{f.id[:8]}` {f.name}")
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._filters_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["ai"] + "$"))
        async def _cb_ai(_, cq: CallbackQuery):
            uid_a = cq.from_user.id if cq.from_user else 0
            cfgs = await self._ai.list_by_owner(uid_a) if uid_a else []
            lines = [self._t("ui_ai_title"), ""]
            if not cfgs:
                lines.append(self._t("ui_none"))
            else:
                lines.append("👇 برای بررسی سلامت، تست اتصال یا تغییر تنظیمات، روی هر پیکربندی کلیک کنید:")
            for c in cfgs:
                status = "🟢" if c.is_enabled else "🔴"
                lines.append(
                    f"{status} `{c.id[:8]}` {c.name} "
                    f"[{self._provider_label(c.provider.value)}/{c.model}]")
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._ai_menu(cfgs))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["stats"] + "$"))
        async def _cb_stats(_, cq: CallbackQuery):
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 " + self._t("ui_refresh"), callback_data=CB["refresh"])],
                [InlineKeyboardButton("⬅️ " + self._t("ui_back"), callback_data=CB["main"])],
            ])
            await self._safe_edit(cq, 
                await self._dashboard_body(), reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["perf"] + "$"))
        async def _cb_perf(_, cq: CallbackQuery):
            await self._safe_edit(cq, await self._perf_body(), reply_markup=self._debug_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["refresh"] + "$"))
        async def _cb_refresh(_, cq: CallbackQuery):
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 " + self._t("ui_refresh"), callback_data=CB["refresh"])],
                [InlineKeyboardButton("⬅️ " + self._t("ui_back"), callback_data=CB["main"])],
            ])
            await self._safe_edit(cq, 
                await self._dashboard_body(), reply_markup=kbd)
            await cq.answer(self._t("ui_done"), show_alert=False)

        @b.on_callback_query(filters.regex("^" + CB["lang"] + "$"))
        async def _cb_lang(_, cq: CallbackQuery):
            await self._safe_edit(cq, self._t("ui_lang_choose"), reply_markup=self._lang_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["lang_set"] + ":"))
        async def _cb_lang_set(_, cq: CallbackQuery):
            code = cq.data.split(":", 1)[1]
            uid = cq.from_user.id if cq.from_user else 0
            # PERSISTENCE ONLY — never mutate the process-wide i18n.current:
            # it is shared by every concurrent user, so one user's choice
            # would leak into everyone else's renders. The language is read
            # back per-user via _resolve_lang(uid).
            if code not in SUPPORTED_LANGUAGES:
                await cq.answer()
                return
            st = self._state(uid)
            st.lang = code
            if self._users is not None:
                try:
                    await self._users.set_language(uid, code)
                except Exception:
                    pass
            try:
                self._log.info("bot", "system", f"language set to {code} for user {uid}")
            except Exception:
                pass
            await self._safe_edit(
                cq,
                await self._main_text(uid, user=cq.from_user),
                reply_markup=self._main_menu(),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["help"] + "$"))
        async def _cb_help(_, cq: CallbackQuery):
            btn_back = "⬅️ " + self._t("ui_back")
            btn_sup = "🆘 " + ("پشتیبانی (@Capxi)" if self._resolve_lang(uid) == "fa" else "Support (@Capxi)")
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton(btn_sup, url="https://t.me/Capxi")],
                [InlineKeyboardButton(btn_back, callback_data=CB["main"])],
            ])
            await self._safe_edit(cq, self._t("ui_help_body"), reply_markup=kbd)
            await cq.answer()

        # ---------------------------------------------------------- web dashboard
        @b.on_callback_query(filters.regex("^" + CB["dashboard"] + "$"))
        async def _cb_dashboard(_, cq: CallbackQuery):
            await self._show_dashboard(cq, force_reset=False)

        @b.on_callback_query(filters.regex("^" + CB["webpass"] + "$"))
        async def _cb_webpass(_, cq: CallbackQuery):
            await self._show_dashboard(cq, force_reset=False)

        @b.on_callback_query(filters.regex("^" + CB["webpass_reset"] + "$"))
        async def _cb_webpass_reset(_, cq: CallbackQuery):
            await self._show_dashboard(cq, force_reset=True)

        # ---------------------------------------------------------- upgrade / VIP
        @b.on_callback_query(filters.regex("^" + CB["upgrade"] + "$"))
        async def _cb_upgrade(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            text = self._upgrade_text(uid)
            is_fa = self._resolve_lang(uid) == "fa"
            btn_buy = "💎 خرید و فعال‌سازی اشتراک (@Capxi)" if is_fa else "💎 Buy VIP Subscription (@Capxi)"
            btn_back = "⬅️ " + self._t("ui_back")
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton(btn_buy, url="https://t.me/Capxi")],
                [InlineKeyboardButton(btn_back, callback_data=CB["main"])],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["back"] + "$"))
        async def _cb_back(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
                await self._safe_edit(cq, text, reply_markup=kbd)
            else:
                await self._safe_edit(
                    cq,
                    await self._main_text(uid, user=cq.from_user),
                    reply_markup=self._main_menu(),
                )
            await cq.answer()

        # ---------------- client settings & personal credentials ----------------
        @b.on_callback_query(filters.regex("^" + CB["settings"] + "$"))
        async def _cb_settings(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            lines = [
                "⚙️ **تنظیمات و سفارشی‌سازی (Settings)**\n",
                "از این بخش می‌توانید تنظیمات اختصاصی حساب کاربری و کلیدهای شخصی خود را مدیریت کنید.\n",
                f"👤 شناسه عددی شما (User ID): `{uid}`",
            ]
            if self._is_admin(uid):
                lines.append("👑 وضعیت دسترسی: **مدیر سیستم (Administrator)**")
            else:
                lines.append("👤 وضعیت دسترسی: **کاربر عادی (Client)**")
            kbd = self._client_settings_menu(uid)
            await self._safe_edit(cq, "\n".join(lines), reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["client_creds"] + "$"))
        async def _cb_client_creds(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            has_custom = False
            masked_hash = ""
            api_id = 0
            if self._credentials:
                try:
                    owned = await self._credentials.list_by_owner(uid)
                    if owned:
                        has_custom = True
                        api_id = owned[0].api_id
                        h = owned[0].api_hash or ""
                        masked_hash = f"{h[:2]}***{h[-4:]}" if len(h) >= 8 else "***"
                except Exception:
                    pass

            is_fa = self._resolve_lang(uid) == "fa"
            if is_fa:
                lines = [
                    "📱 **کلیدهای اختصاصی تلگرام کاربر (Telegram API Credentials)**\n",
                    "شما می‌توانید از کلیدهای اختصاصی (API ID و API Hash) خود برای اتصال نشست‌ها استفاده کنید.\n",
                ]
                if has_custom:
                    lines.extend([
                        "✅ **کلیدهای اختصاصی فعال شما:**",
                        f"• API ID: `{api_id}`",
                        f"• API Hash: `{masked_hash}` (رمزنگاری شده)\n",
                    ])
                else:
                    lines.append("ℹ️ در حال حاضر از **کلیدهای پیش‌فرض و اشتراکی سامانه** استفاده می‌کنید.\n")

                rows = [
                    [
                        InlineKeyboardButton("✏️ تنظیم یا تغییر کلیدهای شخصی", callback_data="clc_edit"),
                    ],
                ]
                if has_custom:
                    rows.append([
                        InlineKeyboardButton("🗑 بازنشانی به پیش‌فرض سامانه", callback_data=CB["client_cred_reset"]),
                    ])
                rows.append([
                    InlineKeyboardButton(self._t("ui_back_main_menu"), callback_data=CB["settings"]),
                ])
            else:
                lines = [
                    "📱 **Personal Telegram API Credentials**\n",
                    "You can configure your own Telegram API ID and API Hash for connecting your accounts.\n",
                ]
                if has_custom:
                    lines.extend([
                        "✅ **Active Custom Credentials:**",
                        f"• API ID: `{api_id}`",
                        f"• API Hash: `{masked_hash}` (encrypted)\n",
                    ])
                else:
                    lines.append("ℹ️ Currently using the **system default credentials**.\n")

                rows = [
                    [
                        InlineKeyboardButton("✏️ Configure Custom Credentials", callback_data="clc_edit"),
                    ],
                ]
                if has_custom:
                    rows.append([
                        InlineKeyboardButton("🗑 Reset to System Default", callback_data=CB["client_cred_reset"]),
                    ])
                rows.append([
                    InlineKeyboardButton("🔙 Back to Settings", callback_data=CB["settings"]),
                ])

            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^clc_edit$"))
        async def _cb_client_creds_edit(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            st.step = "client_api_id"
            await self._persist(uid, st)
            msg = (
                "📱 لطفاً **API ID عددی** خود را ارسال نمایید:\n"
                "(می‌توانید آن را از my.telegram.org دریافت کنید)"
                if self._resolve_lang(uid) == "fa"
                else "📱 Please send your numeric **Telegram API ID** (from my.telegram.org):"
            )
            await self._safe_edit(
                cq, msg,
                reply_markup=self._kbd([[("✖ " + self._t("ui_cancel"), CB["cancel"])]])
            )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["client_cred_reset"] + "$"))
        async def _cb_client_cred_reset(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if self._credentials:
                try:
                    owned = await self._credentials.list_by_owner(uid)
                    for c in owned:
                        await self._credentials.delete(c.id)
                except Exception:
                    pass
            await cq.answer("✅ کلیدهای شخصی حذف شد و تنظیمات به پیش‌فرض بازگشت.", show_alert=True)
            cq.data = CB["client_creds"]
            await _cb_client_creds(_, cq)

        # ---------------- administrator control plane ----------------
        @b.on_callback_query(filters.regex("^" + CB["admin"] + "$"))
        async def _cb_admin_panel(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ فقط مدیران سامانه مجاز به دسترسی هستند.", show_alert=True)
            text, kbd = await self._render_admin_panel(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_stats"] + "$"))
        async def _cb_adm_stats(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            total_users = await self._users.count_all() if self._users else 0
            active_rules = 0
            if self._rules:
                all_r = await self._rules.list_all()
                active_rules = sum(1 for r in all_r if getattr(r, "enabled", True))
            dlq_count = await self._dlq_count()

            lines = [
                "📊 **آمار و گزارش تفصیلی سامانه (System Diagnostics)**\n",
                f"• 👥 کل کاربران ثبت‌شده: `{total_users}`",
                f"• ⚡ قوانین فعال: `{active_rules}`",
                f"• ⚠️ پیام‌های ناموفق / صف خطا (DLQ): `{dlq_count}`",
                f"• 🌐 دامنه پنل وب: `{self._web_url or 'تنظیم نشده'}`",
                "• 🟢 هسته پردازشی (Core): برخط",
                "• 🟢 سرویس لاگر (Logger): برخط",
                "• 🟢 درگاه وب (Web Gateway): برخط",
            ]
            kbd = InlineKeyboardMarkup([
                [InlineKeyboardButton(self._t("ui_back_main_menu"), callback_data=CB["admin"])],
            ])
            await self._safe_edit(cq, "\n".join(lines), reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^" + CB["adm_users"] + r"(?:_pg_(\d+))?$"))
        async def _cb_adm_users(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)

            page = 0
            m = re.match(r"^" + CB["adm_users"] + r"_pg_(\d+)$", cq.data or "")
            if m:
                page = int(m.group(1))

            limit = 6
            offset = page * limit
            users = await self._users.list_all(limit=limit, offset=offset) if self._users else []
            total = await self._users.count_all() if self._users else 0

            lines = [
                f"👥 **مدیریت کاربران و مستاجران (صفحه {page + 1} از {max(1, (total + limit - 1) // limit)}):**\n",
            ]
            rows = []
            for u in users:
                user_uid = u.get("user_id")
                status = u.get("status", "active")
                is_adm = self._is_admin(user_uid)
                status_icon = "👑" if is_adm else ("🟢" if status == "active" else "🔴")
                plan = u.get("plan", "free")
                lines.append(f"{status_icon} `{user_uid}` | پلن: **{plan}** | وضعیت: {status}")

                btn_toggle = (
                    InlineKeyboardButton(f"⛔ تعلیق {user_uid}", callback_data=f"{CB['adm_user_suspend']}_{user_uid}")
                    if status == "active" and not is_adm
                    else (
                        InlineKeyboardButton(f"✅ فعال‌سازی {user_uid}", callback_data=f"{CB['adm_user_activate']}_{user_uid}")
                        if status == "suspended" and not is_adm
                        else None
                    )
                )
                if btn_toggle:
                    rows.append([btn_toggle])

            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"{CB['adm_users']}_pg_{page - 1}"))
            if offset + limit < total:
                nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"{CB['adm_users']}_pg_{page + 1}"))
            if nav:
                rows.append(nav)

            rows.append([InlineKeyboardButton(self._t("ui_back_main_menu"), callback_data=CB["admin"])])
            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^" + CB["adm_user_suspend"] + r"_(\d+)$"))
        async def _cb_adm_user_suspend(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            target = int(cq.matches[0].group(1))
            if self._is_admin(target):
                return await cq.answer("❌ مدیران سامانه قابل تعلیق نیستند.", show_alert=True)
            if self._users:
                await self._users.set_status(target, "suspended")
            await cq.answer(f"⛔ کاربر {target} تعلیق شد.", show_alert=True)
            cq.data = CB["adm_users"]
            await _cb_adm_users(_, cq)

        @b.on_callback_query(filters.regex(r"^" + CB["adm_user_activate"] + r"_(\d+)$"))
        async def _cb_adm_user_activate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            target = int(cq.matches[0].group(1))
            if self._users:
                await self._users.set_status(target, "active")
            await cq.answer(f"✅ حساب کاربر {target} فعال شد.", show_alert=True)
            cq.data = CB["adm_users"]
            await _cb_adm_users(_, cq)

        @b.on_callback_query(filters.regex("^" + CB["adm_bcast"] + "$"))
        async def _cb_adm_bcast(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            st = self._state(uid)
            st.step = "admin_broadcast_text"
            await self._persist(uid, st)
            msg = (
                "📢 **ارسال پیام همگانی به تمام کاربران**\n\n"
                "لطفاً متن پیام خود را ارسال نمایید (امکان استفاده از شکلک‌ها و فرمت متن وجود دارد):"
            )
            await self._safe_edit(cq, msg, reply_markup=self._kbd([[("✖ " + self._t("ui_cancel"), CB["cancel"])]]))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_bcast_confirm"] + "$"))
        async def _cb_adm_bcast_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            st = self._state(uid)
            text = st.buffer.get("bcast_text", "")
            st.buffer.clear()
            await self._persist(uid, st)

            if not text:
                return await cq.answer("❌ متنی برای ارسال یافت نشد.", show_alert=True)

            bcast_id = str(uuid.uuid4())
            now = int(time.time())
            if self._db:
                try:
                    self._db.execute(
                        "INSERT INTO broadcast_queue (id, initiator_user_id, message_text, target, status, "
                        "total_recipients, sent_count, failed_count, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'all', 'running', 0, 0, 0, ?, ?)",
                        (bcast_id, uid, text, now, now),
                    )
                except Exception:
                    pass

            asyncio.create_task(self._run_broadcast_job(bcast_id, uid, text))
            await cq.answer("🚀 ارسال همگانی در پس‌زمینه آغاز گردید.", show_alert=True)
            text_panel, kbd_panel = await self._render_admin_panel(uid)
            await self._safe_edit(cq, text_panel, reply_markup=kbd_panel)

        @b.on_callback_query(filters.regex("^" + CB["adm_bcast_cancel"] + "$"))
        async def _cb_adm_bcast_cancel(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            st.buffer.clear()
            await self._persist(uid, st)
            await cq.answer("❌ ارسال همگانی لغو شد.")
            text_panel, kbd_panel = await self._render_admin_panel(uid)
            await self._safe_edit(cq, text_panel, reply_markup=kbd_panel)

        @b.on_callback_query(filters.regex("^" + CB["adm_forcesub"] + "$"))
        async def _cb_adm_forcesub(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)

            fs_enabled = False
            fs_channel = ""
            if self._db:
                r1 = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_enabled'")
                if r1 and r1["value"] == "1":
                    fs_enabled = True
                r2 = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_channel'")
                if r2 and r2["value"]:
                    fs_channel = str(r2["value"])

            status_str = "🟢 فعال" if fs_enabled else "⚪ غیرفعال"
            lines = [
                "🔒 **تنظیمات قفل عضویت اجباری (Force-Subscription)**\n",
                f"• وضعیت کنونی: {status_str}",
                f"• کانال تنظیم شده: `{fs_channel or 'تنظیم نشده'}`\n",
                "در صورت فعال بودن، کاربران عادی قبل از استفاده از ربات ملزم به عضویت در کانال خواهند بود.",
            ]
            rows = [
                [
                    InlineKeyboardButton(
                        "🔴 غیرفعال‌سازی" if fs_enabled else "🟢 فعال‌سازی",
                        callback_data=CB["adm_forcesub_toggle"],
                    ),
                    InlineKeyboardButton("✏️ تغییر کانال الزامی", callback_data=CB["adm_forcesub_set"]),
                ],
                [InlineKeyboardButton(self._t("ui_back_main_menu"), callback_data=CB["admin"])],
            ]
            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_forcesub_toggle"] + "$"))
        async def _cb_adm_forcesub_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            if self._db:
                r = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_enabled'")
                new_val = "0" if (r and r["value"] == "1") else "1"
                now = int(time.time())
                self._db.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('force_sub_enabled', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (new_val, now),
                )
            await cq.answer("✅ وضعیت قفل عضویت اجباری تغییر یافت.")
            cq.data = CB["adm_forcesub"]
            await _cb_adm_forcesub(_, cq)

        @b.on_callback_query(filters.regex("^" + CB["adm_forcesub_set"] + "$"))
        async def _cb_adm_forcesub_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            st = self._state(uid)
            st.step = "admin_forcesub_channel"
            await self._persist(uid, st)
            msg = "🔒 لطفاً آیدی کانال الزامی (مثلاً `@ChannelID`) یا شناسه عددی آن را ارسال نمایید:"
            await self._safe_edit(cq, msg, reply_markup=self._kbd([[("✖ " + self._t("ui_cancel"), CB["cancel"])]]))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_admins"] + "$"))
        async def _cb_adm_admins(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)

            admins = self._admin_service.list_admins() if self._admin_service else []
            lines = [
                "👮‍♂️ **مدیریت مدیران و دسترسی‌های سامانه (Admin Access Control)**\n",
            ]
            rows = []
            for a in admins:
                aid = a["user_id"]
                role = a.get("role", "admin")
                role_label = "👑 مالک کل (Owner)" if role == "owner" else "🛡 مدیر (Admin)"
                lines.append(f"• `{aid}` — {role_label}")
                if role != "owner":
                    rows.append([
                        InlineKeyboardButton(f"❌ حذف دسترسی {aid}", callback_data=f"{CB['adm_admin_del']}_{aid}")
                    ])

            rows.append([InlineKeyboardButton("➕ افزودن مدیر جدید", callback_data=CB["adm_admin_add"])])
            rows.append([InlineKeyboardButton(self._t("ui_back_main_menu"), callback_data=CB["admin"])])
            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["adm_admin_add"] + "$"))
        async def _cb_adm_admin_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            st = self._state(uid)
            st.step = "admin_add_id"
            await self._persist(uid, st)
            msg = "👮‍♂️ لطفاً شناسه عددی (Telegram User ID) کاربری که می‌خواهید مدیر شود را ارسال نمایید:"
            await self._safe_edit(cq, msg, reply_markup=self._kbd([[("✖ " + self._t("ui_cancel"), CB["cancel"])]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^" + CB["adm_admin_del"] + r"_(\d+)$"))
        async def _cb_adm_admin_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
            target = int(cq.matches[0].group(1))
            try:
                if self._admin_service:
                    self._admin_service.remove_admin(target, removed_by=uid)
                await cq.answer(f"✅ دسترسی مدیریت کاربر {target} لغو شد.", show_alert=True)
            except Exception as exc:
                await cq.answer(f"❌ خطا: {exc}", show_alert=True)
            cq.data = CB["adm_admins"]
            await _cb_adm_admins(_, cq)

        @b.on_callback_query(filters.regex("^" + CB["adm_config"] + "$"))
        async def _cb_adm_config(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not self._is_admin(uid):
                return await cq.answer("⛔ دسترسی غیرمجاز.", show_alert=True)

            lines = [
                "⚙️ **تنظیمات عمومی سامانه و دامنه وب**\n",
                f"• آدرس عمومی پنل وب: `{self._web_url or 'تنظیم نشده'}`",
                "• کلید رمزنگاری محلی (Master Key): بارگذاری‌شده 🔒\n",
                "جهت تنظیم آدرس دامنه عمومی یا مدیریت کلیدهای API سراسری از گزینه‌های زیر استفاده نمایید:",
            ]
            rows = [
                [InlineKeyboardButton("🌐 تنظیم دامنه پنل وب", callback_data=CB["weburl_set"])],
                [InlineKeyboardButton("🔐 کلیدهای API سراسری (Vault)", callback_data=CB["creds"])],
                [InlineKeyboardButton(self._t("ui_back_main_menu"), callback_data=CB["admin"])],
            ]
            await self._safe_edit(cq, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["check_sub"] + "$"))
        async def _cb_check_sub(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            fs_channel = ""
            if self._db:
                r = self._db.query_one("SELECT value FROM app_settings WHERE key='force_sub_channel'")
                if r and r["value"]:
                    fs_channel = str(r["value"])

            is_member = True
            if fs_channel:
                try:
                    chat_member = await self.bot.get_chat_member(fs_channel, uid)
                    st_str = str(getattr(chat_member, "status", "")).lower()
                    if "left" in st_str or "banned" in st_str:
                        is_member = False
                except Exception as exc:
                    logger.warning("Force-sub verification check error: %s", exc)

            if not is_member:
                return await cq.answer("⚠️ شما هنوز در کانال عضو نشده‌اید! لطفاً ابتدا عضو شوید.", show_alert=True)

            await cq.answer("✅ عضویت شما با موفقیت تأیید گردید! خوش آمدید.", show_alert=True)
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
            else:
                text = await self._main_text(uid, user=cq.from_user)
                kbd = self._main_menu(uid)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------- cancel any active flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["cancel"] + "$"))
        async def _cb_cancel(_, cq: CallbackQuery):
            uid = cq.from_user.id
            st = self._state(uid)
            old_step = str(st.step or "")
            st.step = ""
            st.rule_source = ""
            st.rule_target = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            self._login.cancel(uid)
            if old_step.startswith("rule_"):
                # v12.1: scope to the caller's tenant; admins see everything.
                rules = await self._rules.list_by_owner(uid)
                text, kbd = self._render_rules_list(rules, page=0)
                await self._safe_edit(cq, text, reply_markup=kbd)
            else:
                sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
                if not sessions:
                    lang = await self._get_user_lang(uid)
                    if lang == "fa":
                        c_text = "❌ عملیات لغو شد."
                        btn_connect = "🚀 تلاش مجدد برای اتصال"
                        btn_menu = "🏚️ منوی اصلی"
                    elif lang == "ru":
                        c_text = "❌ Операция отменена."
                        btn_connect = "🚀 Подключить снова"
                        btn_menu = "🏚️ Главное меню"
                    elif lang == "zh":
                        c_text = "❌ 操作已取消。"
                        btn_connect = "🚀 重新连接"
                        btn_menu = "🏚️ 主菜单"
                    else:
                        c_text = "❌ Operation cancelled."
                        btn_connect = "🚀 Connect Again"
                        btn_menu = "🏚️ Main Menu"
                    kbd = self._kbd([
                        [(btn_connect, CB["login"])],
                        [(btn_menu, CB["main"])],
                    ])
                    await self._safe_edit(cq, c_text, reply_markup=kbd)
                else:
                    await self._safe_edit(cq, self._t("ui_cancelled"), reply_markup=self._main_menu())
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
                prompt = await self._connect_prompt_text(uid)
                return await cq.message.edit_text(prompt, reply_markup=self._cancel_kbd())
            result = await self._login.start(uid, LoginFlowManager.normalize_phone(phone))
            await cq.answer()
            if result != "send_code":
                key, _, detail = result.partition(":")
                return await cq.message.edit_text(
                    self._t(f"ui_login_fail_{key}", error=detail),
                    reply_markup=self._cancel_kbd())
            st.step = "login_code"
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            st.buffer["code_digits"] = ""
            await self._persist(uid, st)
            await cq.message.edit_text(
                self._login_code_text(st), reply_markup=self._code_keypad(st))

        @b.on_callback_query(filters.regex(r"^k:"))
        async def _cb_keypad_wrap(_, cq: CallbackQuery):
            await self._cb_keypad(cq)

        @b.on_callback_query(filters.regex("^" + CB["login"] + "$"))
        async def _cb_login(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            logger.info("Executing _cb_login for uid=%s", uid)
            try:
                st = self._state(uid)
                st.step = "login_phone"
                await self._persist(uid, st)
                prompt = await self._connect_prompt_text(uid)
                await self._safe_edit(cq, prompt, reply_markup=self._cancel_kbd())
                await cq.answer()
                logger.info("_cb_login prompt sent successfully for uid=%s; step=login_phone", uid)
            except Exception as exc:
                logger.exception("_cb_login failed for uid=%s: %s", uid, exc)
                try:
                    await cq.answer(f"⚠️ خطا: {exc}", show_alert=True)
                except Exception:
                    pass
                    pass

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
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._creds_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["cred_add"] + "$"))
        async def _cb_cred_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "cred_label"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, 
                self._t("ui_cred_label"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["cred_default"] + ":"))
        async def _cb_cred_default(_, cq: CallbackQuery):
            cid = cq.data.split(":", 1)[1]
            if self._credentials is not None:
                try:
                    # v12.1: a forged credential id must not flip another
                    # tenant's default credential.
                    await self._credentials.set_default_owned(
                        cid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
                    try:
                        self._log.info("bot", "cred", f"credential {cid[:8]} set default")
                    except Exception:
                        pass
                except PermissionError:
                    return await cq.answer(self._t("ui_need_admin"), show_alert=True)
                except Exception as exc:
                    await self._record_error("bot", exc, user_id=cq.from_user.id)
            await cq.answer(self._t("ui_cred_set_default", id=cid[:8]), show_alert=True)

        # ---------------- rule add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["rule_add"] + "$"))
        async def _cb_rule_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions and self._is_admin(uid):
                sessions = await self._sessions.list_all()

            if not sessions:
                await self._safe_edit(cq, 
                    self._t("ui_no_sessions", lang=cur_lang), reply_markup=self._sessions_menu())
                return await cq.answer()

            st = self._state(uid)
            st.step = "rule_source"
            st.rule_source = ""
            st.rule_target = ""
            st.buffer = {}

            try:
                await cq.answer(self._t("ui_loading_dialogs", lang=cur_lang))
            except Exception:
                pass

            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            st.buffer["page"] = 0
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="source", page=0, uid=uid)
            await self._safe_edit(cq, text, reply_markup=markup)

        @b.on_callback_query(filters.regex(r"^ch:(s|t):(\d+)$"))
        async def _cb_pick_chat(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"
            idx = int(parts[2])

            chats = st.buffer.get("chats", []) if isinstance(st.buffer, dict) else []
            if not (0 <= idx < len(chats)):
                return await cq.answer(self._t("ui_chat_not_found", lang=cur_lang), show_alert=True)

            selected = chats[idx]
            chat_id = selected["id"]
            chat_title = f"{selected['emoji']} {selected['title']}"

            # Check if this selection is part of an edit flow
            rule_id = st.buffer.get("rule_id") if isinstance(st.buffer, dict) else ""
            in_draft = bool(st.buffer.get("in_draft")) if isinstance(st.buffer, dict) else False
            edit_role = st.buffer.get("edit_role") if isinstance(st.buffer, dict) else (
                "source" if st.step == "rule_edit_source" else ("target" if st.step == "rule_edit_target" else None)
            )

            if rule_id and edit_role:
                rule = await self._rules.get(rule_id)
                if not rule:
                    return await cq.answer(self._t("ui_rule_not_found", lang=cur_lang), show_alert=True)

                draft_obj = st.buffer.get("draft") if (in_draft and isinstance(st.buffer, dict)) else None
                draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
                if in_draft and not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
                    st.buffer["draft"] = draft

                if edit_role == "source":
                    target_id = draft.get("target_chat_id") if in_draft else rule.target_chat_id
                    if str(chat_id).strip() == str(target_id).strip():
                        return await cq.answer(self._t("ui_loop_prevention_src", lang=cur_lang), show_alert=True)

                    if in_draft:
                        draft["source_chat_id"] = str(chat_id)
                        draft["source_name"] = selected["title"]
                        st.step = "rule_editing_draft"
                        await self._persist(uid, st)
                        await cq.answer(self._t("ui_src_selected", lang=cur_lang, title=selected['title']))
                        text, markup = self._render_draft_editor(rule, draft)
                        return await self._safe_edit(cq, text, reply_markup=markup)
                    else:
                        rule.source_chat_id = str(chat_id)
                        rule.source_chat_name = selected["title"]
                        expected_ver = st.buffer.get("version")
                        try:
                            await self._save_owned_rule(rule, uid, expected_ver)
                        except ConcurrencyError:
                            return await cq.answer(self._t("ui_concurrency_error", lang=cur_lang), show_alert=True)
                        st.step = ""
                        st.buffer = {}
                        await self._persist(uid, st)
                        await cq.answer(self._t("ui_src_selected", lang=cur_lang, title=selected['title']))
                        text, markup = self._render_rule_detail(rule)
                        return await self._safe_edit(cq, text, reply_markup=markup)

                elif edit_role == "target":
                    source_id = draft.get("source_chat_id") if in_draft else rule.source_chat_id
                    if str(chat_id).strip() == str(source_id).strip():
                        return await cq.answer(self._t("ui_loop_prevention_tgt", lang=cur_lang), show_alert=True)

                    if in_draft:
                        draft["target_chat_id"] = str(chat_id)
                        draft["target_name"] = selected["title"]
                        st.step = "rule_editing_draft"
                        await self._persist(uid, st)
                        await cq.answer(self._t("ui_tgt_selected", lang=cur_lang, title=selected['title']))
                        text, markup = self._render_draft_editor(rule, draft)
                        return await self._safe_edit(cq, text, reply_markup=markup)
                    else:
                        rule.target_chat_id = str(chat_id)
                        rule.target_chat_name = selected["title"]
                        expected_ver = st.buffer.get("version")
                        try:
                            await self._save_owned_rule(rule, uid, expected_ver)
                        except ConcurrencyError:
                            return await cq.answer(self._t("ui_concurrency_error", lang=cur_lang), show_alert=True)
                        st.step = ""
                        st.buffer = {}
                        await self._persist(uid, st)
                        await cq.answer(self._t("ui_tgt_selected", lang=cur_lang, title=selected['title']))
                        text, markup = self._render_rule_detail(rule)
                        return await self._safe_edit(cq, text, reply_markup=markup)

            if role == "source":
                st.rule_source = chat_id
                st.buffer["source_name"] = chat_title
                st.step = "rule_target"
                st.buffer["page"] = 0
                await self._persist(uid, st)
                await cq.answer(self._t("ui_src_selected", lang=cur_lang, title=selected['title']), show_alert=False)
                text, markup = self._render_chat_picker(st, role="target", page=0, uid=uid)
                await self._safe_edit(cq, text, reply_markup=markup)
            else:
                st.rule_target = chat_id
                target_title = chat_title
                source_title = st.buffer.get("source_name") or st.rule_source
                await cq.answer(self._t("ui_creating_rule", lang=cur_lang), show_alert=False)
                await self._finish_rule_creation(cq, st, source_title, target_title)

        @b.on_callback_query(filters.regex(r"^pg:(s|t):(\d+)$"))
        async def _cb_page_chat(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"
            page = int(parts[2])

            if isinstance(st.buffer, dict):
                st.buffer["page"] = page
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role=role, page=page, uid=uid)
            try:
                await self._safe_edit(cq, text, reply_markup=markup)
            except Exception:
                pass
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfc:(s|t)$"))
        async def _cb_refresh_chats(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"

            try:
                await cq.answer(self._t("ui_refreshing_dialogs", lang=cur_lang))
            except Exception:
                pass

            chats = await self._fetch_dialogs(uid)
            if isinstance(st.buffer, dict):
                st.buffer["chats"] = chats
                st.buffer["page"] = 0
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role=role, page=0, uid=uid)
            await self._safe_edit(cq, text, reply_markup=markup)

        @b.on_callback_query(filters.regex(r"^r_cancel$"))
        async def _cb_rule_cancel(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            st = self._state(uid)
            st.step = ""
            st.rule_source = ""
            st.rule_target = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_rules_list(rules, page=0)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_op_cancelled", lang=cur_lang))

        @b.on_callback_query(filters.regex(r"^r_back_source$"))
        async def _cb_rule_back_source(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            cur_lang = self._resolve_lang(uid)
            st = self._state(uid)
            st.step = "rule_source"
            st.rule_target = ""
            if isinstance(st.buffer, dict):
                st.buffer["page"] = 0
            await self._persist(uid, st)
            text, markup = self._render_chat_picker(st, role="source", page=0, uid=uid)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer(self._t("ui_back_to_source", lang=cur_lang))

        @b.on_callback_query(filters.regex(r"^noop$"))
        async def _cb_noop(_, cq: CallbackQuery):
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rdel:(.+)$"))
        async def _cb_rule_del(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rid = raw.split(":", 1)[1]
            rule = await self._rules.get(rid)
            name = (rule.source_chat_name or rule.source_chat_id) if rule else rid[:8]
            confirm_text = (
                f"⚠️ **تأیید حذف قانون**\n\n"
                f"آیا مطمئن هستید که می‌خواهید قانون «`{name}`» را حذف کنید؟"
            )
            confirm_kbd = self._kbd([
                [("🗑 بله، حذف شود", f"rdelyes:{rid}")],
                [(self._t("ui_cancel_back"), f"rd:{rid}")],
            ])
            await self._safe_edit(cq, confirm_text, reply_markup=confirm_kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rdelyes:(.+)$"))
        async def _cb_rule_del_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rid = raw.split(":", 1)[1]
            try:
                ok = await self._rules.delete_owned(rid, self._scope_uid(uid))
            except PermissionError:
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            if not ok:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            try:
                self._log.info("bot", "rule", f"rule {rid} deleted")
            except Exception:
                pass
            rules = await (self._rules.list_all() if self._is_admin(uid) else self._rules.list_by_owner(uid))
            text, kbd = self._render_rules_list(rules, page=0)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(self._t("ui_rule_deleted"), show_alert=True)

        @b.on_callback_query(filters.regex("^" + CB["rule_toggle"] + ":"))
        async def _cb_rule_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rid = raw.split(":", 1)[1]
            rule = await self._rules.toggle_owned(rid, self._scope_uid(uid))
            if not rule:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            try:
                self._log.info("bot", "rule", f"rule {rid} toggled")
            except Exception:
                pass
            rules = await (self._rules.list_all() if self._is_admin(uid) else self._rules.list_by_owner(uid))
            lines = [self._t("ui_rules_title"), ""]
            if not rules:
                lines.append(self._t("ui_none"))
            for r in rules:
                status = "🟢" if r.is_active else "🔴"
                target = getattr(r, "target_label", None) or (
                    r.target_chat_name or r.target_chat_id)
                lines.append(
                    f"{status} `{r.id[:8]}` **{r.source_chat_name or r.source_chat_id}** ➔ **{target}**")
                mode = getattr(r.forward_mode, "value", r.forward_mode)
                delay = float(getattr(r, "delay_seconds", 0) or 0)
                lines.append(f"     ⚙️ mode={mode} | ⏳ delay={delay:.0f}s")
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._rules_menu(rules))
            await cq.answer(self._t("ui_done"))

        # ---------------- rule detailed settings & controls ----------------
        @b.on_callback_query(filters.regex(r"^rd:(.+)$"))
        async def _cb_rule_detail(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            # v12.1: resolve through the ownership guard — a forged callback id
            # for another tenant's rule must not render.
            rule = await self._rules.get_owned(rule_id, None if self._is_admin(uid) else uid)
            if not rule:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            if not (self._is_admin(uid) or rule.owner_user_id == uid):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtx:(.+)$"))
        async def _cb_rule_replace_menu(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            text, kbd = self._render_replace_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtxa:(.+)$"))
        async def _cb_rule_replace_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            st = self._state(cq.from_user.id)
            st.step = "rule_replace_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(cq.from_user.id, st)
            prompt = (
                "✏️ **افزودن عبارت جایگزین (Text Replace):**\n\n"
                "عبارت مبدأ و عبارت جدید را به یکی از فرمت‌های زیر بفرستید:\n\n"
                "`کلمه قدیمی -> کلمه جدید`\n"
                "`@OldChannel -> @MyChannel`\n"
                "`تخفیف ۱۰٪ -> تخفیف ۵۰٪`\n\n"
                "💡 **جهت حذف کامل یک کلمه:** کافیست بعد از `->` چیزی ننویسید:\n"
                "`کلمه حذفی ->`\n\n"
                "برای بازگشت می‌توانید دکمه زیر را لمس کنید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[(self._t("ui_cancel_back"), f"rtx:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtxc:(.+)$"))
        async def _cb_rule_replace_clear(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["replacements"] = {}
            await self._rules.update(rule)
            await cq.answer("🗑 همه جایگزینی‌ها پاکسازی شدند")
            text, kbd = self._render_replace_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtxd:(.+):(\d+)$"))
        async def _cb_rule_replace_del(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, idx_str = parts[1], parts[2]
            idx = int(idx_str)
            rule = await self._rules.get(rule_id)
            if not rule or not rule.replacements:
                return await cq.answer("❌ موردی یافت نشد", show_alert=True)
            keys = list(rule.replacements.keys())
            if 0 <= idx < len(keys):
                target_key = keys[idx]
                reps = dict(rule.replacements)
                reps.pop(target_key, None)
                if not isinstance(rule.metadata, dict):
                    rule.metadata = {}
                rule.metadata["replacements"] = reps
                await self._rules.update(rule)
                await cq.answer(f"❌ `{target_key}` حذف شد")
            text, kbd = self._render_replace_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rhf:(.+)$"))
        async def _cb_rule_header_footer_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد یا دسترسی ندارید", show_alert=True)
            text, kbd = self._render_header_footer_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rh_s:(.+)$"))
        async def _cb_rule_header_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ دسترسی ندارید", show_alert=True)
            st = self._state(uid)
            st.step = "rule_header_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = (
                "📝 **تنظیم متن هدر (پیشوند پیام):**\n\n"
                "متنی که می‌خواهید در ابتدای تمام پیام‌های ارسالی قرار گیرد را ارسال کنید.\n\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rhf:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rh_d:(.+)$"))
        async def _cb_rule_header_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.header = ""
            await self._save_owned_rule(rule, uid)
            await cq.answer("🗑 هدر با موفقیت حذف شد")
            text, kbd = self._render_header_footer_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rf_s:(.+)$"))
        async def _cb_rule_footer_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ دسترسی ندارید", show_alert=True)
            st = self._state(uid)
            st.step = "rule_footer_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = (
                "📝 **تنظیم متن فوتر (پسوند پیام):**\n\n"
                "متنی که می‌خواهید در انتهای تمام پیام‌های ارسالی قرار گیرد را ارسال کنید (مثلاً آیدی کانال، لینک، متن دلخواه).\n\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rhf:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rf_d:(.+)$"))
        async def _cb_rule_footer_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.footer = ""
            await self._save_owned_rule(rule, uid)
            await cq.answer("🗑 فوتر با موفقیت حذف شد")
            text, kbd = self._render_header_footer_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------- Cleaner Callbacks ----------------
        @b.on_callback_query(filters.regex(r"^rcl:(.+)$"))
        async def _cb_rule_cleaner_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد یا دسترسی ندارید", show_alert=True)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rcl_url:(.+)$"))
        async def _cb_clean_url_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.remove_links = not bool(rule.remove_links)
            await self._save_owned_rule(rule, uid)
            status = "حذف لینک‌ها فعال شد 🚫" if rule.remove_links else "حفظ لینک‌ها مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_usr:(.+)$"))
        async def _cb_clean_usr_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.clean_usernames = not bool(rule.clean_usernames)
            await self._save_owned_rule(rule, uid)
            status = "حذف منشن‌ها فعال شد 🚫" if rule.clean_usernames else "حفظ منشن‌ها مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_tag:(.+)$"))
        async def _cb_clean_tag_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.clean_hashtags = not bool(rule.clean_hashtags)
            await self._save_owned_rule(rule, uid)
            status = "حذف هشتگ‌ها فعال شد 🚫" if rule.clean_hashtags else "حفظ هشتگ‌ها مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_emo:(.+)$"))
        async def _cb_clean_emo_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.remove_emojis = not bool(rule.remove_emojis)
            await self._save_owned_rule(rule, uid)
            status = "پاکسازی ایموجی فعال شد 🧹" if rule.remove_emojis else "حفظ ایموجی مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_phn:(.+)$"))
        async def _cb_clean_phn_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.clean_phones = not bool(rule.clean_phones)
            await self._save_owned_rule(rule, uid)
            status = "حذف شماره تماس فعال شد 🚫" if rule.clean_phones else "حفظ شماره تماس مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_btn:(.+)$"))
        async def _cb_clean_btn_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.clean_buttons = not bool(rule.clean_buttons)
            await self._save_owned_rule(rule, uid)
            status = "حذف دکمه‌های شیشه‌ای فعال شد 🚫" if rule.clean_buttons else "حفظ دکمه‌ها مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_spc:(.+)$"))
        async def _cb_clean_spc_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.clean_empty_lines = not bool(rule.clean_empty_lines)
            await self._save_owned_rule(rule, uid)
            status = "پاکسازی خطوط خالی فعال شد ✂️" if rule.clean_empty_lines else "حفظ خطوط خالی مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_cleaner_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rcl_wrd:(.+)$"))
        async def _cb_clean_wrd_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            st = self._state(uid)
            st.step = "rule_clean_words_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = (
                "📝 **تنظیم کلمات حذفی سفارشی (Custom Words):**\n\n"
                "کلماتی که می‌خواهید به طور خودکار از پیام‌ها پاک شوند را ارسال کنید.\n"
                "(می‌توانید کلمات را با کاما (,) یا در خطوط جداگانه ارسال کنید).\n\n"
                "برای پاکسازی همه کلمات عبارت `clear` را بفرستید.\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rcl:{rule_id}")]]))
            await cq.answer()

        # ---------------- Filters Callbacks ----------------
        @b.on_callback_query(filters.regex(r"^rfl:(.+)$"))
        async def _cb_rule_filters_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد یا دسترسی ندارید", show_alert=True)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_lnk:(.+)$"))
        async def _cb_filter_link_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.block_links = not bool(rule.block_links)
            await self._save_owned_rule(rule, uid)
            status = "فیلتر لینک فعال شد (پیام‌های دارای لینک مسدود می‌شوند) 🚫" if rule.block_links else "فیلتر لینک غیرفعال شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_vid:(.+)$"))
        async def _cb_filter_video_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.block_video = not bool(rule.block_video)
            await self._save_owned_rule(rule, uid)
            status = "مسدودسازی ویدیو فعال شد 🚫" if rule.block_video else "ارسال ویدیو مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_aud:(.+)$"))
        async def _cb_filter_audio_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.block_audio = not bool(rule.block_audio)
            await self._save_owned_rule(rule, uid)
            status = "مسدودسازی فایل صوتی فعال شد 🚫" if rule.block_audio else "ارسال فایل صوتی مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_stk:(.+)$"))
        async def _cb_filter_sticker_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.block_stickers = not bool(rule.block_stickers)
            await self._save_owned_rule(rule, uid)
            status = "مسدودسازی استیکر فعال شد 🚫" if rule.block_stickers else "ارسال استیکر مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_txt:(.+)$"))
        async def _cb_filter_text_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.block_text = not bool(rule.block_text)
            await self._save_owned_rule(rule, uid)
            status = "مسدودسازی پیام متنی فعال شد 🚫" if rule.block_text else "ارسال پیام متنی مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_voc:(.+)$"))
        async def _cb_filter_voice_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.block_voice = not bool(rule.block_voice)
            await self._save_owned_rule(rule, uid)
            status = "مسدودسازی وویس فعال شد 🚫" if rule.block_voice else "ارسال وویس مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_filters_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------- Max Time Edit Callbacks ----------------
        @b.on_callback_query(filters.regex(r"^rmte:(.+)$"))
        async def _cb_rule_max_time_edit_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد یا دسترسی ندارید", show_alert=True)
            text, kbd = self._render_max_time_edit_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rmte_s:(.+?):(\d+)$"))
        async def _cb_rule_max_time_edit_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, secs = parts[1], int(parts[2])
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.max_time_edit = secs
            await self._save_owned_rule(rule, uid)
            msg = f"سقف زمان ادیت به {secs} ثانیه تغییر کرد ✅" if secs > 0 else "محدودیت زمان ادیت برداشته شد (نامحدود) ✅"
            await cq.answer(msg)
            text, kbd = self._render_max_time_edit_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rmte_c:(.+)$"))
        async def _cb_rule_max_time_edit_custom(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            st = self._state(uid)
            st.step = "rule_max_edit_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(uid, st)
            prompt = (
                "⏱ **ورود زمان مجاز ادیت (بر حسب ثانیه):**\n\n"
                "یک عدد به ثانیه ارسال کنید (مثلاً 1800 برای نیم ساعت یا 86400 برای یک روز).\n\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rmte:{rule_id}")]]))
            await cq.answer()

        # ---------------- Profile Management Callbacks ----------------
        @b.on_callback_query(filters.regex(r"^pr_restart$"))
        async def _cb_profile_restart_setup(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = UiState()
            await self._persist(uid, st)
            await cq.answer("✅ ستاپ راه‌اندازی با موفقیت بازنشانی شد.")
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text, kbd = self._render_welcome_onboarding(uid, user=cq.from_user)
            else:
                text = await self._main_text(uid, user=cq.from_user)
                kbd = self._main_menu()
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pr_reset_cfg$"))
        async def _cb_profile_reset_cfg_prompt(_, cq: CallbackQuery):
            text = (
                "⚠️ **هشدار بازنشانی تنظیمات (Reset Config)**\n\n"
                "آیا مطمئن هستید که می‌خواهید تمام تنظیمات پیشرفته (هدر، فوتر، پاک‌کننده، فیلترها و کلمات حذفی) "
                "تمام قوانین خود را به حالت اولیه بازگردانید؟\n\n"
                "*(قوانین و سشن‌های شما حذف نخواهند شد، فقط تنظیمات آن‌ها ریست می‌شوند)*"
            )
            kbd = self._kbd([
                [("✅ بله، ریست کامل تنظیمات", "pr_reset_cfg_yes")],
                [(self._t("ui_cancel_back"), "pr_view")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^pr_reset_cfg_yes$"))
        async def _cb_profile_reset_cfg_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            rules = await self._rules.list_by_owner(uid) if uid else []
            for r in rules:
                r.custom_header = ""
                r.custom_footer = ""
                if not isinstance(r.metadata, dict):
                    r.metadata = {}
                r.metadata = {"replacements": {}}
                r.max_time_edit = 86400
                r.remove_links = False
                await self._save_owned_rule(r, uid)
            await cq.answer("✅ تمام تنظیمات با موفقیت ریست شدند.", show_alert=True)
            body, kbd = await self._render_profile_menu(uid, cq.from_user)
            await self._safe_edit(cq, body, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pr_view$"))
        async def _cb_profile_view(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            body, kbd = await self._render_profile_menu(uid, cq.from_user)
            await self._safe_edit(cq, body, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rlk:(.+)$"))
        async def _cb_rule_toggle_link(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.remove_links = not rule.remove_links
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["remove_links"] = rule.remove_links
            await self._rules.update(rule)
            status = "حذف لینک‌ها فعال شد 🚫" if rule.remove_links else "حفظ لینک‌ها مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rvc:(.+)$"))
        async def _cb_rule_toggle_voice(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            new_val = not rule.block_voice
            rule.metadata["block_voice"] = new_val
            await self._rules.update(rule)
            status = "مسدودسازی ویس فعال شد 🚫" if new_val else "ارسال ویس مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rst:(.+)$"))
        async def _cb_rule_toggle_sticker(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            new_val = not rule.block_stickers
            rule.metadata["block_stickers"] = new_val
            await self._rules.update(rule)
            status = "مسدودسازی استیکر فعال شد 🚫" if new_val else "ارسال استیکر مجاز شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rem:(.+)$"))
        async def _cb_rule_toggle_emoji(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            new_val = not rule.remove_emojis
            rule.metadata["remove_emojis"] = new_val
            await self._rules.update(rule)
            status = "پاکسازی خودکار ایموجی‌ها فعال شد 🧹" if new_val else "حفظ ایموجی‌ها فعال شد 🟢"
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^red:(.+)$"))
        async def _cb_rule_toggle_edit(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.ignore_edits = not rule.ignore_edits
            await self._save_owned_rule(rule, uid)
            status = "نادیده گرفتن ادیت فعال شد ⏹" if rule.ignore_edits else "سینک ادیت مقصد فعال شد 🔄"
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rdl:(.+)$"))
        async def _cb_rule_toggle_delete(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            new_val = not rule.sync_deletes
            rule.sync_deletes = new_val
            await self._save_owned_rule(rule, uid)
            status = "سینک حذف پیام فعال شد 🗑" if new_val else "نادیده گرفتن حذف فعال شد ⏹"
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rmg:(.+)$"))
        async def _cb_rule_toggle_media_group(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            cur = rule.album_mode
            cycle = {"album": "first", "first": "split", "split": "album"}
            next_mode = cycle.get(cur, "album")
            rule.metadata["album_mode"] = next_mode
            await self._rules.update(rule)
            names = {"album": "🖼 آلبوم کامل", "first": "1️⃣ فقط اولین مدیا", "split": "🔀 تفکیک پیام‌ها"}
            await cq.answer(f"چندتصویری: {names.get(next_mode, next_mode)}")
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rmd:(.+)$"))
        async def _cb_rule_toggle_mode(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            cur = rule.forward_mode
            rule.forward_mode = (
                ForwardMode.DIRECT_FORWARD
                if cur == ForwardMode.COPY_MESSAGE
                else ForwardMode.COPY_MESSAGE
            )
            await self._rules.update(rule)
            mode_names = {
                ForwardMode.COPY_MESSAGE: "📋 کپی بدون تگ",
                ForwardMode.DIRECT_FORWARD: "↗️ فوروارد با تگ",
            }
            await cq.answer(f"حالت ارسال: {mode_names.get(rule.forward_mode, rule.forward_mode.value)}")
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg:(.+)$"))
        async def _cb_rule_toggle_active(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد یا متعلق به شما نیست", show_alert=True)
            rule.is_active = not rule.is_active
            await self._save_owned_rule(rule, uid)
            status = "فعال شد 🟢" if rule.is_active else "غیرفعال شد 🔴"
            await cq.answer(f"وضعیت: {status}")
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rrm:(.+)$"))
        async def _cb_rule_remove_ask(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ این قانون یافت نشد یا متعلق به شما نیست.", show_alert=True)
            text = (
                f"⚠️ **آیا از حذف این قانون اطمینان دارید؟**\n\n"
                f"📡 مبدأ: `{rule.source_chat_name or rule.source_chat_id}`\n"
                f"🎯 مقصد: `{rule.target_chat_name or rule.target_chat_id}`\n\n"
                "این عملیات غیرقابل بازگشت است."
            )
            kbd = self._kbd([
                [("🗑 بله، حذف شود", f"rrm_confirm:{rule.id}")],
                [("❌ انصراف", f"rd:{rule.id}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rrm_confirm:(.+)$"))
        async def _cb_rule_remove_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ این قانون یافت نشد یا متعلق به شما نیست.", show_alert=True)
            await self._rules.delete(rule_id)
            await cq.answer("🗑 قانون با موفقیت حذف شد", show_alert=True)
            rules = await self._rules.list_by_owner(uid) if uid else []
            text, kbd = self._render_rules_list(rules, page=0)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rdup:(.+)$"))
        async def _cb_rule_duplicate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ این قانون یافت نشد یا متعلق به شما نیست.", show_alert=True)
            draft = rule.to_draft()
            new_rule = ForwardRule(
                session_id=rule.session_id,
                name=f"{rule.name} (کپی)" if rule.name else "قانون جدید (کپی)",
                source_chat_id=rule.source_chat_id,
                source_chat_name=rule.source_chat_name,
                target_chat_id=rule.target_chat_id,
                target_chat_name=rule.target_chat_name,
                owner_user_id=uid,
                metadata=copy.deepcopy(rule.metadata) if isinstance(rule.metadata, dict) else {},
            )
            draft["name"] = new_rule.name
            new_rule.apply_draft(draft)
            new_rule.owner_user_id = uid
            saved = await self._save_owned_rule(new_rule, uid)
            await cq.answer("📑 قانون با موفقیت تکثیر شد!", show_alert=True)
            text, kbd = self._render_rule_detail(saved)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^radv:(.+)$"))
        async def _cb_rule_advanced_settings(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ این قانون یافت نشد یا متعلق به شما نیست.", show_alert=True)
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^radon_all:(.+)$"))
        async def _cb_rule_all_addons(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ این قانون یافت نشد یا متعلق به شما نیست.", show_alert=True)
            text, kbd = self._render_all_addons(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rgrp:(.+?):(.+)$"))
        async def _cb_rule_group_switch(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, grp = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.feature_group = grp
            await self._save_owned_rule(rule, uid)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(f"دسته: {grp}")

        @b.on_callback_query(filters.regex(r"^(?:radon|radon_tg):(.+?):(.+)$"))
        async def _cb_rule_addon_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            is_modal = raw.startswith("radon_tg:")
            parts = raw.split(":")
            rule_id, addon_key = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            addons = rule.paid_addons
            curr = bool(addons.get(addon_key, False))
            addons[addon_key] = not curr
            rule.paid_addons = addons
            await self._save_owned_rule(rule, uid)
            st_text = "فعال شد 🟢" if not curr else "غیرفعال شد ⚪"
            await cq.answer(f"{addon_key}: {st_text}")
            if is_modal:
                text, kbd = self._render_all_addons(rule)
            else:
                text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtgbg:(.+)$"))
        async def _cb_rule_toggle_debug(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.debug_mode = not rule.debug_mode
            await self._save_owned_rule(rule, uid)
            st = "فعال شد 🟢" if rule.debug_mode else "غیرفعال شد ⚪"
            await cq.answer(f"دیباگ: {st}")
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_fwdhdr:(.+)$"))
        async def _cb_rule_toggle_fwd_header(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.show_forward_header = not rule.show_forward_header
            await self._save_owned_rule(rule, uid)
            st = "فعال شد 🟢" if rule.show_forward_header else "غیرفعال شد ⚪"
            await cq.answer(f"نمایش هدر مبدأ: {st}")
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_repsync:(.+)$"))
        async def _cb_rule_toggle_reply_sync(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.sync_replies = not rule.sync_replies
            await self._save_owned_rule(rule, uid)
            st = "فعال شد 🟢" if rule.sync_replies else "غیرفعال شد ⚪"
            await cq.answer(f"همگام‌سازی ریپلای: {st}")
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_dup:(.+)$"))
        async def _cb_rule_toggle_duplicate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.duplicate_filters = not rule.duplicate_filters
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"Duplicate Filters: {'ON' if rule.duplicate_filters else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_crypto:(.+)$"))
        async def _cb_rule_toggle_crypto(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.crypto_mode = not rule.crypto_mode
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"Crypto Mode: {'ON' if rule.crypto_mode else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_raw:(.+)$"))
        async def _cb_rule_toggle_raw_text(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.process_raw_text = not rule.process_raw_text
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"Process Raw Text: {'ON' if rule.process_raw_text else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_btn2txt:(.+)$"))
        async def _cb_rule_toggle_btn2txt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.button_to_text = not rule.button_to_text
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"Button To Text: {'ON' if rule.button_to_text else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_ocr:(.+)$"))
        async def _cb_rule_toggle_ocr(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.ocr_image_ai = not rule.ocr_image_ai
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"OCR Image AI: {'ON' if rule.ocr_image_ai else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_ai:(.+)$"))
        async def _cb_rule_toggle_ai_rewrite(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.smart_ai_rewrite = not rule.smart_ai_rewrite
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"Smart AI Rewrite: {'ON' if rule.smart_ai_rewrite else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg_digest:(.+)$"))
        async def _cb_rule_toggle_news_digest(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.news_digest = not rule.news_digest
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"News Digest: {'ON' if rule.news_digest else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # --- Delay Forward Handlers ---
        @b.on_callback_query(filters.regex(r"^rdly:(.+)$"))
        async def _cb_rule_delay_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            text, kbd = self._render_delay_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rdly_s:(.+?):(.+)$"))
        async def _cb_rule_delay_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, sec_str = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            try:
                rule.delay_seconds = float(sec_str)
            except ValueError:
                rule.delay_seconds = 0.0
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"تأخیر: {rule.delay_seconds:.0f} ثانیه")
            text, kbd = self._render_delay_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rdly_c:(.+)$"))
        async def _cb_rule_delay_custom(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            st = self._state(uid)
            st.step = f"set_delay_{rule.id}"
            await self._persist(uid, st)
            prompt = (
                f"⏱ **ورود میزان تأخیر دلخواه (Delay)**\n\n"
                f"لطفاً عدد ثانیه تأخیر موردنظر را ارسال کنید (مثلاً `5` یا `15`)."
            )
            kbd = self._kbd([[("❌ انصراف", f"rdly:{rule.id}")]])
            await self._safe_edit(cq, prompt, reply_markup=kbd)
            await cq.answer()

        # --- Blacklist Handlers ---
        @b.on_callback_query(filters.regex(r"^rfl_bl:(.+)$"))
        async def _cb_rule_blacklist_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            text, kbd = self._render_blacklist_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_bl_add:(.+)$"))
        async def _cb_rule_blacklist_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            st = self._state(uid)
            st.step = f"add_bl_{rule.id}"
            await self._persist(uid, st)
            prompt = (
                f"🚫 **افزودن به لیست سیاه (Blacklist)**\n\n"
                f"کلمات یا عبارات موردنظر را ارسال کنید (می‌توانید چند کلمه را با کاما یا در خطوط جداگانه ارسال کنید):"
            )
            kbd = self._kbd([[("❌ انصراف", f"rfl_bl:{rule.id}")]])
            await self._safe_edit(cq, prompt, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_bl_clr:(.+)$"))
        async def _cb_rule_blacklist_clear(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.blacklist = []
            await self._save_owned_rule(rule, uid)
            await cq.answer("🗑 کل لیست سیاه پاکسازی شد")
            text, kbd = self._render_blacklist_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rfl_bu:(.+)$"))
        async def _cb_rule_blacklist_users(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            cnt = len(rule.blacklist_users)
            await cq.answer(f"Blacklist Users: {cnt} blocked", show_alert=True)

        @b.on_callback_query(filters.regex(r"^rfl_wu:(.+)$"))
        async def _cb_rule_whitelist_users(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            cnt = len(rule.whitelist_users)
            await cq.answer(f"Whitelist Users: {cnt} allowed", show_alert=True)

        # --- Whitelist Handlers ---
        @b.on_callback_query(filters.regex(r"^rfl_wl:(.+)$"))
        async def _cb_rule_whitelist_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            text, kbd = self._render_whitelist_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_wl_add:(.+)$"))
        async def _cb_rule_whitelist_add(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            st = self._state(uid)
            st.step = f"add_wl_{rule.id}"
            await self._persist(uid, st)
            prompt = (
                f"✅ **افزودن به لیست سفید (Whitelist)**\n\n"
                f"کلمات مجاز را ارسال کنید (تنها پیام‌های حاوی این کلمات فوروارد خواهند شد):"
            )
            kbd = self._kbd([[("❌ انصراف", f"rfl_wl:{rule.id}")]])
            await self._safe_edit(cq, prompt, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfl_wl_clr:(.+)$"))
        async def _cb_rule_whitelist_clear(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.whitelist = []
            await self._save_owned_rule(rule, uid)
            await cq.answer("🗑 کل لیست سفید پاکسازی شد")
            text, kbd = self._render_whitelist_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # --- Watermark Handlers ---
        @b.on_callback_query(filters.regex(r"^rwm:(.+)$"))
        async def _cb_rule_watermark_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            text, kbd = self._render_watermark_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rwm_set:(.+)$"))
        async def _cb_rule_watermark_set_prompt(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            st = self._state(uid)
            st.step = f"set_wm_{rule.id}"
            await self._persist(uid, st)
            prompt = (
                f"🖼 **تنظیم متن واتر‌مارک**\n\n"
                f"متن یا آیدی موردنظر جهت الصاق به پیام‌ها را ارسال کنید:"
            )
            kbd = self._kbd([[("❌ انصراف", f"rwm:{rule.id}")]])
            await self._safe_edit(cq, prompt, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rwm_del:(.+)$"))
        async def _cb_rule_watermark_del(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.watermark = ""
            await self._save_owned_rule(rule, uid)
            await cq.answer("🗑 واتر‌مارک حذف شد")
            text, kbd = self._render_watermark_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # --- Translation Handlers ---
        @b.on_callback_query(filters.regex(r"^rtr:(.+)$"))
        async def _cb_rule_translate_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            text, kbd = self._render_translate_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rtr_set:(.+?):(.+)$"))
        async def _cb_rule_translate_set(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw.split(":")
            rule_id, lang_code = parts[1], parts[2]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.translate_lang = "" if lang_code == "none" else lang_code
            await self._save_owned_rule(rule, uid)
            status_msg = f"زبان ترجمه: {lang_code.upper()}" if rule.translate_lang else "ترجمه غیرفعال شد"
            await cq.answer(status_msg)
            text, kbd = self._render_translate_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # --- Scheduler, Power, Affiliate, Link Buttons & AI Callbacks ---
        @b.on_callback_query(filters.regex(r"^rsch_auto:(.+)$"))
        async def _cb_rule_toggle_auto_sched(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            curr = bool(rule.auto_post_scheduler.get("enabled", False))
            rule.auto_post_scheduler["enabled"] = not curr
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"Auto Post Scheduler: {'ON' if not curr else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsch_pwr:(.+)$"))
        async def _cb_rule_toggle_power_sched(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            curr = bool(rule.power_schedule.get("enabled", False))
            rule.power_schedule["enabled"] = not curr
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"Power Schedule: {'ON' if not curr else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rafl:(.+)$"))
        async def _cb_rule_toggle_affiliate(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            curr = bool(rule.affiliate_kit.get("enabled", False))
            rule.affiliate_kit["enabled"] = not curr
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"Affiliate Kit: {'ON' if not curr else 'OFF'}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rbtn:(.+)$"))
        async def _cb_rule_link_buttons_info(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            btn_cnt = len(rule.link_buttons)
            await cq.answer(f"Link Buttons: {btn_cnt} active", show_alert=True)

        @b.on_callback_query(filters.regex(r"^rai_cfg:(.+)$"))
        async def _cb_rule_ai_cfg_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            # Cycle creativity presets: 0.2 -> 0.7 -> 1.0
            curr_c = rule.ai_creativity
            next_c = 0.7 if curr_c == 0.2 else (1.0 if curr_c == 0.7 else 0.2)
            rule.ai_creativity = next_c
            await self._save_owned_rule(rule, uid)
            await cq.answer(f"AI Creativity: {next_c}")
            text, kbd = self._render_advanced_settings(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtpc:(.+)$"))
        async def _cb_rule_set_topic(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            st = self._state(uid)
            st.step = f"set_topic_{rule.id}"
            await self._persist(uid, st)
            text = (
                f"#️⃣ **Set up Forwarding Topic**\n\n"
                f"لطفاً شناسه تاپیک مقصد (Topic ID / Message Thread ID) را به صورت عدد ارسال کنید.\n"
                f"شناسه فعلی: `{rule.topic_id or 'None'}`\n\n"
                f"برای حذف تاپیک، عدد `0` را ارسال کنید."
            )
            kbd = self._kbd([[("❌ انصراف", f"rd:{rule.id}")]])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rrn:(.+)$"))
        async def _cb_rule_rename(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            st = self._state(uid)
            st.step = f"rename_rule_{rule.id}"
            await self._persist(uid, st)
            text = (
                f"✏️ **تغییر نام تسک (Task Name)**\n\n"
                f"نام فعلی: `{rule.name or f'task_{rule.id[:8]}'}`\n\n"
                f"لطفاً نام جدید تسک را ارسال کنید:"
            )
            kbd = self._kbd([[("❌ انصراف", f"rd:{rule.id}")]])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        # ---------------- rule edit (draft & direct) handlers ----------------
        @b.on_callback_query(filters.regex(r"^re:(.+)$"))
        async def _cb_rule_edit_draft(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                st = self._state(uid)
                st.step = ""
                if isinstance(st.buffer, dict):
                    st.buffer.clear()
                await self._persist(uid, st)
                rules = await self._rules.list_by_owner(uid) if uid else []
                text, kbd = self._render_rules_list(rules, page=0)
                try:
                    await self._safe_edit(cq, text, reply_markup=kbd)
                except Exception:
                    pass
                return await cq.answer("❌ این قانون یافت نشد یا ممکن است حذف شده باشد.", show_alert=True)

            st = self._state(uid)
            st.step = "rule_editing_draft"
            draft = rule.to_draft()
            st.buffer = {
                "rule_id": rule.id,
                "version": rule.version,
                "draft": draft,
                "in_draft": True,
            }
            await self._persist(uid, st)
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer("✏️ حالت پیش‌نویس ویرایش فعال شد")

        @b.on_callback_query(filters.regex(r"^ed_save:(.+)$"))
        async def _cb_rule_draft_save(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ این قانون یافت نشد یا ممکن است حذف شده باشد.", show_alert=True)

            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                return await cq.answer("⚠️ داده‌های پیش‌نویس منقضی شده‌اند. لطفاً دوباره وارد شوید.", show_alert=True)

            src = str(draft.get("source_chat_id", "")).strip()
            dst = str(draft.get("target_chat_id", "")).strip()
            if not src or not dst:
                return await cq.answer("❌ مبدأ یا مقصد نمی‌توانند خالی باشند.", show_alert=True)
            if src == dst:
                return await cq.answer("❌ مبدأ و مقصد نمی‌توانند یکسان باشند (جلوگیری از ایجاد حلقه)!", show_alert=True)

            rule.apply_draft(draft)
            expected_version = st.buffer.get("version")
            try:
                await self._rules.update(rule, expected_version=expected_version)
            except ConcurrencyError:
                return await cq.answer("⚠️ خطا: این قانون هم‌زمان ویرایش شده است. تغییرات جدید را بررسی کنید.", show_alert=True)
            except Exception as exc:
                return await cq.answer(f"❌ خطا در ذخیره‌سازی: {exc}", show_alert=True)

            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await cq.answer("💾 تغییرات قانون با موفقیت ذخیره و اعمال شدند", show_alert=True)
            text, kbd = self._render_rule_detail(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------------------------------------------------------
        # AI Rewrite per-rule menu (pipeline stage — off by default)
        # ---------------------------------------------------------------
        def _ai_badge(draft: Dict[str, Any]) -> str:
            if not draft.get("ai_config_id"):
                return "❌ خاموش"
            pol = str(draft.get("ai_fallback_policy") or "DROP")
            return f"🟢 {pol}"

        async def _render_ai_menu(rule_id: str, draft: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
            cfg_id = str(draft.get("ai_config_id") or "")
            prompt_id = str(draft.get("prompt_id") or "")
            pol = str(draft.get("ai_fallback_policy") or "DROP")
            tmo = draft.get("ai_timeout_seconds") or 15.0
            lines = ["🤖 **بازنویسی هوشمند پیام (AI Rewrite)**", ""]
            if cfg_id:
                try:
                    cfg = await self._ai.get(cfg_id)
                    lines.append(f"🧠 پیکربندی: `{cfg.name}` [{self._provider_label(cfg.provider.value)}/{cfg.model}]" if cfg else f"🧠 پیکربندی: `{cfg_id[:8]}` (یافت نشد!)")
                except Exception:
                    lines.append(f"🧠 پیکربندی: `{cfg_id[:8]}`")
            else:
                lines.append("🧠 پیکربندی: ❌ خاموش (پیام‌ها بدون تغییر فوروارد می‌شوند)")
            if prompt_id and self._prompts:
                try:
                    tpl = await self._prompts.get_template(prompt_id)
                    lines.append(f"📝 پرامپت: {tpl.name} (v{tpl.current_version})" if tpl else f"📝 پرامپت: `{prompt_id[:8]}` (یافت نشد!)")
                except Exception:
                    pass
            lines += [
                f"⚠️ رفتار خطا: **{pol}**" + (" (ارسال متن اصلی)" if pol == "SEND_ORIGINAL" else " (عدم ارسال)" if pol == "DROP" else ""),
                f"⏱ Timeout: {tmo}s",
                "",
                "💡 این مرحله فقط وقتی پیکربندی AI انتخاب شده باشد اجرا می‌شود.",
            ]
            rows: List[List[Tuple[str, str]]] = [
                [("🧠 انتخاب پیکربندی AI", f"r_ai_cfg:{rule_id}")],
                [("📝 انتخاب پرامپت", f"r_ai_prompt:{rule_id}")],
                [("⚠️ تغییر رفتار خطا", f"r_ai_policy:{rule_id}")],
                [("🧪 تست با متن نمونه", f"r_ai_test:{rule_id}")],
                [("⬅️ بازگشت به ادیتور", f"rd:{rule_id}")],
            ]
            return "\n".join(lines), self._kbd(rows)

        @b.on_callback_query(filters.regex(r"^r_ai:(.+)$"))
        async def _cb_rule_ai_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict) or st.buffer.get("rule_id") != rule_id:
                rule = await self._owned_rule(rule_id, uid)
                if not rule:
                    return await cq.answer("❌ قانون یافت نشد یا متعلق به شما نیست", show_alert=True)
                draft = rule.to_draft()
            draft["ai_status_badge"] = _ai_badge(draft)
            text, kbd = await _render_ai_menu(rule_id, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_ai_cfg:(.+)$"))
        async def _cb_rule_ai_pick_cfg(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            st = self._state(uid)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else {}
            cfgs = await self._ai.list_by_owner(uid) if (self._ai and uid) else []
            current = str(draft.get("ai_config_id") or "")
            rows: List[List[Tuple[str, str]]] = [
                [("❌ خاموش (بدون بازنویسی)", f"r_aicset:{rule_id}:")],
            ]
            for c in cfgs:
                mark = "✅" if c.id == current else ("🟢" if c.is_enabled else "🔴")
                rows.append([(f"{mark} {c.name} [{self._provider_label(c.provider.value)}/{c.model}]", f"r_aicset:{rule_id}:{c.id}")])
            rows.append([("⬅️ بازگشت", f"r_ai:{rule_id}")])
            await self._safe_edit(cq, "🧠 انتخاب پیکربندی AI برای بازنویسی:", reply_markup=self._kbd(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_aicset:(.+?):(.*)$"))
        async def _cb_rule_ai_set_cfg(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            raw = cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)
            _, rule_id, cfg_id = raw.split(":", 2)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                return await cq.answer("⚠️ پیش‌نویس منقضی شده", show_alert=True)
            draft["ai_config_id"] = cfg_id or ""
            draft["ai_status_badge"] = _ai_badge(draft)
            await self._persist(uid, st)
            text, kbd = await _render_ai_menu(rule_id, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer("✅ پیکربندی AI ثبت شد (در Save نهایی می‌آید)")

        @b.on_callback_query(filters.regex(r"^r_ai_prompt:(.+)$"))
        async def _cb_rule_ai_pick_prompt(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else {}
            current = str(draft.get("prompt_id") or "")
            rows: List[List[Tuple[str, str]]] = [
                [("⬜ بدون پرامپت سیستمی", f"r_aipset:{rule_id}:")],
            ]
            if self._prompts:
                for t in await self._prompts.list_templates():
                    mark = "✅" if t.id == current else ("⭐" if t.is_system else "👤")
                    rows.append([(f"{mark} {t.name} (v{t.current_version})", f"r_aipset:{rule_id}:{t.id}")])
            rows.append([("⬅️ بازگشت", f"r_ai:{rule_id}")])
            await self._safe_edit(cq, "📝 انتخاب پرامپت بازنویسی (هر دو رابط بات و داشبورد همین‌ها را می‌بینند):", reply_markup=self._kbd(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_aipset:(.+?):(.*)$"))
        async def _cb_rule_ai_set_prompt(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            raw = cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)
            _, rule_id, prompt_id = raw.split(":", 2)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                return await cq.answer("⚠️ پیش‌نویس منقضی شده", show_alert=True)
            draft["prompt_id"] = prompt_id or ""
            await self._persist(uid, st)
            text, kbd = await _render_ai_menu(rule_id, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer("✅ پرامپت ثبت شد (در Save نهایی می‌آید)")

        @b.on_callback_query(filters.regex(r"^r_ai_policy:(.+)$"))
        async def _cb_rule_ai_pick_policy(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            rows: List[List[Tuple[str, str]]] = [
                [("🟢 SEND_ORIGINAL — ارسال متن اصلی", f"r_aifset:{rule_id}:SEND_ORIGINAL")],
                [("🔴 DROP — عدم ارسال (Fail Closed)", f"r_aifset:{rule_id}:DROP")],
                [("🔁 RETRY — تلاش مجدد", f"r_aifset:{rule_id}:RETRY")],
                [("🔒 QUARANTINE — قرنطینه", f"r_aifset:{rule_id}:QUARANTINE")],
                [("⬅️ بازگشت", f"r_ai:{rule_id}")],
            ]
            await self._safe_edit(cq, "⚠️ اگر AI در دسترس نباشد یا خروجی نامعتبر باشد چه اتفاقی بیفتد؟", reply_markup=self._kbd(rows))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_aifset:(.+?):(.+)$"))
        async def _cb_rule_ai_set_policy(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            raw = cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)
            _, rule_id, policy = raw.split(":", 2)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                return await cq.answer("⚠️ پیش‌نویس منقضی شده", show_alert=True)
            draft["ai_fallback_policy"] = policy
            draft["ai_status_badge"] = _ai_badge(draft)
            await self._persist(uid, st)
            text, kbd = await _render_ai_menu(rule_id, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer(f"✅ رفتار خطا: {policy}")

        @b.on_callback_query(filters.regex(r"^r_ai_test:(.+)$"))
        async def _cb_rule_ai_test(_, cq: CallbackQuery):
            """Dry-run: uses the real prompt+config pipeline, sends nothing."""
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            rule_id = (cq.data.decode() if isinstance(cq.data, (bytes, bytearray)) else str(cq.data)).split(":", 1)[1]
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else {}
            cfg_id = str(draft.get("ai_config_id") or "")
            if not cfg_id:
                return await cq.answer("⚠️ اول یک پیکربندی AI انتخاب کنید", show_alert=True)
            sample = str(draft.get("ai_test_text") or "💎 XAUUSD SIGNAL: BUY NOW @ 2655 | TP 2680 | SL 2640 https://t.me/signal #gold")
            await self._safe_edit(cq, f"⏳ در حال تست بازنویسی…\n\nمتن نمونه:\n{sample[:200]}", reply_markup=self._kbd([[("⬅️ بازگشت", f"r_ai:{rule_id}")]]))
            try:
                prompt_id = str(draft.get("prompt_id") or "")
                if prompt_id and self._prompts and self._ai_transformer:
                    result = await self._prompts.test_prompt(prompt_id, sample, cfg_id, self._ai_transformer)
                else:
                    ok, text_out = await self._ai.test_rewrite_owned(cfg_id, sample, self._scope_uid(cq.from_user.id if cq.from_user else 0))
                    result = {"success": ok, "transformed_text": text_out if ok else "", "error_message": "" if ok else text_out,
                              "execution_duration_ms": 0.0, "model_used": "", "prompt_version_used": 0}
            except Exception as exc:
                result = {"success": False, "transformed_text": "", "error_message": f"{type(exc).__name__}: {exc}",
                          "execution_duration_ms": 0.0, "model_used": "", "prompt_version_used": 0}
            if result.get("success"):
                body = (
                    "✅ **نتیجه تست بازنویسی:**\n\n"
                    f"{result.get('transformed_text') or '(خالی)'}\n\n"
                    f"⏱ {result.get('execution_duration_ms', 0):.0f}ms"
                    + (f" | 🧠 {result.get('model_used')}" if result.get("model_used") else "")
                    + (f" | 📝 v{result.get('prompt_version_used')}" if result.get("prompt_version_used") else "")
                )
            else:
                body = f"❌ **خطا در تست:**\n`{result.get('error_message') or 'unknown'}`"
            await self._safe_edit(cq, body, reply_markup=self._kbd([
                [("🔄 تست مجدد", f"r_ai_test:{rule_id}")],
                [("⬅️ بازگشت", f"r_ai:{rule_id}")],
            ]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ed_can:(.+)$"))
        async def _cb_rule_draft_cancel(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)

            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            await cq.answer("❌ ویرایش پیش‌نویس لغو شد و هیچ تغییری ذخیره نگردید")
            if rule:
                text, kbd = self._render_rule_detail(rule)
                await self._safe_edit(cq, text, reply_markup=kbd)
            else:
                rules = await self._rules.list_by_owner(uid) if uid else []
                text, kbd = self._render_rules_list(rules, page=0)
                await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_s:(.+)$"))
        async def _cb_rule_draft_source(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._owned_rule(rule_id, uid)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد یا متعلق به شما نیست", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            st.step = "rule_edit_source"
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            if "draft" not in st.buffer:
                st.buffer["draft"] = rule.to_draft()
            st.buffer["rule_id"] = rule.id
            st.buffer["version"] = rule.version
            st.buffer["in_draft"] = True
            st.buffer["edit_role"] = "source"
            st.buffer["page"] = 0
            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="source", page=0)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ed_t:(.+)$"))
        async def _cb_rule_draft_target(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            st.step = "rule_edit_target"
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            if "draft" not in st.buffer:
                st.buffer["draft"] = rule.to_draft()
            st.buffer["rule_id"] = rule.id
            st.buffer["version"] = rule.version
            st.buffer["in_draft"] = True
            st.buffer["edit_role"] = "target"
            st.buffer["page"] = 0
            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="target", page=0)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^res:(.+)$"))
        async def _cb_rule_edit_source(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            st.step = "rule_edit_source"
            st.buffer = {
                "rule_id": rule.id,
                "version": rule.version,
                "in_draft": False,
                "edit_role": "source",
                "page": 0,
            }
            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="source", page=0)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ret:(.+)$"))
        async def _cb_rule_edit_target(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            st.step = "rule_edit_target"
            st.buffer = {
                "rule_id": rule.id,
                "version": rule.version,
                "in_draft": False,
                "edit_role": "target",
                "page": 0,
            }
            chats = await self._fetch_dialogs(uid)
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="target", page=0)
            await self._safe_edit(cq, text, reply_markup=markup)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ed_m:(.+)$"))
        async def _cb_rule_draft_mode(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            cur = draft.get("forward_mode", ForwardMode.COPY_MESSAGE.value)
            cur_val = getattr(cur, "value", str(cur))
            new_mode = ForwardMode.DIRECT_FORWARD.value if cur_val == ForwardMode.COPY_MESSAGE.value else ForwardMode.COPY_MESSAGE.value
            draft["forward_mode"] = new_mode
            await self._persist(uid, st)
            await cq.answer("حالت ارسال تغییر یافت")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_a:(.+)$"))
        async def _cb_rule_draft_active(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["is_active"] = not bool(draft.get("is_active", True))
            await self._persist(uid, st)
            stat = "فعال" if draft["is_active"] else "غیرفعال"
            await cq.answer(f"وضعیت در پیش‌نویس: {stat}")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_lk:(.+)$"))
        async def _cb_rule_draft_link(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["remove_links"] = not bool(draft.get("remove_links", False))
            await self._persist(uid, st)
            stat = "حذف لینک‌ها 🚫" if draft["remove_links"] else "مجاز 🟢"
            await cq.answer(f"لینک: {stat}")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_vc:(.+)$"))
        async def _cb_rule_draft_voice(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["block_voice"] = not bool(draft.get("block_voice", False))
            await self._persist(uid, st)
            stat = "مسدود 🚫" if draft["block_voice"] else "مجاز 🟢"
            await cq.answer(f"ویس: {stat}")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_st:(.+)$"))
        async def _cb_rule_draft_sticker(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["block_stickers"] = not bool(draft.get("block_stickers", False))
            await self._persist(uid, st)
            stat = "مسدود 🚫" if draft["block_stickers"] else "مجاز 🟢"
            await cq.answer(f"استیکر: {stat}")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_em:(.+)$"))
        async def _cb_rule_draft_emoji(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["remove_emojis"] = not bool(draft.get("remove_emojis", False))
            await self._persist(uid, st)
            stat = "پاکسازی 🧹" if draft["remove_emojis"] else "حفظ 🟢"
            await cq.answer(f"ایموجی: {stat}")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_ed:(.+)$"))
        async def _cb_rule_draft_edit(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["ignore_edits"] = not bool(draft.get("ignore_edits", False))
            await self._persist(uid, st)
            stat = "نادیده ⏹" if draft["ignore_edits"] else "سینک 🔄"
            await cq.answer(f"ویرایش پیام: {stat}")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_dl:(.+)$"))
        async def _cb_rule_draft_delete(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            draft["sync_deletes"] = not bool(draft.get("sync_deletes", False))
            await self._persist(uid, st)
            stat = "سینک 🗑" if draft["sync_deletes"] else "نادیده ⏹"
            await cq.answer(f"حذف پیام: {stat}")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_mg:(.+)$"))
        async def _cb_rule_draft_media_group(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            uid = cq.from_user.id
            st = self._state(uid)
            draft = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
            if not isinstance(draft, dict):
                draft = rule.to_draft()
                st.buffer["draft"] = draft

            cur = str(draft.get("album_mode") or "album")
            cycle = {"album": "first", "first": "split", "split": "album"}
            next_mode = cycle.get(cur, "album")
            draft["album_mode"] = next_mode
            await self._persist(uid, st)
            names = {"album": "🖼 آلبوم کامل", "first": "1️⃣ فقط اولین مدیا", "split": "🔀 تفکیک پیام‌ها"}
            await cq.answer(f"چندتصویری: {names.get(next_mode, next_mode)}")
            text, kbd = self._render_draft_editor(rule, draft)
            await self._safe_edit(cq, text, reply_markup=kbd)

        # ---------------- smart routing & VIP controls ----------------
        @b.on_callback_query(filters.regex(r"^rsm:(.+)$"))
        async def _cb_smart_rule_menu(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmpth:(.+)$"))
        async def _cb_smart_toggle_route_path(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            use_mid = bool(getattr(rule, "use_intermediate", False))
            cat = getattr(rule, "message_category", "ALL")
            f_mode = getattr(rule.forward_mode, "value", str(rule.forward_mode))

            if not use_mid and f_mode != "DIRECT_FORWARD":
                # Currently Route 1 -> Switch to Route 2 (VIP Hop A -> C -> B)
                rule.use_intermediate = True
                rule.message_category = "VIP_ONLY"
                rule.forward_mode = ForwardMode.CUSTOM_HEADER_COPY
                ans = "💎 تغییر به مسیر ۲ (A ➔ C ➔ B)"
            elif use_mid:
                # Currently Route 2 -> Switch to Route 3 (Native A -> B)
                rule.use_intermediate = False
                rule.message_category = "ALL"
                rule.forward_mode = ForwardMode.DIRECT_FORWARD
                ans = "↗️ تغییر به مسیر ۳ (Native Forward A ➔ B)"
            else:
                # Currently Route 3 -> Switch to Route 1 (Direct Copy A -> B)
                rule.use_intermediate = False
                rule.message_category = "ALL"
                rule.forward_mode = ForwardMode.COPY_MESSAGE
                ans = "📋 تغییر به مسیر ۱ (Direct Copy A ➔ B)"

            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["use_intermediate"] = rule.use_intermediate
            rule.metadata["message_category"] = rule.message_category
            await self._rules.update(rule)
            await cq.answer(ans)
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmmod:(.+)$"))
        async def _cb_smart_toggle_match_mode(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)

            if not isinstance(rule.detection_criteria, dict):
                rule.detection_criteria = {}
            cur = (rule.detection_criteria.get("match_mode") or "ANY").upper()
            nxt = "ALL" if cur == "ANY" else "ANY"
            rule.detection_criteria["match_mode"] = nxt
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["detection_criteria"] = rule.detection_criteria
            await self._rules.update(rule)
            await cq.answer(f"منطق شروط: {nxt} ({'همه شروط' if nxt == 'ALL' else 'حداقل یکی'})")
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmtx:(.+)$"))
        async def _cb_smart_set_text_prompt(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            st = self._state(cq.from_user.id)
            st.step = "smart_set_keywords"
            st.buffer = {"rule_id": rule_id}
            await self._persist(cq.from_user.id, st)
            prompt = (
                "🔎 **تنظیم کلیدواژه‌های متنی (VIP Text Keywords):**\n\n"
                "کلمات یا عبارات مورد نظر را با کاما (virgool) جدا کرده و ارسال کنید.\n"
                "نمونه:\n"
                "`VIP, SIGNAL, GOLD, تحلیل اختصاصی`\n\n"
                "برای حذف تمام کلیدواژه‌ها عبارت `حذف` را ارسال کنید.\n"
                "برای انصراف دکمه زیر را لمس نمایید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmrx:(.+)$"))
        async def _cb_smart_set_regex_prompt(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            st = self._state(cq.from_user.id)
            st.step = "smart_set_regex"
            st.buffer = {"rule_id": rule_id}
            await self._persist(cq.from_user.id, st)
            prompt = (
                "🔣 **تنظیم الگوی رجکس (Regex Pattern):**\n\n"
                "الگوی Regex مورد نظر برای تطبیق متن پیام‌های VIP را ارسال کنید.\n"
                "نمونه‌ها:\n"
                "• `(?i)TP\\d+\\s+HIT`\n"
                "• `(?i)(gold|xauusd)\\s+(buy|sell)`\n\n"
                "برای حذف Regex عبارت `حذف` را ارسال کنید.\n"
                "برای انصراف دکمه زیر را لمس نمایید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmc:(.+)$"))
        async def _cb_smart_toggle_category(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            cur = getattr(rule, "message_category", "ALL")
            cycle = {"ALL": "VIP_ONLY", "VIP_ONLY": "NORMAL_ONLY", "NORMAL_ONLY": "ALL"}
            nxt = cycle.get(cur, "ALL")
            rule.message_category = nxt
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["message_category"] = nxt
            await self._rules.update(rule)
            labels = {"ALL": "🌟 همه پیام‌ها", "VIP_ONLY": "💎 فقط VIP", "NORMAL_ONLY": "👤 فقط عادی"}
            await cq.answer(f"دسته پیام: {labels.get(nxt, nxt)}")
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmt:(.+)$"))
        async def _cb_smart_toggle_intermediate(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            cur = bool(getattr(rule, "use_intermediate", False))
            rule.use_intermediate = not cur
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["use_intermediate"] = rule.use_intermediate
            await self._rules.update(rule)
            ans = "🟢 ارسال به کانال واسط فعال شد" if rule.use_intermediate else "🔴 ارسال به کانال واسط خاموش شد"
            await cq.answer(ans)
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmm:(.+)$"))
        async def _cb_smart_toggle_mode(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            cur = getattr(rule.forward_mode, "value", str(rule.forward_mode))
            cycle = {
                "COPY_MESSAGE": ForwardMode.DIRECT_FORWARD,
                "DIRECT_FORWARD": ForwardMode.CUSTOM_HEADER_COPY,
                "CUSTOM_HEADER_COPY": ForwardMode.COPY_MESSAGE,
                "REWRITE_AI": ForwardMode.COPY_MESSAGE,
            }
            nxt_mode = cycle.get(cur, ForwardMode.COPY_MESSAGE)
            rule.forward_mode = nxt_mode
            await self._rules.update(rule)
            names = {
                ForwardMode.COPY_MESSAGE: "📋 کپی بدون تگ",
                ForwardMode.DIRECT_FORWARD: "↗️ فوروارد با تگ",
                ForwardMode.CUSTOM_HEADER_COPY: "🏷 کپی با هدر اختصاصی",
            }
            await cq.answer(f"حالت: {names.get(nxt_mode, str(nxt_mode))}")
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmpu:(.+)$"))
        async def _cb_smart_priority_up(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.priority = int(getattr(rule, "priority", 10) or 10) + 10
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["priority"] = rule.priority
            await self._rules.update(rule)
            await cq.answer(f"اولویت: {rule.priority}")
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmpd:(.+)$"))
        async def _cb_smart_priority_down(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            cur = int(getattr(rule, "priority", 10) or 10)
            rule.priority = max(0, cur - 10)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["priority"] = rule.priority
            await self._rules.update(rule)
            await cq.answer(f"اولویت: {rule.priority}")
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmhd:(.+)$"))
        async def _cb_smart_clear_header(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.custom_header = ""
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["custom_header"] = ""
            await self._rules.update(rule)
            await cq.answer("🗑 هدر اختصاصی حذف شد")
            text, kbd = self._render_smart_menu(rule)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rsmw:(.+)$"))
        async def _cb_smart_set_intermediate_prompt(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            st = self._state(cq.from_user.id)
            st.step = "smart_set_intermediate"
            st.buffer = {"rule_id": rule_id}
            await self._persist(cq.from_user.id, st)
            prompt = (
                "🔄 **تنظیم کانال واسط VIP (Intermediate Channel):**\n\n"
                "لطفاً شناسه عددی، یوزرنیم، یا لینک دعوت کانال واسط را ارسال کنید:\n"
                "نمونه‌ها:\n"
                "• `-1001234567890`\n"
                "• `@my_intermediate_channel`\n"
                "• `https://t.me/+uVg1efOFMEc2NTNk`\n\n"
                "⚠️ *نکته ضد لوپ: کانال واسط نباید با کانال مبدأ یا مقصد یکسان باشد.*\n\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmh:(.+)$"))
        async def _cb_smart_set_header_prompt(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            st = self._state(cq.from_user.id)
            st.step = "smart_set_header"
            st.buffer = {"rule_id": rule_id}
            await self._persist(cq.from_user.id, st)
            prompt = (
                "🏷 **تنظیم هدر اختصاصی کپی (Custom Header):**\n\n"
                "متنی که می‌خواهید به عنوان هدر ویژه در ابتدای پیام‌های کپی‌شده قرار گیرد را ارسال کنید.\n"
                "نمونه:\n"
                "💎 `VIP Alert | تحلیل طلایی اختصاصی`\n\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rsmtst:(.+)$"))
        async def _cb_smart_test_prompt(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            st = self._state(cq.from_user.id)
            st.step = "smart_test_rule"
            st.buffer = {"rule_id": rule_id}
            await self._persist(cq.from_user.id, st)
            prompt = (
                "🧪 **شبیه‌ساز و تست زنده قانون (Dry Run / Test):**\n\n"
                "یک متن یا پیام تستی ارسال کنید تا موتور هوشمند بدون ایجاد تغییر یا ارسال واقعی، ارزیابی کند:\n"
                "1. تشخیص نوع پیام (VIP یا کاربر عادی)\n"
                "2. تطبیق با قانون و اولویت مسیر\n"
                "3. مسیر انتقال (مستقیم یا کانال واسط)\n"
                "4. متن پیش‌نمایش به همراه هدرها و فیلترها\n\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
            await cq.answer()

        # ---------------- filter add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["filter_add"] + "$"))
        async def _cb_filter_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "filter_name"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, 
                self._t("ui_filter_name"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["filter_del"] + ":"))
        async def _cb_filter_del(_, cq: CallbackQuery):
            fid = cq.data.split(":", 1)[1]
            # v12.1: IDOR guard — a non-admin can only delete their own filter.
            fuid = cq.from_user.id if cq.from_user else 0
            try:
                await self._filters.delete_owned(fid, None if self._is_admin(fuid) else fuid)
            except PermissionError:
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await self._safe_edit(cq, self._t("ui_done"), reply_markup=self._filters_menu())
            await cq.answer()

        # ---------------- ai add flow ----------------
        @b.on_callback_query(filters.regex("^" + CB["ai_add"] + "$"))
        async def _cb_ai_add(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_name"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, self._t("ui_ai_name"), reply_markup=self._cancel_kbd())
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
            st.step = "ai_base_url"
            from core.infrastructure.ai.providers import OpenAICompatibleProvider
            default_url = OpenAICompatibleProvider.default_base_urls.get(provider, "")
            st.buffer["default_base_url"] = default_url
            prompt_text = (
                f"🌐 **تنظیم آدرس سرور / هاست ({self._provider_label(provider)}):**\n\n"
                f"آدرس پیش‌فرض:\n`{default_url or 'ندارد (نیازمند آدرس دستی)'}`\n\n"
                "✍️ اگر مایلید از هاست اختصاصی یا پورت دیگری استفاده کنید، آدرس آن را ارسال نمایید:\n"
                "(مثال: `http://sub.legoten.com:4455/v1`)\n\n"
                "یا برای استفاده از آدرس پیش‌فرض دکمه زیر را لمس کنید:"
            )
            kbd = self._kbd([
                [("✅ استفاده از پیش‌فرض (یا بعدی)", "ai_url_default")],
                [("❌ " + self._t("ui_cancel"), CB["cancel"])],
            ])
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, prompt_text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex("^ai_url_default$"))
        async def _cb_ai_url_default(_, cq: CallbackQuery):
            st = self._state(cq.from_user.id)
            if st.step != "ai_base_url":
                return await cq.answer()
            st.ai_base_url = st.buffer.get("default_base_url", "")
            st.step = "ai_model"
            kb = self._model_picker(st, st.ai_provider)
            await self._persist(cq.from_user.id, st)
            msg_text = (
                self._t("ui_ai_model")
                + "\n(از لیست زیر انتخاب کنید یا نام مدل دلخواه مانند `coding` را تایپ کنید):"
            )
            if kb is not None:
                await self._safe_edit(cq, msg_text, reply_markup=kb)
            else:
                await self._safe_edit(cq, msg_text, reply_markup=self._cancel_kbd())
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
            await self._safe_edit(cq, self._t("ui_ai_key"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^aid:(.+)"))
        async def _cb_ai_detail(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                await cq.answer("❌ این پیکربندی یافت نشد.", show_alert=True)
                _d_uid = self._scope_uid(cq.from_user.id if cq.from_user else 0)
                cfgs = await (self._ai.list_all() if _d_uid is None else self._ai.list_by_owner(cq.from_user.id))
                return await self._safe_edit(cq, self._t("ui_ai_title"), reply_markup=self._ai_menu(cfgs))
            text, kbd = self._render_ai_detail(cfg)
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_hc:(.+)"))
        async def _cb_ai_hc(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            await cq.answer("⏳ در حال بررسی سلامت اتصال و سنجش پینگ...", show_alert=False)
            ok, reply_or_err, latency = await self._ai.health_check_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            health_info = {
                "ok": ok,
                "latency_ms": latency,
                "reply": reply_or_err if ok else "",
                "error": reply_or_err if not ok else "",
            }
            text, kbd = self._render_ai_detail(cfg, health_info=health_info)
            try:
                await self._safe_edit(cq, text, reply_markup=kbd)
            except Exception:
                pass

        @b.on_callback_query(filters.regex(r"^(?:ai_sample|" + CB["ai_test"] + r"):(.+)"))
        async def _cb_ai_sample(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            await cq.answer("⏳ در حال ارسال متن نمونه جهت بازنویسی...", show_alert=False)
            sample_text = "سلام! این یک پیام آزمایشی جهت سنجش کارکرد هوش مصنوعی در فوروارد تلگرام است."
            ok, reply = await self._ai.test_rewrite_owned(aid, sample_text, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if ok:
                body = f"✅ **تست بازنویسی با موفقیت انجام شد:**\n\n📝 **پاسخ مدل ({cfg.model}):**\n`{reply[:1000]}`"
            else:
                body = f"❌ **خطا در بازنویسی:**\n\n`{reply[:1000]}`"
            kbd = self._kbd([[("⬅️ بازگشت به پیکربندی", f"aid:{aid}")]])
            await self._safe_edit(cq, body, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ai_tog:(.+)"))
        async def _cb_ai_toggle(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            cfg.is_enabled = not cfg.is_enabled
            try:
                await self._ai.update_owned(cfg, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            except PermissionError:
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            status_text = "فعال شد 🟢" if cfg.is_enabled else "غیرفعال شد 🔴"
            await cq.answer(f"وضعیت با موفقیت {status_text}", show_alert=False)
            text, kbd = self._render_ai_detail(cfg)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ai_del_ask:(.+)"))
        async def _cb_ai_del_ask(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            text = (
                f"⚠️ **آیا از حذف این پیکربندی هوش مصنوعی اطمینان دارید؟**\n\n"
                f"🏷 نام: `{cfg.name}`\n"
                f"🆔 شناسه: `{cfg.id[:8]}`"
            )
            kbd = self._kbd([
                [("🗑 بله، حذف شود", f"ai_del_confirm:{aid}")],
                [(self._t("ui_cancel_back"), f"aid:{aid}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^(?:ai_del_confirm|" + CB["ai_del"] + r"):(.+)"))
        async def _cb_ai_del_confirm(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            # v12.1: IDOR guard — a non-admin can only delete their own config.
            auid = cq.from_user.id if cq.from_user else 0
            try:
                await self._ai.delete_owned(aid, None if self._is_admin(auid) else auid)
            except PermissionError:
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await cq.answer("✅ پیکربندی حذف شد.", show_alert=True)
            cfgs = await (self._ai.list_all() if self._is_admin(auid) else self._ai.list_by_owner(auid))
            lines = [self._t("ui_ai_title"), "", "✅ پیکربندی با موفقیت حذف گردید."]
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._ai_menu(cfgs))

        @b.on_callback_query(filters.regex(r"^ai_eh:(.+)"))
        async def _cb_ai_edit_host(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_edit_host"
            st.buffer["ai_edit_id"] = aid
            await self._persist(cq.from_user.id, st)
            current = cfg.base_url or "(پیش‌فرض)"
            text = (
                f"🌐 **تغییر آدرس سرور / هاست:**\n\n"
                f"آدرس فعلی: `{current}`\n\n"
                "✍️ لطفاً آدرس هاست جدید را ارسال کنید:\n"
                "(مثال: `http://sub.legoten.com:4455/v1`)\n"
                "یا برای بازنشانی به حالت پیش‌فرض عبارت `default` را بفرستید:"
            )
            kbd = self._kbd([
                [("❌ " + self._t("ui_cancel"), f"aid:{aid}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_em:(.+)"))
        async def _cb_ai_edit_model(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_edit_model"
            st.buffer["ai_edit_id"] = aid
            await self._persist(cq.from_user.id, st)
            text = (
                f"🧠 **تغییر مدل هوش مصنوعی:**\n\n"
                f"مدل فعلی: `{cfg.model}`\n\n"
                "✍️ لطفاً نام مدل جدید را تایپ و ارسال کنید:\n"
                "(مثال: `coding` یا `gpt-4o-mini`)"
            )
            kbd = self._kbd([
                [("❌ " + self._t("ui_cancel"), f"aid:{aid}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_ek:(.+)"))
        async def _cb_ai_edit_key(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get_owned(aid, self._scope_uid(cq.from_user.id if cq.from_user else 0))
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "ai_edit_key"
            st.buffer["ai_edit_id"] = aid
            await self._persist(cq.from_user.id, st)
            text = (
                f"🔑 **تغییر کلید API:**\n\n"
                "✍️ لطفاً کلید دسترسی جدید را ارسال کنید:\n"
                "(پیام حاوی کلید پس از دریافت جهت امنیت حذف خواهد شد)"
            )
            kbd = self._kbd([
                [("❌ " + self._t("ui_cancel"), f"aid:{aid}")],
            ])
            await self._safe_edit(cq, text, reply_markup=kbd)
            await cq.answer()

        # ---------------- PV Responder (AI PV Assistant) ----------------
        @b.on_callback_query(filters.regex(r"^pv:menu$"))
        async def _cb_pv_menu(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_menu(uid, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:toggle$"))
        async def _cb_pv_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer("❌ شناسه کاربر نامعتبر است.", show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer("❌ ماژول در دسترس نیست.", show_alert=True)
            cfg = resp.get_config_for_user(uid)
            cfg.enabled = not cfg.enabled
            resp.save_config_for_user(uid, cfg)
            status_msg = "پاسخگوی پی‌وی فعال شد 🟢" if cfg.enabled else "پاسخگوی پی‌وی غیرفعال شد 🔴"
            await cq.answer(status_msg)
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_menu(uid, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:persona$"))
        async def _cb_pv_persona(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_persona_menu(lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:set_p:(.+)$"))
        async def _cb_pv_set_persona(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer("❌ شناسه کاربر نامعتبر است.", show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer("❌ ماژول در دسترس نیست.", show_alert=True)
            cfg = resp.get_config_for_user(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            p_type = raw.split(":", 2)[2]
            if p_type == "casual":
                from core.infrastructure.telegram.pv_responder import DEFAULT_PERSONA
                cfg.persona_prompt = DEFAULT_PERSONA
                resp.save_config_for_user(uid, cfg)
                await cq.answer("✅ لحن خودمانی و جذاب تنظیم شد.")
                lang = await self._get_user_lang(uid)
                text, kbd = self._render_pv_menu(uid, lang=lang)
                await self._safe_edit(cq, text, reply_markup=kbd)
            elif p_type == "business":
                cfg.persona_prompt = (
                    "شما صاحب این اکانت شخصی تلگرام هستید. با لحنی کاملاً مؤدبانه، حرفه‌ای، "
                    "محترمانه و در عین حال روان و انسانی به زبان فارسی به پیام‌ها پاسخ دهید. "
                    "هرگز اشاره نکن که هوش مصنوعی یا ربات هستی. پاسخ‌ها موجز و صریح باشند."
                )
                resp.save_config_for_user(uid, cfg)
                await cq.answer("✅ لحن کاری و محترمانه تنظیم شد.")
                lang = await self._get_user_lang(uid)
                text, kbd = self._render_pv_menu(uid, lang=lang)
                await self._safe_edit(cq, text, reply_markup=kbd)
            elif p_type == "short":
                cfg.persona_prompt = (
                    "شما صاحب اکانت تلگرام هستید. پیام‌ها را بسیار کوتاه، خودمانی، "
                    "سریع و حداکثر در یک یا دو جمله جذاب و دوستانه پاسخ بده. "
                    "اصلاً شبیه ربات نباش و هرگز نگو بات هستی."
                )
                resp.save_config_for_user(uid, cfg)
                await cq.answer("✅ لحن کوتاه و سریع تنظیم شد.")
                lang = await self._get_user_lang(uid)
                text, kbd = self._render_pv_menu(uid, lang=lang)
                await self._safe_edit(cq, text, reply_markup=kbd)
            elif p_type == "custom":
                st = self._state(uid)
                st.step = "pv_persona_input"
                await self._persist(uid, st)
                prompt_text = (
                    "✍️ **تنظیم پرامپت اختصاصی برای هوش مصنوعی پی‌وی:**\n\n"
                    "لطفاً دستورالعمل یا شخصیت دلخواه خود را به زبان فارسی ارسال کنید.\n"
                    "مثال: «مثل یک دوست صمیمی و پرانرژی حرف بزن و بگو فعلاً بیرون هستم و شب پیام میدم»"
                )
                await self._safe_edit(cq, prompt_text, reply_markup=self._kbd([[("❌ انصراف", "pv:persona")]]))
                await cq.answer()

        @b.on_callback_query(filters.regex(r"^pv:delays$"))
        async def _cb_pv_delays(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            await cq.answer()
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_delays_menu(lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:set_d:(.+)$"))
        async def _cb_pv_set_delay(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer("❌ شناسه کاربر نامعتبر است.", show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer("❌ ماژول در دسترس نیست.", show_alert=True)
            cfg = resp.get_config_for_user(uid)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            d_type = raw.split(":", 2)[2]
            if d_type == "fast":
                cfg.typing_delay_min = 1.5
                cfg.typing_delay_max = 3.0
            elif d_type == "normal":
                cfg.typing_delay_min = 2.0
                cfg.typing_delay_max = 4.5
            elif d_type == "slow":
                cfg.typing_delay_min = 3.5
                cfg.typing_delay_max = 6.5
            resp.save_config_for_user(uid, cfg)
            await cq.answer("✅ زمان‌بندی تأخیر با موفقیت اعمال شد.")
            lang = await self._get_user_lang(uid)
            text, kbd = self._render_pv_menu(uid, lang=lang)
            await self._safe_edit(cq, text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:test$"))
        async def _cb_pv_test(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            if not uid:
                return await cq.answer("❌ شناسه کاربر نامعتبر است.", show_alert=True)
            st = self._state(uid)
            st.step = "pv_test_input"
            await self._persist(uid, st)
            prompt = (
                "🧪 **تست شبیه‌سازی پاسخگوی هوشمند پی‌وی:**\n\n"
                "یک پیام نمونه که ممکن است مخاطبی برای شما بفرستد ارسال کنید "
                "(مثلاً: «سلام چطوری؟ کجایی؟» یا «سلام فایل پروژه آماده شد؟»)\n"
                "تا ببینید هوش مصنوعی با شخصیت انتخابی شما چگونه پاسخ می‌دهد."
            )
            await self._safe_edit(cq, prompt, reply_markup=self._kbd([[("❌ انصراف", "pv:menu")]]))
            await cq.answer()

        # ---------------- backup ----------------
        @b.on_callback_query(filters.regex("^" + CB["backup"] + "$"))
        async def _cb_backup(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                await self._safe_edit(cq, 
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

        # ---------------- migration backup/restore -----------------------------
        # Full-installation snapshot: everything needed to move this box to
        # another server. The bundle carries live credentials, so the encrypted
        # variant is the safe one to send over Telegram.
        async def _do_migration_export(cq: CallbackQuery, passphrase: str):
            if self._backup is None:
                await self._safe_edit(cq, self._t("ui_migrate_unavailable"),
                                      reply_markup=self._migrate_menu())
                return await cq.answer()
            try:
                await self._safe_edit(cq, self._t("ui_migrate_packing"),
                                      reply_markup=None)
                owner = cq.from_user.id if not self._is_admin(cq.from_user.id) else None
                doc = await self._backup.create_snapshot(owner)
                text = serialize_bundle(doc)
                fname = "atf-backup.json"
                if passphrase:
                    text = encrypt_bundle(text, passphrase)
                    fname = "atf-backup.enc"
                path = os.path.join(tempfile.gettempdir(), f"atf_{cq.from_user.id}_{fname}")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                counts = doc.get("counts") or {}
                lines = [self._t("ui_migrate_exported"),
                         "─────────────────────"]
                for k in ("sessions", "forward_rules", "filter_rules",
                          "ai_configs", "prompt_templates", "api_credentials"):
                    if counts.get(k):
                        lines.append(f"• {k}: {counts[k]}")
                if passphrase:
                    lines.insert(1, "🔒 AES-GCM " + self._t("ui_migrate_encrypted"))
                await self.bot.send_document(
                    chat_id=cq.message.chat.id, document=path,
                    caption="\n".join(lines),
                )
                os.remove(path)
                try:
                    self._log.info("bot", "backup", "migration snapshot exported")
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("migration export failed")
                await self._record_error("bot", exc, user_id=cq.from_user.id)
                await cq.message.reply_text(
                    self._t("ui_migrate_fail", error=tg_detail(exc)))
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["migrate"] + "$"))
        async def _cb_migrate(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await self._safe_edit(cq, self._t("ui_migrate_title"),
                                  reply_markup=self._migrate_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["migrate_export"] + "$"))
        async def _cb_migrate_export(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await cq.answer()
            await _do_migration_export(cq, "")

        @b.on_callback_query(filters.regex("^" + CB["migrate_export_enc"] + "$"))
        async def _cb_migrate_export_enc(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await cq.answer()
            st = self._state(cq.from_user.id)
            st.step = "migrate_passphrase"
            st.buffer["migrate_chat_id"] = cq.message.chat.id
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, self._t("ui_migrate_ask_passphrase"),
                                  reply_markup=self._kbd(
                                      [[("✖ " + self._t("ui_cancel"), CB["cancel"])]]))

        @b.on_callback_query(filters.regex("^" + CB["migrate_import"] + "$"))
        async def _cb_migrate_import(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await cq.answer()
            st = self._state(cq.from_user.id)
            st.step = "migrate_await_file"
            st.buffer["migrate_overwrite"] = False
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(cq, self._t("ui_migrate_await_file"),
                                  reply_markup=self._kbd(
                                      [[("⚡ " + self._t("ui_migrate_overwrite"), CB["migrate_overwrite"])],
                                       [("✖ " + self._t("ui_cancel"), CB["cancel"])]]))

        @b.on_callback_query(filters.regex("^" + CB["migrate_overwrite"] + "$"))
        async def _cb_migrate_overwrite(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await cq.answer()
            st = self._state(cq.from_user.id)
            if st.step == "migrate_await_file":
                st.buffer["migrate_overwrite"] = not st.buffer.get("migrate_overwrite", False)
                await self._persist(cq.from_user.id, st)
                await self._safe_edit(
                    cq,
                    self._t("ui_migrate_await_file")
                    + ("\n\n⚠️ " + self._t("ui_migrate_overwrite_on")
                       if st.buffer.get("migrate_overwrite") else ""),
                    reply_markup=self._kbd(
                        [[("⚡ " + self._t("ui_migrate_overwrite"), CB["migrate_overwrite"])],
                         [("✖ " + self._t("ui_cancel"), CB["cancel"])]]))

        @b.on_callback_query(filters.regex("^" + CB["migrate_help"] + "$"))
        async def _cb_migrate_help(_, cq: CallbackQuery):
            await self._safe_edit(cq, self._t("ui_migrate_help_text"),
                                  reply_markup=self._migrate_menu())
            await cq.answer()

        # ---------------- logs panel ----------------
        @b.on_callback_query(filters.regex("^" + CB["logs"] + "$"))
        async def _cb_logs(_, cq: CallbackQuery):
            await self._safe_edit(cq, self._t("ui_logs_title"), reply_markup=self._logs_menu())
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
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._logs_menu())
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
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._logs_menu())
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
            await self._safe_edit(cq, "\n".join(lines), reply_markup=self._logs_menu())
            await cq.answer()

        # ---------------- debug ----------------
        @b.on_callback_query(filters.regex("^" + CB["debug"] + "$"))
        async def _cb_debug(_, cq: CallbackQuery):
            await self._safe_edit(cq, 
                await self._debug_body(), reply_markup=self._debug_menu())
            await cq.answer()

        # ---------------- web dashboard URL configuration ----------------
        @b.on_callback_query(filters.regex("^" + CB["weburl"] + "$"))
        async def _cb_weburl(_, cq: CallbackQuery):
            await self._safe_edit(
                cq, self._weburl_body(), reply_markup=self._weburl_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["weburl_set"] + "$"))
        async def _cb_weburl_set(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "weburl_input"
            await self._persist(cq.from_user.id, st)
            await self._safe_edit(
                cq, self._t("ui_weburl_prompt"), reply_markup=self._cancel_kbd())
            await cq.answer()

        @b.on_callback_query(filters.regex("^weburl_clear$"))
        async def _cb_weburl_clear(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            if self._settings is not None:
                try:
                    await self._settings.set("web_url", "")
                except Exception as exc:
                    logger.warning("clear web_url failed: %s", exc)
            self._web_url = ""
            await self._safe_edit(
                cq, self._t("ui_weburl_cleared"), reply_markup=self._main_menu())
            await cq.answer()

        # ---------------- free-text FSM router ----------------
        @b.on_message(filters.private & filters.text & ~filters.command(["start", "menu", "cancel", "connect", "rules", "panel", "settings", "profile", "forward"]), group=-1)
        async def _text(_, message: Message):
            # Every step of every flow is traced: if the bot ever goes quiet
            # again, the log shows exactly which step died and why.
            uid = message.from_user.id if message.from_user else 0
            text = (message.text or "").strip()
            preview = (text[:40] + "...") if len(text) > 40 else text
            step = ""
            try:
                step = self._state(uid).step or ""
            except Exception as exc:
                logger.warning("Could not read state for %s: %s", uid, exc)

            logger.info("ProBotUI._text: uid=%s step=%r text=%r", uid, step, preview)

            if not step:
                # Not in an active UI flow — pass to BotManager in group 0
                try:
                    message.continue_propagation()
                except Exception:
                    pass
                return

            try:
                self._log.info(
                    "bot", "fsm",
                    f"text in from {uid} step={step}: {preview!r}")
            except Exception:
                pass
            try:
                await self._route_text(message)
                try:
                    message.stop_propagation()
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("ProBotUI._route_text failed for %s step=%s: %s", uid, step, exc)
                try:
                    self._log.error(
                        "bot", "fsm",
                        f"route_text failed for {uid} step={step}: {type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass
                try:
                    await message.reply_text(
                        self._t("ui_internal_error"),
                        reply_markup=self._cancel_kbd())
                except Exception:
                    pass

        @b.on_message(filters.private & filters.document, group=-2)
        async def _document(_, message: Message):
            """Receive a migration bundle file and restore it."""
            uid = message.from_user.id if message.from_user else 0
            step = ""
            try:
                step = self._state(uid).step or ""
            except Exception:
                pass
            if step != "migrate_await_file":
                try:
                    message.continue_propagation()
                except Exception:
                    pass
                return
            await self._route_document(message)

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

    # -------------------------------------------------------------- web access
    @staticmethod
    def _detect_public_host() -> str:
        """Best-effort public host for the web dashboard.

        Used only when the operator has not configured ``web_url`` explicitly.
        ``localhost`` is useless in Telegram: the link is opened on the user's
        own device, so it must point at a reachable address of *this* server.
        Returns the first global (non-loopback / non-private) address seen on
        the host's interfaces; falls back to ``localhost`` only if none found.
        """
        try:
            import ipaddress
            import socket

            seen: list[str] = []
            for info in socket.getaddrinfo(socket.gethostname(), None):
                addr = str(info[4][0])
                if addr in seen:
                    continue
                seen.append(addr)
                try:
                    ip = ipaddress.ip_address(addr)
                except ValueError:
                    continue
                if not ip.is_global or ip.is_loopback or ip.is_link_local:
                    continue
                if ip.version == 6:
                    return f"[{ip}]"
                return str(ip)
        except Exception:
            pass
        return "localhost"

    def _web_base_url(self) -> str:
        if self._web_url:
            return self._web_url
        return f"http://{self._detect_public_host()}:8088"

    def _weburl_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [(("✏️ " + self._t("ui_weburl_set")), CB["weburl_set"])],
            [(("🗑 " + self._t("ui_weburl_clear")), "weburl_clear")],
            [(("⬅️ " + self._t("ui_back")), CB["main"])],
        ])

    def _weburl_body(self) -> str:
        current = self._web_url or self._t("ui_weburl_none")
        return self._t("ui_weburl_body").replace("{cur}", current)

    async def _step_rename_rule(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("❌ قانون یافت نشد.", reply_markup=self._main_menu())
        rule.name = text.strip()
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_rule_detail(rule)
        await message.reply_text(f"✅ نام تسک به `{rule.name}` تغییر یافت.", reply_markup=kbd)

    async def _step_set_topic(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("❌ قانون یافت نشد.", reply_markup=self._main_menu())
        clean_txt = text.strip().lstrip("#")
        try:
            tid = int(clean_txt)
            rule.topic_id = tid if tid > 0 else 0
        except ValueError:
            return await message.reply_text("⚠️ لطفاً فقط یک عدد معتبر ارسال کنید (یا 0 برای غیرفعال‌سازی).")
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_rule_detail(rule)
        await message.reply_text(f"✅ تاپیک فوروارد روی `#{rule.topic_id or 'None'}` تنظیم شد.", reply_markup=kbd)

    async def _step_set_delay(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("❌ قانون یافت نشد.", reply_markup=self._main_menu())
        try:
            sec = max(0.0, float(text.strip()))
            rule.delay_seconds = sec
        except ValueError:
            return await message.reply_text("⚠️ لطفاً فقط یک عدد معتبر (برحسب ثانیه) ارسال کنید (مثال: `5` یا `0`).")
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_delay_menu(rule)
        await message.reply_text(f"✅ میزان تأخیر فوروارد روی `{rule.delay_seconds:.0f}` ثانیه تنظیم شد.", reply_markup=kbd)

    async def _step_add_bl(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("❌ قانون یافت نشد.", reply_markup=self._main_menu())
        new_words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        bl = list(rule.blacklist or [])
        for w in new_words:
            if w not in bl:
                bl.append(w)
        rule.blacklist = bl
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_blacklist_menu(rule)
        await message.reply_text(f"✅ تعداد {len(new_words)} کلمه به لیست سیاه افزوده شد.", reply_markup=kbd)

    async def _step_add_wl(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("❌ قانون یافت نشد.", reply_markup=self._main_menu())
        new_words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        wl = list(rule.whitelist or [])
        for w in new_words:
            if w not in wl:
                wl.append(w)
        rule.whitelist = wl
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_whitelist_menu(rule)
        await message.reply_text(f"✅ تعداد {len(new_words)} کلمه به لیست سفید افزوده شد.", reply_markup=kbd)

    async def _step_set_wm(self, message: Message, st: "UiState", text: str, rule_id: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("❌ قانون یافت نشد.", reply_markup=self._main_menu())
        rule.watermark = text.strip()
        await self._save_owned_rule(rule, uid)
        st.step = ""
        await self._persist(uid, st)
        card_text, kbd = self._render_watermark_menu(rule)
        await message.reply_text(f"✅ واتر‌مارک تنظیم شد: `{rule.watermark}`", reply_markup=kbd)

    async def _step_weburl_input(
        self, message: Message, st: "UiState", text: str
    ) -> None:
        lang = self._lang_for(st)
        url = (text or "").strip()
        st.step = ""
        await self._persist(message.from_user.id, st)
        if not (url.startswith("http://") or url.startswith("https://")) or " " in url:
            return await message.reply_text(
                self._t("ui_weburl_invalid", lang=lang), reply_markup=self._cancel_kbd())
        url = url.rstrip("/")
        if self._settings is not None:
            try:
                await self._settings.set("web_url", url)
            except Exception as exc:
                logger.warning("save web_url failed: %s", exc)
                return await message.reply_text(
                    self._t("ui_internal_error", lang=lang), reply_markup=self._main_menu())
        self._web_url = url
        await message.reply_text(
            self._t("ui_weburl_saved", lang=lang).replace("{url}", url),
            reply_markup=self._main_menu())

    async def _step_client_api_id(self, message: Message, st: "UiState", text: str) -> None:
        clean = (text or "").strip()
        if not clean.isdigit() or int(clean) <= 0:
            return await message.reply_text(
                "❌ API ID نامعتبر است. لطفاً فقط یک عدد مثبت وارد نمایید:"
                if self._resolve_lang(uid) == "fa"
                else "❌ Invalid API ID. Please enter a valid positive number:",
                reply_markup=self._cancel_kbd(),
            )
        st.buffer["client_api_id"] = int(clean)
        st.step = "client_api_hash"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            "🔑 اکنون **API Hash** (رشته ۳۲ حرفی) خود را ارسال نمایید:"
            if self._resolve_lang(uid) == "fa"
            else "🔑 Now enter your 32-character **API Hash**:",
            reply_markup=self._cancel_kbd(),
        )

    async def _step_client_api_hash(self, message: Message, st: "UiState", text: str) -> None:
        api_hash = (text or "").strip()
        uid = message.from_user.id
        if len(api_hash) != 32:
            return await message.reply_text(
                "❌ API Hash باید دقیقاً یک رشته ۳۲ کاراکتری هگزادسیمال باشد. لطفاً مجدداً ارسال کنید:"
                if self._resolve_lang(uid) == "fa"
                else "❌ API Hash must be exactly 32 hexadecimal characters. Please try again:",
                reply_markup=self._cancel_kbd(),
            )
        api_id = int(st.buffer.get("client_api_id", 0))
        st.step = ""
        st.buffer.clear()
        await self._persist(uid, st)

        if self._credentials:
            try:
                owned = await self._credentials.list_by_owner(uid)
                for c in owned:
                    await self._credentials.delete(c.id)
                await self._credentials.create(
                    label=f"user_{uid}_cred",
                    api_id=api_id,
                    api_hash=api_hash,
                    is_default=True,
                    owner_user_id=uid,
                )
            except Exception as exc:
                logger.error("Failed saving user credentials: %s", exc)

        if self._pool:
            try:
                self._pool.register_credentials(f"user_{uid}", api_id, api_hash)
            except Exception:
                pass

        await message.reply_text(
            "✅ **کلیدهای اختصاصی شما با موفقیت ذخیره و فعال گردید.**\n"
            "از این پس نشست‌های جدید شما با کلیدهای شخصی خودتان متصل خواهند شد."
            if self._resolve_lang(uid) == "fa"
            else "✅ **Your personal Telegram API credentials were saved successfully.**",
            reply_markup=self._client_settings_menu(uid),
        )

    async def _step_admin_broadcast_text(self, message: Message, st: "UiState", text: str) -> None:
        uid = message.from_user.id
        if not self._is_admin(uid):
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("⛔ دسترسی غیرمجاز.")

        bcast_text = (message.text or "").strip()
        if not bcast_text:
            return await message.reply_text("❌ متن پیام نمی‌تواند خالی باشد.", reply_markup=self._cancel_kbd())

        st.buffer["bcast_text"] = bcast_text
        st.step = ""
        await self._persist(uid, st)

        count = 0
        if self._users:
            try:
                count = await self._users.count_all()
            except Exception:
                pass

        preview_text = (
            f"📢 **پیش‌نمایش پیام همگانی:**\n\n"
            f"{bcast_text}\n\n"
            f"👥 تعداد کل دریافت‌کنندگان واجد شرایط: `{count}`\n"
            f"⚠️ پیام به صورت پس‌زمینه و با رعایت محدودیت Rate Limit ارسال خواهد شد."
        )
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تایید و ارسال همگانی", callback_data=CB["adm_bcast_confirm"])],
            [InlineKeyboardButton("❌ انصراف", callback_data=CB["adm_bcast_cancel"])],
        ])
        await message.reply_text(preview_text, reply_markup=kbd)

    async def _step_admin_forcesub_channel(self, message: Message, st: "UiState", text: str) -> None:
        uid = message.from_user.id
        if not self._is_admin(uid):
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("⛔ دسترسی غیرمجاز.")

        channel = (text or "").strip()
        if not channel.startswith("@") and not channel.startswith("-100"):
            channel = "@" + channel.lstrip("@")

        now = int(time.time())
        if self._db:
            try:
                self._db.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('force_sub_channel', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (channel, now),
                )
                self._db.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('force_sub_enabled', '1', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (now,),
                )
            except Exception as exc:
                logger.error("Failed saving force_sub_channel: %s", exc)

        st.step = ""
        await self._persist(uid, st)
        text_panel, kbd_panel = await self._render_admin_panel(uid)
        await message.reply_text(f"✅ کانال عضویت اجباری با موفقیت به `{channel}` تنظیم و فعال شد.", reply_markup=kbd_panel)

    async def _step_admin_add_id(self, message: Message, st: "UiState", text: str) -> None:
        uid = message.from_user.id
        if not self._is_admin(uid):
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text("⛔ دسترسی غیرمجاز.")

        clean = (text or "").strip()
        if not clean.isdigit() or int(clean) <= 0:
            return await message.reply_text("❌ شناسه عددی نامعتبر است. فقط یک عدد مثبت وارد نمایید:", reply_markup=self._cancel_kbd())

        target_uid = int(clean)
        st.step = ""
        await self._persist(uid, st)

        if self._admin_service:
            try:
                self._admin_service.add_admin(target_uid, added_by=uid)
            except Exception as exc:
                return await message.reply_text(f"❌ خطا در افزودن مدیر: {exc}")

        text_panel, kbd_panel = await self._render_admin_panel(uid)
        await message.reply_text(f"✅ دسترسی مدیریت برای کاربر `{target_uid}` فعال گردید.", reply_markup=kbd_panel)

    async def _run_broadcast_job(self, bcast_id: str, admin_uid: int, text: str) -> None:
        recipients = []
        if self._db:
            try:
                rows = self._db.query_all("SELECT user_id FROM users WHERE status != 'suspended'")
                for r in rows or []:
                    recipients.append(int(r["user_id"]))
            except Exception as exc:
                logger.error("Failed querying users for broadcast: %s", exc)

        sent = 0
        failed = 0
        for uid in recipients:
            try:
                await self.bot.send_message(uid, text)
                sent += 1
            except Exception as exc:
                failed += 1
                exc_str = str(exc).lower()
                if "flood" in exc_str:
                    await asyncio.sleep(2.0)
            await asyncio.sleep(0.04)

        if self._db:
            try:
                self._db.execute(
                    "UPDATE broadcast_queue SET status='completed', sent_count=?, failed_count=?, updated_at=? WHERE id=?",
                    (sent, failed, int(time.time()), bcast_id),
                )
            except Exception:
                pass

        if admin_uid > 0:
            try:
                await self.bot.send_message(
                    admin_uid,
                    f"📢 **ارسال پیام همگانی به پایان رسید:**\n\n"
                    f"✅ ارسال موفق: `{sent}`\n"
                    f"❌ ناموفق: `{failed}`\n"
                    f"👥 کل دریافت‌کنندگان: `{len(recipients)}`",
                )
            except Exception:
                pass

    async def _mint_web_token(self, user_id: int) -> Optional[str]:
        """Mint a JWT for this Telegram user via the Account servicer."""
        if self._accounts is None:
            return None
        try:
            from core.proto import pb
            resp = await self._accounts.IssueWebToken(
                pb.IssueWebTokenRequest(user_id=user_id), None
            )
            if getattr(resp, "success", False):
                return resp.token
        except Exception:
            return None
        return None

    async def _show_dashboard(self, cq: CallbackQuery, force_reset: bool = False) -> None:
        """Dashboard button: delivers secure Username, Password, and 1-tap Login button."""
        uid = cq.from_user.id
        first_name = getattr(cq.from_user, "first_name", "") or ""
        base = self._web_base_url()

        username = f"tg_{uid}"
        password = ""
        token = ""
        if self._accounts:
            try:
                username, password, token = await self._accounts.get_or_create_credentials(
                    uid, display_name=first_name, force_reset=force_reset
                )
            except Exception as e:
                logger.error("Failed to provision credentials for %s: %s", uid, e)
                token = await self._mint_web_token(uid) or ""

        magic_link = f"{base}/?token=" + token if token else base

        lines = [
            self._t("ui_dashboard_body"),
            "",
            f"👤 {self._t('ui_webpass_username')}: <code>{username}</code>",
        ]
        if password and not password.startswith("("):
            lines.append(f"🔑 {self._t('ui_webpass_password')}: <code>{password}</code>")
        elif password:
            lines.append(f"🔑 {self._t('ui_webpass_password')}: <i>{password}</i>")
        lines.append(f"🌐 {self._t('ui_webpass_url')}: <code>{base}</code>")
        lines.append("")
        lines.append(self._t("ui_dashboard_tip"))

        text = "\n".join(lines)
        buttons = [
            [InlineKeyboardButton("🌐 " + self._t("ui_dashboard_open"), url=magic_link)],
            [InlineKeyboardButton("🔄 " + self._t("ui_webpass_reset_btn"), callback_data=CB["webpass_reset"])],
            [InlineKeyboardButton("⬅️ " + self._t("ui_back"), callback_data=CB["main"])],
        ]
        kbd = InlineKeyboardMarkup(buttons)
        try:
            await self._safe_edit(cq, text, reply_markup=kbd, disable_web_page_preview=True)
        except Exception:
            try:
                await cq.message.reply_text(text, reply_markup=kbd, disable_web_page_preview=True)
            except Exception:
                pass
        try:
            self._log.info("bot", "system", f"web dashboard link and credentials issued to {uid}")
        except Exception:
            pass
        await cq.answer()

    async def _issue_web_credentials(self, cq: CallbackQuery) -> None:
        """Create / refresh the web account credentials for this Telegram user."""
        uid = cq.from_user.id
        if self._accounts is None:
            await cq.answer(self._t("ui_dashboard_offline"), show_alert=True)
            return
        try:
            from core.proto import pb
            resp = await self._accounts.IssueWebToken(
                pb.IssueWebTokenRequest(user_id=uid), None
            )
            if getattr(resp, "success", False) and resp.account:
                acc = resp.account
                body = (
                    self._t("ui_webpass_body") + "\n\n"
                    f"👤 {self._t('ui_webpass_username')}: `{acc.username}`\n"
                    f"🆔 {self._t('ui_webpass_userid')}: `{acc.user_id}`\n\n"
                    + self._t("ui_webpass_note")
                )
                await self._safe_edit(cq, body, reply_markup=self._main_menu())
                try:
                    self._log.info("bot", "system",
                                   f"web credentials issued to {uid} ({acc.username})")
                except Exception:
                    pass
                await cq.answer()
                return
            await cq.answer(resp.message or self._t("ui_webpass_fail"), show_alert=True)
        except Exception as exc:
            await cq.answer(str(exc)[:180], show_alert=True)

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
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text_ob, kbd_ob = self._render_welcome_onboarding(uid, user=message.from_user)
                return await message.reply_text(text_ob, reply_markup=kbd_ob)
            return await message.reply_text(
                self._t("ui_use_menu"), reply_markup=self._main_menu()
            )

        if st.step.startswith("rename_rule_"):
            rule_id = st.step.split("rename_rule_", 1)[1]
            return await self._step_rename_rule(message, st, text, rule_id)
        if st.step.startswith("set_topic_"):
            rule_id = st.step.split("set_topic_", 1)[1]
            return await self._step_set_topic(message, st, text, rule_id)
        if st.step.startswith("set_delay_"):
            rule_id = st.step.split("set_delay_", 1)[1]
            return await self._step_set_delay(message, st, text, rule_id)
        if st.step.startswith("add_bl_"):
            rule_id = st.step.split("add_bl_", 1)[1]
            return await self._step_add_bl(message, st, text, rule_id)
        if st.step.startswith("add_wl_"):
            rule_id = st.step.split("add_wl_", 1)[1]
            return await self._step_add_wl(message, st, text, rule_id)
        if st.step.startswith("set_wm_"):
            rule_id = st.step.split("set_wm_", 1)[1]
            return await self._step_set_wm(message, st, text, rule_id)

        handler = getattr(self, f"_step_{st.step}", None)
        if handler is None:
            st.step = ""
            await self._persist(uid, st)
            sessions = await self._sessions.list_by_owner(uid) if (self._sessions and uid) else []
            if not sessions:
                text_ob, kbd_ob = self._render_welcome_onboarding(uid, user=message.from_user)
                return await message.reply_text(text_ob, reply_markup=kbd_ob)
            return await message.reply_text(
                self._t("ui_use_menu"), reply_markup=self._main_menu()
            )
        await handler(message, st, text)

    async def _route_document(self, message: Message) -> None:
        """Handle a document sent during the migration-import flow."""
        uid = message.from_user.id
        st = self._state(uid)
        if self._backup is None:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_migrate_unavailable"), reply_markup=self._main_menu())

        doc = message.document
        if doc is None:
            return await message.reply_text(self._t("ui_migrate_need_file"))
        size_mb = (doc.file_size or 0) / (1024 * 1024)
        if size_mb > 20:
            return await message.reply_text(self._t("ui_migrate_file_too_big"))

        path = os.path.join(tempfile.gettempdir(), f"atf_import_{uid}_{doc.file_id[:12]}")
        try:
            await message.download(file_name=path)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                blob = fh.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        if blob.startswith("enc:v1:"):
            # ask for the passphrase, keep the bundle in the FSM buffer
            st.buffer["migrate_blob"] = blob
            st.step = "migrate_import_passphrase"
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_migrate_ask_passphrase"),
                reply_markup=self._kbd([[("✖ " + self._t("ui_cancel"), CB["cancel"])]]))

        await self._finish_migration_import(message, st, blob, "")

    async def _finish_migration_import(
        self, message: Message, st: UiState, blob: str, passphrase: str,
    ) -> None:
        lang = self._lang_for(st)
        uid = message.from_user.id
        overwrite = bool(st.buffer.get("migrate_overwrite", False))
        try:
            text = decrypt_bundle(blob, passphrase)
            doc = deserialize_bundle(text)
        except ValueError as exc:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(f"❌ {exc}")

        try:
            stats = await self._backup.restore_snapshot(
                doc, None, overwrite_existing=overwrite)
        except Exception as exc:
            logger.exception("migration import failed")
            await self._record_error("bot", exc, user_id=uid)
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_migrate_fail", lang=lang, error=tg_detail(exc)))

        st.step = ""
        st.buffer.pop("migrate_blob", None)
        await self._persist(uid, st)
        lines = [self._t("ui_migrate_imported", lang=lang), "─────────────────────"]
        total_in = total_sk = total_fl = 0
        for k, v in stats.items():
            total_in += v.get("imported", 0)
            total_sk += v.get("skipped", 0)
            total_fl += v.get("failed", 0)
            lines.append(f"• {k}: ✅{v.get('imported', 0)}"
                         + (f" ⏭{v.get('skipped', 0)}" if v.get("skipped") else "")
                         + (f" ❌{v.get('failed', 0)}" if v.get("failed") else ""))
        lines.append(f"\n**جمع:** ✅ {total_in} · ⏭ {total_sk} · ❌ {total_fl}")
        try:
            self._log.info("bot", "backup",
                           f"migration snapshot imported by {uid}: {stats}")
        except Exception:
            pass
        await message.reply_text("\n".join(lines), reply_markup=self._main_menu())

    # ------------------------------------------------------------------ #
    # Step handlers (FSM)
    # ------------------------------------------------------------------ #
    async def _step_migrate_passphrase(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        """Passphrase for the snapshot being EXPORTED right now."""
        uid = message.from_user.id
        passphrase = (text or "").strip()
        if not passphrase:
            return await message.reply_text(self._t("ui_migrate_passphrase_required", lang=lang))
        st.step = ""
        await self._persist(uid, st)
        await self._do_migration_export_from_message(message, passphrase)

    async def _step_migrate_import_passphrase(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        """Passphrase for an ENCRYPTED bundle that was just uploaded."""
        uid = message.from_user.id
        passphrase = (text or "").strip()
        blob = st.buffer.get("migrate_blob") or ""
        if not blob:
            st.step = ""
            await self._persist(uid, st)
            return await message.reply_text(
                self._t("ui_migrate_need_file", lang=lang), reply_markup=self._migrate_menu())
        if not passphrase:
            return await message.reply_text(self._t("ui_migrate_passphrase_required", lang=lang))
        await self._finish_migration_import(message, st, blob, passphrase)

    async def _do_migration_export_from_message(self, message: Message, passphrase: str) -> None:
        """Export with a passphrase, invoked once the user has typed one."""
        uid = message.from_user.id
        if self._backup is None:
            return await message.reply_text(
                self._t("ui_migrate_unavailable"), reply_markup=self._migrate_menu())
        try:
            owner = uid if not self._is_admin(uid) else None
            doc = await self._backup.create_snapshot(owner)
            text = serialize_bundle(doc)
            fname = "atf-backup.enc"
            text = encrypt_bundle(text, passphrase)
            path = os.path.join(tempfile.gettempdir(), f"atf_{uid}_{fname}")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            counts = doc.get("counts") or {}
            lines = [self._t("ui_migrate_exported"),
                     "🔒 AES-GCM " + self._t("ui_migrate_encrypted"),
                     "─────────────────────"]
            for k in ("sessions", "forward_rules", "filter_rules",
                      "ai_configs", "prompt_templates", "api_credentials"):
                if counts.get(k):
                    lines.append(f"• {k}: {counts[k]}")
            await self.bot.send_document(
                chat_id=message.chat.id, document=path, caption="\n".join(lines))
            os.remove(path)
            try:
                self._log.info("bot", "backup", "migration snapshot exported (encrypted)")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("migration export failed")
            await self._record_error("bot", exc, user_id=uid)
            await message.reply_text(self._t("ui_migrate_fail", error=tg_detail(exc)))

    async def _step_login_phone(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        uid = message.from_user.id
        raw_text = (text or "").strip()
        masked_input = (raw_text[:4] + "****" + raw_text[-4:]) if len(raw_text) > 8 else "***"
        logger.info("Processing login phone input from uid=%s: input=%s", uid, masked_input)
        try:
            self._log.info("bot", "login", f"received phone input from {uid}: {masked_input}")
        except Exception:
            pass

        if not self._login.valid_phone(raw_text):
            logger.warning("Invalid phone format from uid=%s: %s", uid, masked_input)
            await message.reply_text(
                self._t("ui_login_phone_bad", lang=lang), reply_markup=self._cancel_kbd())
            return

        phone = self._login.normalize_phone(raw_text)
        logger.info("Normalized phone for uid=%s: %s. Initiating login flow...", uid, phone[:4] + "****" + phone[-4:])

        try:
            result = await self._login.start(uid, phone)
        except Exception as exc:
            logger.exception("LoginFlow.start exception for uid=%s phone=%s: %s", uid, phone, exc)
            try:
                self._log.error("bot", "login", f"start exception for uid={uid}: {type(exc).__name__}: {exc}")
            except Exception:
                pass
            await message.reply_text(
                f"❌ خطای غیرمنتظره در ارسال کد:\n`{type(exc).__name__}: {exc}`",
                reply_markup=self._cancel_kbd(),
            )
            return

        logger.info("LoginFlow.start returned result=%r for uid=%s", result, uid)

        if result != "send_code":
            key, _, detail = result.partition(":")
            err_msg = f"send_code failed for {phone}: {result}"
            logger.error(err_msg)
            try:
                self._log.error("bot", "login", err_msg)
            except Exception:
                pass

            # Safe translation lookup with fallback
            trans_key = f"ui_login_fail_{key}"
            try:
                fail_text = self._t(trans_key, error=detail)
            except Exception:
                fail_text = f"❌ ارسال کد ناموفق بود: {result}"

            await message.reply_text(fail_text, reply_markup=self._cancel_kbd())
            return

        st.step = "login_code"
        st.login_phone = phone
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        st.buffer["code_digits"] = ""
        await self._persist(uid, st)
        logger.info("Login code sent successfully to %s. State updated to login_code for uid=%s", phone[:4] + "****", uid)
        try:
            self._log.info("bot", "login", f"code sent to {phone[:4]}****, awaiting code from {uid}")
        except Exception:
            pass
        await message.reply_text(self._login_code_text(st), reply_markup=self._code_keypad(st))

    async def _cb_keypad(self, cq: CallbackQuery) -> None:
        uid = cq.from_user.id if cq.from_user else 0
        st = self._state(uid)
        if st.step != "login_code":
            await cq.answer()
            return

        raw_data = cq.data or ""
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8", errors="ignore")
        action = str(raw_data)[2:]  # "1", "2", ... or "del"
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        digits = str(st.buffer.get("code_digits", "") or "")

        if action == "del":
            digits = digits[:-1]
            st.buffer["code_digits"] = digits
            await self._persist(uid, st)
            await cq.answer(f"{digits or 'پاک شد'}")
            try:
                await self._safe_edit(cq, 
                    self._login_code_text(st),
                    reply_markup=self._code_keypad(st),
                )
            except Exception:
                pass
            return

        if action.isdigit():
            if len(digits) < 5:
                digits += str(action)
                st.buffer["code_digits"] = digits
                await self._persist(uid, st)
                await cq.answer(f"{digits}")

            if len(digits) < 5:
                try:
                    await self._safe_edit(cq, 
                        self._login_code_text(st),
                        reply_markup=self._code_keypad(st),
                    )
                except Exception:
                    pass
            else:
                try:
                    cur_lang = self._resolve_lang(uid)
                    await self._safe_edit(cq, 
                        self._t("ui_verifying_code", lang=cur_lang, code=' '.join(digits)),
                    )
                except Exception:
                    pass
                await self._submit_code_flow(cq.message, st, digits, uid=uid)

    async def _step_login_code(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        norm_text = (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        clean = re.sub(r"\D", "", norm_text)
        logger.info("Received login code input from uid=%s (raw_len=%d, clean_len=%d)",
                    uid, len(text), len(clean))
        if len(clean) not in (5, 6):
            cur_lang = self._resolve_lang(uid)
            await message.reply_text(
                self._t("ui_login_code_invalid", lang=cur_lang),
                reply_markup=self._code_keypad(st),
            )
            return
        await self._submit_code_flow(message, st, clean, uid=uid)

    async def _submit_code_flow(
        self, message: Message, st: UiState, clean_code: str, uid: Optional[int] = None
    ) -> None:
        lang = self._lang_for(st)
        if uid is None:
            uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        logger.info("Submitting login code for uid=%s (len=%d)", uid, len(clean_code))
        try:
            result = await self._login.submit_code(uid, clean_code)
        except Exception as exc:
            logger.exception("submit_code failed for uid=%s: %s", uid, exc)
            await message.reply_text(f"❌ خطا در تأیید کد: `{type(exc).__name__}: {exc}`", reply_markup=self._cancel_kbd())
            return

        logger.info("submit_code result for uid=%s: %r", uid, result)
        if result == "send_password":
            st.step = "login_password"
            if isinstance(st.buffer, dict):
                st.buffer.pop("code_digits", None)
            await self._persist(uid, st)
            await message.reply_text(
                self._t("ui_login_password", lang=lang), reply_markup=self._cancel_kbd())
            return
        await self._finish_login(message, st, result, uid=uid)

    async def _step_login_password(self, message: Message, st: UiState, text: str) -> None:
        uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        logger.info("Received 2FA password input from uid=%s", uid)
        try:
            result = await self._login.submit_password(uid, text)
        except Exception as exc:
            logger.exception("submit_password failed for uid=%s: %s", uid, exc)
            await message.reply_text(f"❌ خطا در تأیید رمز دو مرحله‌ای: `{type(exc).__name__}: {exc}`", reply_markup=self._cancel_kbd())
            return
        await self._finish_login(message, st, result, uid=uid)

    async def _finish_login(
        self, message: Message, st: UiState, result: str, uid: Optional[int] = None
    ) -> None:
        lang = self._lang_for(st)
        if uid is None:
            uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        phone = getattr(st, "login_phone", "") or ""

        if result in ("auth_expired", "code_expired"):
            st.step = "login_phone"
            if isinstance(st.buffer, dict):
                st.buffer.pop("code_digits", None)
            await self._persist(uid, st)
            retry_btn = self._kbd([[("🔄 " + self._t("ui_login_retry", lang=lang), CB["login_retry"])]]) if phone else self._cancel_kbd()
            if result == "code_expired":
                text = (
                    "⌛ این کد منقضی شده یا توسط تلگرام مسدود شده است.\n\n"
                    "💡 **علت:** ارسال کد خالی در چت، سیستم ضدسرقت تلگرام را فعال کرده و کد را باطل می‌کند.\n\n"
                    "👇 دکمه «🔄 دریافت مجدد کد» را بزنید و سپس کد جدید را چسبیده با mycode (مانند: `mycode73737`) یا با **کیپد عددی** وارد کنید:"
                )
                await message.reply_text(text, reply_markup=retry_btn)
            else:
                await message.reply_text(self._t("ui_login_expired", lang=lang, phone=phone), reply_markup=retry_btn)
            return

        if result == "invalid_code":
            if isinstance(st.buffer, dict):
                st.buffer["code_digits"] = ""
            await self._persist(uid, st)
            await message.reply_text(
                "❌ کد وارد شده نادرست است.\n\n"
                "لطفاً مجدداً با الگوی چسبیده (مانند: `mycode73737`) یا با **کیپد عددی زیر** وارد کنید:",
                reply_markup=self._code_keypad(st),
            )
            return

        st.step = ""
        if isinstance(st.buffer, dict):
            st.buffer.clear()
        await self._persist(uid, st)
        if result.startswith("login_success"):
            try:
                self._log.info("bot", "login", f"login success for {phone}")
            except Exception:
                pass
            await message.reply_text(
                self._t("ui_login_done", lang=lang, phone=phone),
                reply_markup=self._main_menu(),
            )
        else:
            key, _, detail = result.partition(":")
            await message.reply_text(
                self._t("ui_login_fail", lang=lang, error=detail or key),
                reply_markup=self._main_menu(),
            )

    # ---------------- credential FSM ----------------
    async def _step_cred_label(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.cred_label = text[:64]
        st.step = "cred_api_id"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_cred_api_id", lang=lang), reply_markup=self._cancel_kbd())

    async def _step_cred_api_id(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        raw = text.strip()
        if not raw.isdigit():
            return await message.reply_text(
                self._t("ui_cred_api_id_bad", lang=lang), reply_markup=self._cancel_kbd())
        st.cred_api_id = raw
        st.step = "cred_api_hash"
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_cred_api_hash", lang=lang), reply_markup=self._cancel_kbd())

    async def _step_cred_api_hash(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.cred_api_hash = text.strip()
        st.step = ""
        if self._credentials is None:
            await self._persist(message.from_user.id, st)
            return await message.reply_text(
                self._t("ui_cred_fail", lang=lang, error="vault unavailable"),
                reply_markup=self._creds_menu(),
            )
        try:
            cred = await self._credentials.create(
                label=st.cred_label or "default",
                api_id=int(st.cred_api_id),
                api_hash=st.cred_api_hash,
                is_default=True,
                # v12: stamp the Telegram user who added this credential so the
                # owner-scoped queries (backup export, listing) can see it.
                owner_user_id=int(message.from_user.id or 0),
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
                self._t("ui_cred_done", lang=lang, id=cred.id[:8]),
                reply_markup=self._creds_menu(),
            )
        except Exception as exc:
            await self._record_error("bot", exc, user_id=message.from_user.id)
            await message.reply_text(
                self._t("ui_cred_fail", lang=lang, error=tg_detail(exc)),
                reply_markup=self._creds_menu(),
            )
        finally:
            try:
                await message.delete()
            except Exception:
                pass  # api_hash is sensitive — best-effort cleanup only

    # ---------------- rule FSM ----------------
    async def _step_rule_source(self, message: Message, st: UiState, text: str) -> None:
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        matched_title = raw
        if isinstance(st.buffer, dict) and "chats" in st.buffer:
            for c in st.buffer["chats"]:
                if c["id"] == raw or (c.get("username") and c["username"].lower() == raw.lstrip("@").lower()):
                    matched_title = f"{c['emoji']} {c['title']}"
                    raw = c["id"]
                    break

        if matched_title == raw:
            try:
                _fsm_uid = self._scope_uid(uid)
                sessions = (await self._sessions.list_all() if _fsm_uid is None
                            else await self._sessions.list_by_owner(uid))
                if sessions:
                    client = self._pool.get(sessions[0].id)
                    if client and getattr(client, "is_connected", False):
                        chat_obj = await client.get_chat(raw)
                        c_id = getattr(chat_obj, "id", None)
                        if chat_obj and c_id is not None:
                            raw = str(c_id)
                            c_title = getattr(chat_obj, "title", None) or getattr(chat_obj, "first_name", None) or raw
                            matched_title = f"📢 {c_title}"
            except Exception:
                pass

        st.rule_source = raw
        if not isinstance(st.buffer, dict):
            st.buffer = {}
        st.buffer["source_name"] = matched_title
        st.step = "rule_target"
        st.buffer["page"] = 0
        await self._persist(uid, st)

        picker_text, markup = self._render_chat_picker(st, role="target", page=0)
        await message.reply_text(picker_text, reply_markup=markup)

    async def _step_rule_target(self, message: Message, st: UiState, text: str) -> None:
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        matched_title = raw
        if isinstance(st.buffer, dict) and "chats" in st.buffer:
            for c in st.buffer["chats"]:
                if c["id"] == raw or (c.get("username") and c["username"].lower() == raw.lstrip("@").lower()):
                    matched_title = f"{c['emoji']} {c['title']}"
                    raw = c["id"]
                    break

        if matched_title == raw:
            try:
                _fsm_uid = self._scope_uid(uid)
                sessions = (await self._sessions.list_all() if _fsm_uid is None
                            else await self._sessions.list_by_owner(uid))
                if sessions:
                    client = self._pool.get(sessions[0].id)
                    if client and getattr(client, "is_connected", False):
                        chat_obj = await client.get_chat(raw)
                        c_id = getattr(chat_obj, "id", None)
                        if chat_obj and c_id is not None:
                            raw = str(c_id)
                            c_title = getattr(chat_obj, "title", None) or getattr(chat_obj, "first_name", None) or raw
                            matched_title = f"📢 {c_title}"
            except Exception:
                pass

        st.rule_target = raw
        source_title = st.buffer.get("source_name") or st.rule_source
        await self._finish_rule_creation(message, st, source_title, matched_title)

    async def _step_rule_edit_source(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        in_draft = bool(st.buffer.get("in_draft")) if isinstance(st.buffer, dict) else False
        draft_obj = st.buffer.get("draft") if (in_draft and isinstance(st.buffer, dict)) else None
        draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
        if in_draft and not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
            st.buffer["draft"] = draft

        if raw.lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = "rule_editing_draft" if in_draft else ""
            if not in_draft and isinstance(st.buffer, dict):
                st.buffer = {}
            await self._persist(uid, st)
            if in_draft:
                text_out, kbd = self._render_draft_editor(rule, draft)
                await message.reply_text(text_out, reply_markup=kbd)
            else:
                text_out, kbd = self._render_rule_detail(rule)
                await message.reply_text(text_out, reply_markup=kbd)
            return

        matched_title = raw
        if isinstance(st.buffer, dict) and "chats" in st.buffer:
            for c in st.buffer["chats"]:
                if c["id"] == raw or (c.get("username") and c["username"].lower() == raw.lstrip("@").lower()):
                    matched_title = f"{c['emoji']} {c['title']}"
                    raw = c["id"]
                    break

        if matched_title == raw:
            try:
                _fsm_uid = self._scope_uid(uid)
                sessions = (await self._sessions.list_all() if _fsm_uid is None
                            else await self._sessions.list_by_owner(uid))
                if sessions:
                    client = self._pool.get(sessions[0].id)
                    if client and getattr(client, "is_connected", False):
                        chat_obj = await client.get_chat(raw)
                        c_id = getattr(chat_obj, "id", None)
                        if chat_obj and c_id is not None:
                            raw = str(c_id)
                            c_title = getattr(chat_obj, "title", None) or getattr(chat_obj, "first_name", None) or raw
                            matched_title = f"📢 {c_title}"
            except Exception:
                pass

        target_id = draft.get("target_chat_id") if in_draft else rule.target_chat_id
        if str(raw).strip() == str(target_id).strip():
            cancel_cb = f"re:{rule.id}" if in_draft else f"rd:{rule.id}"
            await message.reply_text(
                "❌ مبدأ نمی‌تواند با مقصد یکسان باشد (جلوگیری از ایجاد حلقه)!\n"
                "لطفاً شناسه یا یوزرنیم دیگری ارسال کنید:",
                reply_markup=self._kbd([[(self._t("ui_cancel_back", lang=lang), cancel_cb)]]),
            )
            return

        if in_draft:
            draft["source_chat_id"] = str(raw)
            draft["source_name"] = matched_title
            st.step = "rule_editing_draft"
            await self._persist(uid, st)
            await message.reply_text(f"✅ مبدأ پیش‌نویس تغییر یافت: {matched_title}")
            text_out, markup = self._render_draft_editor(rule, draft)
            await message.reply_text(text_out, reply_markup=markup)
        else:
            rule.source_chat_id = str(raw)
            rule.source_chat_name = matched_title
            expected_ver = st.buffer.get("version") if isinstance(st.buffer, dict) else None
            try:
                await self._save_owned_rule(rule, uid, expected_ver)
            except ConcurrencyError:
                await message.reply_text("⚠️ خطا: این قانون هم‌زمان ویرایش شده است.", reply_markup=self._main_menu())
                return
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text(f"✅ مبدأ قانون با موفقیت تغییر یافت: {matched_title}")
            text_out, markup = self._render_rule_detail(rule)
            await message.reply_text(text_out, reply_markup=markup)

    async def _step_rule_edit_target(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        in_draft = bool(st.buffer.get("in_draft")) if isinstance(st.buffer, dict) else False
        draft_obj = st.buffer.get("draft") if (in_draft and isinstance(st.buffer, dict)) else None
        draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
        if in_draft and not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
            st.buffer["draft"] = draft

        if raw.lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = "rule_editing_draft" if in_draft else ""
            if not in_draft and isinstance(st.buffer, dict):
                st.buffer = {}
            await self._persist(uid, st)
            if in_draft:
                text_out, kbd = self._render_draft_editor(rule, draft)
                await message.reply_text(text_out, reply_markup=kbd)
            else:
                text_out, kbd = self._render_rule_detail(rule)
                await message.reply_text(text_out, reply_markup=kbd)
            return

        matched_title = raw
        if isinstance(st.buffer, dict) and "chats" in st.buffer:
            for c in st.buffer["chats"]:
                if c["id"] == raw or (c.get("username") and c["username"].lower() == raw.lstrip("@").lower()):
                    matched_title = f"{c['emoji']} {c['title']}"
                    raw = c["id"]
                    break

        if matched_title == raw:
            try:
                _fsm_uid = self._scope_uid(uid)
                sessions = (await self._sessions.list_all() if _fsm_uid is None
                            else await self._sessions.list_by_owner(uid))
                if sessions:
                    client = self._pool.get(sessions[0].id)
                    if client and getattr(client, "is_connected", False):
                        chat_obj = await client.get_chat(raw)
                        c_id = getattr(chat_obj, "id", None)
                        if chat_obj and c_id is not None:
                            raw = str(c_id)
                            c_title = getattr(chat_obj, "title", None) or getattr(chat_obj, "first_name", None) or raw
                            matched_title = f"📢 {c_title}"
            except Exception:
                pass

        source_id = draft.get("source_chat_id") if in_draft else rule.source_chat_id
        if str(raw).strip() == str(source_id).strip():
            cancel_cb = f"re:{rule.id}" if in_draft else f"rd:{rule.id}"
            await message.reply_text(
                "❌ مقصد نمی‌تواند با مبدأ یکسان باشد (جلوگیری از ایجاد حلقه)!\n"
                "لطفاً شناسه یا یوزرنیم دیگری ارسال کنید:",
                reply_markup=self._kbd([[(self._t("ui_cancel_back", lang=lang), cancel_cb)]]),
            )
            return

        if in_draft:
            draft["target_chat_id"] = str(raw)
            draft["target_name"] = matched_title
            st.step = "rule_editing_draft"
            await self._persist(uid, st)
            await message.reply_text(f"✅ مقصد پیش‌نویس تغییر یافت: {matched_title}")
            text_out, markup = self._render_draft_editor(rule, draft)
            await message.reply_text(text_out, reply_markup=markup)
        else:
            rule.target_chat_id = str(raw)
            rule.target_chat_name = matched_title
            expected_ver = st.buffer.get("version") if isinstance(st.buffer, dict) else None
            try:
                await self._save_owned_rule(rule, uid, expected_ver)
            except ConcurrencyError:
                await message.reply_text("⚠️ خطا: این قانون هم‌زمان ویرایش شده است.", reply_markup=self._main_menu())
                return
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text(f"✅ مقصد قانون با موفقیت تغییر یافت: {matched_title}")
            text_out, markup = self._render_rule_detail(rule)
            await message.reply_text(text_out, reply_markup=markup)

    async def _step_rule_editing_draft(self, message: Message, st: UiState, text: str) -> None:
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if raw.lower() in ("/cancel", "انصراف", "لغو", "خروج"):
            st.step = ""
            st.buffer = {}
            await self._persist(uid, st)
            text_out, kbd = self._render_rule_detail(rule)
            await message.reply_text("❌ ویرایش لغو شد و تغییرات پیش‌نویس ذخیره نشدند.", reply_markup=kbd)
            return

        draft_obj = st.buffer.get("draft") if isinstance(st.buffer, dict) else None
        draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
        if not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
            st.buffer["draft"] = draft

        text_out, kbd = self._render_draft_editor(rule, draft)
        await message.reply_text(
            "💡 شما در حال ویرایش پیش‌نویس قانون هستید.\n"
            "لطفاً از دکمه‌های شیشه‌ای زیر برای تغییر تنظیمات، ذخیره یا لغو استفاده کنید:",
            reply_markup=kbd,
        )

    # ---------------- rule replace & header/footer FSM ----------------
    async def _step_rule_replace_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_replace_menu(rule)
            await message.reply_text("❌ عملیات افزودن جایگزینی لغو شد.", reply_markup=menu_kbd)
            return

        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        new_replacements = {}
        delimiters = ["->", "=>", "➔", "—>", ":"]

        for line in lines:
            delim_found = None
            for d in delimiters:
                if d in line:
                    delim_found = d
                    break
            if delim_found:
                parts = line.split(delim_found, 1)
                src = parts[0].strip()
                dst = parts[1].strip()
                if src:
                    new_replacements[src] = dst
            else:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    new_replacements[parts[0].strip()] = parts[1].strip()
                elif len(parts) == 1:
                    new_replacements[parts[0].strip()] = ""

        if not new_replacements:
            await message.reply_text(
                "⚠️ فرمت ورودی نامعتبر است.\n\n"
                "لطفاً به صورت زیر ارسال کنید:\n"
                "`کلمه قدیمی -> کلمه جدید`\n\n"
                "جهت حذف کلمه:\n"
                "`کلمه حذفی ->`",
                reply_markup=self._kbd([[("❌ انصراف", f"rtx:{rule_id}")]]),
            )
            return

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        current_reps = dict(rule.replacements)
        current_reps.update(new_replacements)
        rule.metadata["replacements"] = current_reps
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        added_list = "\n".join([f"• `{k}` ➔ `{v or '(حذف)'}`" for k, v in new_replacements.items()])
        await message.reply_text(
            f"✅ **موارد زیر به جایگزینی متن اضافه شدند:**\n\n{added_list}",
        )
        menu_text, menu_kbd = self._render_replace_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_header_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_header_footer_menu(rule)
            await message.reply_text("❌ تنظیم هدر لغو شد.", reply_markup=menu_kbd)
            return

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["header"] = text.strip()
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text("✅ **هدر با موفقیت تنظیم شد.**")
        menu_text, menu_kbd = self._render_header_footer_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_footer_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_header_footer_menu(rule)
            await message.reply_text("❌ تنظیم فوتر لغو شد.", reply_markup=menu_kbd)
            return

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["footer"] = text.strip()
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text("✅ **فوتر با موفقیت تنظیم شد.**")
        menu_text, menu_kbd = self._render_header_footer_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_clean_words_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ تسک یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_cleaner_menu(rule)
            await message.reply_text("❌ عملیات لغو شد.", reply_markup=menu_kbd)
            return

        words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["clean_words"] = words
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(f"✅ **لیست {len(words)} کلمه جهت حذف با موفقیت ذخیره شد.**")
        menu_text, menu_kbd = self._render_cleaner_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_max_edit_input(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ تسک یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        clean = text.strip()
        if clean.lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            menu_text, menu_kbd = self._render_max_time_edit_menu(rule)
            await message.reply_text("❌ عملیات لغو شد.", reply_markup=menu_kbd)
            return

        if not clean.isdigit():
            await message.reply_text(
                "❌ لطفاً فقط یک عدد صحیح معتبر بر حسب ثانیه ارسال کنید (مثلاً `3600`):",
                reply_markup=self._cancel_kbd(),
            )
            return

        val = int(clean)
        rule.max_time_edit = val
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(f"✅ **محدودیت زمان ویرایش پیام روی {val} ثانیه تنظیم شد.**")
        menu_text, menu_kbd = self._render_max_time_edit_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    # ---------------- smart routing FSM steps ----------------
    async def _step_smart_set_intermediate(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        raw_input = text.strip()
        if raw_input.lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text("❌ تنظیم کانال واسط لغو شد.", reply_markup=m_kbd)
            return

        # Loop prevention check
        norm_input = raw_input.replace("https://t.me/", "").replace("t.me/", "")
        if (raw_input == str(rule.source_chat_id) or norm_input == str(rule.source_chat_name or "").lstrip("@") or
                raw_input == str(rule.target_chat_id) or norm_input == str(rule.target_chat_name or "").lstrip("@")):
            await message.reply_text(
                "⚠️ **خطای ضد لوپ (Loop Prevention):**\n"
                "کانال واسط نمی‌تواند با کانال مبدأ یا مقصد یکسان باشد!\n"
                "لطفاً یک کانال یا گروه واسط متفاوت وارد کنید یا دکمه انصراف را لمس نمایید.",
                reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule.id}")]])
            )
            return

        # Resolve peer info if client available
        resolved_name = raw_input
        try:
            clients = self._pool.all_clients()
            client = next(iter(clients.values()), None) if clients else None
            if client and hasattr(client, "is_connected") and client.is_connected:
                peer = raw_input
                if peer.startswith("https://t.me/+") or peer.startswith("t.me/+"):
                    pass
                else:
                    if peer.startswith("@"):
                        chat = await client.get_chat(peer)
                    elif peer.startswith("-100") or (peer.startswith("-") and peer[1:].isdigit()):
                        chat = await client.get_chat(int(peer))
                    else:
                        chat = await client.get_chat(peer)
                    if chat:
                        raw_input = str(chat.id)
                        resolved_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id)
        except Exception:
            pass

        rule.intermediate_channel_id = str(raw_input)
        rule.intermediate_channel_name = str(resolved_name)
        rule.use_intermediate = True
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["intermediate_channel_id"] = str(raw_input)
        rule.metadata["intermediate_channel_name"] = str(resolved_name)
        rule.metadata["use_intermediate"] = True
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(
            f"✅ **کانال واسط VIP تنظیم و فعال شد:**\n"
            f"📡 نام/شناسه: `{resolved_name}`\n"
            f"🆔 آیدی مسیر: `{raw_input}`"
        )
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_header(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text("❌ تنظیم هدر اختصاصی لغو شد.", reply_markup=m_kbd)
            return

        rule.custom_header = text.strip()
        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["custom_header"] = text.strip()
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text("✅ **هدر اختصاصی کپی با موفقیت ذخیره شد.**")
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_keywords(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text("❌ تنظیم کلیدواژه‌ها لغو شد.", reply_markup=m_kbd)
            return

        raw = text.strip()
        if not isinstance(rule.detection_criteria, dict):
            rule.detection_criteria = {}

        if raw in ("حذف", "پاک", "none", "clear", "حذف همه"):
            rule.detection_criteria.pop("text_contains", None)
            rule.detection_criteria.pop("keywords", None)
            ans = "🗑 کلیدواژه‌های متنی حذف شدند."
        else:
            kws = [k.strip() for k in raw.replace("،", ",").split(",") if k.strip()]
            rule.detection_criteria["text_contains"] = kws
            rule.detection_criteria["keywords"] = kws
            ans = f"✅ {len(kws)} کلیدواژه ذخیره شد: " + ", ".join(f"`{k}`" for k in kws)

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["detection_criteria"] = rule.detection_criteria
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(ans)
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_regex(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text("❌ تنظیم Regex لغو شد.", reply_markup=m_kbd)
            return

        raw = text.strip()
        if not isinstance(rule.detection_criteria, dict):
            rule.detection_criteria = {}

        if raw in ("حذف", "پاک", "none", "clear"):
            rule.detection_criteria.pop("regex_pattern", None)
            rule.detection_criteria.pop("text_regex", None)
            ans = "🗑 الگوی Regex حذف شد."
        else:
            import re
            try:
                re.compile(raw)
            except re.error as exc:
                await message.reply_text(f"❌ الگوی Regex نامعتبر است:\n`{exc}`\n\nلطفاً یک الگوی معتبر بفرستید یا /cancel را ارسال کنید.")
                return
            rule.detection_criteria["regex_pattern"] = raw
            rule.detection_criteria["text_regex"] = raw
            ans = f"✅ الگوی Regex با موفقیت ذخیره شد:\n`{raw}`"

        if not isinstance(rule.metadata, dict):
            rule.metadata = {}
        rule.metadata["detection_criteria"] = rule.detection_criteria
        await self._save_owned_rule(rule, uid)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(ans)
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_test_rule(self, message: Message, st: UiState, text: str) -> None:
        uid = int(message.from_user.id if message.from_user else 0)
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._owned_rule(rule_id, uid)
        if not rule:
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            await message.reply_text("❌ قانون یافت نشد یا منقضی شده است.", reply_markup=self._main_menu())
            return

        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            st.step = ""
            st.buffer = {}
            await self._persist(message.from_user.id, st)
            m_text, m_kbd = self._render_smart_menu(rule)
            await message.reply_text("❌ تست هوشمند لغو شد.", reply_markup=m_kbd)
            return

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        from ...domain.services import SmartRoutingEngine
        from ...domain.entities import MessagePayload

        # Build simulated payload
        payload = MessagePayload(
            message_id=999999,
            chat_id=str(rule.source_chat_id),
            text=text.strip(),
            date=int(time.time()),
        )

        # Check if forwarded message attributes were present in user's test message
        f_origin = getattr(message, "forward_origin", None)
        f_chat = getattr(message, "forward_from_chat", None)
        if f_origin:
            chat_obj = getattr(f_origin, "chat", None)
            payload.forward_origin = {
                "type": getattr(f_origin, "type", "channel"),
                "from_chat_id": str(chat_obj.id) if chat_obj else "",
                "from_chat_title": getattr(chat_obj, "title", ""),
                "sender_name": getattr(f_origin, "sender_user_name", ""),
            }
        elif f_chat:
            payload.forward_origin = {
                "type": "channel",
                "from_chat_id": str(f_chat.id),
                "from_chat_title": getattr(f_chat, "title", ""),
            }

        engine = SmartRoutingEngine()
        decision, route_reason = engine.decide_route(payload, rule)
        matched, reason = engine.evaluate_rule(payload, rule)
        is_vip, vip_reason = engine.is_vip_origin(payload, rule)
        detected_cat = "VIP" if is_vip else ("NORMAL" if not payload.forward_origin else "UNKNOWN_ORIGIN")
        cat_labels = {"ALL": "🌟 عمومی", "VIP": "💎 VIP", "NORMAL": "👤 عادی (بدون فوروارد)", "UNKNOWN_ORIGIN": "❓ منشأ ناشناخته"}

        crit = getattr(rule, "detection_criteria", {}) or {}
        m_mode = (crit.get("match_mode") or "ANY").upper()
        kws = crit.get("text_contains") or crit.get("keywords") or []
        rx = crit.get("regex_pattern") or crit.get("text_regex") or ""

        route_label = {
            RoutingDecision.BRANDING_VIA_C: "💎 مسیر ۲: برندینگ واسط (A ➔ C ➔ B)",
            RoutingDecision.DIRECT_COPY: "📋 مسیر ۱: ارسال مستقیم و تمیز (A ➔ B)",
            RoutingDecision.DIRECT_FORWARD: "↗️ مسیر ۳: فوروارد رسمی مستقیم (A ➔ B)",
            RoutingDecision.CUSTOM_HEADER_COPY: "🏷 کپی با هدر اختصاصی (A ➔ B)",
            RoutingDecision.DROP: "⛔ عدم ارسال (عدم تطبیق با قانون)",
        }.get(decision, str(decision))

        action_lines = []
        if matched and decision != RoutingDecision.DROP:
            if decision == RoutingDecision.BRANDING_VIA_C:
                action_lines.append(f"1️⃣ کپی بدون تگ یا Re-upload به C ➔ `{rule.intermediate_channel_name or rule.intermediate_channel_id}`")
                action_lines.append(f"2️⃣ فوروارد نیتیو اختصاصی از C به B ➔ `{rule.target_chat_name or rule.target_chat_id}`")
            elif decision == RoutingDecision.DIRECT_FORWARD:
                action_lines.append(f"• فوروارد مستقیم از A به B ➔ `{rule.target_chat_name or rule.target_chat_id}`")
            else:
                action_lines.append(f"• کپی مستقیم از A به B ➔ `{rule.target_chat_name or rule.target_chat_id}`")
        else:
            action_lines.append("• هیچ پیامی ارسال نخواهد شد (عدم تطبیق با شروط).")

        result_text = (
            "🧪 **نتیجه شبیه‌سازی و تست زنده روتینگ هوشمند (Dry Run):**\n"
            "────────────────────\n"
            f"📋 **قانون مورد تست:** `{rule.id[:8]}` ({rule.source_chat_name or rule.source_chat_id} ➔ {rule.target_chat_name or rule.target_chat_id})\n"
            f"🎯 **وضعیت تطبیق کلی:** {'✅ تطبیق موفق' if matched else '❌ عدم تطبیق'}\n"
            f"🏷 **رده پیام ورودی:** {cat_labels.get(detected_cat, detected_cat)}\n"
            f"🛣 **مسیر انتخابی روتینگ:**\n**{route_label}**\n\n"
            f"🔍 **تحلیل شروط موتور:**\n"
            f"• منطق ترکیب شروط: `{m_mode}`\n"
            f"• فیلتر کلیدواژه‌ها: `{', '.join(kws) if kws else 'تعیین نشده'}`\n"
            f"• فیلتر Regex: `{rx if rx else 'تعیین نشده'}`\n"
            f"• دلیل تصمیم: `{route_reason or reason}`\n\n"
            "🚀 **مراحل عملیات اجرایی:**\n" +
            "\n".join(action_lines) + "\n\n"
            "🛡 **سیاست محتوای قفل:** در صورت Restricted بودن فوروارد، دانلود و Clean Copy خودکار انجام می‌شود.\n"
            "🔒 **کنترل ضد لوپ:** پیام ارسالی به عنوان پیام ورودی جدید پردازش نخواهد شد."
        )
        await message.reply_text(result_text)
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    # ---------------- filter FSM ----------------
    async def _step_filter_name(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.step = "filter_blacklist"
        st.buffer["name"] = text.strip()
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_filter_blacklist", lang=lang), reply_markup=self._cancel_kbd())

    async def _step_filter_blacklist(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.step = ""
        blacklist = [w.strip() for w in text.replace("،", ",").split(",") if w.strip()]
        try:
            fr = FilterRule(
                name=st.buffer.get("name", "filter"), blacklist_keywords=blacklist,
                # v12.1: stamp the creator so owner-scoped reads can see it.
                owner_user_id=int(message.from_user.id or 0) if message.from_user else 0)
            await self._filters.create(fr)
            try:
                self._log.info(
                    "bot", "filter",
                    f"filter {fr.name} created with {len(blacklist)} blacklist words")
            except Exception:
                pass
            await self._persist(message.from_user.id, st)
            await message.reply_text(
                self._t("ui_filter_done", lang=lang, name=fr.name), reply_markup=self._filters_menu())
        except Exception as exc:
            await self._record_error("bot", exc, user_id=message.from_user.id)
            await message.reply_text(
                self._t("ui_filter_fail", lang=lang, error=tg_detail(exc)),
                reply_markup=self._filters_menu(),
            )

    # ---------------- AI config FSM ----------------
    async def _step_ai_name(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.step = "ai_provider"
        st.ai_name = text.strip()
        await self._persist(message.from_user.id, st)
        await message.reply_text(
            self._t("ui_ai_provider", lang=lang), reply_markup=self._provider_picker())

    async def _step_ai_provider(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        provider = text.strip().lower()
        if provider not in {p.value for p in AIProviderType}:
            return await message.reply_text(
                self._t("ui_ai_provider_bad", lang=lang), reply_markup=self._cancel_kbd())
        st.ai_provider = provider
        st.step = "ai_base_url"
        from core.infrastructure.ai.providers import OpenAICompatibleProvider
        default_url = OpenAICompatibleProvider.default_base_urls.get(provider, "")
        st.buffer["default_base_url"] = default_url
        prompt_text = (
            f"🌐 **تنظیم آدرس سرور / هاست ({self._provider_label(provider)}):**\n\n"
            f"آدرس پیش‌فرض:\n`{default_url or 'ندارد (نیازمند آدرس دستی)'}`\n\n"
            "✍️ لطفاً آدرس سرور را ارسال کنید (مثال: `http://sub.legoten.com:4455/v1`):\n"
            "یا برای استفاده از پیش‌فرض دکمه زیر را لمس نمایید:"
        )
        kbd = self._kbd([
            [("✅ استفاده از پیش‌فرض (یا بعدی)", "ai_url_default")],
            [("❌ " + self._t("ui_cancel", lang=lang), CB["cancel"])],
        ])
        await self._persist(message.from_user.id, st)
        await message.reply_text(prompt_text, reply_markup=kbd)

    async def _step_ai_base_url(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        url = text.strip()
        if url.lower() in ("default", "none", "-", "پیش‌فرض", "پیش فرض"):
            url = str(st.buffer.get("default_base_url", ""))
        elif not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url
        st.ai_base_url = url
        st.step = "ai_model"
        kb = self._model_picker(st, st.ai_provider)
        await self._persist(message.from_user.id, st)
        msg_text = (
            f"✅ آدرس ذخیره شد: `{url or 'پیش‌فرض'}`\n\n"
            + self._t("ui_ai_model", lang=lang)
            + "\n(از لیست زیر انتخاب کنید یا نام مدل دلخواه مانند `coding` را تایپ کنید):"
        )
        if kb is not None:
            await message.reply_text(msg_text, reply_markup=kb)
        else:
            await message.reply_text(msg_text, reply_markup=self._cancel_kbd())

    async def _step_ai_model(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.ai_model = text.strip()
        st.step = "ai_api_key"
        await self._persist(message.from_user.id, st)
        await message.reply_text(self._t("ui_ai_key", lang=lang), reply_markup=self._cancel_kbd())

    async def _step_ai_api_key(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        st.step = ""
        user_id = message.from_user.id
        try:
            cfg = AIConfig(
                name=st.ai_name,
                provider=AIProviderType(st.ai_provider),
                model=st.ai_model,
                base_url=st.ai_base_url or "",
                api_key=text.strip(),
                # v12.1: stamp the creator so owner-scoped reads can see it.
                owner_user_id=int(user_id or 0),
            )
            created = await self._ai.create(cfg)
            try:
                self._log.info(
                    "bot", "ai", f"AI config {cfg.name} created ({cfg.provider.value})")
            except Exception:
                pass
            await self._persist(user_id, st)

            # Immediately test health
            ok, reply_or_err, latency = await self._ai.health_check(created.id)
            health_info = {
                "ok": ok,
                "latency_ms": latency,
                "reply": reply_or_err if ok else "",
                "error": reply_or_err if not ok else "",
            }
            detail_text, detail_kbd = self._render_ai_detail(created, health_info=health_info)
            banner = "✅ **پیکربندی هوش مصنوعی با موفقیت ثبت و آزمایش شد!**\n\n" if ok else "⚠️ **پیکربندی ذخیره شد اما تست اتصال با خطا مواجه گردید!**\n\n"
            await message.reply_text(banner + detail_text, reply_markup=detail_kbd)
        except Exception as exc:
            await self._record_error("ai", exc, user_id=user_id)
            cfgs = await (self._ai.list_all() if self._is_admin(user_id) else self._ai.list_by_owner(user_id))
            await message.reply_text(
                self._t("ui_ai_fail", lang=lang, error=tg_detail(exc)),
                reply_markup=self._ai_menu(cfgs))
        finally:
            try:
                await message.delete()
            except Exception:
                pass  # api key is sensitive — best-effort cleanup

    async def _step_ai_edit_host(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        cfg = await self._ai.get_owned(aid, self._scope_uid(message.from_user.id if message.from_user else 0)) if aid else None
        _step_uid = message.from_user.id if message.from_user else 0
        if not cfg:
            cfgs = await (self._ai.list_all() if self._is_admin(_step_uid) else self._ai.list_by_owner(_step_uid))
            await message.reply_text("❌ پیکربندی یافت نشد.", reply_markup=self._ai_menu(cfgs))
            return
        new_url = text.strip()
        if new_url.lower() in ("default", "none", "-", "پیش‌فرض", "پیش فرض"):
            new_url = ""
        elif not new_url.startswith("http://") and not new_url.startswith("https://"):
            new_url = "http://" + new_url
        cfg.base_url = new_url
        try:
            await self._ai.update_owned(cfg, self._scope_uid(message.from_user.id if message.from_user else 0))
        except PermissionError:
            return await message.reply_text(self._t("ui_need_admin", lang=lang))
        ok, reply_or_err, latency = await self._ai.health_check_owned(cfg.id, self._scope_uid(message.from_user.id if message.from_user else 0))
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text("✅ آدرس هاست به‌روزرسانی شد:\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_ai_edit_model(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        cfg = await self._ai.get_owned(aid, self._scope_uid(message.from_user.id if message.from_user else 0)) if aid else None
        _step_uid = message.from_user.id if message.from_user else 0
        if not cfg:
            cfgs = await (self._ai.list_all() if self._is_admin(_step_uid) else self._ai.list_by_owner(_step_uid))
            await message.reply_text("❌ پیکربندی یافت نشد.", reply_markup=self._ai_menu(cfgs))
            return
        cfg.model = text.strip()
        try:
            await self._ai.update_owned(cfg, self._scope_uid(message.from_user.id if message.from_user else 0))
        except PermissionError:
            return await message.reply_text(self._t("ui_need_admin", lang=lang))
        ok, reply_or_err, latency = await self._ai.health_check_owned(cfg.id, self._scope_uid(message.from_user.id if message.from_user else 0))
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text("✅ مدل با موفقیت تغییر کرد:\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_ai_edit_key(self, message: Message, st: UiState, text: str) -> None:
        lang = self._lang_for(st)
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        try:
            await message.delete()
        except Exception:
            pass
        cfg = await self._ai.get_owned(aid, self._scope_uid(message.from_user.id if message.from_user else 0)) if aid else None
        _step_uid = message.from_user.id if message.from_user else 0
        if not cfg:
            cfgs = await (self._ai.list_all() if self._is_admin(_step_uid) else self._ai.list_by_owner(_step_uid))
            await message.reply_text("❌ پیکربندی یافت نشد.", reply_markup=self._ai_menu(cfgs))
            return
        cfg.api_key = text.strip()
        try:
            await self._ai.update_owned(cfg, self._scope_uid(message.from_user.id if message.from_user else 0))
        except PermissionError:
            return await message.reply_text(self._t("ui_need_admin", lang=lang))
        ok, reply_or_err, latency = await self._ai.health_check_owned(cfg.id, self._scope_uid(message.from_user.id if message.from_user else 0))
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text("✅ کلید API با موفقیت به‌روزرسانی شد:\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_pv_persona_input(self, message: Message, st: UiState, text: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        st.step = ""
        st.buffer = {}
        await self._persist(uid, st)
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            await message.reply_text("❌ ماژول پاسخگوی پی‌وی یافت نشد.", reply_markup=self._main_menu())
            return
        lang = await self._get_user_lang(uid)
        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            text_out, kbd = self._render_pv_menu(uid, lang=lang)
            await message.reply_text("❌ تنظیم پرامپت لغو شد.", reply_markup=kbd)
            return

        cfg = resp.get_config_for_user(uid)
        cfg.persona_prompt = text.strip()
        resp.save_config_for_user(uid, cfg)
        await message.reply_text("✅ پرامپت و شخصیت پاسخگوی پی‌وی با موفقیت ثبت گردید!")
        text_out, kbd = self._render_pv_menu(uid, lang=lang)
        await message.reply_text(text_out, reply_markup=kbd)

    async def _step_pv_test_input(self, message: Message, st: UiState, text: str) -> None:
        uid = message.from_user.id if message.from_user else 0
        st.step = ""
        st.buffer = {}
        await self._persist(uid, st)
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            await message.reply_text("❌ ماژول پاسخگوی پی‌وی یافت نشد.", reply_markup=self._main_menu())
            return
        lang = await self._get_user_lang(uid)
        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            text_out, kbd = self._render_pv_menu(uid, lang=lang)
            await message.reply_text("❌ تست لغو شد.", reply_markup=kbd)
            return

        cfg = resp.get_config_for_user(uid)
        ai_cfg = await resp.get_active_ai_config(uid)
        if not ai_cfg:
            text_out, kbd = self._render_pv_menu(uid, lang=lang)
            await message.reply_text("⚠️ هیچ هوش مصنوعی فعالی برای اکانت شما یافت نشد. لطفاً در بخش AI یک مدل فعال کنید.", reply_markup=kbd)
            return

        wait_msg = await message.reply_text("⏳ در حال پردازش پیام با شخصیت تنظیم‌شده و شبیه‌سازی تایپینگ...")
        try:
            from core.infrastructure.ai.providers import OpenAICompatibleProvider, AIProviderFactory
            prov_key = ai_cfg.provider.value if hasattr(ai_cfg.provider, "value") else str(ai_cfg.provider)
            factory = resp._ai_factory or AIProviderFactory()
            provider_cls = factory._registry.get(prov_key, OpenAICompatibleProvider)
            provider = provider_cls()

            messages = [
                {"role": "system", "content": cfg.persona_prompt},
                {"role": "user", "content": text.strip()},
            ]
            if hasattr(provider, "chat_complete"):
                reply_text = await provider.chat_complete(ai_cfg, messages, temperature=0.7)
            else:
                reply_text = await provider.rewrite(ai_cfg, text.strip())

            body = (
                "🧪 **نتیجه شبیه‌سازی پاسخگوی هوشمند پی‌وی:**\n\n"
                f"👤 **پیام ورودی مخاطب:**\n«{text.strip()}»\n\n"
                f"🤖 **پاسخ شبیه‌سازی‌شده (مدل {ai_cfg.model}):**\n"
                f"«{reply_text.strip()}»\n\n"
                f"⏱ تاخیر شبیه‌سازی تایپینگ: `{cfg.typing_delay_min:.1f}` تا `{cfg.typing_delay_max:.1f}` ثانیه"
            )
            try:
                await wait_msg.delete()
            except Exception:
                pass
            text_out, kbd = self._render_pv_menu(uid, lang=lang)
            await message.reply_text(body, reply_markup=kbd)
        except Exception as exc:
            try:
                await wait_msg.delete()
            except Exception:
                pass
            text_out, kbd = self._render_pv_menu()
            await message.reply_text(f"❌ خطا در اجرای تست هوش مصنوعی: {exc}", reply_markup=kbd)
