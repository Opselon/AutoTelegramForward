from enum import Enum
from dataclasses import dataclass
from typing import Optional

class RoutingType(str, Enum):
    UNKNOWN = "UNKNOWN"
    CHANNEL_TO_CHANNEL = "CHANNEL_TO_CHANNEL"
    PV_TO_PV = "PV_TO_PV"
    CHANNEL_TO_PV = "CHANNEL_TO_PV"
    PV_TO_CHANNEL = "PV_TO_CHANNEL"

class ForwardMode(str, Enum):
    DIRECT_FORWARD = "DIRECT_FORWARD"  # Native Telegram forward preserving original author header
    COPY_MESSAGE = "COPY_MESSAGE"      # Clean duplicate without original author header

class ContentMode(str, Enum):
    """What payload is delivered to the target."""
    AUTO = "AUTO"                  # text with caption fallback (default)
    TEXT_ONLY = "TEXT_ONLY"        # strip media, send text only
    MEDIA_ONLY = "MEDIA_ONLY"      # send media, drop text
    TEXT_AND_MEDIA = "TEXT_AND_MEDIA"  # send both as separate messages

class TriggerEvent(str, Enum):
    """Which Telegram event fires a rule."""
    NEW_MESSAGE = "NEW_MESSAGE"
    EDITED_MESSAGE = "EDITED_MESSAGE"
    CHANNEL_POST = "CHANNEL_POST"
    EDITED_CHANNEL_POST = "EDITED_CHANNEL_POST"

class MediaType(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"
    VOICE = "voice"
    AUDIO = "audio"
    ANIMATION = "animation"
    STICKER = "sticker"
    POLL = "poll"
    LOCATION = "location"
    CONTACT = "contact"
    OTHER = "other"

class AIProviderType(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    GROQ = "groq"
    DEEPSEEK = "deepseek"
    NINEROUTER = "9router"
    NINE_ROUTER_ALIAS = "ninerouter"
    OPENROUTER = "openrouter"
    XAI = "xai"
    MISTRAL = "mistral"
    TOGETHER = "together"
    FIREWORKS = "fireworks"
    CUSTOM = "custom"

class FilterAction(str, Enum):
    ALLOW = "ALLOW"
    DROP = "DROP"
