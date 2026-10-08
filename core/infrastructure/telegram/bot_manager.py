"""Telegram Bot Manager: command + callback surface over the use cases."""

import base64
import json
import logging
import time
from typing import Callable, Optional

from pyrogram import Client, filters
from pyrogram.types import Message

from ...application.use_cases import (
    AIConfigUseCases,
    FilterRuleUseCases,
    ForwardRuleUseCases,
    MessageForwardingUseCase,
    SessionUseCases,
)
from ...domain.entities import AIConfig, FilterRule, ForwardRule
from ...domain.value_objects import (
    AIProviderType,
    ForwardMode,
    RoutingType,
)
from .client_pool import ClientPool
from .i18n import I18n, SUPPORTED_LANGUAGES
from .login_flow import LoginFlowManager

logger = logging.getLogger(__name__)

LANG_FLAG = {"en": "English 🇬🇧", "fa": "فارسی 🇮🇷", "ru": "Русский 🇷🇺", "zh": "中文 🇨🇳"}


class BotManager:
    """Owns the bot client and all command handlers. One bot token per install."""

    def __init__(
        self,
        bot_token: str,
        admin_ids: list,
        pool: ClientPool,
        sessions: SessionUseCases,
        rules: ForwardRuleUseCases,
        filter_rules: FilterRuleUseCases,
        ai_configs: AIConfigUseCases,
        pipeline: MessageForwardingUseCase,
        i18n: I18n,
        crypto,
        credentials=None,
        bot_tokens=None,
        dispatcher=None,
    ) -> None:
        self.bot = Client(
            "atf_bot", api_id=pool.api_id, api_hash=pool.api_hash,
            bot_token=bot_token, in_memory=True,
        )
        self._admin_ids = set(admin_ids)
        self._pool = pool
        self._sessions = sessions
        self._rules = rules
        self._filters = filter_rules
        self._ai = ai_configs
        self._pipeline = pipeline
        self.i18n = i18n
        self._crypto = crypto
        self._credentials = credentials
        self._bot_tokens = bot_tokens
        self._dispatcher = dispatcher
        self._login = LoginFlowManager(pool, sessions)
        self._register_handlers()
        # Set by main.py after ProBotUI mounts: uid -> bool. When the button
        # UI owns an active flow for a user, BotManager stays silent so the
        # user never gets double replies / double code sends.
        self.ui_step_checker: Optional[Callable[[int], bool]] = None

    @property
    def login_manager(self) -> LoginFlowManager:
        """Shared login state machine (also used by ProBotUI)."""
        return self._login

    # ------------------------------------------------------------------ #
    def _is_admin(self, message: Message) -> bool:
        if not self._admin_ids:
            return True  # open mode when no admin configured
        return message.from_user and message.from_user.id in self._admin_ids

    def _t(self, uid: int, key: str, **kw) -> str:
        return self.i18n.t(key, **kw)

    def _register_handlers(self) -> None:
        b = self.bot

        @b.on_message(filters.command("start") & filters.private)
        async def _start(client, message: Message):
            await message.reply_text(self.i18n.t("start"))

        @b.on_message(filters.command("help") & filters.private)
        async def _help(client, message: Message):
            await message.reply_text(self.i18n.t("help"))

        @b.on_message(filters.command("lang") & filters.private)
        async def _lang(client, message: Message):
            lines = [f"{code} — {name}" for code, name in LANG_FLAG.items()]
            await message.reply_text(self.i18n.t("choose_lang") + "\n" + "\n".join(lines))
            self._awaiting_lang[message.chat.id] = True

        self._awaiting_lang = {}

        @b.on_message(filters.command("login") & filters.private)
        async def _login(client, message: Message):
            if not self._is_admin(message):
                return await message.reply_text(self.i18n.t("need_admin"))
            await message.reply_text(self.i18n.t("send_phone"))

        @b.on_message(filters.command("cancel") & filters.private)
        async def _cancel(client, message: Message):
            if self._login.cancel(message.from_user.id):
                await message.reply_text(self.i18n.t("cancelled"))
            else:
                await message.reply_text(self.i18n.t("cancelled"))

        @b.on_message(filters.command("sessions") & filters.private)
        async def _sessions(client, message: Message):
            sessions = await self._sessions.list_all()
            if not sessions:
                return await message.reply_text(self.i18n.t("no_sessions"))
            items = "\n".join(
                self.i18n.t(
                    "session_item",
                    id=s.id[:8], phone=s.phone_number,
                    status=self.i18n.t("session_active" if s.is_active else "session_inactive"),
                )
                for s in sessions
            )
            await message.reply_text(self.i18n.t("sessions_list", list=items))

        @b.on_message(filters.command("rules") & filters.private)
        async def _rules_cmd(client, message: Message):
            rules = await self._rules.list_all()
            if not rules:
                await message.reply_text(self.i18n.t("no_rules"))
                return await self._prompt_rule_add(message)
            items = "\n".join(
                self.i18n.t(
                    "rule_item",
                    id=r.id[:8], source=r.source_chat_name or r.source_chat_id,
                    target=r.target_chat_name or r.target_chat_id,
                    mode=r.forward_mode.value,
                    status="🟢" if r.is_active else "🔴",
                )
                for r in rules
            )
            await message.reply_text(self.i18n.t("rules_list", list=items))
            await self._prompt_rule_add(message)

        @b.on_message(filters.command("ai") & filters.private)
        async def _ai_cmd(client, message: Message):
            cfgs = await self._ai.list_all()
            if not cfgs:
                await message.reply_text(self.i18n.t("ai_no_configs"))
                return await self._prompt_ai_add(message)
            items = "\n".join(
                self.i18n.t(
                    "ai_item", id=c.id[:8], name=c.name or c.id[:8],
                    provider=c.provider.value, model=c.model,
                    status="🟢" if c.is_enabled else "🔴",
                )
                for c in cfgs
            )
            await message.reply_text(self.i18n.t("ai_list", list=items))
            await self._prompt_ai_add(message)

        @b.on_message(filters.command("stats") & filters.private)
        async def _stats(client, message: Message):
            s = self._pipeline.stats
            await message.reply_text(
                self.i18n.t(
                    "stats", processed=s.processed, forwarded=s.forwarded,
                    filtered=s.filtered, rewritten=s.rewritten, errors=s.errors,
                    uptime=int(time.time()) - s.started_at,
                )
            )

        @b.on_message(filters.command("backup") & filters.private)
        async def _backup(client, message: Message):
            if not self._is_admin(message):
                return await message.reply_text(self.i18n.t("need_admin"))
            parts = (message.text or "").split()
            if len(parts) < 2:
                sessions = await self._sessions.list_all()
                if not sessions:
                    return await message.reply_text(self.i18n.t("no_sessions"))
                target = sessions[0]
            else:
                target = await self._sessions.get(parts[1])
                if not target:
                    return await message.reply_text(self.i18n.t("rule_not_found"))
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
                await message.reply_document(
                    document=path,
                    file_name=path,
                    caption=self.i18n.t("backup_ready", phone=target.phone_number),
                )
                import os
                os.remove(path)
            except Exception as exc:
                await message.reply_text(self.i18n.t("backup_failed", error=str(exc)))

        @b.on_message(filters.command("restore") & filters.private)
        async def _restore(client, message: Message):
            if not self._is_admin(message):
                return await message.reply_text(self.i18n.t("need_admin"))
            if not message.document:
                return await message.reply_text(self.i18n.t("restore_failed", error="attach .atf file"))
            path = await message.download()
            try:
                blob = open(path, encoding="utf-8").read().strip()
                bundle = json.loads(base64.urlsafe_b64decode(blob.encode("ascii")))
                session = await self._sessions.create(
                    phone_number=bundle["phone"],
                    session_string_encrypted=bundle["session_string"],
                )
                session.user_id = bundle.get("user_id", "")
                session.username = bundle.get("username", "")
                session.first_name = bundle.get("first_name", "")
                session.activate()
                await self._sessions._repo.update(session)
                await self._pool.start_with_session_string(session.id, bundle["session_string"])
                await message.reply_text(self.i18n.t("restore_done", phone=session.phone_number))
            except Exception as exc:
                await message.reply_text(self.i18n.t("restore_failed", error=str(exc)))

        # Plain text router: FSM (login steps / lang pick / rule add / ai add)
        @b.on_message(filters.private & ~filters.command([
            "start", "help", "lang", "login", "cancel", "sessions",
            "rules", "ai", "stats", "backup", "restore",
        ]))
        async def _text(client, message: Message):
            await self._route_text(message)

    # ------------------------------------------------------------------ #
    async def _prompt_rule_add(self, message: Message):
        await message.reply_text(self.i18n.t("rule_add_hint"))
        self._awaiting_rule = message.chat.id

    async def _prompt_ai_add(self, message: Message):
        await message.reply_text(self.i18n.t("ai_add_hint"))
        self._awaiting_ai = message.chat.id

    async def _route_text(self, message: Message):
        uid = message.from_user.id if message.from_user else 0
        text = (message.text or "").strip()

        # When the button UI owns an active flow for this user, it handles
        # the reply — BotManager stays silent to avoid double answers.
        if callable(self.ui_step_checker):
            try:
                if self.ui_step_checker(uid):
                    return
            except Exception:
                pass

        # Language picker
        if self._awaiting_lang.pop(message.chat.id, None):
            if text.lower() in SUPPORTED_LANGUAGES:
                self.i18n.set_language(text.lower())
                await message.reply_text(self.i18n.t("lang_changed"))
            return

        # Login FSM
        state = self._login.pending_for(uid)
        if state is None:
            # No login in progress — but the user may just be answering
            # /login's "send your phone" prompt. Accept it directly.
            if LoginFlowManager.valid_phone(text):
                result = await self._login.start(uid, text)
                await self._login_reply(message, result)
                return
        else:
            if state.step == "code":
                result = await self._login.submit_code(uid, text)
                await self._login_reply(message, result)
                return
            if state.step == "password":
                result = await self._login.submit_password(uid, text)
                await self._login_reply(message, result)
                return

        # Rule add: "source target"
        if getattr(self, "_awaiting_rule", None) == message.chat.id:
            self._awaiting_rule = None
            parts = text.split()
            if len(parts) >= 2:
                sessions = await self._sessions.list_all()
                session = sessions[0] if sessions else None
                if not session:
                    return await message.reply_text(self.i18n.t("no_sessions"))
                rule = ForwardRule(
                    session_id=session.id,
                    source_chat_id=parts[0], source_chat_name=parts[0],
                    target_chat_id=parts[1], target_chat_name=parts[1],
                    routing_type=RoutingType.CHANNEL_TO_CHANNEL,
                    forward_mode=ForwardMode.COPY_MESSAGE,
                )
                await self._rules.create(rule)
                await message.reply_text(
                    self.i18n.t("rule_created", source=rule.source_chat_id, target=rule.target_chat_id)
                )
            return

        # AI add: "provider model api_key"
        if getattr(self, "_awaiting_ai", None) == message.chat.id:
            self._awaiting_ai = None
            parts = text.split(maxsplit=2)
            if len(parts) >= 3 and parts[0].lower() in {p.value for p in AIProviderType}:
                cfg = AIConfig(
                    name=f"{parts[0]}-{parts[1]}",
                    provider=AIProviderType(parts[0].lower()),
                    model=parts[1], api_key=parts[2],
                )
                await self._ai.create(cfg)
                await message.reply_text(self.i18n.t("ai_created", name=cfg.name))
            return

        await message.reply_text(self.i18n.t("unknown_command"))

    async def _login_reply(self, message: Message, result: str):
        if result.startswith("login_success:"):
            session_id = result.split(":", 1)[1]
            try:
                session = await self._sessions.get(session_id)
                phone = session.phone_number if session else ""
            except Exception:
                phone = ""
            await message.reply_text(self.i18n.t("login_success", phone=phone))
        elif ":" in result:
            key, _, detail = result.partition(":")
            await message.reply_text(self.i18n.t(key, error=detail))
        else:
            await message.reply_text(self.i18n.t(result, error=""))

    # ------------------------------------------------------------------ #
    async def start(self):
        await self.bot.start()

    async def stop(self):
        await self.bot.stop()
