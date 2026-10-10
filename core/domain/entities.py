import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .value_objects import (
    AIFallbackPolicy,
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
    owner_user_id: int = 0

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
    name: str = ""
    description: str = ""
    source_chat_id: str = ""
    source_chat_name: str = ""
    # Multi-target fan-out: primary target (legacy single field) + extras.
    target_chat_id: str = ""
    target_chat_name: str = ""
    target_chat_ids: List[str] = field(default_factory=list)
    routing_type: RoutingType = RoutingType.CHANNEL_TO_CHANNEL
    forward_mode: ForwardMode = ForwardMode.COPY_MESSAGE
    is_active: bool = True
    priority: int = 10  # higher number = evaluated first
    execution_order: int = 0
    multi_route: bool = False  # If True, subsequent matching rules also execute
    message_category: str = "ALL"  # ALL | VIP | REGULAR | CHANNEL_POST | USER_MESSAGE
    detection_criteria: Dict[str, Any] = field(default_factory=dict)
    intermediate_channel_id: str = ""
    intermediate_channel_name: str = ""
    intermediate_target_chat_id: str = ""
    use_intermediate: bool = False
    intermediate_mode: str = "NONE"  # NONE | VIA_INTERMEDIATE | MASK_ORIGIN
    fallback_mode: Any = ForwardMode.COPY_MESSAGE  # resolved in __post_init__; NEVER native forward
    fallback_enabled: bool = True
    custom_header: str = ""
    custom_footer: str = ""
    header_enabled: bool = False
    rate_limit_per_minute: int = 25
    rate_limit_burst: int = 0
    media_handling: str = "AUTO"  # AUTO | TEXT_ONLY | MEDIA_ONLY | TEXT_AND_MEDIA | SPLIT_LONG_CAPTION
    dedupe_policy: str = "STRICT"  # STRICT | RELAXED | DISABLED
    max_retries: int = 3
    retry_backoff_base: float = 2.0
    retry_policy: Dict[str, Any] = field(default_factory=dict)
    template_text: str = ""
    template_media: str = ""
    template_album: str = ""
    caption_max_length: int = 0
    preserve_signature: bool = False
    is_paused: bool = False
    paused_until: int = 0
    filter_rule_id: Optional[str] = None
    ai_config_id: Optional[str] = None
    remove_links: bool = False
    link_policy: str = "PRESERVE_ALL"
    domain_allowlist: List[str] = field(default_factory=list)
    domain_blocklist: List[str] = field(default_factory=list)
    link_rewrite_map: Dict[str, str] = field(default_factory=dict)
    allowed_media_types: List[str] = field(default_factory=list)
    split_long_caption: bool = True
    owner_user_id: int = 0
    album_aggregation_window_seconds: float = 0.8
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
    # AI Transformation configuration (P2)
    ai_fallback_policy: Any = AIFallbackPolicy.DROP  # resolved in __post_init__
    ai_prompt_version: int = 0  # 0 = latest active version
    ai_timeout_seconds: float = 15.0
    ai_secondary_config_id: Optional[str] = None
    # free-form metadata for future flexibility
    metadata: Dict[str, Any] = field(default_factory=dict)
    # version for optimistic concurrency control (prevents lost updates)
    version: int = 1
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)

    def __post_init__(self):
        # Sync metadata with first-class fields if provided via metadata
        if isinstance(self.metadata, dict):
            if not self.name and self.metadata.get("name"):
                self.name = str(self.metadata["name"])
            if not self.description and self.metadata.get("description"):
                self.description = str(self.metadata["description"])
            if self.priority == 10 and "priority" in self.metadata:
                try:
                    self.priority = int(self.metadata["priority"])
                except (ValueError, TypeError):
                    pass
            if not self.multi_route and self.metadata.get("multi_route"):
                self.multi_route = bool(self.metadata["multi_route"])
            if self.message_category == "ALL" and self.metadata.get("message_category"):
                self.message_category = str(self.metadata["message_category"])
            if not self.detection_criteria and isinstance(self.metadata.get("detection_criteria"), dict):
                self.detection_criteria = dict(self.metadata["detection_criteria"])
            if not self.intermediate_channel_id and self.metadata.get("intermediate_channel_id"):
                self.intermediate_channel_id = str(self.metadata["intermediate_channel_id"])
            if self.intermediate_mode == "NONE" and self.metadata.get("intermediate_mode"):
                self.intermediate_mode = str(self.metadata["intermediate_mode"])
            if not self.custom_header and self.metadata.get("header"):
                # NB: deliberately NOT promoted into custom_header — metadata["header"]
                # must stay live-editable (header property falls back to it).
                pass
            if not self.custom_footer and self.metadata.get("footer"):
                pass
            if self.fallback_mode == "COPY_MESSAGE" and self.metadata.get("fallback_mode"):
                self.fallback_mode = str(self.metadata["fallback_mode"])
        # Fallback mode MUST NEVER be native forward
        if self.fallback_mode is None:
            self.fallback_mode = ForwardMode.COPY_MESSAGE
        elif isinstance(self.fallback_mode, str):
            try:
                self.fallback_mode = ForwardMode(self.fallback_mode)
            except ValueError:
                self.fallback_mode = ForwardMode.COPY_MESSAGE
        if getattr(self.fallback_mode, "value", self.fallback_mode) in ("DIRECT_FORWARD", "FORWARD"):
            self.fallback_mode = ForwardMode.COPY_MESSAGE

        # Normalize content_mode: accept ContentMode | str | None.
        if self.content_mode is None:
            self.content_mode = ContentMode.AUTO
        elif isinstance(self.content_mode, str):
            try:
                self.content_mode = ContentMode(self.content_mode)
            except ValueError:
                self.content_mode = ContentMode.AUTO
        # Normalize ai_fallback_policy: accept AIFallbackPolicy | str | None.
        if self.ai_fallback_policy is None:
            self.ai_fallback_policy = AIFallbackPolicy.DROP
        elif isinstance(self.ai_fallback_policy, str):
            try:
                self.ai_fallback_policy = AIFallbackPolicy(self.ai_fallback_policy)
            except ValueError:
                self.ai_fallback_policy = AIFallbackPolicy.DROP
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
        return self.custom_header or (str(self.metadata.get("header") or "") if isinstance(self.metadata, dict) else "")

    @property
    def footer(self) -> str:
        return self.custom_footer or (str(self.metadata.get("footer") or "") if isinstance(self.metadata, dict) else "")

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
        return bool(self.metadata.get("sync_edits", False)) if isinstance(self.metadata, dict) else False

    @property
    def sync_deletes(self) -> bool:
        return bool(self.metadata.get("sync_deletes", False)) if isinstance(self.metadata, dict) else False

    @property
    def album_mode(self) -> str:
        return str(self.metadata.get("album_mode") or "album") if isinstance(self.metadata, dict) else "album"

    @property
    def link_replacement(self) -> str:
        return str(self.metadata.get("link_replacement") or "") if isinstance(self.metadata, dict) else ""

    @property
    def enabled(self) -> bool:
        return bool(self.is_active)

    @property
    def source_chat_ids(self) -> List[str]:
        s_ids = self.detection_criteria.get("source_chat_ids", []) if isinstance(self.detection_criteria, dict) else []
        if not s_ids and self.source_chat_id:
            return [str(self.source_chat_id)]
        return [str(s) for s in s_ids]

    @property
    def forward_origin_chat_ids(self) -> List[str]:
        if not isinstance(self.detection_criteria, dict):
            return []
        return [str(x) for x in (self.detection_criteria.get("forward_origin_chat_ids") or self.detection_criteria.get("origin_chat_ids") or [])]

    @property
    def forward_origin_titles(self) -> List[str]:
        if not isinstance(self.detection_criteria, dict):
            return []
        return [str(x) for x in (self.detection_criteria.get("forward_origin_titles") or self.detection_criteria.get("origin_titles") or [])]

    @property
    def text_contains(self) -> List[str]:
        if not isinstance(self.detection_criteria, dict):
            return []
        return [str(x) for x in (self.detection_criteria.get("text_contains") or self.detection_criteria.get("keywords") or [])]

    @property
    def regex_pattern(self) -> Optional[str]:
        if not isinstance(self.detection_criteria, dict):
            return None
        return self.detection_criteria.get("regex_pattern") or self.detection_criteria.get("text_regex")

    @property
    def match_mode(self) -> str:
        if not isinstance(self.detection_criteria, dict):
            return "ANY"
        return str(self.detection_criteria.get("match_mode") or "ANY").upper()

    @property
    def destination_chat_id(self) -> str:
        return self.target_chat_id

    @property
    def intermediate_chat_id(self) -> str:
        return self.intermediate_channel_id

    @property
    def delivery_mode(self) -> str:
        return getattr(self.forward_mode, "value", str(self.forward_mode))

    def matches_source(self, chat_id: str) -> bool:
        if not self.is_active:
            return False
        cid = str(chat_id).strip()
        if self.source_chat_id:
            src = str(self.source_chat_id).strip()
            if src == cid or src.lstrip("@").lower() == cid.lstrip("@").lower():
                return True
        if self.source_chat_name:
            src_name = str(self.source_chat_name).strip()
            if src_name == cid or src_name.lstrip("@").lower() == cid.lstrip("@").lower():
                return True
        return False

    def to_draft(self) -> Dict[str, Any]:
        """Produce an editable dictionary representation of all rule parameters."""
        f_mode = getattr(self.forward_mode, "value", str(self.forward_mode))
        return {
            "id": self.id,
            "session_id": self.session_id,
            "name": self.name,
            "description": self.description,
            "source_chat_id": self.source_chat_id,
            "source_chat_name": self.source_chat_name,
            "target_chat_id": self.target_chat_id,
            "target_chat_name": self.target_chat_name,
            "target_chat_ids": list(self.target_chat_ids),
            "forward_mode": f_mode,
            "is_active": bool(self.is_active),
            "priority": int(self.priority),
            "execution_order": int(self.execution_order),
            "multi_route": bool(self.multi_route),
            "message_category": str(self.message_category),
            "detection_criteria": dict(self.detection_criteria),
            "intermediate_channel_id": str(self.intermediate_channel_id),
            "intermediate_mode": str(self.intermediate_mode),
            "fallback_mode": str(self.fallback_mode),
            "remove_links": bool(self.remove_links),
            "link_policy": str(getattr(self, "link_policy", "PRESERVE_ALL")),
            "domain_allowlist": list(getattr(self, "domain_allowlist", [])),
            "domain_blocklist": list(getattr(self, "domain_blocklist", [])),
            "allowed_media_types": list(getattr(self, "allowed_media_types", [])),
            "split_long_caption": bool(getattr(self, "split_long_caption", True)),
            "block_voice": bool(self.block_voice),
            "block_stickers": bool(self.block_stickers),
            "remove_emojis": bool(self.remove_emojis),
            "ignore_edits": bool(self.ignore_edits),
            "sync_deletes": bool(self.sync_deletes),
            "album_mode": str(self.album_mode),
            "header": str(self.header),
            "footer": str(self.footer),
            "custom_header": str(self.custom_header),
            "custom_footer": str(self.custom_footer),
            "replacements": dict(self.replacements),
            "version": int(getattr(self, "version", 1)),
        }

    def apply_draft(self, draft: Dict[str, Any]) -> None:
        """Apply draft modifications into the rule entity with validation."""
        if not isinstance(draft, dict):
            return

        if "name" in draft and draft["name"] is not None:
            self.name = str(draft["name"])
        if "description" in draft and draft["description"] is not None:
            self.description = str(draft["description"])
        if "priority" in draft and draft["priority"] is not None:
            try:
                self.priority = int(draft["priority"])
            except (ValueError, TypeError):
                pass
        if "execution_order" in draft and draft["execution_order"] is not None:
            try:
                self.execution_order = int(draft["execution_order"])
            except (ValueError, TypeError):
                pass
        if "multi_route" in draft:
            self.multi_route = bool(draft["multi_route"])
        if "message_category" in draft and draft["message_category"]:
            self.message_category = str(draft["message_category"]).upper()
        if "detection_criteria" in draft and isinstance(draft["detection_criteria"], dict):
            self.detection_criteria = dict(draft["detection_criteria"])
        if "intermediate_channel_id" in draft and draft["intermediate_channel_id"] is not None:
            self.intermediate_channel_id = str(draft["intermediate_channel_id"])
        if "intermediate_mode" in draft and draft["intermediate_mode"] is not None:
            self.intermediate_mode = str(draft["intermediate_mode"])
        if "fallback_mode" in draft and draft["fallback_mode"]:
            fb = str(draft["fallback_mode"]).upper()
            self.fallback_mode = "COPY_MESSAGE" if fb in ("DIRECT_FORWARD", "FORWARD") else fb
        if "custom_header" in draft and draft["custom_header"] is not None:
            self.custom_header = str(draft["custom_header"])
        if "custom_footer" in draft and draft["custom_footer"] is not None:
            self.custom_footer = str(draft["custom_footer"])

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

        if "link_policy" in draft and draft["link_policy"]:
            self.link_policy = str(draft["link_policy"])
            self.metadata["link_policy"] = self.link_policy

        if "domain_allowlist" in draft and isinstance(draft["domain_allowlist"], list):
            self.domain_allowlist = [str(x) for x in draft["domain_allowlist"]]
            self.metadata["domain_allowlist"] = self.domain_allowlist

        if "domain_blocklist" in draft and isinstance(draft["domain_blocklist"], list):
            self.domain_blocklist = [str(x) for x in draft["domain_blocklist"]]
            self.metadata["domain_blocklist"] = self.domain_blocklist

        if "allowed_media_types" in draft and isinstance(draft["allowed_media_types"], list):
            self.allowed_media_types = [str(x) for x in draft["allowed_media_types"]]
            self.metadata["allowed_media_types"] = self.allowed_media_types

        if "split_long_caption" in draft:
            self.split_long_caption = bool(draft["split_long_caption"])
            self.metadata["split_long_caption"] = self.split_long_caption

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
    owner_user_id: int = 0


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
    owner_user_id: int = 0


@dataclass
class PromptTemplate:
    """Aggregate root for reusable system & user prompt templates."""

    id: str = field(default_factory=_new_id)
    name: str = ""
    description: str = ""
    system_prompt: str = ""
    user_prompt_template: str = "{text}"
    target_language: str = "en"
    current_version: int = 1
    is_system: bool = False
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)


@dataclass
class PromptVersion:
    """Immutable historic snapshot of a prompt template for auditing and rollback."""

    id: str = field(default_factory=_new_id)
    prompt_id: str = ""
    version: int = 1
    system_prompt: str = ""
    user_prompt_template: str = "{text}"
    change_summary: str = ""
    is_active: bool = True
    created_at: int = field(default_factory=_now)



@dataclass
class MessagePayload:
    """Immutable-ish snapshot of an incoming Telegram message (domain-level)."""

    message_id: int
    chat_id: str
    chat_type: str = "channel"  # channel | supergroup | group | private
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    chat_username: Optional[str] = None
    chat_title: Optional[str] = None
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
    # origin of a forwarded message: {type, from_chat_id, from_message_id, sender_name, ...}
    forward_origin: Optional[dict] = None
    # album grouping id — multiple media messages share one id
    media_group_id: Optional[str] = None
    views: int = 0
    entities: Optional[List[Any]] = None
    caption_entities: Optional[List[Any]] = None
    reply_markup: Optional[Any] = None

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
    intermediate_chat_id: Optional[str] = None
    intermediate_message_id: Optional[int] = None
    delivery_stage: str = "DIRECT"
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)

