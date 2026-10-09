import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .value_objects import (
    AIProviderType,
    ContentMode,
    DeliveryStatus,
    FilterAction,
    ForwardMode,
    MediaType,
    RoutingType,
    TriggerEvent,
)


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


@dataclass
class TelegramSession:
    """Aggregate root for an authorized Telegram user-account session."""

    id: str = field(default_factory=_new_id)
    phone_number: str = ""
    session_string_encrypted: str = ""
    user_id: str = ""
    username: str = ""
    first_name: str = ""
    is_active: bool = True
    is_authorized: bool = False
    # id of the api_id/api_hash credential used for this session ("default"
    # = the config.yaml pair). Stored encrypted is unnecessary — credentials
    # live in their own encrypted table.
    api_credential_id: str = "default"
    # optional per-session proxy: {"scheme": "socks5", "host": ..., "port": ...}
    proxy: Optional[dict] = None
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)

    def mark_updated(self) -> None:
        self.updated_at = _now()

    def activate(self) -> None:
        self.is_active = True
        self.is_authorized = True
        self.mark_updated()

    def deactivate(self) -> None:
        self.is_active = False
        self.mark_updated()


@dataclass
class ForwardRule:
    """Aggregate root for a single source-chat -> many target-chat routing rule."""

    id: str = field(default_factory=_new_id)
    session_id: str = ""
    source_chat_id: str = ""
    source_chat_name: str = ""
    # Multi-target fan-out: primary target (legacy single field) + extras.
    target_chat_id: str = ""
    target_chat_name: str = ""
    target_chat_ids: List[str] = field(default_factory=list)
    routing_type: RoutingType = RoutingType.CHANNEL_TO_CHANNEL
    forward_mode: ForwardMode = ForwardMode.COPY_MESSAGE
    is_active: bool = True
    filter_rule_id: Optional[str] = None
    ai_config_id: Optional[str] = None
    remove_links: bool = False
    custom_caption_template: str = ""
    # Delay (seconds) before forwarding — anti-detection / scheduling.
    delay_seconds: float = 0.0
    # Skip messages that already arrived (by id) — restart-safe dedupe.
    skip_history: bool = True
    # Only forward messages newer than this timestamp (0 = no floor).
    since_ts: int = 0
    # ignore edits instead of re-forwarding them
    ignore_edits: bool = False
    # Which Telegram events fire this rule (subset of TriggerEvent values).
    trigger_events: List[str] = field(default_factory=lambda: [TriggerEvent.NEW_MESSAGE.value])
    # What content is delivered (ContentMode value).
    content_mode: Any = None  # resolved to ContentMode.AUTO in __post_init__
    # optional per-rule proxy override (same shape as session proxy)
    proxy: Optional[dict] = None
    # free-form metadata for future flexibility
    metadata: Dict[str, Any] = field(default_factory=dict)
    # version for optimistic concurrency control (prevents lost updates)
    version: int = 1
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)

    def __post_init__(self):
        # Normalize content_mode: accept ContentMode | str | None.
        if self.content_mode is None:
            self.content_mode = ContentMode.AUTO
        elif isinstance(self.content_mode, str):
            try:
                self.content_mode = ContentMode(self.content_mode)
            except ValueError:
                self.content_mode = ContentMode.AUTO
        # Normalize trigger_events: accept List[str] | str | None.
        if self.trigger_events is None:
            self.trigger_events = [TriggerEvent.NEW_MESSAGE.value]
        elif isinstance(self.trigger_events, str):
            self.trigger_events = [self.trigger_events]

    def mark_updated(self) -> None:
        self.updated_at = _now()

    @property
    def all_targets(self) -> List[str]:
        """Every target chat id for this rule (primary first, deduped)."""
        seen, out = set(), []
        for tid in [self.target_chat_id, *self.target_chat_ids]:
            if tid and tid not in seen:
                seen.add(tid)
                out.append(tid)
        return out

    @property
    def target_label(self) -> str:
        targets = self.all_targets
        if not targets:
            return ""
        names = [self.target_chat_name] + [
            t for t in self.target_chat_ids if t != self.target_chat_id
        ]
        if len(targets) == 1:
            return self.target_chat_name or targets[0]
        return f"{len(targets)} targets"

    @property
    def replacements(self) -> Dict[str, str]:
        if not isinstance(self.metadata, dict):
            return {}
        reps = self.metadata.get("replacements")
        return reps if isinstance(reps, dict) else {}

    @property
    def header(self) -> str:
        return str(self.metadata.get("header") or "") if isinstance(self.metadata, dict) else ""

    @property
    def footer(self) -> str:
        return str(self.metadata.get("footer") or "") if isinstance(self.metadata, dict) else ""

    @property
    def block_voice(self) -> bool:
        return bool(self.metadata.get("block_voice", False)) if isinstance(self.metadata, dict) else False

    @property
    def block_stickers(self) -> bool:
        return bool(self.metadata.get("block_stickers", False)) if isinstance(self.metadata, dict) else False

    @property
    def remove_emojis(self) -> bool:
        return bool(self.metadata.get("remove_emojis", False)) if isinstance(self.metadata, dict) else False

    @property
    def sync_edits(self) -> bool:
        return bool(self.metadata.get("sync_edits", True)) if isinstance(self.metadata, dict) else True

    @property
    def sync_deletes(self) -> bool:
        return bool(self.metadata.get("sync_deletes", False)) if isinstance(self.metadata, dict) else False

    @property
    def album_mode(self) -> str:
        return str(self.metadata.get("album_mode") or "album") if isinstance(self.metadata, dict) else "album"

    @property
    def link_replacement(self) -> str:
        return str(self.metadata.get("link_replacement") or "") if isinstance(self.metadata, dict) else ""

    def matches_source(self, chat_id: str) -> bool:
        return self.is_active and self.source_chat_id == str(chat_id)

    def to_draft(self) -> Dict[str, Any]:
        """Produce an editable dictionary representation of all rule parameters."""
        f_mode = getattr(self.forward_mode, "value", str(self.forward_mode))
        return {
            "id": self.id,
            "session_id": self.session_id,
            "source_chat_id": self.source_chat_id,
            "source_chat_name": self.source_chat_name,
            "target_chat_id": self.target_chat_id,
            "target_chat_name": self.target_chat_name,
            "forward_mode": f_mode,
            "is_active": bool(self.is_active),
            "remove_links": bool(self.remove_links),
            "block_voice": bool(self.block_voice),
            "block_stickers": bool(self.block_stickers),
            "remove_emojis": bool(self.remove_emojis),
            "ignore_edits": bool(self.ignore_edits),
            "sync_deletes": bool(self.sync_deletes),
            "album_mode": str(self.album_mode),
            "header": str(self.header),
            "footer": str(self.footer),
            "replacements": dict(self.replacements),
            "version": int(getattr(self, "version", 1)),
        }

    def apply_draft(self, draft: Dict[str, Any]) -> None:
        """Apply draft modifications into the rule entity with validation."""
        if not isinstance(draft, dict):
            return

        if "source_chat_id" in draft and draft["source_chat_id"]:
            self.source_chat_id = str(draft["source_chat_id"])
        if "source_name" in draft and draft["source_name"] is not None:
            self.source_chat_name = str(draft["source_name"])
        elif "source_chat_name" in draft and draft["source_chat_name"] is not None:
            self.source_chat_name = str(draft["source_chat_name"])

        if "target_chat_id" in draft and draft["target_chat_id"]:
            self.target_chat_id = str(draft["target_chat_id"])
        if "target_name" in draft and draft["target_name"] is not None:
            self.target_chat_name = str(draft["target_name"])
        elif "target_chat_name" in draft and draft["target_chat_name"] is not None:
            self.target_chat_name = str(draft["target_chat_name"])

        if "forward_mode" in draft:
            try:
                self.forward_mode = ForwardMode(draft["forward_mode"])
            except (ValueError, TypeError):
                pass

        if "is_active" in draft:
            self.is_active = bool(draft["is_active"])

        if "ignore_edits" in draft:
            self.ignore_edits = bool(draft["ignore_edits"])

        if not isinstance(self.metadata, dict):
            self.metadata = {}

        if "remove_links" in draft:
            self.remove_links = bool(draft["remove_links"])
            self.metadata["remove_links"] = self.remove_links

        if "block_voice" in draft:
            self.metadata["block_voice"] = bool(draft["block_voice"])

        if "block_stickers" in draft:
            self.metadata["block_stickers"] = bool(draft["block_stickers"])

        if "remove_emojis" in draft:
            self.metadata["remove_emojis"] = bool(draft["remove_emojis"])

        if "sync_deletes" in draft:
            self.metadata["sync_deletes"] = bool(draft["sync_deletes"])

        if "album_mode" in draft:
            self.metadata["album_mode"] = str(draft["album_mode"])

        if "header" in draft:
            self.metadata["header"] = str(draft["header"] or "")

        if "footer" in draft:
            self.metadata["footer"] = str(draft["footer"] or "")

        if "replacements" in draft and isinstance(draft["replacements"], dict):
            self.metadata["replacements"] = dict(draft["replacements"])

        self.mark_updated()


@dataclass
class FilterRule:
    """Value-rich entity describing what content is allowed through a rule."""

    id: str = field(default_factory=_new_id)
    name: str = ""
    whitelist_keywords: List[str] = field(default_factory=list)
    blacklist_keywords: List[str] = field(default_factory=list)
    regex_patterns: List[str] = field(default_factory=list)
    allowed_media_types: List[str] = field(default_factory=list)
    blocked_media_types: List[str] = field(default_factory=list)
    drop_service_messages: bool = True
    min_message_length: int = 0
    max_message_length: int = 0


@dataclass
class AIConfig:
    """Aggregate root for an LLM rewrite configuration."""

    id: str = field(default_factory=_new_id)
    name: str = ""
    provider: AIProviderType = AIProviderType.OPENAI
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    system_prompt: str = (
        "You are an intelligent editor. Clean up and improve the text while "
        "keeping the original meaning."
    )
    user_prompt_template: str = "{text}"
    temperature: float = 0.7
    is_enabled: bool = True
    target_language: str = "en"


@dataclass
class MessagePayload:
    """Immutable-ish snapshot of an incoming Telegram message (domain-level)."""

    message_id: int
    chat_id: str
    chat_type: str = "channel"  # channel | supergroup | group | private
    sender_id: Optional[str] = None
    text: str = ""
    caption: str = ""
    media_type: MediaType = MediaType.TEXT
    has_media: bool = False
    is_service: bool = False
    reply_to_message_id: Optional[int] = None
    # epoch seconds of the message on Telegram's side
    date: int = 0
    # True when this event is an edit of an older message
    is_edit: bool = False
    # origin of a forwarded message: {type, from_chat_id, from_message_id, ...}
    forward_origin: Optional[dict] = None
    # album grouping id — multiple media messages share one id
    media_group_id: Optional[str] = None
    views: int = 0

    @property
    def effective_text(self) -> str:
        return self.caption if (self.has_media and self.caption) else self.text


@dataclass
class EvaluationResult:
    """Outcome of running a message through the filter + AI pipeline."""

    action: FilterAction = FilterAction.ALLOW
    reason: str = ""
    rewritten_text: Optional[str] = None
    links_removed: bool = False


@dataclass
class MessageMapping:
    """Persistent mapping between a source Telegram message and target message.

    Used for sync_edits and sync_deletes propagation.
    """

    id: str = field(default_factory=_new_id)
    rule_id: str = ""
    source_chat_id: str = ""
    source_message_id: int = 0
    target_chat_id: str = ""
    target_message_id: Optional[int] = None
    media_group_id: Optional[str] = None
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)


@dataclass
class DeliveryJob:
    """Durable unit of work for non-blocking asynchronous message delivery.

    Survives restarts, supports atomic lease claiming, per-target rate limiting,
    and exponential backoff retry.
    """

    id: str = field(default_factory=_new_id)
    rule_id: str = ""
    source_chat_id: str = ""
    source_message_id: int = 0
    target_chat_id: str = ""
    payload_data: Dict[str, Any] = field(default_factory=dict)
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    max_attempts: int = 5
    next_retry_at: float = 0.0
    lease_until: float = 0.0
    worker_id: Optional[str] = None
    error_detail: Optional[str] = None
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)

