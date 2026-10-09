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
    CUSTOM_HEADER_COPY = "CUSTOM_HEADER_COPY"  # Duplicate with custom header + copy

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
    VIDEO_NOTE = "video_note"
    VENUE = "venue"
    DICE = "dice"
    GAME = "game"
    INVOICE = "invoice"
    STORY = "story"
    GIVEAWAY = "giveaway"
    SERVICE = "service"
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


class DeliveryStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RECEIVED = "RECEIVED"
    CLASSIFIED = "CLASSIFIED"
    COPYING_TO_C = "COPYING_TO_C"
    COPIED_TO_C = "COPIED_TO_C"
    FORWARDING_TO_B = "FORWARDING_TO_B"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    FAILED_C = "FAILED_C"
    FAILED_B = "FAILED_B"
    DEAD_LETTER = "DEAD_LETTER"
    UNKNOWN = "UNKNOWN"


class LinkPolicy(str, Enum):
    PRESERVE_ALL = "PRESERVE_ALL"
    REMOVE_ALL_URLS = "REMOVE_ALL_URLS"
    REMOVE_EXTERNAL_URLS = "REMOVE_EXTERNAL_URLS"
    REMOVE_SELECTED_DOMAINS = "REMOVE_SELECTED_DOMAINS"
    ALLOWLIST_ONLY = "ALLOWLIST_ONLY"
    REWRITE_LINKS = "REWRITE_LINKS"
    REMOVE_BUTTON_URLS = "REMOVE_BUTTON_URLS"
    CUSTOM_LINK_POLICY = "CUSTOM_LINK_POLICY"


class RoutingDecision(str, Enum):
    DIRECT_COPY = "DIRECT_COPY"
    BRANDING_VIA_C = "BRANDING_VIA_C"
    DIRECT_FORWARD = "DIRECT_FORWARD"
    CUSTOM_HEADER_COPY = "CUSTOM_HEADER_COPY"
    DROP = "DROP"


class AIFallbackPolicy(str, Enum):
    SEND_ORIGINAL = "SEND_ORIGINAL"
    DROP = "DROP"
    RETRY = "RETRY"
    QUARANTINE = "QUARANTINE"


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

