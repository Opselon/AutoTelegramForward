"""AI Private Chat Auto-Responder (PV Assistant) for Telegram User Accounts.

Features:
- Configurable ON/OFF toggle
- Persona matching the user's natural, engaging Persian voice (completely human-like, non-robotic)
- Simulated typing indicator (ChatAction.TYPING) with realistic human delays
- Contextual multi-turn awareness (incorporates recent messages)
- Anti-spam & loop prevention with configurable cooldown per peer
- Selectable AI provider & model backend
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("atf.pv_responder")

DEFAULT_PERSONA = (
    "شما صاحب این اکانت شخصی تلگرام هستید و دارید مستقیماً به پیام‌های شخصی (پی‌وی) "
    "مخاطبان و دوستان خود پاسخ می‌دهید.\n\n"
    "قوانین حیاتی و شیوه صحبت کردن:\n"
    "۱. لحن شما باید کاملاً خودمانی، بسیار جذاب، صمیمی، محترمانه و زنده به زبان فارسی باشد؛ "
    "دقیقاً مانند یک جوان هوشمند، خوش‌برخورد و امروزی ایرانی در چت تلگرام.\n"
    "۲. هرگز و تحت هیچ شرایطی نگو که ربات، هوش مصنوعی، مدل زبانی یا دستیار مجازی هستی! "
    "وانمود کن که خود کاربر هستی.\n"
    "۳. پاسخ‌ها کوتاه، صمیمانه، خوش‌طبع و موجز باشند (از پاسخ‌های طولانی، رسمی یا مقاله‌ای پرهیز کن).\n"
    "۴. از اموجی‌های متناسب و طبیعی چت (مانند 🙏، 😉، 🤝، 😂، ✌️) به‌جا و به اندازه استفاده کن.\n"
    "۵. اگر سوال فنی یا کاری پرسیدند، هوشمندانه و کوتاه راهنمایی کن. اگر گفت‌وگوی احوال‌پرسی است، گرم و صمیمی باش."
)

CONFIG_PATH = Path("data/pv_responder_config.json")


@dataclass
class PVResponderConfig:
    enabled: bool = False
    ai_config_id: Optional[str] = None
    persona_prompt: str = DEFAULT_PERSONA
    typing_delay_min: float = 2.0
    typing_delay_max: float = 4.5
    cooldown_seconds: int = 15
    ignore_bots: bool = True
    history_limit: int = 4

    @classmethod
    def from_dict(cls, data: dict) -> "PVResponderConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            ai_config_id=data.get("ai_config_id") or None,
            persona_prompt=data.get("persona_prompt") or DEFAULT_PERSONA,
            typing_delay_min=float(data.get("typing_delay_min", 2.0)),
            typing_delay_max=float(data.get("typing_delay_max", 4.5)),
            cooldown_seconds=int(data.get("cooldown_seconds", 15)),
            ignore_bots=bool(data.get("ignore_bots", True)),
            history_limit=int(data.get("history_limit", 4)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


class AIPVResponder:
    """Intelligent private chat assistant executing on connected user sessions."""

    def __init__(self, ai_repo: Any = None, ai_factory: Any = None, db: Any = None) -> None:
        self._ai_repo = ai_repo
        self._ai_factory = ai_factory
        self._db = db
        self._last_replied_ts: Dict[str, float] = {}
        self._user_configs: Dict[int, PVResponderConfig] = {}

    @property
    def config(self) -> PVResponderConfig:
        return self.get_config_for_user(0)

    @config.setter
    def config(self, cfg: PVResponderConfig) -> None:
        self.save_config_for_user(0, cfg)

    def _user_config_path(self, user_id: int) -> Path:
        if not user_id:
            return CONFIG_PATH
        return CONFIG_PATH.parent / f"pv_responder_config_{user_id}.json"

    def get_config_for_user(self, user_id: int = 0) -> PVResponderConfig:
        if user_id in self._user_configs:
            return self._user_configs[user_id]
        path = self._user_config_path(user_id)
        try:
            if path.exists() and path.stat().st_size > 0:
                data = json.loads(path.read_text(encoding="utf-8"))
                cfg = PVResponderConfig.from_dict(data)
                self._user_configs[user_id] = cfg
                return cfg
        except Exception as exc:
            logger.warning("Failed to load PV responder config for user %s: %s", user_id, exc)
        cfg = PVResponderConfig()
        self._user_configs[user_id] = cfg
        return cfg

    def save_config_for_user(self, user_id: int, cfg: PVResponderConfig) -> None:
        self._user_configs[user_id] = cfg
        path = self._user_config_path(user_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to save PV responder config for user %s: %s", user_id, exc)

    def _load_config(self) -> PVResponderConfig:
        return self.get_config_for_user(0)

    def save_config(self, cfg: PVResponderConfig) -> None:
        self.save_config_for_user(0, cfg)

    async def get_active_ai_config(self, user_id: int = 0) -> Optional[Any]:
        if not self._ai_repo:
            return None
        cfg = self.get_config_for_user(user_id)
        if cfg.ai_config_id:
            ai_c = (
                await self._ai_repo.get_owned(cfg.ai_config_id, user_id)
                if user_id and hasattr(self._ai_repo, "get_owned")
                else await self._ai_repo.get(cfg.ai_config_id)
            )
            if ai_c and ai_c.is_enabled:
                return ai_c
        # Fallback to user's first enabled AI config
        try:
            cfgs = (
                await self._ai_repo.list_by_owner(user_id)
                if user_id and hasattr(self._ai_repo, "list_by_owner")
                else await self._ai_repo.list_all()
            )
            for c in cfgs:
                if c.is_enabled:
                    return c
        except Exception:
            pass
        return None

    def _get_owner_user_id(self, client: Any) -> int:
        session_id = getattr(client, "session_id", None) or getattr(client, "name", None)
        if not session_id or not self._db:
            return 0
        try:
            row = self._db.query_one("SELECT owner_user_id FROM sessions WHERE id=?", (str(session_id),))
            if row and row["owner_user_id"]:
                return int(row["owner_user_id"])
        except Exception:
            pass
        return 0

    async def handle_message(self, client: Any, message: Any) -> bool:
        """Handle incoming private message on a user account."""
        owner_user_id = self._get_owner_user_id(client)
        user_config = self.get_config_for_user(owner_user_id)
        if not user_config.enabled:
            return False

        # 1. Validation checks
        chat = getattr(message, "chat", None)
        if not chat or str(getattr(chat, "type", "")).lower() not in ("private", "chattype.private"):
            return False

        from_user = getattr(message, "from_user", None)
        if not from_user:
            return False

        if getattr(from_user, "is_self", False):
            return False

        if user_config.ignore_bots and getattr(from_user, "is_bot", False):
            return False

        text = (message.text or message.caption or "").strip()
        if not text:
            return False

        user_key = f"{getattr(client, 'session_id', 'client')}:{from_user.id}"
        now = time.time()
        last_ts = self._last_replied_ts.get(user_key, 0.0)
        if now - last_ts < user_config.cooldown_seconds:
            logger.debug("PV Responder: skipping %s due to cooldown (%.1fs < %ds)", user_key, now - last_ts, user_config.cooldown_seconds)
            return False

        ai_cfg = await self.get_active_ai_config(owner_user_id)
        if not ai_cfg:
            logger.warning("PV Responder is enabled for user %s but no active AI config was found", owner_user_id)
            return False

        # 2. Extract recent conversation context for natural replies
        conversation_history: List[Dict[str, str]] = []
        try:
            chat_id = chat.id
            history_msgs = []
            async for m in client.get_chat_history(chat_id, limit=user_config.history_limit):
                if m.id == message.id:
                    continue
                content = (m.text or m.caption or "").strip()
                if content:
                    role = "assistant" if (m.from_user and m.from_user.is_self) else "user"
                    history_msgs.append({"role": role, "content": content})
            history_msgs.reverse()
            conversation_history = history_msgs
        except Exception as exc:
            logger.debug("Could not fetch chat history for context: %s", exc)

        # Build messages payload
        messages = [{"role": "system", "content": user_config.persona_prompt}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": text})

        # 3. Simulated human typing delay
        calc_delay = min(
            max(len(text) * 0.03 + random.uniform(1.8, 3.2), user_config.typing_delay_min),
            user_config.typing_delay_max,
        )

        try:
            from pyrogram.enums import ChatAction
            await client.send_chat_action(chat.id, ChatAction.TYPING)
        except Exception:
            pass

        t_start = time.time()

        # 4. Generate AI response
        reply_text = ""
        try:
            from core.infrastructure.ai.providers import OpenAICompatibleProvider, AIProviderFactory
            prov_key = ai_cfg.provider.value if hasattr(ai_cfg.provider, "value") else str(ai_cfg.provider)
            factory = self._ai_factory or AIProviderFactory()
            provider_cls = factory._registry.get(prov_key, OpenAICompatibleProvider)
            provider = provider_cls()

            if hasattr(provider, "chat_complete"):
                reply_text = await provider.chat_complete(ai_cfg, messages, temperature=0.7)
            else:
                # Fallback to single text rewrite
                context_prompt = f"پیام دریافتی: {text}"
                reply_text = await provider.rewrite(ai_cfg, context_prompt)
        except Exception as exc:
            logger.warning("PV Responder AI call failed: %s", exc)
            return False

        if not reply_text or not reply_text.strip():
            return False

        # 5. Respect typing time before sending
        elapsed = time.time() - t_start
        remaining = calc_delay - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

        # 6. Send the reply
        try:
            await client.send_message(chat.id, reply_text.strip())
            self._last_replied_ts[user_key] = time.time()
            logger.info("PV Responder successfully replied to uid=%s in chat=%s", from_user.id, chat.id)
            return True
        except Exception as exc:
            logger.error("PV Responder failed to send message to chat=%s: %s", chat.id, exc)
            return False
