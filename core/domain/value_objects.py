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


@dataclass(frozen=True)
class ChannelReference:
    """A chat a task sends from or to, as the domain sees it.

    ``chat_id`` is always the resolved Telegram peer id kept as a *string*:
    channel ids exceed 32-bit range and a numeric round-trip is how precision
    is lost. ``title`` is display metadata only and never an identity.
    """

    chat_id: str
    title: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "chat_id", str(self.chat_id or "").strip())
        object.__setattr__(self, "title", str(self.title or "").strip())

    @property
    def is_resolved(self) -> bool:
        return bool(self.chat_id)


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


@dataclass(frozen=True)
class DeliveryAttemptKey:
    """The deterministic business identity of one logical delivery.

    The send-side idempotency fence is keyed on *this*, never on a random
    per-attempt id: a claim, a claim-after-crash and a duplicate check must
    all resolve to the same row no matter which path generates them (durable
    worker, legacy direct dispatch, replay). Every field is a stable
    identifier that already exists on the job, so the key is reproducible by
    any caller from the data it already holds.

    ``job_kind`` separates the primary forward from its secondary
    deliverables (the keyword reply), exactly as ``delivery_jobs``' own
    UNIQUE key does — without it the reply's claim would collide with the
    forward's and one of the two would be silently refused.
    """

    rule_id: str
    source_chat_id: str
    source_message_id: int
    target_chat_id: str
    job_kind: str = "forward"

    @property
    def id(self) -> str:
        """The storage id — identical to the delivery job's own id.

        Keeping the fence row's id equal to the job id means a DBA joining
        ``delivery_attempts`` to ``delivery_jobs`` needs no lookup table, and
        a crash between the two writes leaves exactly one obvious orphan.
        """
        suffix = "" if self.job_kind == "forward" else f":{self.job_kind}"
        return (
            f"{self.rule_id}:{self.source_chat_id}:"
            f"{self.source_message_id}:{self.target_chat_id}{suffix}"
        )


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

