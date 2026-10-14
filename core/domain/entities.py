import re
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


# Local compiled patterns for CleanerRule. Defined here rather than imported
# from services.py to keep entities.py free of a services -> entities cycle.
_CLEANER_URL_RE = re.compile(
    r"https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+",
    re.IGNORECASE,
)
_CLEANER_MENTION_RE = re.compile(r"@[A-Za-z0-9_]{4,64}")
_CLEANER_HASHTAG_RE = re.compile(r"#\w+")
_CLEANER_EMOJI_RE = re.compile(
    r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\ufe00-\ufe0f]",
    flags=re.UNICODE,
)


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
            if "show_forward_header" in self.metadata:
                self.show_forward_header = bool(self.metadata["show_forward_header"])
            if self.delay_seconds == 0.0 and ("forward_delay_seconds" in self.metadata or "delay_seconds" in self.metadata):
                try:
                    self.delay_seconds = float(self.metadata.get("forward_delay_seconds") or self.metadata.get("delay_seconds") or 0.0)
                except (ValueError, TypeError):
                    pass
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

    @replacements.setter
    def replacements(self, val: Dict[str, str]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["replacements"] = dict(val or {})

    @property
    def header(self) -> str:
        return self.custom_header or (str(self.metadata.get("header") or "") if isinstance(self.metadata, dict) else "")

    @header.setter
    def header(self, val: str) -> None:
        self.custom_header = str(val or "")
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["header"] = self.custom_header

    @property
    def footer(self) -> str:
        return self.custom_footer or (str(self.metadata.get("footer") or "") if isinstance(self.metadata, dict) else "")

    @footer.setter
    def footer(self, val: str) -> None:
        self.custom_footer = str(val or "")
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["footer"] = self.custom_footer

    @property
    def block_voice(self) -> bool:
        return bool(self.metadata.get("block_voice", False)) if isinstance(self.metadata, dict) else False

    @block_voice.setter
    def block_voice(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["block_voice"] = bool(val)

    @property
    def block_stickers(self) -> bool:
        return bool(self.metadata.get("block_stickers", False)) if isinstance(self.metadata, dict) else False

    @block_stickers.setter
    def block_stickers(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["block_stickers"] = bool(val)

    @property
    def block_video(self) -> bool:
        return bool(self.metadata.get("block_video", False)) if isinstance(self.metadata, dict) else False

    @block_video.setter
    def block_video(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["block_video"] = bool(val)

    @property
    def block_audio(self) -> bool:
        return bool(self.metadata.get("block_audio", False)) if isinstance(self.metadata, dict) else False

    @block_audio.setter
    def block_audio(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["block_audio"] = bool(val)

    @property
    def block_text(self) -> bool:
        return bool(self.metadata.get("block_text", False)) if isinstance(self.metadata, dict) else False

    @block_text.setter
    def block_text(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["block_text"] = bool(val)

    @property
    def block_links(self) -> bool:
        return bool(self.metadata.get("block_links", False)) if isinstance(self.metadata, dict) else False

    @block_links.setter
    def block_links(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["block_links"] = bool(val)

    @property
    def clean_usernames(self) -> bool:
        return bool(self.metadata.get("clean_usernames", False)) if isinstance(self.metadata, dict) else False

    @clean_usernames.setter
    def clean_usernames(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["clean_usernames"] = bool(val)

    @property
    def clean_hashtags(self) -> bool:
        return bool(self.metadata.get("clean_hashtags", False)) if isinstance(self.metadata, dict) else False

    @clean_hashtags.setter
    def clean_hashtags(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["clean_hashtags"] = bool(val)

    @property
    def clean_phones(self) -> bool:
        return bool(self.metadata.get("clean_phones", False)) if isinstance(self.metadata, dict) else False

    @clean_phones.setter
    def clean_phones(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["clean_phones"] = bool(val)

    @property
    def clean_buttons(self) -> bool:
        return bool(self.metadata.get("clean_buttons", False)) if isinstance(self.metadata, dict) else False

    @clean_buttons.setter
    def clean_buttons(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["clean_buttons"] = bool(val)

    @property
    def clean_empty_lines(self) -> bool:
        return bool(self.metadata.get("clean_empty_lines", False)) if isinstance(self.metadata, dict) else False

    @clean_empty_lines.setter
    def clean_empty_lines(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["clean_empty_lines"] = bool(val)

    @property
    def clean_custom_words(self) -> List[str]:
        if not isinstance(self.metadata, dict):
            return []
        words = self.metadata.get("clean_custom_words", [])
        return list(words) if isinstance(words, list) else []

    @clean_custom_words.setter
    def clean_custom_words(self, val: List[str]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["clean_custom_words"] = [str(w).strip() for w in val if str(w).strip()]

    @property
    def max_time_edit(self) -> int:
        if not isinstance(self.metadata, dict):
            return 86400
        return int(self.metadata.get("max_time_edit", 86400) or 86400)

    @max_time_edit.setter
    def max_time_edit(self, val: int) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["max_time_edit"] = int(val)

    @property
    def remove_emojis(self) -> bool:
        return bool(self.metadata.get("remove_emojis", False)) if isinstance(self.metadata, dict) else False

    @remove_emojis.setter
    def remove_emojis(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["remove_emojis"] = bool(val)

    @property
    def sync_edits(self) -> bool:
        return bool(self.metadata.get("sync_edits", False)) if isinstance(self.metadata, dict) else False

    @property
    def sync_deletes(self) -> bool:
        return bool(self.metadata.get("sync_deletes", False)) if isinstance(self.metadata, dict) else False

    @sync_deletes.setter
    def sync_deletes(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["sync_deletes"] = bool(val)

    @property
    def album_mode(self) -> str:
        return str(self.metadata.get("album_mode") or "album") if isinstance(self.metadata, dict) else "album"

    # --- Task Overview & Routing Extensions (Screens 1 & 5) ---
    @property
    def topic_id(self) -> Optional[int]:
        if not isinstance(self.metadata, dict):
            return None
        val = self.metadata.get("topic_id")
        return int(val) if val is not None and str(val).isdigit() else None

    @topic_id.setter
    def topic_id(self, val: Optional[int]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["topic_id"] = val

    @property
    def debug_mode(self) -> bool:
        return bool(self.metadata.get("debug_mode", False)) if isinstance(self.metadata, dict) else False

    @debug_mode.setter
    def debug_mode(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["debug_mode"] = bool(val)

    @property
    def sender_type(self) -> str:
        return str(self.metadata.get("sender_type") or "Personal Telegram Account") if isinstance(self.metadata, dict) else "Personal Telegram Account"

    @sender_type.setter
    def sender_type(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["sender_type"] = str(val or "Personal Telegram Account")

    # --- Filters Section (Screen 2) ---
    @property
    def duplicate_filters(self) -> bool:
        return bool(self.metadata.get("duplicate_filters", False)) if isinstance(self.metadata, dict) else False

    @duplicate_filters.setter
    def duplicate_filters(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["duplicate_filters"] = bool(val)

    # --- Cleaner (v2.0 app update) -----------------------------------------
    # The cleaner is carried as metadata and resolved to a CleanerRule at
    # transform time. Kept on the rule (not a separate table) because it is
    # per-task configuration, not a shared resource.
    @property
    def cleaner_enabled(self) -> bool:
        if isinstance(self.metadata, dict) and "cleaner_enabled" in self.metadata:
            return bool(self.metadata["cleaner_enabled"])
        # If legacy cleaner fields are active, consider cleaner enabled
        if bool(self.remove_links or self.clean_usernames or self.clean_hashtags or self.remove_emojis or self.clean_custom_words):
            return True
        return False

    @cleaner_enabled.setter
    def cleaner_enabled(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["cleaner_enabled"] = bool(val)

    @property
    def cleaner_config(self) -> dict:
        if not isinstance(self.metadata, dict):
            return {}
        cfg = self.metadata.get("cleaner_config")
        return cfg if isinstance(cfg, dict) else {}

    @cleaner_config.setter
    def cleaner_config(self, val: dict) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["cleaner_config"] = val if isinstance(val, dict) else {}

    def build_cleaner(self) -> Optional["CleanerRule"]:
        """Materialize the configured cleaner, or None when disabled/empty."""
        if not self.cleaner_enabled:
            return None
        raw_cfg = self.cleaner_config
        if not isinstance(raw_cfg, dict) or not raw_cfg:
            # Check if any legacy cleaner settings are present
            if not (self.clean_custom_words or self.remove_links or self.clean_usernames or self.clean_hashtags or self.remove_emojis):
                return None
            raw_cfg = {}
        cfg = dict(raw_cfg)

        # Merge legacy fields if not explicitly specified in cfg
        patterns = list(cfg.get("patterns") or []) if isinstance(cfg.get("patterns"), list) else []
        if self.clean_custom_words:
            for w in self.clean_custom_words:
                sw = str(w).strip()
                if sw and sw not in patterns:
                    patterns.append(sw)

        remove_urls = bool(cfg.get("remove_urls", self.remove_links))
        remove_mentions = bool(cfg.get("remove_mentions", self.clean_usernames))
        remove_hashtags = bool(cfg.get("remove_hashtags", self.clean_hashtags))
        remove_emojis = bool(cfg.get("remove_emojis", self.remove_emojis))

        clean_pats = [str(p) for p in patterns if isinstance(p, str) and p.strip()]
        if not clean_pats and not (remove_urls or remove_mentions or remove_hashtags or remove_emojis):
            return None

        try:
            return CleanerRule(
                id=f"{self.id}:cleaner",
                name=str(cfg.get("name") or "cleaner"),
                patterns=clean_pats,
                use_regex=bool(cfg.get("use_regex", False)),
                remove_urls=remove_urls,
                remove_mentions=remove_mentions,
                remove_hashtags=remove_hashtags,
                remove_emojis=remove_emojis,
                apply_to_caption=bool(cfg.get("apply_to_caption", True)),
                case_sensitive=bool(cfg.get("case_sensitive", False)),
                is_active=True,
                owner_user_id=self.owner_user_id,
            )
        except Exception:
            return None

    # --- Show-forward-header convenience flag (v2.0 app update) ------------
    # DIRECT_FORWARD on Telegram keeps the original sender header; COPY_MESSAGE
    # (the engine default) is a clean copy. The UI toggle maps to forward_mode.
    @property
    def show_forward_header(self) -> bool:
        return getattr(self, "forward_mode", None) == ForwardMode.DIRECT_FORWARD

    @show_forward_header.setter
    def show_forward_header(self, val: bool) -> None:
        self.forward_mode = ForwardMode.DIRECT_FORWARD if bool(val) else ForwardMode.COPY_MESSAGE

    @property
    def sync_replies(self) -> bool:
        return bool(self.metadata.get("sync_replies", False)) if isinstance(self.metadata, dict) else False

    @sync_replies.setter
    def sync_replies(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["sync_replies"] = bool(val)

    @property
    def crypto_mode(self) -> bool:
        return bool(self.metadata.get("crypto_mode", False)) if isinstance(self.metadata, dict) else False

    @crypto_mode.setter
    def crypto_mode(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["crypto_mode"] = bool(val)

    @property
    def blacklist(self) -> List[str]:
        if not isinstance(self.metadata, dict):
            return []
        bl = self.metadata.get("blacklist", [])
        return list(bl) if isinstance(bl, list) else []

    @blacklist.setter
    def blacklist(self, val: List[str]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["blacklist"] = list(val or [])

    @property
    def whitelist(self) -> List[str]:
        if not isinstance(self.metadata, dict):
            return []
        wl = self.metadata.get("whitelist", [])
        return list(wl) if isinstance(wl, list) else []

    @whitelist.setter
    def whitelist(self, val: List[str]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["whitelist"] = list(val or [])

    @property
    def blacklist_users(self) -> List[str]:
        if not isinstance(self.metadata, dict):
            return []
        bu = self.metadata.get("blacklist_users", [])
        return list(bu) if isinstance(bu, list) else []

    @blacklist_users.setter
    def blacklist_users(self, val: List[str]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["blacklist_users"] = list(val or [])

    @property
    def whitelist_users(self) -> List[str]:
        if not isinstance(self.metadata, dict):
            return []
        wu = self.metadata.get("whitelist_users", [])
        return list(wu) if isinstance(wu, list) else []

    @whitelist_users.setter
    def whitelist_users(self, val: List[str]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["whitelist_users"] = list(val or [])

    # --- Schedules Section (Screen 2) ---
    @property
    def auto_post_scheduler(self) -> Dict[str, Any]:
        if not isinstance(self.metadata, dict):
            return {"enabled": False}
        sched = self.metadata.get("auto_post_scheduler", {})
        return dict(sched) if isinstance(sched, dict) else {"enabled": bool(sched)}

    @auto_post_scheduler.setter
    def auto_post_scheduler(self, val: Dict[str, Any]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["auto_post_scheduler"] = val

    @property
    def auto_posting(self) -> bool:
        return bool(self.auto_post_scheduler.get("enabled", False))

    @auto_posting.setter
    def auto_posting(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        curr = dict(self.auto_post_scheduler)
        curr["enabled"] = bool(val)
        self.metadata["auto_post_scheduler"] = curr

    @property
    def power_schedule(self) -> Dict[str, Any]:
        if not isinstance(self.metadata, dict):
            return {"enabled": False}
        ps = self.metadata.get("power_schedule", {})
        return dict(ps) if isinstance(ps, dict) else {"enabled": bool(ps)}

    @power_schedule.setter
    def power_schedule(self, val: Dict[str, Any]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["power_schedule"] = val

    @property
    def power_control(self) -> bool:
        return bool(self.power_schedule.get("enabled", False))

    @power_control.setter
    def power_control(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        curr = dict(self.power_schedule)
        curr["enabled"] = bool(val)
        self.metadata["power_schedule"] = curr

    # --- Processing Messages Section (Screen 2) ---
    @property
    def process_raw_text(self) -> bool:
        return bool(self.metadata.get("process_raw_text", False)) if isinstance(self.metadata, dict) else False

    @process_raw_text.setter
    def process_raw_text(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["process_raw_text"] = bool(val)

    @property
    def button_to_text(self) -> bool:
        return bool(self.metadata.get("button_to_text", False)) if isinstance(self.metadata, dict) else False

    @button_to_text.setter
    def button_to_text(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["button_to_text"] = bool(val)

    # --- Modify Content Section (Screen 3) ---
    @property
    def translate_lang(self) -> str:
        return str(self.metadata.get("translate_lang") or "") if isinstance(self.metadata, dict) else ""

    @translate_lang.setter
    def translate_lang(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["translate_lang"] = str(val or "")

    @property
    def watermark(self) -> str:
        return str(self.metadata.get("watermark") or "") if isinstance(self.metadata, dict) else ""

    @watermark.setter
    def watermark(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["watermark"] = str(val or "")

    @property
    def link_buttons(self) -> List[Dict[str, str]]:
        if not isinstance(self.metadata, dict):
            return []
        btns = self.metadata.get("link_buttons", [])
        return list(btns) if isinstance(btns, list) else []

    @link_buttons.setter
    def link_buttons(self, val: List[Dict[str, str]]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["link_buttons"] = list(val or [])

    @property
    def affiliate_kit(self) -> Dict[str, Any]:
        if not isinstance(self.metadata, dict):
            return {"enabled": False}
        ak = self.metadata.get("affiliate_kit", {})
        return dict(ak) if isinstance(ak, dict) else {"enabled": bool(ak)}

    @affiliate_kit.setter
    def affiliate_kit(self, val: Dict[str, Any]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["affiliate_kit"] = val

    # --- AI Features Section (Screen 3) ---
    @property
    def ai_creativity(self) -> float:
        if not isinstance(self.metadata, dict):
            return 0.7
        try:
            return float(self.metadata.get("ai_creativity", 0.7))
        except (ValueError, TypeError):
            return 0.7

    @ai_creativity.setter
    def ai_creativity(self, val: float) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["ai_creativity"] = float(val)

    @property
    def ai_max_tokens(self) -> int:
        if not isinstance(self.metadata, dict):
            return 600
        try:
            return int(self.metadata.get("ai_max_tokens", 600))
        except (ValueError, TypeError):
            return 600

    @ai_max_tokens.setter
    def ai_max_tokens(self, val: int) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["ai_max_tokens"] = int(val)

    @property
    def ocr_image_ai(self) -> bool:
        return bool(self.metadata.get("ocr_image_ai", False)) if isinstance(self.metadata, dict) else False

    @ocr_image_ai.setter
    def ocr_image_ai(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["ocr_image_ai"] = bool(val)

    @property
    def smart_ai_rewrite(self) -> bool:
        return bool(self.metadata.get("smart_ai_rewrite", False)) if isinstance(self.metadata, dict) else False

    @smart_ai_rewrite.setter
    def smart_ai_rewrite(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["smart_ai_rewrite"] = bool(val)

    @property
    def news_digest(self) -> bool:
        return bool(self.metadata.get("news_digest", False)) if isinstance(self.metadata, dict) else False

    @news_digest.setter
    def news_digest(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["news_digest"] = bool(val)

    # --- Feature Groups & Add-on Extensions (Screen 4) ---
    @property
    def feature_group(self) -> str:
        return str(self.metadata.get("feature_group") or "all") if isinstance(self.metadata, dict) else "all"

    @feature_group.setter
    def feature_group(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["feature_group"] = str(val or "all")

    @property
    def paid_addons(self) -> Dict[str, Any]:
        if not isinstance(self.metadata, dict):
            return {}
        addons = self.metadata.get("paid_addons", {})
        return dict(addons) if isinstance(addons, dict) else {}

    @paid_addons.setter
    def paid_addons(self, val: Dict[str, Any]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["paid_addons"] = dict(val or {})

    # --- Phase 2: runtime-executable advanced settings ---------------------
    # These properties are the single source of truth for the advanced task
    # configuration. Both the durable queue and the direct dispatcher read
    # them at *dispatch time*, so a change made in any client (bot, web, TMA,
    # mobile) takes effect on the very next message without a restart.

    @property
    def forward_delay_seconds(self) -> float:
        """Delay (seconds) applied before dispatch — anti-detection/scheduling."""
        if not isinstance(self.metadata, dict):
            return float(self.delay_seconds or 0.0)
        raw = self.metadata.get("forward_delay_seconds", self.delay_seconds)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return float(self.delay_seconds or 0.0)
        # Bounded: a delay is a scheduling hint, not an unbounded sleep. The
        # hard cap keeps a misconfigured rule from stalling a worker forever.
        return max(0.0, min(val, 3600.0))

    @forward_delay_seconds.setter
    def forward_delay_seconds(self, val: Optional[float]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        try:
            self.metadata["forward_delay_seconds"] = max(0.0, min(float(val or 0.0), 3600.0))
        except (TypeError, ValueError):
            self.metadata["forward_delay_seconds"] = 0.0

    @property
    def edit_time_limit_seconds(self) -> int:
        """Only sync edits made within this many seconds of the original."""
        if not isinstance(self.metadata, dict):
            return 0
        try:
            return max(0, int(self.metadata.get("edit_time_limit_seconds", 0)))
        except (TypeError, ValueError):
            return 0

    @edit_time_limit_seconds.setter
    def edit_time_limit_seconds(self, val: Optional[int]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        try:
            self.metadata["edit_time_limit_seconds"] = max(0, int(val or 0))
        except (TypeError, ValueError):
            self.metadata["edit_time_limit_seconds"] = 0

    @property
    def keyword_confirmation(self) -> bool:
        if not isinstance(self.metadata, dict):
            return False
        return bool(self.metadata.get("keyword_confirmation", False))

    @keyword_confirmation.setter
    def keyword_confirmation(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["keyword_confirmation"] = bool(val)

    @property
    def keyword_reply(self) -> str:
        """Reply text sent when a whitelisted keyword triggers."""
        if not isinstance(self.metadata, dict):
            return ""
        return str(self.metadata.get("keyword_reply") or "")

    @keyword_reply.setter
    def keyword_reply(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["keyword_reply"] = str(val or "")

    @property
    def auto_delete_seconds(self) -> int:
        """Delete the forwarded message from the target after N seconds (0 = off)."""
        if not isinstance(self.metadata, dict):
            return 0
        try:
            return max(0, int(self.metadata.get("auto_delete_seconds", 0)))
        except (TypeError, ValueError):
            return 0

    @auto_delete_seconds.setter
    def auto_delete_seconds(self, val: Optional[int]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        try:
            self.metadata["auto_delete_seconds"] = max(0, int(val or 0))
        except (TypeError, ValueError):
            self.metadata["auto_delete_seconds"] = 0

    @property
    def watermark_text(self) -> str:
        """Watermark appended to forwarded text (alias of `watermark`)."""
        if not isinstance(self.metadata, dict):
            return ""
        return str(self.metadata.get("watermark_text") or self.metadata.get("watermark") or "")

    @watermark_text.setter
    def watermark_text(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["watermark_text"] = str(val or "")

    def is_addon_enabled(self, key: str) -> bool:
        """Server-side paid-addon entitlement check.

        A client-supplied `paid_addons` flag is NEVER trusted on its own: the
        addon must be marked enabled in the persisted rule metadata. This is
        the single enforcement point consulted by the pipeline before any
        premium transformation runs.
        """
        if not isinstance(self.metadata, dict):
            return False
        addons = self.metadata.get("paid_addons", {})
        if not isinstance(addons, dict):
            return False
        return bool(addons.get(key, False))

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
            # AI Rewrite settings (pipeline stage, off by default)
            "ai_config_id": str(self.ai_config_id or ""),
            "ai_fallback_policy": getattr(getattr(self, "ai_fallback_policy", None), "value", str(getattr(self, "ai_fallback_policy", "DROP"))),
            "ai_prompt_version": int(getattr(self, "ai_prompt_version", 0) or 0),
            "ai_timeout_seconds": float(getattr(self, "ai_timeout_seconds", 15.0) or 15.0),
            "prompt_id": str((self.metadata or {}).get("prompt_id", "") or ""),
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
        # AI Rewrite (pipeline stage)
        if "ai_config_id" in draft:
            self.ai_config_id = str(draft.get("ai_config_id") or "") or None
        if "ai_fallback_policy" in draft and draft["ai_fallback_policy"]:
            try:
                self.ai_fallback_policy = AIFallbackPolicy(str(draft["ai_fallback_policy"]).upper())
            except ValueError:
                pass
        if "ai_prompt_version" in draft and draft["ai_prompt_version"] is not None:
            try:
                self.ai_prompt_version = max(0, int(draft["ai_prompt_version"]))
            except (ValueError, TypeError):
                pass
        if "ai_timeout_seconds" in draft and draft["ai_timeout_seconds"] is not None:
            try:
                self.ai_timeout_seconds = min(120.0, max(1.0, float(draft["ai_timeout_seconds"])))
            except (ValueError, TypeError):
                pass
        if "prompt_id" in draft:
            if not isinstance(self.metadata, dict):
                self.metadata = {}
            pid = str(draft.get("prompt_id") or "")
            if pid:
                self.metadata["prompt_id"] = pid
            else:
                self.metadata.pop("prompt_id", None)

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
class CleanerRule:
    """Content cleaner — strips specific text/regex from a message body.

    Implements the "Cleaner" feature from the v2.0 app update: delete specific
    content (words, URLs, @handles, #hashtags, emoji, or raw patterns) from
    forwarded messages based on user conditions.

    Semantics:
    - ``patterns`` are removed literally (regex when ``use_regex``).
    - ``remove_urls`` strips http(s)/t.me links. ``remove_mentions`` strips
      @username. ``remove_hashtags`` strips #tags. ``remove_emojis`` strips
      pictographs (delegated to the existing EMOJI_REGEX).
    - ``apply_to_caption`` also cleans media captions, not just bodies.
    - Everything is a removal-only transform: it never drops a message.
    """
    id: str = field(default_factory=_new_id)
    name: str = ""
    patterns: List[str] = field(default_factory=list)
    use_regex: bool = False
    remove_urls: bool = False
    remove_mentions: bool = False
    remove_hashtags: bool = False
    remove_emojis: bool = False
    apply_to_caption: bool = True
    case_sensitive: bool = False
    is_active: bool = True
    owner_user_id: int = 0

    def clean(self, text: str) -> str:
        """Return ``text`` with the configured content removed."""
        if not text:
            return text
        out = text
        for pat in self.patterns:
            if not pat:
                continue
            try:
                if self.use_regex:
                    flags = 0 if self.case_sensitive else re.IGNORECASE
                    out = re.sub(pat, "", out, flags=flags)
                elif self.case_sensitive:
                    out = out.replace(pat, "")
                else:
                    out = re.sub(re.escape(pat), "", out, flags=re.IGNORECASE)
            except re.error:
                # A malformed pattern must not break forwarding.
                continue
        if self.remove_urls:
            out = _CLEANER_URL_RE.sub("", out)
        if self.remove_mentions:
            out = _CLEANER_MENTION_RE.sub("", out)
        if self.remove_hashtags:
            out = _CLEANER_HASHTAG_RE.sub("", out)
        if self.remove_emojis:
            out = _CLEANER_EMOJI_RE.sub("", out)
        # Collapse the whitespace holes the removals punched out so the
        # rendered message does not show "word  word" artifacts.
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip()


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
    # v12: user prompts are per-tenant; system prompts (is_system) stay shared.
    owner_user_id: int = 0


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
    edit_date: Optional[int] = None
    # epoch seconds of the message this edit replaces (0/None when unknown)
    original_date: Optional[int] = None
    album_messages: List[Any] = field(default_factory=list)

    @property
    def effective_text(self) -> str:
        raw = self.caption if (self.has_media and self.caption) else self.text
        if raw is None:
            return ""
        return str(raw) if type(raw) is str else f"{raw}"


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

