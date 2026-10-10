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
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
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
        pv_responder=None,
        web_url: str = "",
        account_servicer=None,
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
        self._pv_responder = pv_responder
        self._web_url = (web_url or "").rstrip("/")
        self._accounts = account_servicer
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

    def _code_keypad(self, st: UiState) -> InlineKeyboardMarkup:
        return self._kbd([
            [("1", "k:1"), ("2", "k:2"), ("3", "k:3")],
            [("4", "k:4"), ("5", "k:5"), ("6", "k:6")],
            [("7", "k:7"), ("8", "k:8"), ("9", "k:9")],
            [("⌫", "k:del"), ("0", "k:0"), ("❌ " + self._t("ui_cancel"), CB["cancel"])],
        ])

    def _login_code_text(self, st: UiState) -> str:
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

    # ------------------------------------------------------------------ #
    # Menus
    # ------------------------------------------------------------------ #
    async def _main_text(self, uid: int) -> str:
        """Main-menu header: live overview banner + the standard title.

        Mirrors the web dashboard's stat badges so both surfaces show the
        same numbers. Falls back gracefully to the plain title when the
        repositories are unavailable (e.g. during startup).
        """
        lines = [self._t("ui_main_title")]
        try:
            rules = await self._rules.list_by_owner(uid)
            sessions = await self._sessions.list_by_owner(uid)
            active_rules = sum(1 for r in rules if getattr(r, "is_active", False))
            live_sessions = sum(1 for s in sessions if getattr(s, "is_active", False))
            fwd_24h = await self._forwarded_24h(uid)
            dlq = await self._dlq_count()
            lines.append("")
            lines.append(self._t("ui_overview_title"))
            lines.append(f"⚡ {self._t('ui_overview_rules')}: {active_rules}/{len(rules)}")
            lines.append(f"📱 {self._t('ui_overview_sessions')}: {live_sessions}/{len(sessions)}")
            lines.append(f"📤 {self._t('ui_overview_forwarded')}: {fwd_24h}")
            if dlq > 0:
                lines.append(f"❌ {self._t('ui_overview_dlq')}: {dlq}")
        except Exception:
            logger.debug("overview banner unavailable", exc_info=True)
        return "\n".join(lines)

    async def _forwarded_24h(self, uid: int) -> int:
        """Forwarded message count in the trailing 24h.

        The metrics table is global (per-process), so this sums the hourly
        buckets rather than trying to attribute to a single owner.
        """
        if self._metrics is None:
            return 0
        try:
            buckets = await self._metrics.hourly(24)
            return sum(int(b.get("forwarded") or 0) for b in (buckets or []))
        except Exception:
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

    def _main_menu(self) -> InlineKeyboardMarkup:
        return self._kbd([
            [("📱 " + self._t("ui_sessions"), CB["sessions"]), ("⚡ " + self._t("ui_rules"), CB["rules"])],
            [("🧹 " + self._t("ui_filters"), CB["filters"]), ("🤖 " + self._t("ui_ai"), CB["ai"])],
            [("📊 " + self._t("ui_stats"), CB["stats"]), ("🔑 " + self._t("ui_creds"), CB["creds"])],
            [("🛠 " + self._t("ui_debug"), CB["debug"]), ("🌐 " + self._t("ui_lang"), CB["lang"])],
            [("💻 " + self._t("ui_dashboard"), CB["dashboard"])],
            [("📚 " + self._t("ui_help"), CB["help"])],
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
                    nav_row.append(("◀️ قبلی", f"r_pg:{page - 1}"))
                nav_row.append((f"📄 {page + 1} / {total_pages}", "noop"))
                if page < total_pages - 1:
                    nav_row.append(("بعدی ▶️", f"r_pg:{page + 1}"))
                rows.append(nav_row)

        rows.append([("⬅️ " + self._t("ui_back"), CB["main"])])
        return self._kbd(rows)

    def _render_rules_list(
        self, rules: Optional[list] = None, page: int = 0, page_size: int = 5
    ) -> Tuple[str, InlineKeyboardMarkup]:
        rules_list = rules if rules is not None else []
        lines = [self._t("ui_rules_title"), ""]
        if not rules_list:
            lines.append(self._t("ui_none"))
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
            lines.append(f"📄 صفحه {page + 1} از {total_pages} (مجموع: {total_items} قانون)")

        return "\n".join(lines), self._rules_menu(rules_list, page=page, page_size=page_size)

    def _render_rule_detail(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        """Render full settings and action dashboard for an individual rule."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        status_str = "🟢 فعال" if rule.is_active else "🔴 غیرفعال"

        f_mode = getattr(rule, "forward_mode", ForwardMode.COPY_MESSAGE)
        mode_val = getattr(f_mode, "value", str(f_mode))
        mode_names = {
            "COPY_MESSAGE": "📋 کپی بدون تگ (Copy)",
            "DIRECT_FORWARD": "↗️ فوروارد با تگ (Forward)",
            "REWRITE_AI": "🤖 بازنویسی با هوش مصنوعی",
        }
        mode_title = mode_names.get(mode_val, mode_val)

        reps_count = len(rule.replacements)
        reps_badge = f"{reps_count} مورد فعال" if reps_count else "غیرفعال"

        hdr_badge = "تنظیم شده" if rule.header else "خالی"
        ftr_badge = "تنظیم شده" if rule.footer else "خالی"

        lk_badge = "🚫 حذف" if (rule.remove_links or (isinstance(rule.metadata, dict) and bool(rule.metadata.get("remove_links")))) else "🟢 مجاز"
        vc_badge = "🚫 مسدود" if rule.block_voice else "🟢 مجاز"
        st_badge = "🚫 مسدود" if rule.block_stickers else "🟢 مجاز"
        em_badge = "🧹 پاکسازی" if rule.remove_emojis else "🟢 حفظ"
        ed_badge = "⏹ نادیده" if rule.ignore_edits else "🔄 سینک"
        dl_badge = "🗑 سینک" if rule.sync_deletes else "⏹ نادیده"

        album_names = {
            "album": "🖼 آلبوم کامل",
            "first": "1️⃣ فقط اولین مدیا",
            "split": "🔀 تفکیک پیام‌ها",
        }
        alb_badge = album_names.get(rule.album_mode, "🖼 آلبوم کامل")

        # Smart routing summary
        smart_cat = getattr(rule, "message_category", "ALL")
        cat_labels = {"ALL": "🌟 همه پیام‌ها", "VIP_ONLY": "💎 فقط VIP", "NORMAL_ONLY": "👤 فقط عادی"}
        cat_badge = cat_labels.get(smart_cat, smart_cat)
        use_mid = bool(getattr(rule, "use_intermediate", False))
        mid_id = getattr(rule, "intermediate_channel_id", "") or ""
        mid_badge = f"🟢 فعال (`{mid_id[:12]}`)" if (use_mid and mid_id) else "🔴 غیرفعال"

        text = (
            "⚙️ **تنظیمات پیشرفته قانون انتقال**\n\n"
            f"📡 **مبدأ:** `{src_label}` (`{rule.source_chat_id}`)\n"
            f"🎯 **مقصد:** `{dst_label}` (`{rule.target_chat_id}`)\n"
            f"📊 **وضعیت:** {status_str}\n"
            f"⚡ **حالت ارسال:** {mode_title}\n"
            "────────────────────\n"
            f"💎 **رده هوشمند:** {cat_badge} | **واسط VIP:** {mid_badge}\n"
            f"🔤 **جایگزینی متن:** {reps_badge}\n"
            f"📝 **هدر:** {hdr_badge} | **فوتر:** {ftr_badge}\n"
            f"🔗 **لینک:** {lk_badge} | 🎙 **ویس:** {vc_badge}\n"
            f"🎭 **استیکر:** {st_badge} | 😀 **ایموجی:** {em_badge}\n"
            f"✏️ **ویرایش پیام:** {ed_badge} | 🗑 **حذف پیام:** {dl_badge}\n"
            f"🖼 **چندتصویری:** {alb_badge}\n\n"
            "👇 جهت تغییر و تنظیم هر بخش، دکمه مربوطه را لمس کنید:"
        )

        rows: List[List[Tuple[str, str]]] = [
            [("✏️ ویرایش پیش‌نویس (Draft Editor)", f"re:{rule.id}")],
            [
                ("📡 تغییر مبدأ", f"res:{rule.id}"),
                ("🎯 تغییر مقصد", f"ret:{rule.id}"),
            ],
            [("💎 مسیریابی هوشمند و VIP (Smart Rules)", f"rsm:{rule.id}")],
            [("🔤 جایگزینی متن (Text Replace)", f"rtx:{rule.id}")],
            [("📝 تنظیم هدر و فوتر", f"rhf:{rule.id}")],
            [
                (f"🔗 لینک: {lk_badge}", f"rlk:{rule.id}"),
                (f"🎙 ویس: {vc_badge}", f"rvc:{rule.id}"),
            ],
            [
                (f"🎭 استیکر: {st_badge}", f"rst:{rule.id}"),
                (f"😀 ایموجی: {em_badge}", f"rem:{rule.id}"),
            ],
            [
                (f"✏️ ادیت: {ed_badge}", f"red:{rule.id}"),
                (f"🗑 حذف: {dl_badge}", f"rdl:{rule.id}"),
            ],
            [
                (f"🖼 آلبوم: {alb_badge}", f"rmg:{rule.id}"),
                ("⚡ تغییر حالت", f"rmd:{rule.id}"),
            ],
            [
                ("🔄 تغییر وضعیت (🟢/🔴)", f"rtg:{rule.id}"),
                ("🗑 حذف قانون", f"rrm:{rule.id}"),
            ],
            [("⬅️ بازگشت به لیست قوانین", CB["rules"])],
        ]
        return text, self._kbd(rows)

    def _render_draft_editor(self, rule: ForwardRule, draft: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
        """Render comprehensive draft editor screen where settings are staged safely."""
        src_label = draft.get("source_name") or draft.get("source_chat_id", "")
        dst_label = draft.get("target_name") or draft.get("target_chat_id", "")
        status_str = "🟢 فعال" if draft.get("is_active", True) else "🔴 غیرفعال"

        f_mode = draft.get("forward_mode", ForwardMode.COPY_MESSAGE.value)
        mode_val = getattr(f_mode, "value", str(f_mode))
        mode_names = {
            "COPY_MESSAGE": "📋 کپی بدون تگ (Copy)",
            "DIRECT_FORWARD": "↗️ فوروارد با تگ (Forward)",
            "REWRITE_AI": "🤖 بازنویسی با AI",
        }
        mode_title = mode_names.get(mode_val, mode_val)

        reps = draft.get("replacements") or {}
        reps_count = len(reps) if isinstance(reps, dict) else 0
        reps_badge = f"{reps_count} مورد" if reps_count else "خالی"

        hdr = str(draft.get("header") or "")
        ftr = str(draft.get("footer") or "")
        hdr_badge = f"`{hdr[:14]}…`" if len(hdr) > 14 else (f"`{hdr}`" if hdr else "خالی")
        ftr_badge = f"`{ftr[:14]}…`" if len(ftr) > 14 else (f"`{ftr}`" if ftr else "خالی")

        lk_badge = "🚫 حذف" if draft.get("remove_links") else "🟢 مجاز"
        vc_badge = "🚫 مسدود" if draft.get("block_voice") else "🟢 مجاز"
        st_badge = "🚫 مسدود" if draft.get("block_stickers") else "🟢 مجاز"
        em_badge = "🧹 پاکسازی" if draft.get("remove_emojis") else "🟢 حفظ"
        ed_badge = "⏹ نادیده" if draft.get("ignore_edits") else "🔄 سینک"
        dl_badge = "🗑 سینک" if draft.get("sync_deletes") else "⏹ نادیده"

        album_names = {
            "album": "🖼 آلبوم کامل",
            "first": "1️⃣ فقط اولین مدیا",
            "split": "🔀 تفکیک پیام‌ها",
        }
        alb_mode = str(draft.get("album_mode") or "album")
        alb_badge = album_names.get(alb_mode, "🖼 آلبوم کامل")

        text = (
            "✏️ **پیش‌نویس ویرایش قانون (Draft Mode)**\n\n"
            f"🆔 شناسه قانون: `{rule.id[:8]}` (نسخه دیتابیس: {rule.version})\n"
            "────────────────────\n"
            f"📡 **مبدأ:** `{src_label}` (`{draft.get('source_chat_id')}`)\n"
            f"🎯 **مقصد:** `{dst_label}` (`{draft.get('target_chat_id')}`)\n"
            f"📊 **وضعیت:** {status_str}\n"
            f"⚡ **حالت ارسال:** {mode_title}\n"
            "────────────────────\n"
            f"🔤 **جایگزینی کلمات:** {reps_badge}\n"
            f"📝 **هدر:** {hdr_badge} | **فوتر:** {ftr_badge}\n"
            f"🔗 **لینک:** {lk_badge} | 🎙 **ویس:** {vc_badge}\n"
            f"🎭 **استیکر:** {st_badge} | 😀 **ایموجی:** {em_badge}\n"
            f"✏️ **ویرایش پیام:** {ed_badge} | 🗑 **حذف پیام:** {dl_badge}\n"
            f"🖼 **چندتصویری:** {alb_badge}\n\n"
            "💡 *تغییرات شما تا پیش از لمس «💾 ذخیره تغییرات»، در پیش‌نویس موقت نگهداری می‌شوند و قانون اصلی بدون تغییر باقی می‌ماند.*"
        )

        rows: List[List[Tuple[str, str]]] = [
            [
                ("📡 تغییر مبدأ", f"ed_s:{rule.id}"),
                ("🎯 تغییر مقصد", f"ed_t:{rule.id}"),
            ],
            [
                (f"⚡ حالت: {mode_val}", f"ed_m:{rule.id}"),
                (f"🔄 وضعیت: {status_str}", f"ed_a:{rule.id}"),
            ],
            [
                (f"🔗 لینک: {lk_badge}", f"ed_lk:{rule.id}"),
                (f"🎙 ویس: {vc_badge}", f"ed_vc:{rule.id}"),
            ],
            [
                (f"🎭 استیکر: {st_badge}", f"ed_st:{rule.id}"),
                (f"😀 ایموجی: {em_badge}", f"ed_em:{rule.id}"),
            ],
            [
                (f"✏️ ادیت: {ed_badge}", f"ed_ed:{rule.id}"),
                (f"🗑 حذف: {dl_badge}", f"ed_dl:{rule.id}"),
            ],
            [
                (f"🖼 آلبوم: {alb_badge}", f"ed_mg:{rule.id}"),
                (f"🔤 جایگزینی ({reps_count})", f"rtx:{rule.id}"),
            ],
            [
                ("📝 تنظیم هدر و فوتر", f"rhf:{rule.id}"),
            ],
            [
                ("💾 ذخیره تغییرات (Save)", f"ed_save:{rule.id}"),
                ("❌ لغو ویرایش (Cancel)", f"ed_can:{rule.id}"),
            ],
            [
                ("⬅️ بازگشت به کارت قانون", f"rd:{rule.id}"),
            ],
        ]
        return text, self._kbd(rows)

    def _render_replace_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        """Render text replacement configuration menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)
        reps = rule.replacements

        lines = [
            "🔤 **تنظیمات جایگزینی متن (Text Replace)**\n",
            f"قانون: **{src_label} ➔ {dst_label}**\n",
            "────────────────────",
            "📋 **لیست عبارات جایگزین فعال:**",
        ]
        if not reps:
            lines.append("❌ هنوز هیچ جایگزینی برای این قانون تعریف نشده است.\n")
        else:
            for idx, (old, new) in enumerate(reps.items(), start=1):
                rep_display = f"`{new}`" if new else "*(حذف کامل کلمه)*"
                lines.append(f"{idx}. `{old}` ➔ {rep_display}")
            lines.append("")

        lines.extend([
            "💡 **راهنما:**",
            "برای اضافه کردن جایگزینی جدید روی دکمه «➕ افزودن جایگزینی» بزنید.",
            "فرمت ارسال: `کلمه مبدأ -> کلمه مقصد`",
            "جهت حذف یک کلمه: `کلمه حذفی ->`",
        ])

        rows: List[List[Tuple[str, str]]] = [
            [("➕ افزودن جایگزینی جدید", f"rtxa:{rule.id}")],
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
            rows.append([("🗑 پاکسازی همه جایگزینی‌ها", f"rtxc:{rule.id}")])

        rows.append([("⬅️ بازگشت به تنظیمات قانون", f"rd:{rule.id}")])
        return "\n".join(lines), self._kbd(rows)

    def _render_header_footer_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
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
            [("⬅️ بازگشت به تنظیمات قانون", f"rd:{rule.id}")],
        ]
        return "\n".join(lines), self._kbd(rows)

    def _render_smart_menu(self, rule: ForwardRule) -> Tuple[str, InlineKeyboardMarkup]:
        """Render dedicated smart routing, VIP channel, and deduplication menu."""
        src_label = rule.source_chat_name or rule.source_chat_id
        dst_label = getattr(rule, "target_label", None) or (rule.target_chat_name or rule.target_chat_id)

        use_mid = bool(getattr(rule, "use_intermediate", False))
        mid_id = getattr(rule, "intermediate_channel_id", "") or ""
        mid_name = getattr(rule, "intermediate_channel_name", "") or ""
        mid_display = f"`{mid_name or mid_id}`" if mid_id else "❌ تنظیم نشده"

        cat = getattr(rule, "message_category", "ALL")
        crit = getattr(rule, "detection_criteria", {}) or {}
        match_mode = (crit.get("match_mode") or "ANY").upper()
        keywords = crit.get("text_contains") or crit.get("keywords") or []
        kw_display = ", ".join(f"`{k}`" for k in keywords) if keywords else "❌ تعیین نشده"
        regex_p = crit.get("regex_pattern") or crit.get("text_regex") or ""
        regex_display = f"`{regex_p}`" if regex_p else "❌ تعیین نشده"

        f_mode = getattr(rule, "forward_mode", ForwardMode.COPY_MESSAGE)
        mode_val = getattr(f_mode, "value", str(f_mode))

        # Determine active routing path
        if use_mid and mid_id and cat in ("VIP", "VIP_ONLY"):
            path_desc = "💎 **مسیر ۲: برندینگ واسط (A ➔ C ➔ B)**\n*(فقط پیام‌های منتخب VIP ابتدا در C ایجاد و با هدر C به B فوروارد می‌شوند)*"
            path_tag = "مسیر ۲ (A ➔ C ➔ B)"
        elif mode_val == "DIRECT_FORWARD":
            path_desc = "↗️ **مسیر ۳: فوروارد رسمی مستقیم (A ➔ B)**\n*(پیام‌ها با تگ فوروارد کانال مبدأ A به B ارسال می‌شوند)*"
            path_tag = "مسیر ۳ (Native A ➔ B)"
        else:
            path_desc = "📋 **مسیر ۱: مستقیم و تمیز (A ➔ B)**\n*(پیام‌های عادی، نظرات و چت بدون دخالت C مستقیم به B کپی می‌شوند)*"
            path_tag = "مسیر ۱ (مستقیم A ➔ B)"

        chdr = getattr(rule, "custom_header", "") or ""
        hdr_display = f"`{chdr[:25]}…`" if len(chdr) > 25 else (f"`{chdr}`" if chdr else "❌ تنظیم نشده")
        prio = int(getattr(rule, "priority", 10) or 10)

        lines = [
            "💎 **کنترل پنل مسیریابی هوشمند و VIP (Smart Routing Engine)**\n",
            f"قانون: **{src_label} ➔ {dst_label}**\n",
            "────────────────────",
            f"🛣 **مسیر فعال جریان پیام:**\n{path_desc}\n",
            f"📡 **کانال واسط C:** {mid_display}",
            f"🧩 **منطق ترکیب شروط VIP:** `{match_mode}` ({'الزام همزمان همه شروط' if match_mode == 'ALL' else 'تحقق حداقل یک شرط'})",
            f"🔎 **کلیدواژه‌های متنی:** {kw_display}",
            f"🔣 **الگوی Regex:** {regex_display}",
            f"🏷 **هدر اختصاصی کپی:** {hdr_display}",
            f"🎯 **اولویت اجرای قانون:** `{prio}` (قوانین با اولویت بالاتر اول اجرا می‌شوند)",
            "🛡 **سیاست محتوای قفل/حفاظت‌شده:** فعال (Clean Copy & Re-upload در صورت محدودیت تلگرام)",
            "🔒 **ضد لوپ و ضد دابل‌سند:** فعال با بررسی Idempotency Key و منشأ پیام\n",
            "👇 جهت تغییر تنظیمات، گزینه‌های زیر را لمس کنید:"
        ]

        # Buttons
        rows: List[List[Tuple[str, str]]] = [
            [("🛣 تغییر مسیر: " + path_tag[:20], f"rsmpth:{rule.id}")],
            [
                ("🧩 منطق: " + match_mode, f"rsmmod:{rule.id}"),
                ("📡 کانال واسط C", f"rsmw:{rule.id}"),
            ],
            [
                ("🔎 تنظیم کلیدواژه", f"rsmtx:{rule.id}"),
                ("🔣 تنظیم Regex", f"rsmrx:{rule.id}"),
            ],
            [
                ("🏷 هدر اختصاصی", f"rsmh:{rule.id}"),
                ("🗑 حذف هدر", f"rsmhd:{rule.id}"),
            ],
            [
                ("➕ اولویت (+10)", f"rsmpu:{rule.id}"),
                ("➖ اولویت (-10)", f"rsmpd:{rule.id}"),
            ],
            [("🧪 تست و شبیه‌سازی زنده قانون (Dry Run)", f"rsmtst:{rule.id}")],
            [("⬅️ بازگشت به تنظیمات قانون", f"rd:{rule.id}")],
        ]
        return "\n".join(lines), self._kbd(rows)

    def _render_chat_picker(self, st: UiState, role: str, page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
        """Render a rich, paginated dialog selection screen with inline buttons."""
        chats = st.buffer.get("chats", []) if isinstance(st.buffer, dict) else []
        rc = "s" if role == "source" else "t"
        page_size = 5

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

        if chats:
            total_items = len(chats)
            total_pages = max(1, (total_items + page_size - 1) // page_size)
            page = max(0, min(page, total_pages - 1))
            start = page * page_size
            end = min(start + page_size, total_items)
            page_items = chats[start:end]

            if role == "source":
                header = (
                    "📥 **تغییر/انتخاب کانال یا گروه مبدأ (Source):**\n"
                    "پیام‌های جدید از این گفتگو خوانده و رله خواهند شد.\n"
                    "────────────────────"
                )
            else:
                src_name = st.buffer.get("source_name") or st.rule_source
                header = (
                    f"✅ **مبدأ:** {src_name} (`{st.rule_source}`)\n\n"
                    "📤 **تغییر/انتخاب کانال یا گروه مقصد (Target):**\n"
                    "پیام‌ها به این گفتگو کپی و ارسال خواهند شد.\n"
                    "────────────────────"
                )

            lines = [header]
            for idx, item in enumerate(page_items, start=start + 1):
                clean_title = _clean_md(item["title"])
                uname = f" • @{item['username']}" if item.get("username") else ""
                lines.append(f"{item['emoji']} **{idx}. {clean_title}**\n   🆔 `{item['id']}`{uname}")

            lines.append("────────────────────")
            lines.append(f"📄 **صفحه {page + 1} از {total_pages}** (مجموع: {total_items} گفتگو)")
            lines.append("👇 یکی از دکمه‌های زیر را لمس کنید یا شناسه/آیدی را بفرستید:")
            text = "\n".join(lines)

            rows: List[List[Tuple[str, str]]] = []
            for local_idx, item in enumerate(page_items):
                global_idx = start + local_idx
                title_disp = item["title"][:22] + "…" if len(item["title"]) > 22 else item["title"]
                btn_label = f"{item['emoji']} {start + local_idx + 1}. {title_disp}"
                rows.append([(btn_label, f"ch:{rc}:{global_idx}")])

            nav_row: List[Tuple[str, str]] = []
            if page > 0:
                nav_row.append(("◀️ قبلی", f"pg:{rc}:{page - 1}"))
            nav_row.append((f"📄 {page + 1} / {total_pages}", "noop"))
            if page < total_pages - 1:
                nav_row.append(("بعدی ▶️", f"pg:{rc}:{page + 1}"))
            rows.append(nav_row)

            bottom_row: List[Tuple[str, str]] = [
                ("🔄 تازه‌سازی لیست", f"rfc:{rc}"),
            ]
            if st.step == "rule_target":
                bottom_row.append(("⬅️ تغییر مبدأ", "r_back_source"))
            bottom_row.append(("❌ انصراف", cancel_cb))
            rows.append(bottom_row)
            return text, self._kbd(rows)

        # Fallback when no chats could be fetched automatically
        if role == "source":
            text = (
                "📥 **انتخاب کانال یا گروه مبدأ (Source):**\n\n"
                "⚠️ لیست گفتگوهای سشن به صورت خودکار یافت نشد (یا هنوز در کانالی عضو نیستید).\n\n"
                "✍️ لطفاً شناسه عددی، یوزرنیم یا لینک کانال مبدأ را دستی بفرستید:\n"
                "👈 نمونه‌ها:\n"
                "• شناسه عددی: `-1001234567890`\n"
                "• آیدی عمومی: `@channel_username`\n"
                "• لینک اختصاصی: `https://t.me/c/1234567890`"
            )
        else:
            src_name = st.buffer.get("source_name") or st.rule_source
            text = (
                f"✅ **مبدأ:** {src_name} (`{st.rule_source}`)\n\n"
                "📤 **انتخاب کانال یا گروه مقصد (Target):**\n\n"
                "⚠️ لیست گفتگوهای سشن به صورت خودکار یافت نشد.\n\n"
                "✍️ لطفاً شناسه عددی، یوزرنیم یا لینک مقصد را بفرستید:\n"
                "👈 مثلاً: `-1009876543210` یا `@target_channel`"
            )

        fallback_rows: List[List[Tuple[str, str]]] = [
            [("🔄 تلاش مجدد برای دریافت لیست", f"rfc:{rc}")],
        ]
        if st.step == "rule_target":
            fallback_rows.append([("⬅️ تغییر مبدأ", "r_back_source")])
        fallback_rows.append([("❌ انصراف", cancel_cb)])
        return text, self._kbd(fallback_rows)

    async def _fetch_dialogs(self) -> List[Dict[str, Any]]:
        """Fetch available channels/groups from connected session clients."""
        sessions = await self._sessions.list_all()
        active_sessions = [s for s in sessions if getattr(s, "is_active", True) and getattr(s, "is_authorized", False)]
        if not active_sessions and sessions:
            active_sessions = sessions

        chats: List[Dict[str, Any]] = []
        seen_ids = set()

        for session in active_sessions:
            client = self._pool.get(session.id)
            if not client or not getattr(client, "is_connected", False):
                continue

            try:
                async with asyncio.timeout(6.0):
                    async for dialog in client.get_dialogs(limit=80):  # type: ignore[union-attr]
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
                            title = "پیام‌های ذخیره شده (Saved Messages)"

                        chats.append({
                            "id": str(chat.id),
                            "title": str(title),
                            "emoji": emoji,
                            "kind": kind,
                            "username": getattr(chat, "username", "") or "",
                        })
            except Exception as exc:
                logger.warning("Could not fetch dialogs for session %s: %s", session.id, exc)

        priority = {"channel": 0, "group": 1, "saved": 2, "bot": 3, "user": 4}
        chats.sort(key=lambda c: priority.get(c["kind"], 99))
        return chats

    async def _finish_rule_creation(self, event: Any, st: UiState, source_title: str, target_title: str) -> None:
        """Persist ForwardRule and present the completion card."""
        uid: int = int(event.from_user.id if getattr(event, "from_user", None) else (event.chat.id if getattr(event, "chat", None) else 0))

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
            err_text = (
                "⚠️ **خطای حلقه (Loop Detected)!**\n\n"
                "شناسه کانال مبدأ و مقصد قانون نمی‌تواند یکسان باشد. فوروارد پیام از یک چت به خودش مجاز نیست.\n"
                "لطفاً مجدداً با مبدأ یا مقصد متفاوت تلاش کنید."
            )
            markup = self._kbd([
                [("➕ تلاش مجدد", CB["rule_add"])],
                [("🏠 منوی اصلی", CB["main"])],
            ])
            if hasattr(event, "edit_message_text"):
                await event.edit_message_text(err_text, reply_markup=markup)
            else:
                await event.reply_text(err_text, reply_markup=markup)
            return

        sessions = await self._sessions.list_all()
        if not sessions:
            st.step = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            msg_text = self._t("ui_no_sessions")
            if hasattr(event, "edit_message_text"):
                await event.edit_message_text(msg_text, reply_markup=self._sessions_menu())
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
                "🎉 **قانون هدایت با موفقیت ایجاد و فعال شد!**\n\n"
                f"📥 **مبدأ:** {source_title}\n"
                f"   🆔 `{st.rule_source}`\n\n"
                f"📤 **مقصد:** {target_title}\n"
                f"   🆔 `{st.rule_target}`\n\n"
                "⚙️ **نحوه فوروارد:** کپی مستقیم (بدون نقل‌قول و تگ منبع)\n"
                "🟢 **وضعیت:** فعال و در حال مانیتورینگ زنده"
            )
            markup = self._kbd([
                [("⚙️ تنظیمات و فیلترهای پیشرفته این قانون", f"rd:{rule.id}")],
                [("➕ افزودن قانون جدید", CB["rule_add"]), ("⚡ مشاهده قوانین", CB["rules"])],
                [("🏠 منوی اصلی", CB["main"])],
            ])

            if hasattr(event, "edit_message_text"):
                await event.edit_message_text(success_text, reply_markup=markup)
            else:
                await event.reply_text(success_text, reply_markup=markup)

        except Exception as exc:
            await self._record_error("bot", exc, user_id=uid)
            err_text = self._t("ui_rule_fail", error=tg_detail(exc))
            if hasattr(event, "edit_message_text"):
                await event.edit_message_text(err_text, reply_markup=self._rules_menu())
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

    def _render_pv_menu(self) -> Tuple[str, InlineKeyboardMarkup]:
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            return "❌ ماژول پاسخگوی هوشمند پی‌وی بارگذاری نشده است.", self._kbd([[("⬅️ بازگشت", CB["ai"])]])

        cfg = resp.config
        status_str = "🟢 فعال" if cfg.enabled else "🔴 غیرفعال"

        persona_preview = (cfg.persona_prompt[:140] + "...") if len(cfg.persona_prompt) > 140 else cfg.persona_prompt

        text = (
            "🤖 **پاسخگوی فوق‌العاده هوشمند پی‌وی (AI PV Assistant)**\n\n"
            f"📊 **وضعیت فعالیت:** {status_str}\n"
            f"⏱ **شبیه‌سازی تایپینگ انسانی:** `{cfg.typing_delay_min:.1f}` تا `{cfg.typing_delay_max:.1f}` ثانیه\n"
            f"⏳ **کول‌داون ضداسپم:** هر `{cfg.cooldown_seconds}` ثانیه\n"
            f"🤖 **نادیده‌گرفتن ربات‌ها:** {'✅ بله' if cfg.ignore_bots else '❌ خیر'}\n\n"
            f"🎭 **لحن و شخصیت فعلی:**\n"
            f"_{persona_preview}_\n\n"
            "💡 **ویژگی‌ها:** با اتصال این بخش به هوش مصنوعی، هر پیامی به پی‌وی اکانت شما بیاید با "
            "شبیه‌سازی اکشنِ Typing و لحنی کاملاً انسانی، جذاب و فارسی پاسخ داده می‌شود بدون اینکه کسی شک کند."
        )

        tog_btn = "🔴 غیرفعال‌سازی" if cfg.enabled else "🟢 فعال‌سازی"
        rows = [
            [(f"🔘 وضعیت: {tog_btn}", "pv:toggle")],
            [("🎭 تنظیم لحن و پرامپت شخصیت", "pv:persona")],
            [("⏱ تنظیم زمان تأخیر و تایپینگ", "pv:delays")],
            [("🧪 تست نمونه پاسخگویی", "pv:test")],
            [("⬅️ " + self._t("ui_back"), CB["ai"])],
        ]
        return text, self._kbd(rows)

    def _render_pv_persona_menu(self) -> Tuple[str, InlineKeyboardMarkup]:
        text = (
            "🎭 **تنظیم لحن و شخصیت پاسخگوی پی‌وی:**\n\n"
            "یکی از سبک‌های آماده زیر را انتخاب کنید یا پرامپت دلخواه بنویسید:"
        )
        rows = [
            [("😃 لحن خودمانی، جذاب و بسیار طبیعی (پیش‌فرض)", "pv:set_p:casual")],
            [("💼 لحن کاری، مؤدبانه و حرفه‌ای", "pv:set_p:business")],
            [("⚡ پاسخ‌های بسیار کوتاه و دوستانه", "pv:set_p:short")],
            [("✏️ نوشتن پرامپت اختصاصی و دلخواه", "pv:set_p:custom")],
            [("⬅️ بازگشت به تنظیمات پی‌وی", "pv:menu")],
        ]
        return text, self._kbd(rows)

    def _render_pv_delays_menu(self) -> Tuple[str, InlineKeyboardMarkup]:
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
            try:
                cq.continue_propagation()
            except Exception:
                pass

        @b.on_message(filters.command("start") & filters.private)
        async def _start(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /start from uid=%s", uid)
            try:
                await message.reply_text(self._t("ui_welcome"), reply_markup=self._main_menu())
                try:
                    self._log.info("bot", "system", f"/start by {uid}")
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("/start failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("menu") & filters.private)
        async def _menu(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /menu from uid=%s", uid)
            try:
                await message.reply_text(self._t("ui_main_title"), reply_markup=self._main_menu())
            except Exception as exc:
                logger.exception("/menu failed for uid=%s: %s", uid, exc)

        @b.on_message(filters.command("rules") & filters.private)
        async def _rules_cmd(_, message: Message):
            uid = message.from_user.id if message.from_user else 0
            logger.info("Handling /rules from uid=%s", uid)
            try:
                rules = await self._rules.list_all()
                text, kbd = self._render_rules_list(rules, page=0)
                await message.reply_text(text, reply_markup=kbd)
            except Exception as exc:
                logger.exception("/rules failed for uid=%s: %s", uid, exc)

        @b.on_callback_query(filters.regex("^" + CB["main"] + "$"))
        async def _cb_main(_, cq: CallbackQuery):
            await cq.edit_message_text(
                await self._main_text(cq.from_user.id if cq.from_user else 0),
                reply_markup=self._main_menu(),
            )
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["sessions"] + "$"))
        async def _cb_sessions(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            logger.info("Executing _cb_sessions for uid=%s", uid)
            try:
                sessions = await (self._sessions.list_all() if self._is_admin(uid) else self._sessions.list_by_owner(uid))
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
            except Exception as exc:
                logger.exception("_cb_sessions failed for uid=%s: %s", uid, exc)
                try:
                    await cq.answer(f"⚠️ خطا: {exc}", show_alert=True)
                except Exception:
                    pass

        @b.on_callback_query(filters.regex("^" + CB["rules"] + "$"))
        async def _cb_rules(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            rules = await (self._rules.list_all() if self._is_admin(uid) else self._rules.list_by_owner(uid))
            text, kbd = self._render_rules_list(rules, page=0)
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_pg:(\d+)$"))
        async def _cb_rules_page(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            page = int(raw.split(":", 1)[1])
            rules = await (self._rules.list_all() if self._is_admin(uid) else self._rules.list_by_owner(uid))
            text, kbd = self._render_rules_list(rules, page=page)
            try:
                await cq.edit_message_text(text, reply_markup=kbd)
            except Exception:
                pass
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
            else:
                lines.append("👇 برای بررسی سلامت، تست اتصال یا تغییر تنظیمات، روی هر پیکربندی کلیک کنید:")
            for c in cfgs:
                status = "🟢" if c.is_enabled else "🔴"
                lines.append(
                    f"{status} `{c.id[:8]}` {c.name} "
                    f"[{self._provider_label(c.provider.value)}/{c.model}]")
            await cq.edit_message_text("\n".join(lines), reply_markup=self._ai_menu(cfgs))
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

        @b.on_callback_query(filters.regex("^" + CB["back"] + "$"))
        async def _cb_back(_, cq: CallbackQuery):
            await cq.edit_message_text(self._t("ui_main_title"), reply_markup=self._main_menu())
            await cq.answer()

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
                rules = await self._rules.list_all()
                text, kbd = self._render_rules_list(rules, page=0)
                await cq.edit_message_text(text, reply_markup=kbd)
            else:
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
                if not self._is_admin(uid):
                    return await cq.answer(self._t("ui_need_admin"), show_alert=True)
                st = self._state(uid)
                st.step = "login_phone"
                await self._persist(uid, st)
                await cq.edit_message_text(
                    self._t("ui_login_phone"), reply_markup=self._cancel_kbd())
                await cq.answer()
                logger.info("_cb_login prompt sent successfully for uid=%s; step=login_phone", uid)
            except Exception as exc:
                logger.exception("_cb_login failed for uid=%s: %s", uid, exc)
                try:
                    await cq.answer(f"⚠️ خطا: {exc}", show_alert=True)
                except Exception:
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
            st.rule_source = ""
            st.rule_target = ""
            st.buffer = {}

            try:
                await cq.answer("⏳ در حال دریافت لیست گفتگوها...")
            except Exception:
                pass

            chats = await self._fetch_dialogs()
            st.buffer["chats"] = chats
            st.buffer["page"] = 0
            await self._persist(cq.from_user.id, st)

            text, markup = self._render_chat_picker(st, role="source", page=0)
            await cq.edit_message_text(text, reply_markup=markup)

        @b.on_callback_query(filters.regex(r"^ch:(s|t):(\d+)$"))
        async def _cb_pick_chat(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"
            idx = int(parts[2])

            chats = st.buffer.get("chats", []) if isinstance(st.buffer, dict) else []
            if not (0 <= idx < len(chats)):
                return await cq.answer("⚠️ گفتگو یافت نشد. لطفاً لیست را تازه‌سازی کنید.", show_alert=True)

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
                    return await cq.answer("❌ این قانون یافت نشد یا ممکن است حذف شده باشد.", show_alert=True)

                draft_obj = st.buffer.get("draft") if (in_draft and isinstance(st.buffer, dict)) else None
                draft: Dict[str, Any] = draft_obj if isinstance(draft_obj, dict) else rule.to_draft()
                if in_draft and not isinstance(draft_obj, dict) and isinstance(st.buffer, dict):
                    st.buffer["draft"] = draft

                if edit_role == "source":
                    target_id = draft.get("target_chat_id") if in_draft else rule.target_chat_id
                    if str(chat_id).strip() == str(target_id).strip():
                        return await cq.answer("❌ مبدأ نمی‌تواند با مقصد یکسان باشد (جلوگیری از ایجاد حلقه)!", show_alert=True)

                    if in_draft:
                        draft["source_chat_id"] = str(chat_id)
                        draft["source_name"] = selected["title"]
                        st.step = "rule_editing_draft"
                        await self._persist(uid, st)
                        await cq.answer(f"✅ مبدأ پیش‌نویس تغییر کرد: {selected['title']}")
                        text, markup = self._render_draft_editor(rule, draft)
                        return await cq.edit_message_text(text, reply_markup=markup)
                    else:
                        rule.source_chat_id = str(chat_id)
                        rule.source_chat_name = selected["title"]
                        expected_ver = st.buffer.get("version")
                        try:
                            await self._rules.update(rule, expected_version=expected_ver)
                        except ConcurrencyError:
                            return await cq.answer("⚠️ خطا: این قانون هم‌زمان ویرایش شده است.", show_alert=True)
                        st.step = ""
                        st.buffer = {}
                        await self._persist(uid, st)
                        await cq.answer(f"✅ مبدأ قانون تغییر کرد: {selected['title']}")
                        text, markup = self._render_rule_detail(rule)
                        return await cq.edit_message_text(text, reply_markup=markup)

                elif edit_role == "target":
                    source_id = draft.get("source_chat_id") if in_draft else rule.source_chat_id
                    if str(chat_id).strip() == str(source_id).strip():
                        return await cq.answer("❌ مقصد نمی‌تواند با مبدأ یکسان باشد (جلوگیری از ایجاد حلقه)!", show_alert=True)

                    if in_draft:
                        draft["target_chat_id"] = str(chat_id)
                        draft["target_name"] = selected["title"]
                        st.step = "rule_editing_draft"
                        await self._persist(uid, st)
                        await cq.answer(f"✅ مقصد پیش‌نویس تغییر کرد: {selected['title']}")
                        text, markup = self._render_draft_editor(rule, draft)
                        return await cq.edit_message_text(text, reply_markup=markup)
                    else:
                        rule.target_chat_id = str(chat_id)
                        rule.target_chat_name = selected["title"]
                        expected_ver = st.buffer.get("version")
                        try:
                            await self._rules.update(rule, expected_version=expected_ver)
                        except ConcurrencyError:
                            return await cq.answer("⚠️ خطا: این قانون هم‌زمان ویرایش شده است.", show_alert=True)
                        st.step = ""
                        st.buffer = {}
                        await self._persist(uid, st)
                        await cq.answer(f"✅ مقصد قانون تغییر کرد: {selected['title']}")
                        text, markup = self._render_rule_detail(rule)
                        return await cq.edit_message_text(text, reply_markup=markup)

            if role == "source":
                st.rule_source = chat_id
                st.buffer["source_name"] = chat_title
                st.step = "rule_target"
                st.buffer["page"] = 0
                await self._persist(uid, st)
                await cq.answer(f"✅ مبدأ: {selected['title']}", show_alert=False)
                text, markup = self._render_chat_picker(st, role="target", page=0)
                await cq.edit_message_text(text, reply_markup=markup)
            else:
                st.rule_target = chat_id
                target_title = chat_title
                source_title = st.buffer.get("source_name") or st.rule_source
                await cq.answer("✅ در حال ایجاد قانون...", show_alert=False)
                await self._finish_rule_creation(cq, st, source_title, target_title)

        @b.on_callback_query(filters.regex(r"^pg:(s|t):(\d+)$"))
        async def _cb_page_chat(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"
            page = int(parts[2])

            if isinstance(st.buffer, dict):
                st.buffer["page"] = page
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role=role, page=page)
            try:
                await cq.edit_message_text(text, reply_markup=markup)
            except Exception:
                pass
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rfc:(s|t)$"))
        async def _cb_refresh_chats(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            raw_data = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            parts = raw_data.split(":")
            role = "source" if parts[1] == "s" else "target"

            try:
                await cq.answer("🔄 در حال تازه‌سازی لیست گفتگوها...")
            except Exception:
                pass

            chats = await self._fetch_dialogs()
            if isinstance(st.buffer, dict):
                st.buffer["chats"] = chats
                st.buffer["page"] = 0
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role=role, page=0)
            await cq.edit_message_text(text, reply_markup=markup)

        @b.on_callback_query(filters.regex(r"^r_cancel$"))
        async def _cb_rule_cancel(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            st.step = ""
            st.rule_source = ""
            st.rule_target = ""
            if isinstance(st.buffer, dict):
                st.buffer.clear()
            await self._persist(uid, st)
            rules = await self._rules.list_all()
            text, kbd = self._render_rules_list(rules, page=0)
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer("❌ عملیات ایجاد قانون لغو شد")

        @b.on_callback_query(filters.regex(r"^r_back_source$"))
        async def _cb_rule_back_source(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            uid = cq.from_user.id
            st = self._state(uid)
            st.step = "rule_source"
            st.rule_target = ""
            if isinstance(st.buffer, dict):
                st.buffer["page"] = 0
            await self._persist(uid, st)
            text, markup = self._render_chat_picker(st, role="source", page=0)
            await cq.edit_message_text(text, reply_markup=markup)
            await cq.answer("⬅️ بازگشت به انتخاب مبدأ")

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
                [("❌ انصراف و بازگشت", f"rd:{rid}")],
            ])
            await cq.edit_message_text(confirm_text, reply_markup=confirm_kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rdelyes:(.+)$"))
        async def _cb_rule_del_confirm(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rid = raw.split(":", 1)[1]
            rule = await self._rules.get(rid)
            if not rule:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            if not (self._is_admin(uid) or rule.owner_user_id == uid):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await self._rules.delete(rid)
            try:
                self._log.info("bot", "rule", f"rule {rid} deleted")
            except Exception:
                pass
            rules = await (self._rules.list_all() if self._is_admin(uid) else self._rules.list_by_owner(uid))
            text, kbd = self._render_rules_list(rules, page=0)
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer(self._t("ui_rule_deleted"), show_alert=True)

        @b.on_callback_query(filters.regex("^" + CB["rule_toggle"] + ":"))
        async def _cb_rule_toggle(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rid = raw.split(":", 1)[1]
            rule = await self._rules.get(rid)
            if not rule:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            if not (self._is_admin(uid) or rule.owner_user_id == uid):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await self._rules.toggle(rid)
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
            await cq.edit_message_text("\n".join(lines), reply_markup=self._rules_menu(rules))
            await cq.answer(self._t("ui_done"))

        # ---------------- rule detailed settings & controls ----------------
        @b.on_callback_query(filters.regex(r"^rd:(.+)$"))
        async def _cb_rule_detail(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer(self._t("ui_none"), show_alert=True)
            if not (self._is_admin(uid) or rule.owner_user_id == uid):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            rule = await self._rules.get(rule_id)
            if not rule:
                st = self._state(cq.from_user.id)
                st.step = ""
                if isinstance(st.buffer, dict):
                    st.buffer.clear()
                await self._persist(cq.from_user.id, st)
                rules = await self._rules.list_all()
                text, kbd = self._render_rules_list(rules, page=0)
                try:
                    await cq.edit_message_text(text, reply_markup=kbd)
                except Exception:
                    pass
                return await cq.answer("❌ این قانون یافت نشد یا ممکن است حذف شده باشد.", show_alert=True)
            text, kbd = self._render_rule_detail(rule)
            await cq.edit_message_text(text, reply_markup=kbd)
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
            await cq.edit_message_text(text, reply_markup=kbd)
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
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف و بازگشت", f"rtx:{rule_id}")]]))
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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rhf:(.+)$"))
        async def _cb_rule_header_footer_menu(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            text, kbd = self._render_header_footer_menu(rule)
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rh_s:(.+)$"))
        async def _cb_rule_header_set(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            st = self._state(cq.from_user.id)
            st.step = "rule_header_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(cq.from_user.id, st)
            prompt = (
                "📝 **تنظیم متن هدر (پیشوند پیام):**\n\n"
                "متنی که می‌خواهید در ابتدای تمام پیام‌های ارسالی قرار گیرد را ارسال کنید.\n\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف", f"rhf:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rh_d:(.+)$"))
        async def _cb_rule_header_del(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["header"] = ""
            await self._rules.update(rule)
            await cq.answer("🗑 هدر با موفقیت حذف شد")
            text, kbd = self._render_header_footer_menu(rule)
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rf_s:(.+)$"))
        async def _cb_rule_footer_set(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            st = self._state(cq.from_user.id)
            st.step = "rule_footer_input"
            st.buffer = {"rule_id": rule_id}
            await self._persist(cq.from_user.id, st)
            prompt = (
                "📝 **تنظیم متن فوتر (پسوند پیام):**\n\n"
                "متنی که می‌خواهید در انتهای تمام پیام‌های ارسالی قرار گیرد را ارسال کنید (مثلاً آیدی کانال، لینک، متن دلخواه).\n\n"
                "برای انصراف دکمه زیر را لمس کنید:"
            )
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف", f"rhf:{rule_id}")]]))
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^rf_d:(.+)$"))
        async def _cb_rule_footer_del(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            rule.metadata["footer"] = ""
            await self._rules.update(rule)
            await cq.answer("🗑 فوتر با موفقیت حذف شد")
            text, kbd = self._render_header_footer_menu(rule)
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^red:(.+)$"))
        async def _cb_rule_toggle_edit(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.ignore_edits = not rule.ignore_edits
            await self._rules.update(rule)
            status = "نادیده گرفتن ادیت فعال شد ⏹" if rule.ignore_edits else "سینک ادیت مقصد فعال شد 🔄"
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rdl:(.+)$"))
        async def _cb_rule_toggle_delete(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            if not isinstance(rule.metadata, dict):
                rule.metadata = {}
            new_val = not rule.sync_deletes
            rule.metadata["sync_deletes"] = new_val
            await self._rules.update(rule)
            status = "سینک حذف پیام فعال شد 🗑" if new_val else "نادیده گرفتن حذف فعال شد ⏹"
            await cq.answer(status)
            text, kbd = self._render_rule_detail(rule)
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rtg:(.+)$"))
        async def _cb_rule_toggle_active(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                return await cq.answer("❌ قانون یافت نشد", show_alert=True)
            rule.is_active = not rule.is_active
            await self._rules.update(rule)
            status = "فعال شد 🟢" if rule.is_active else "غیرفعال شد 🔴"
            await cq.answer(f"وضعیت: {status}")
            text, kbd = self._render_rule_detail(rule)
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^rrm:(.+)$"))
        async def _cb_rule_remove(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            await self._rules.delete(rule_id)
            await cq.answer("🗑 قانون با موفقیت حذف شد", show_alert=True)
            rules = await self._rules.list_all()
            text, kbd = self._render_rules_list(rules, page=0)
            await cq.edit_message_text(text, reply_markup=kbd)

        # ---------------- rule edit (draft & direct) handlers ----------------
        @b.on_callback_query(filters.regex(r"^re:(.+)$"))
        async def _cb_rule_edit_draft(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            rule_id = raw.split(":", 1)[1]
            rule = await self._rules.get(rule_id)
            if not rule:
                st = self._state(cq.from_user.id)
                st.step = ""
                if isinstance(st.buffer, dict):
                    st.buffer.clear()
                await self._persist(cq.from_user.id, st)
                rules = await self._rules.list_all()
                text, kbd = self._render_rules_list(rules, page=0)
                try:
                    await cq.edit_message_text(text, reply_markup=kbd)
                except Exception:
                    pass
                return await cq.answer("❌ این قانون یافت نشد یا ممکن است حذف شده باشد.", show_alert=True)

            uid = cq.from_user.id
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
            await cq.edit_message_text(text, reply_markup=kbd)
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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            rule = await self._rules.get(rule_id)
            await cq.answer("❌ ویرایش پیش‌نویس لغو شد و هیچ تغییری ذخیره نگردید")
            if rule:
                text, kbd = self._render_rule_detail(rule)
                await cq.edit_message_text(text, reply_markup=kbd)
            else:
                rules = await self._rules.list_all()
                text, kbd = self._render_rules_list(rules, page=0)
                await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ed_s:(.+)$"))
        async def _cb_rule_draft_source(_, cq: CallbackQuery):
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
            if not isinstance(st.buffer, dict):
                st.buffer = {}
            if "draft" not in st.buffer:
                st.buffer["draft"] = rule.to_draft()
            st.buffer["rule_id"] = rule.id
            st.buffer["version"] = rule.version
            st.buffer["in_draft"] = True
            st.buffer["edit_role"] = "source"
            st.buffer["page"] = 0
            chats = await self._fetch_dialogs()
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="source", page=0)
            await cq.edit_message_text(text, reply_markup=markup)
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
            chats = await self._fetch_dialogs()
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="target", page=0)
            await cq.edit_message_text(text, reply_markup=markup)
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
            chats = await self._fetch_dialogs()
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="source", page=0)
            await cq.edit_message_text(text, reply_markup=markup)
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
            chats = await self._fetch_dialogs()
            st.buffer["chats"] = chats
            await self._persist(uid, st)

            text, markup = self._render_chat_picker(st, role="target", page=0)
            await cq.edit_message_text(text, reply_markup=markup)
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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)
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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
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
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(text, reply_markup=kbd)

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
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
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
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
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
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف", f"rsm:{rule_id}")]]))
            await cq.answer()

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
            await cq.edit_message_text(prompt_text, reply_markup=kbd)
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
                await cq.edit_message_text(msg_text, reply_markup=kb)
            else:
                await cq.edit_message_text(msg_text, reply_markup=self._cancel_kbd())
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

        @b.on_callback_query(filters.regex(r"^aid:(.+)"))
        async def _cb_ai_detail(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get(aid)
            if not cfg:
                await cq.answer("❌ این پیکربندی یافت نشد.", show_alert=True)
                cfgs = await self._ai.list_all()
                return await cq.edit_message_text(self._t("ui_ai_title"), reply_markup=self._ai_menu(cfgs))
            text, kbd = self._render_ai_detail(cfg)
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_hc:(.+)"))
        async def _cb_ai_hc(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get(aid)
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            await cq.answer("⏳ در حال بررسی سلامت اتصال و سنجش پینگ...", show_alert=False)
            ok, reply_or_err, latency = await self._ai.health_check(aid)
            health_info = {
                "ok": ok,
                "latency_ms": latency,
                "reply": reply_or_err if ok else "",
                "error": reply_or_err if not ok else "",
            }
            text, kbd = self._render_ai_detail(cfg, health_info=health_info)
            try:
                await cq.edit_message_text(text, reply_markup=kbd)
            except Exception:
                pass

        @b.on_callback_query(filters.regex(r"^(?:ai_sample|" + CB["ai_test"] + r"):(.+)"))
        async def _cb_ai_sample(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get(aid)
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            await cq.answer("⏳ در حال ارسال متن نمونه جهت بازنویسی...", show_alert=False)
            sample_text = "سلام! این یک پیام آزمایشی جهت سنجش کارکرد هوش مصنوعی در فوروارد تلگرام است."
            ok, reply = await self._ai.test_rewrite(aid, sample_text)
            if ok:
                body = f"✅ **تست بازنویسی با موفقیت انجام شد:**\n\n📝 **پاسخ مدل ({cfg.model}):**\n`{reply[:1000]}`"
            else:
                body = f"❌ **خطا در بازنویسی:**\n\n`{reply[:1000]}`"
            kbd = self._kbd([[("⬅️ بازگشت به پیکربندی", f"aid:{aid}")]])
            await cq.edit_message_text(body, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ai_tog:(.+)"))
        async def _cb_ai_toggle(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get(aid)
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            cfg.is_enabled = not cfg.is_enabled
            await self._ai.update(cfg)
            status_text = "فعال شد 🟢" if cfg.is_enabled else "غیرفعال شد 🔴"
            await cq.answer(f"وضعیت با موفقیت {status_text}", show_alert=False)
            text, kbd = self._render_ai_detail(cfg)
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^ai_del_ask:(.+)"))
        async def _cb_ai_del_ask(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get(aid)
            if not cfg:
                return await cq.answer("❌ پیکربندی یافت نشد.", show_alert=True)
            text = (
                f"⚠️ **آیا از حذف این پیکربندی هوش مصنوعی اطمینان دارید؟**\n\n"
                f"🏷 نام: `{cfg.name}`\n"
                f"🆔 شناسه: `{cfg.id[:8]}`"
            )
            kbd = self._kbd([
                [("🗑 بله، حذف شود", f"ai_del_confirm:{aid}")],
                [("⬅️ انصراف و بازگشت", f"aid:{aid}")],
            ])
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^(?:ai_del_confirm|" + CB["ai_del"] + r"):(.+)"))
        async def _cb_ai_del_confirm(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            await self._ai.delete(aid)
            await cq.answer("✅ پیکربندی حذف شد.", show_alert=True)
            cfgs = await self._ai.list_all()
            lines = [self._t("ui_ai_title"), "", "✅ پیکربندی با موفقیت حذف گردید."]
            await cq.edit_message_text("\n".join(lines), reply_markup=self._ai_menu(cfgs))

        @b.on_callback_query(filters.regex(r"^ai_eh:(.+)"))
        async def _cb_ai_edit_host(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get(aid)
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
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_em:(.+)"))
        async def _cb_ai_edit_model(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get(aid)
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
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^ai_ek:(.+)"))
        async def _cb_ai_edit_key(_, cq: CallbackQuery):
            aid = str(cq.data or "").split(":", 1)[1]
            cfg = await self._ai.get(aid)
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
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer()

        # ---------------- PV Responder (AI PV Assistant) ----------------
        @b.on_callback_query(filters.regex(r"^pv:menu$"))
        async def _cb_pv_menu(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await cq.answer()
            text, kbd = self._render_pv_menu()
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:toggle$"))
        async def _cb_pv_toggle(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer("❌ ماژول در دسترس نیست.", show_alert=True)
            resp.config.enabled = not resp.config.enabled
            resp.save_config(resp.config)
            status_msg = "پاسخگوی پی‌وی فعال شد 🟢" if resp.config.enabled else "پاسخگوی پی‌وی غیرفعال شد 🔴"
            await cq.answer(status_msg)
            text, kbd = self._render_pv_menu()
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:persona$"))
        async def _cb_pv_persona(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await cq.answer()
            text, kbd = self._render_pv_persona_menu()
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:set_p:(.+)$"))
        async def _cb_pv_set_persona(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer("❌ ماژول در دسترس نیست.", show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            p_type = raw.split(":", 2)[2]
            if p_type == "casual":
                from core.infrastructure.telegram.pv_responder import DEFAULT_PERSONA
                resp.config.persona_prompt = DEFAULT_PERSONA
                resp.save_config(resp.config)
                await cq.answer("✅ لحن خودمانی و جذاب تنظیم شد.")
                text, kbd = self._render_pv_menu()
                await cq.edit_message_text(text, reply_markup=kbd)
            elif p_type == "business":
                resp.config.persona_prompt = (
                    "شما صاحب این اکانت شخصی تلگرام هستید. با لحنی کاملاً مؤدبانه، حرفه‌ای، "
                    "محترمانه و در عین حال روان و انسانی به زبان فارسی به پیام‌ها پاسخ دهید. "
                    "هرگز اشاره نکن که هوش مصنوعی یا ربات هستی. پاسخ‌ها موجز و صریح باشند."
                )
                resp.save_config(resp.config)
                await cq.answer("✅ لحن کاری و محترمانه تنظیم شد.")
                text, kbd = self._render_pv_menu()
                await cq.edit_message_text(text, reply_markup=kbd)
            elif p_type == "short":
                resp.config.persona_prompt = (
                    "شما صاحب اکانت تلگرام هستید. پیام‌ها را بسیار کوتاه، خودمانی، "
                    "سریع و حداکثر در یک یا دو جمله جذاب و دوستانه پاسخ بده. "
                    "اصلاً شبیه ربات نباش و هرگز نگو بات هستی."
                )
                resp.save_config(resp.config)
                await cq.answer("✅ لحن کوتاه و سریع تنظیم شد.")
                text, kbd = self._render_pv_menu()
                await cq.edit_message_text(text, reply_markup=kbd)
            elif p_type == "custom":
                st = self._state(cq.from_user.id)
                st.step = "pv_persona_input"
                await self._persist(cq.from_user.id, st)
                prompt_text = (
                    "✍️ **تنظیم پرامپت اختصاصی برای هوش مصنوعی پی‌وی:**\n\n"
                    "لطفاً دستورالعمل یا شخصیت دلخواه خود را به زبان فارسی ارسال کنید.\n"
                    "مثال: «مثل یک دوست صمیمی و پرانرژی حرف بزن و بگو فعلاً بیرون هستم و شب پیام میدم»"
                )
                await cq.edit_message_text(prompt_text, reply_markup=self._kbd([[("❌ انصراف", "pv:persona")]]))
                await cq.answer()

        @b.on_callback_query(filters.regex(r"^pv:delays$"))
        async def _cb_pv_delays(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            await cq.answer()
            text, kbd = self._render_pv_delays_menu()
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:set_d:(.+)$"))
        async def _cb_pv_set_delay(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            resp = getattr(self, "_pv_responder", None)
            if not resp:
                return await cq.answer("❌ ماژول در دسترس نیست.", show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
            d_type = raw.split(":", 2)[2]
            if d_type == "fast":
                resp.config.typing_delay_min = 1.5
                resp.config.typing_delay_max = 3.0
            elif d_type == "normal":
                resp.config.typing_delay_min = 2.0
                resp.config.typing_delay_max = 4.5
            elif d_type == "slow":
                resp.config.typing_delay_min = 3.5
                resp.config.typing_delay_max = 6.5
            resp.save_config(resp.config)
            await cq.answer("✅ زمان‌بندی تأخیر با موفقیت اعمال شد.")
            text, kbd = self._render_pv_menu()
            await cq.edit_message_text(text, reply_markup=kbd)

        @b.on_callback_query(filters.regex(r"^pv:test$"))
        async def _cb_pv_test(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            st = self._state(cq.from_user.id)
            st.step = "pv_test_input"
            await self._persist(cq.from_user.id, st)
            prompt = (
                "🧪 **تست شبیه‌سازی پاسخگوی هوشمند پی‌وی:**\n\n"
                "یک پیام نمونه که ممکن است مخاطبی برای شما بفرستد ارسال کنید "
                "(مثلاً: «سلام چطوری؟ کجایی؟» یا «سلام فایل پروژه آماده شد؟»)\n"
                "تا ببینید هوش مصنوعی با شخصیت انتخابی شما چگونه پاسخ می‌دهد."
            )
            await cq.edit_message_text(prompt, reply_markup=self._kbd([[("❌ انصراف", "pv:menu")]]))
            await cq.answer()

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
        @b.on_message(filters.private & filters.text & ~filters.command(["start", "menu"]), group=-1)
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
    def _web_base_url(self) -> str:
        return self._web_url or "http://localhost:8088"

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
            await cq.edit_message_text(text, reply_markup=kbd, disable_web_page_preview=True)
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
                await cq.edit_message_text(body, reply_markup=self._main_menu())
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
                self._t("ui_login_phone_bad"), reply_markup=self._cancel_kbd())
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
                await cq.edit_message_text(
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
                    await cq.edit_message_text(
                        self._login_code_text(st),
                        reply_markup=self._code_keypad(st),
                    )
                except Exception:
                    pass
            else:
                try:
                    await cq.edit_message_text(
                        f"⏳ در حال بررسی و تأیید کد: `{' '.join(digits)}`...",
                    )
                except Exception:
                    pass
                await self._submit_code_flow(cq.message, st, digits, uid=uid)

    async def _step_login_code(self, message: Message, st: UiState, text: str) -> None:
        uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        norm_text = (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        clean = re.sub(r"\D", "", norm_text)
        logger.info("Received login code input from uid=%s (raw_len=%d, clean_len=%d)",
                    uid, len(text), len(clean))
        if len(clean) not in (5, 6):
            await message.reply_text(
                "❌ کد ورود باید ۵ یا ۶ رقم باشد.\n\n"
                "💡 **الگوی ارسال:**\n"
                "• بنویسید چسبیده به mycode: `mycode73737`\n"
                "• یا از **کیپد عددی زیر** استفاده کنید\n"
                "• یا ارقام را با **فاصله** بفرستید: `1 2 3 4 5`",
                reply_markup=self._code_keypad(st),
            )
            return
        await self._submit_code_flow(message, st, clean, uid=uid)

    async def _submit_code_flow(
        self, message: Message, st: UiState, clean_code: str, uid: Optional[int] = None
    ) -> None:
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
                self._t("ui_login_password"), reply_markup=self._cancel_kbd())
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
        if uid is None:
            uid = message.from_user.id if message.from_user else (message.chat.id if message.chat else 0)
        phone = getattr(st, "login_phone", "") or ""

        if result in ("auth_expired", "code_expired"):
            st.step = "login_phone"
            if isinstance(st.buffer, dict):
                st.buffer.pop("code_digits", None)
            await self._persist(uid, st)
            retry_btn = self._kbd([[("🔄 " + self._t("ui_login_retry"), CB["login_retry"])]]) if phone else self._cancel_kbd()
            if result == "code_expired":
                text = (
                    "⌛ این کد منقضی شده یا توسط تلگرام مسدود شده است.\n\n"
                    "💡 **علت:** ارسال کد خالی در چت، سیستم ضدسرقت تلگرام را فعال کرده و کد را باطل می‌کند.\n\n"
                    "👇 دکمه «🔄 دریافت مجدد کد» را بزنید و سپس کد جدید را چسبیده با mycode (مانند: `mycode73737`) یا با **کیپد عددی** وارد کنید:"
                )
                await message.reply_text(text, reply_markup=retry_btn)
            else:
                await message.reply_text(self._t("ui_login_expired", phone=phone), reply_markup=retry_btn)
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
                self._t("ui_login_done", phone=phone),
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
                sessions = await self._sessions.list_all()
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
                sessions = await self._sessions.list_all()
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
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
                sessions = await self._sessions.list_all()
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
                reply_markup=self._kbd([[("❌ انصراف و بازگشت", cancel_cb)]]),
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
                await self._rules.update(rule, expected_version=expected_ver)
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
        raw = text.strip()
        uid = int(message.from_user.id if message.from_user else (message.chat.id if message.chat else 0))

        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
                sessions = await self._sessions.list_all()
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
                reply_markup=self._kbd([[("❌ انصراف و بازگشت", cancel_cb)]]),
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
                await self._rules.update(rule, expected_version=expected_ver)
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
        rule = await self._rules.get(rule_id) if rule_id else None
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
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
        await self._rules.update(rule)

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
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
        await self._rules.update(rule)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text("✅ **هدر با موفقیت تنظیم شد.**")
        menu_text, menu_kbd = self._render_header_footer_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    async def _step_rule_footer_input(self, message: Message, st: UiState, text: str) -> None:
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
        await self._rules.update(rule)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text("✅ **فوتر با موفقیت تنظیم شد.**")
        menu_text, menu_kbd = self._render_header_footer_menu(rule)
        await message.reply_text(menu_text, reply_markup=menu_kbd)

    # ---------------- smart routing FSM steps ----------------
    async def _step_smart_set_intermediate(self, message: Message, st: UiState, text: str) -> None:
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
        await self._rules.update(rule)

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
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
        await self._rules.update(rule)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text("✅ **هدر اختصاصی کپی با موفقیت ذخیره شد.**")
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_keywords(self, message: Message, st: UiState, text: str) -> None:
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
        await self._rules.update(rule)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(ans)
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_set_regex(self, message: Message, st: UiState, text: str) -> None:
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
        await self._rules.update(rule)

        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)

        await message.reply_text(ans)
        m_text, m_kbd = self._render_smart_menu(rule)
        await message.reply_text(m_text, reply_markup=m_kbd)

    async def _step_smart_test_rule(self, message: Message, st: UiState, text: str) -> None:
        rule_id = st.buffer.get("rule_id", "") if isinstance(st.buffer, dict) else ""
        rule = await self._rules.get(rule_id) if rule_id else None
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
            [("❌ " + self._t("ui_cancel"), CB["cancel"])],
        ])
        await self._persist(message.from_user.id, st)
        await message.reply_text(prompt_text, reply_markup=kbd)

    async def _step_ai_base_url(self, message: Message, st: UiState, text: str) -> None:
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
            + self._t("ui_ai_model")
            + "\n(از لیست زیر انتخاب کنید یا نام مدل دلخواه مانند `coding` را تایپ کنید):"
        )
        if kb is not None:
            await message.reply_text(msg_text, reply_markup=kb)
        else:
            await message.reply_text(msg_text, reply_markup=self._cancel_kbd())

    async def _step_ai_model(self, message: Message, st: UiState, text: str) -> None:
        st.ai_model = text.strip()
        st.step = "ai_api_key"
        await self._persist(message.from_user.id, st)
        await message.reply_text(self._t("ui_ai_key"), reply_markup=self._cancel_kbd())

    async def _step_ai_api_key(self, message: Message, st: UiState, text: str) -> None:
        st.step = ""
        user_id = message.from_user.id
        try:
            cfg = AIConfig(
                name=st.ai_name,
                provider=AIProviderType(st.ai_provider),
                model=st.ai_model,
                base_url=st.ai_base_url or "",
                api_key=text.strip(),
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
            cfgs = await self._ai.list_all()
            await message.reply_text(
                self._t("ui_ai_fail", error=tg_detail(exc)),
                reply_markup=self._ai_menu(cfgs))
        finally:
            try:
                await message.delete()
            except Exception:
                pass  # api key is sensitive — best-effort cleanup

    async def _step_ai_edit_host(self, message: Message, st: UiState, text: str) -> None:
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        cfg = await self._ai.get(aid) if aid else None
        if not cfg:
            cfgs = await self._ai.list_all()
            await message.reply_text("❌ پیکربندی یافت نشد.", reply_markup=self._ai_menu(cfgs))
            return
        new_url = text.strip()
        if new_url.lower() in ("default", "none", "-", "پیش‌فرض", "پیش فرض"):
            new_url = ""
        elif not new_url.startswith("http://") and not new_url.startswith("https://"):
            new_url = "http://" + new_url
        cfg.base_url = new_url
        await self._ai.update(cfg)
        ok, reply_or_err, latency = await self._ai.health_check(cfg.id)
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text("✅ آدرس هاست به‌روزرسانی شد:\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_ai_edit_model(self, message: Message, st: UiState, text: str) -> None:
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        cfg = await self._ai.get(aid) if aid else None
        if not cfg:
            cfgs = await self._ai.list_all()
            await message.reply_text("❌ پیکربندی یافت نشد.", reply_markup=self._ai_menu(cfgs))
            return
        cfg.model = text.strip()
        await self._ai.update(cfg)
        ok, reply_or_err, latency = await self._ai.health_check(cfg.id)
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text("✅ مدل با موفقیت تغییر کرد:\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_ai_edit_key(self, message: Message, st: UiState, text: str) -> None:
        aid = str(st.buffer.get("ai_edit_id", ""))
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        try:
            await message.delete()
        except Exception:
            pass
        cfg = await self._ai.get(aid) if aid else None
        if not cfg:
            cfgs = await self._ai.list_all()
            await message.reply_text("❌ پیکربندی یافت نشد.", reply_markup=self._ai_menu(cfgs))
            return
        cfg.api_key = text.strip()
        await self._ai.update(cfg)
        ok, reply_or_err, latency = await self._ai.health_check(cfg.id)
        health_info = {
            "ok": ok,
            "latency_ms": latency,
            "reply": reply_or_err if ok else "",
            "error": reply_or_err if not ok else "",
        }
        detail_text, detail_kbd = self._render_ai_detail(cfg, health_info=health_info)
        await message.reply_text("✅ کلید API با موفقیت به‌روزرسانی شد:\n\n" + detail_text, reply_markup=detail_kbd)

    async def _step_pv_persona_input(self, message: Message, st: UiState, text: str) -> None:
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            await message.reply_text("❌ ماژول پاسخگوی پی‌وی یافت نشد.", reply_markup=self._main_menu())
            return
        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            text_out, kbd = self._render_pv_menu()
            await message.reply_text("❌ تنظیم پرامپت لغو شد.", reply_markup=kbd)
            return

        resp.config.persona_prompt = text.strip()
        resp.save_config(resp.config)
        await message.reply_text("✅ پرامپت و شخصیت پاسخگوی پی‌وی با موفقیت ثبت گردید!")
        text_out, kbd = self._render_pv_menu()
        await message.reply_text(text_out, reply_markup=kbd)

    async def _step_pv_test_input(self, message: Message, st: UiState, text: str) -> None:
        st.step = ""
        st.buffer = {}
        await self._persist(message.from_user.id, st)
        resp = getattr(self, "_pv_responder", None)
        if not resp:
            await message.reply_text("❌ ماژول پاسخگوی پی‌وی یافت نشد.", reply_markup=self._main_menu())
            return
        if text.strip().lower() in ("/cancel", "انصراف", "لغو", "بازگشت"):
            text_out, kbd = self._render_pv_menu()
            await message.reply_text("❌ تست لغو شد.", reply_markup=kbd)
            return

        ai_cfg = await resp.get_active_ai_config()
        if not ai_cfg:
            text_out, kbd = self._render_pv_menu()
            await message.reply_text("⚠️ هیچ هوش مصنوعی فعالی برای پاسخگویی یافت نشد. لطفاً در بخش AI یک مدل فعال کنید.", reply_markup=kbd)
            return

        wait_msg = await message.reply_text("⏳ در حال پردازش پیام با شخصیت تنظیم‌شده و شبیه‌سازی تایپینگ...")
        try:
            from core.infrastructure.ai.providers import OpenAICompatibleProvider, AIProviderFactory
            prov_key = ai_cfg.provider.value if hasattr(ai_cfg.provider, "value") else str(ai_cfg.provider)
            factory = resp._ai_factory or AIProviderFactory()
            provider_cls = factory._registry.get(prov_key, OpenAICompatibleProvider)
            provider = provider_cls()

            messages = [
                {"role": "system", "content": resp.config.persona_prompt},
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
                f"⏱ تاخیر شبیه‌سازی تایپینگ: `{resp.config.typing_delay_min:.1f}` تا `{resp.config.typing_delay_max:.1f}` ثانیه"
            )
            try:
                await wait_msg.delete()
            except Exception:
                pass
            text_out, kbd = self._render_pv_menu()
            await message.reply_text(body, reply_markup=kbd)
        except Exception as exc:
            try:
                await wait_msg.delete()
            except Exception:
                pass
            text_out, kbd = self._render_pv_menu()
            await message.reply_text(f"❌ خطا در اجرای تست هوش مصنوعی: {exc}", reply_markup=kbd)
