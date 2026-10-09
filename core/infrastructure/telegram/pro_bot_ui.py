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

        text = (
            "⚙️ **تنظیمات پیشرفته قانون انتقال**\n\n"
            f"📡 **مبدأ:** `{src_label}` (`{rule.source_chat_id}`)\n"
            f"🎯 **مقصد:** `{dst_label}` (`{rule.target_chat_id}`)\n"
            f"📊 **وضعیت:** {status_str}\n"
            f"⚡ **حالت ارسال:** {mode_title}\n"
            "────────────────────\n"
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
            await cq.edit_message_text(self._t("ui_main_title"), reply_markup=self._main_menu())
            await cq.answer()

        @b.on_callback_query(filters.regex("^" + CB["sessions"] + "$"))
        async def _cb_sessions(_, cq: CallbackQuery):
            uid = cq.from_user.id if cq.from_user else 0
            logger.info("Executing _cb_sessions for uid=%s", uid)
            try:
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
            except Exception as exc:
                logger.exception("_cb_sessions failed for uid=%s: %s", uid, exc)
                try:
                    await cq.answer(f"⚠️ خطا: {exc}", show_alert=True)
                except Exception:
                    pass

        @b.on_callback_query(filters.regex("^" + CB["rules"] + "$"))
        async def _cb_rules(_, cq: CallbackQuery):
            rules = await self._rules.list_all()
            text, kbd = self._render_rules_list(rules, page=0)
            await cq.edit_message_text(text, reply_markup=kbd)
            await cq.answer()

        @b.on_callback_query(filters.regex(r"^r_pg:(\d+)$"))
        async def _cb_rules_page(_, cq: CallbackQuery):
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, (bytes, bytearray)) else str(cq.data or "")
            page = int(raw.split(":", 1)[1])
            rules = await self._rules.list_all()
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

        @b.on_callback_query(filters.regex("^" + CB["rule_del"] + ":"))
        async def _cb_rule_del(_, cq: CallbackQuery):
            rid = cq.data.split(":", 1)[1]
            await self._rules.delete(rid)
            try:
                self._log.info("bot", "rule", f"rule {rid} deleted")
            except Exception:
                pass
            rules = await self._rules.list_all()
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
            await cq.answer(self._t("ui_rule_deleted"))

        @b.on_callback_query(filters.regex("^" + CB["rule_toggle"] + ":"))
        async def _cb_rule_toggle(_, cq: CallbackQuery):
            rid = cq.data.split(":", 1)[1]
            await self._rules.toggle(rid)
            try:
                self._log.info("bot", "rule", f"rule {rid} toggled")
            except Exception:
                pass
            rules = await self._rules.list_all()
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
            if not self._is_admin(cq.from_user.id):
                return await cq.answer(self._t("ui_need_admin"), show_alert=True)
            raw = cq.data.decode("utf-8") if isinstance(cq.data, bytes) else str(cq.data or "")
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
