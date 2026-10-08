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
    OPENROUTER = "openrouter"
    CUSTOM = "custom"

class FilterAction(str, Enum):
    ALLOW = "ALLOW"
    DROP = "DROP"
