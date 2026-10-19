import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional, Tuple

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
# branding.py is dependency-free, so a module-level import is safe here (unlike
# content_policy, which is imported lazily inside the property below).
from . import branding as branding_policy

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .content_policy import ContentPolicy


def _new_id() -> str:
    return str(uuid.uuid4())


def _chat_key(chat_id: Any) -> str:
    """Normalise a chat id so every spelling of it compares equal.

    ``-100123`` as an int, as a float and as a string is one chat; an
    ``@username`` is passed through unchanged. Used to match per-source and
    per-target content-policy overrides.
    """
    if chat_id is None:
        return ""
    raw = str(chat_id).strip()
    if not raw:
        return ""
    try:
        return str(int(float(raw)))
    except (TypeError, ValueError):
        return raw


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
# Phone numbers: a bounded candidate with no nested quantifiers (no ReDoS), then a
# callback that accepts only things that really look like a contact number — a
# leading "+" or at least 10 digits carrying a separator. A bare 10-digit product
# id is deliberately left alone.
_CLEANER_PHONE_CANDIDATE_RE = re.compile(r"(?<![\d])\+?\d[\d\s\-().]{5,18}\d(?![\d])")
_CLEANER_PHONE_SEPARATOR_RE = re.compile(r"[\s\-().]")
_CLEANER_DIGITS_RE = re.compile(r"\D")
_CLEANER_BLANK_LINES_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)*")


def _strip_phone_numbers(text: str) -> str:
    """Remove contact numbers from ``text`` without touching ordinary numbers."""

    def _replace(match: "re.Match[str]") -> str:
        candidate = match.group(0)
        digits = _CLEANER_DIGITS_RE.sub("", candidate)
        if not 8 <= len(digits) <= 15:
            return candidate
        if candidate.strip().startswith("+"):
            return ""
        if len(digits) >= 10 and _CLEANER_PHONE_SEPARATOR_RE.search(candidate):
            return ""
        return candidate

    return _CLEANER_PHONE_CANDIDATE_RE.sub(_replace, text)


def _now() -> int:
    return int(time.time())


#: Longest delay a task may configure, in seconds (24h). One constant for every
#: layer that bounds a delay: the advanced-settings schema advertises 86400, the
#: legacy first-class field is clamped here, and both dispatch paths cap at it —
#: so a value the UI accepts is a value the worker actually honours.
MAX_FORWARD_DELAY_SECONDS = 86400.0

#: Effective task statuses (see ``ForwardRule.status_state``). "Running" is the
#: only state in which the forwarder acts on a new message; the other three all
#: mean work is stopped, and each says *why* so the UI can be honest about it.
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_PAUSED_UNTIL = "paused_until"
STATUS_DISABLED = "disabled"
TASK_STATUSES = (STATUS_RUNNING, STATUS_PAUSED, STATUS_PAUSED_UNTIL, STATUS_DISABLED)


def clamp_delay_seconds(value: Any, *, maximum: float = MAX_FORWARD_DELAY_SECONDS) -> float:
    """Return a delay the forwarding engine can honour, whatever the client sent.

    Client-supplied numbers arrive as text or as callback payloads, so they are
    not trustworthy: `nan` reached SQLite as NULL (NOT NULL violation on save),
    `inf` reached the job payload as invalid JSON (`Infinity`), and a negative
    delay left the screens showing a value the queue ignored. Anything that is not
    a finite number in ``[0, maximum]`` is normalized instead of persisted.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(seconds) or math.isinf(seconds):
        return 0.0
    if seconds <= 0.0:
        return 0.0
    return min(seconds, float(maximum))


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

    def is_currently_paused(self, now: Optional[float] = None) -> bool:
        """Return True if rule is explicitly paused and temporary pause (if any) has not elapsed."""
        if not self.is_paused:
            return False
        if self.paused_until and self.paused_until > 0:
            current_ts = time.time() if now is None else now
            if current_ts >= self.paused_until:
                return False
        return True

    # -- effective task status (one authority for UI + engine) ---------------
    #
    # Two independent mechanisms stop a task, and the engine obeys both:
    #   * the owner's on/off switch (`is_active`) — off means the task is not
    #     routed at all (`matches_source` is False, the SQL never selects it);
    #   * the timed pause (`is_paused` + `paused_until`) — the routing still
    #     selects it, and the pipeline drops the message while the durable worker
    #     postpones any job it had already accepted.
    # Reading `is_active` alone is how a paused task came to be displayed as
    # RUNNING while nothing was being forwarded, so every surface derives its
    # status from `status_state()` instead.
    def status_state(self, now: Optional[float] = None) -> str:
        """The task's real status: RUNNING | PAUSED | PAUSED_UNTIL | DISABLED.

        An expired ``paused_until`` is RUNNING: the pause has elapsed, and
        ``is_currently_paused()`` — what the engine asks — reports False too.
        """
        if not self.is_active:
            return STATUS_DISABLED
        if not self.is_paused:
            return STATUS_RUNNING
        if self.paused_until and self.paused_until > 0:
            current_ts = time.time() if now is None else now
            return STATUS_PAUSED_UNTIL if current_ts < self.paused_until else STATUS_RUNNING
        return STATUS_PAUSED

    def engine_accepts_new_work(self, now: Optional[float] = None) -> bool:
        """True only when the forwarder would really act on a new message."""
        return bool(self.is_active) and not self.is_currently_paused(now)
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
        # A delay is a scheduling hint the workers must be able to honour: it is
        # normalized here, at construction, because every write path (bot screens,
        # Go API -> RPC, web dashboard, backup restore, task duplication) builds a
        # ForwardRule and only some of them validate their input.
        self.delay_seconds = clamp_delay_seconds(self.delay_seconds)
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

        # The target list and its primary are two spellings of one fact: the
        # legacy `target_chat_id` mirrors the first list entry. Sources enforce
        # this in their setter; targets cannot (the list is a plain field read by
        # the dispatcher and by older clients), so the invariant is applied here
        # as well — otherwise a task built as `ForwardRule(target_chat_ids=[...])`
        # routes correctly (all_targets reads the list) while `target_chat_id`,
        # `destination_chat_id` and every legacy reader see nothing.
        if self.target_chat_ids:
            self.target_chat_ids = [str(t).strip() for t in self.target_chat_ids if str(t).strip()]
            if not self.target_chat_id and self.target_chat_ids:
                self.target_chat_id = self.target_chat_ids[0]
                if not self.target_chat_name:
                    self.target_chat_name = self.target_chat_id

    def set_targets(self, ids, names=None) -> None:
        """Set the whole target list, mirroring the first entry into the primary.

        The sanctioned write path for multi-target tasks: it keeps
        `target_chat_ids` and `target_chat_id` in step whatever the caller knows
        about (a caller that only has ids still gets a correct primary).
        """
        seen: set = set()
        uniq: List[str] = []
        for raw in ids or []:
            tid = str(raw).strip()
            if tid and tid not in seen:
                seen.add(tid)
                uniq.append(tid)
        self.target_chat_ids = uniq
        if uniq:
            self.target_chat_id = uniq[0]
            clean_names = [str(n).strip() for n in (names or []) if str(n).strip()]
            if clean_names:
                self.target_chat_name = clean_names[0]
            elif not self.target_chat_name:
                self.target_chat_name = uniq[0]

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

    def clear_header(self) -> None:
        """Remove the custom header from every key that can carry it.

        The header is readable from two places — ``custom_header`` (the field the
        panel writes) and ``metadata["header"]`` (which the setter mirrors for
        rules restored from a backup or written by the older API) — while the
        panel's clear button wrote a third, ``metadata["custom_header"]``.
        Clearing one and reading another left the "deleted" header in the
        outgoing message: with ``custom_header=""`` and ``metadata["header"]``
        still holding the text, ``rule.header`` returned the old text and
        ``transform_text`` kept prepending it. Clear all of them together so a
        delete is a delete.
        """
        self.custom_header = ""
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["header"] = ""
        self.metadata["custom_header"] = ""

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

    @sync_edits.setter
    def sync_edits(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["sync_edits"] = bool(val)

    @property
    def sync_deletes(self) -> bool:
        return bool(self.metadata.get("sync_deletes", False)) if isinstance(self.metadata, dict) else False

    @sync_deletes.setter
    def sync_deletes(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["sync_deletes"] = bool(val)

    @property
    def sync_edits_effective(self) -> bool:
        """Whether an edit must update the already-delivered copy in place.

        ``ignore_edits`` is the switch the task panel exposes ("Message Edit:
        Sync / Ignore"); ``sync_edits`` is an older post-send gate written by
        API clients and by proto zero-values, which no panel shows.

        The panel's Ignore is a **veto** and is checked first: a task whose
        panel reads "Ignore" must stop updating its copies even when a stale
        ``sync_edits=True`` sits in the metadata. It did not (measured on the
        deployed image: ``ignore_edits=True`` with ``metadata["sync_edits"]=True``
        still edited the target), so the visible switch was decoration for every
        task the older API had touched. When the panel allows edits, an explicit
        legacy ``sync_edits`` still decides, so a client that only knows the old
        field keeps its meaning.
        """
        if bool(self.ignore_edits):
            return False
        if isinstance(self.metadata, dict) and "sync_edits" in self.metadata:
            return bool(self.metadata["sync_edits"])
        return True

    @property
    def album_mode(self) -> str:
        return str(self.metadata.get("album_mode") or "album") if isinstance(self.metadata, dict) else "album"

    @album_mode.setter
    def album_mode(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["album_mode"] = str(val or "album")

    # --- Delivery route: the owner's account, or a bot they attached -------- #
    @property
    def bot_token_id(self) -> str:
        """Id of the stored, encrypted bot token this task forwards through."""
        return str(self.metadata.get("bot_token_id") or "") if isinstance(self.metadata, dict) else ""

    @bot_token_id.setter
    def bot_token_id(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["bot_token_id"] = str(val or "")

    @property
    def bot_token_label(self) -> str:
        """Human label of the attached bot (its username) — never the token."""
        return str(self.metadata.get("bot_token_label") or "") if isinstance(self.metadata, dict) else ""

    @bot_token_label.setter
    def bot_token_label(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["bot_token_label"] = str(val or "")

    @property
    def delivery_transport(self) -> str:
        """``USER`` (the owner's own account, the default) or ``BOT``.

        A task can only ask for the bot route when a token is actually attached;
        otherwise it silently falls back to the account route, because a task
        that stops forwarding is worse than one that forwards as the owner.
        """
        if not isinstance(self.metadata, dict):
            return "USER"
        wanted = str(self.metadata.get("delivery_transport") or "USER").strip().upper()
        if wanted == "BOT" and self.bot_token_id:
            return "BOT"
        return "USER"

    @delivery_transport.setter
    def delivery_transport(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["delivery_transport"] = "BOT" if str(val or "").strip().upper() == "BOT" else "USER"

    @property
    def use_bot_route(self) -> bool:
        return self.delivery_transport == "BOT"

    @property
    def inline_buttons_raw(self) -> Any:
        """Buttons to attach to every copy in bot-route mode (raw config)."""
        return self.metadata.get("inline_buttons") if isinstance(self.metadata, dict) else None

    @inline_buttons_raw.setter
    def inline_buttons_raw(self, val: Any) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["inline_buttons"] = val or None

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
        if bool(self.remove_links or self.clean_usernames or self.clean_hashtags or self.remove_emojis
                or self.clean_custom_words or self.clean_phones or self.clean_buttons
                or self.clean_empty_lines):
            return True
        return False

    @cleaner_enabled.setter
    def cleaner_enabled(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["cleaner_enabled"] = bool(val)

    #: The cleaner options, named by the flag that carries each one.
    CLEANER_OPTION_FLAGS: "ClassVar[Tuple[str, ...]]" = (
        "clean_usernames",
        "clean_hashtags",
        "clean_phones",
        "clean_empty_lines",
        "clean_buttons",
        "remove_links",
        "remove_emojis",
    )

    @property
    def cleaner_active_option_count(self) -> int:
        """How many cleaner options the engine will actually apply.

        The panel badge is derived from this rather than from a hand-written
        subset, so the screen cannot drift from the engine: with the master
        switch off ``build_cleaner()`` returns None and nothing is cleaned (so
        the count is 0), and every option the engine does run is counted exactly
        once — a non-empty custom-word list included.
        """
        if not self.cleaner_enabled:
            return 0
        count = sum(1 for flag in self.CLEANER_OPTION_FLAGS if getattr(self, flag, False))
        return count + (1 if self.clean_custom_words else 0)

    #: The Filters screen's media/content toggles, named by their flag. These are
    #: exactly the gates `DurableMessagePipeline` applies in its FILTER stage.
    FILTER_OPTION_FLAGS: "ClassVar[Tuple[str, ...]]" = (
        "block_links",
        "block_video",
        "block_audio",
        "block_stickers",
        "block_text",
        "block_voice",
    )

    @property
    def filter_active_option_count(self) -> int:
        """How many content filters the engine will actually apply.

        The `ui_rule_btn_filters` badge opens the Filters screen (`rfl:`), which
        owns six toggles — so the badge counts the toggles the pipeline enforces,
        plus one when an advanced filter rule is attached (it is a single rule
        object carrying its own keyword/media/length/regex conditions, and the
        screen names it). Both render sites read this, and the count is derived
        from the same flags the engine reads, so the badge cannot disagree with
        either the screen or the pipeline.
        """
        count = sum(1 for flag in self.FILTER_OPTION_FLAGS if getattr(self, flag, False))
        return count + (1 if self.filter_rule_id else 0)

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
            if not (self.clean_custom_words or self.remove_links or self.clean_usernames
                    or self.clean_hashtags or self.remove_emojis or self.clean_phones
                    or self.clean_buttons or self.clean_empty_lines):
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
        remove_phones = bool(cfg.get("remove_phone_numbers", self.clean_phones))
        remove_empty_lines = bool(cfg.get("remove_empty_lines", self.clean_empty_lines))
        strip_buttons = bool(cfg.get("strip_buttons", self.clean_buttons))

        clean_pats = [str(p) for p in patterns if isinstance(p, str) and p.strip()]
        if not clean_pats and not (
            remove_urls or remove_mentions or remove_hashtags or remove_emojis
            or remove_phones or remove_empty_lines or strip_buttons
        ):
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
                remove_phone_numbers=remove_phones,
                remove_empty_lines=remove_empty_lines,
                strip_buttons=strip_buttons,
                apply_to_caption=bool(cfg.get("apply_to_caption", True)),
                case_sensitive=bool(cfg.get("case_sensitive", False)),
                is_active=True,
                owner_user_id=self.owner_user_id,
            )
        except Exception:
            return None

    # --- Per-task content rules (blacklist / whitelist-only / replace) ------
    # Carried as metadata like the cleaner, because they are per-task
    # configuration rather than a shared resource, and because adding table
    # columns would need a migration for what is really a settings blob.
    #
    # Storage shape (``metadata["content_policy"]``)::
    #
    #     {
    #       "enabled": true,
    #       "blacklist_words": [...],        # never forward if matched
    #       "whitelist_words": [...],        # forward only if matched
    #       "whitelist_mode": "any|all|phrase",
    #       "remove_links": true, "block_links": false,
    #       "strip_words": [...], "replacements": {"old": "new"},
    #       "regex_block": [...], "regex_require": [...],
    #       "min_length": 0, "max_length": 0,
    #       "source_policies": {"-100a": {...}},   # per-source overrides
    #       "target_policies": {"-100x": {...}},   # per-target overrides
    #     }
    #
    # Overrides are additive constraints: the chain is (task, source|target) and
    # every policy in it must allow the message. Nothing in a chain can relax
    # the task's own rules.
    @property
    def content_policy_config(self) -> Dict[str, Any]:
        """Raw stored policy dict (never None, never shared with the caller)."""
        if not isinstance(self.metadata, dict):
            return {}
        cfg = self.metadata.get("content_policy")
        return dict(cfg) if isinstance(cfg, dict) else {}

    @content_policy_config.setter
    def content_policy_config(self, val: Dict[str, Any]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["content_policy"] = dict(val) if isinstance(val, dict) else {}

    def _policy_key(self, key: str, default: Any) -> Any:
        cfg = self.content_policy_config
        val = cfg.get(key, default)
        return val

    def _set_policy_key(self, key: str, value: Any) -> None:
        cfg = self.content_policy_config
        cfg[key] = value
        self.content_policy_config = cfg

    @property
    def content_policy_enabled(self) -> bool:
        """Per-task on/off switch for the content rules."""
        cfg = self.content_policy_config
        if "enabled" in cfg:
            return bool(cfg.get("enabled"))
        return self.content_policy.has_rules

    @content_policy_enabled.setter
    def content_policy_enabled(self, val: bool) -> None:
        self._set_policy_key("enabled", bool(val))

    @property
    def blacklist_words(self) -> List[str]:
        return [str(x) for x in (self._policy_key("blacklist_words", []) or [])]

    @blacklist_words.setter
    def blacklist_words(self, val: Any) -> None:
        self._set_policy_key("blacklist_words", list(val or []))

    @property
    def whitelist_words(self) -> List[str]:
        return [str(x) for x in (self._policy_key("whitelist_words", []) or [])]

    @whitelist_words.setter
    def whitelist_words(self, val: Any) -> None:
        self._set_policy_key("whitelist_words", list(val or []))

    @property
    def whitelist_mode(self) -> str:
        mode = str(self._policy_key("whitelist_mode", "any") or "any").lower()
        return mode if mode in ("any", "all", "phrase") else "any"

    @whitelist_mode.setter
    def whitelist_mode(self, val: str) -> None:
        self._set_policy_key("whitelist_mode", str(val or "any").lower())

    @property
    def strip_words(self) -> List[str]:
        return [str(x) for x in (self._policy_key("strip_words", []) or [])]

    @strip_words.setter
    def strip_words(self, val: Any) -> None:
        self._set_policy_key("strip_words", list(val or []))

    @property
    def source_policies(self) -> Dict[str, Any]:
        raw = self._policy_key("source_policies", {})
        return dict(raw) if isinstance(raw, dict) else {}

    @source_policies.setter
    def source_policies(self, val: Any) -> None:
        self._set_policy_key("source_policies", dict(val) if isinstance(val, dict) else {})

    @property
    def target_policies(self) -> Dict[str, Any]:
        raw = self._policy_key("target_policies", {})
        return dict(raw) if isinstance(raw, dict) else {}

    @target_policies.setter
    def target_policies(self, val: Any) -> None:
        self._set_policy_key("target_policies", dict(val) if isinstance(val, dict) else {})

    # ------------------------------------------------------------------ #
    # Branding hop (A -> C -> B)
    #
    # Our own channel C is posted first and the final target B receives a
    # *forward of C*, so B's header names C instead of the source channel.
    # The storage shape and the trigger semantics live in core/domain/branding.py
    # (``metadata["branding_hop"]``, a JSON blob — no migration needed); these
    # properties are the single write path every client uses, which is what keeps
    # the bot, the dashboard and the mobile app from disagreeing.
    # ------------------------------------------------------------------ #
    @property
    def branding_config(self) -> Dict[str, Any]:
        """Raw stored branding config (never None, never shared with the caller)."""
        return branding_policy.read_config(self)

    @branding_config.setter
    def branding_config(self, val: Any) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        if not isinstance(val, dict):
            val = {}
        merged = branding_policy.read_config(self)
        merged.update(val)
        self.metadata[branding_policy.BRANDING_META_KEY] = merged

    def _branding_key(self, key: str) -> Any:
        return branding_policy.read_config(self)[key]

    def _set_branding_key(self, key: str, value: Any) -> None:
        branding_policy.write_config(self, {key: value})

    @property
    def branding_triggers_enabled(self) -> bool:
        """When False the hop is unconditional; when True the triggers below gate it."""
        return bool(self._branding_key("triggers_enabled"))

    @branding_triggers_enabled.setter
    def branding_triggers_enabled(self, val: Any) -> None:
        self._set_branding_key("triggers_enabled", bool(val))

    @property
    def branding_origin_ids(self) -> List[str]:
        """Forward-origin chat ids that arm the hop."""
        return list(self._branding_key("origin_ids"))

    @branding_origin_ids.setter
    def branding_origin_ids(self, val: Any) -> None:
        self._set_branding_key("origin_ids", val)

    @property
    def branding_origin_titles(self) -> List[str]:
        """Forward-origin channel *names* ("Forwarded from ...") that arm the hop."""
        return list(self._branding_key("origin_titles"))

    @branding_origin_titles.setter
    def branding_origin_titles(self, val: Any) -> None:
        self._set_branding_key("origin_titles", val)

    @property
    def branding_keywords(self) -> List[str]:
        """Message-text words that arm the hop."""
        return list(self._branding_key("keywords"))

    @branding_keywords.setter
    def branding_keywords(self, val: Any) -> None:
        self._set_branding_key("keywords", val)

    @property
    def branding_match_mode(self) -> str:
        """``any`` (at least one trigger matches) or ``all`` (every one must)."""
        return str(self._branding_key("match_mode"))

    @branding_match_mode.setter
    def branding_match_mode(self, val: Any) -> None:
        self._set_branding_key("match_mode", val)

    @property
    def branding_require_forward(self) -> bool:
        """Gate: refuse the hop unless the message is itself a forward."""
        return bool(self._branding_key("require_forward"))

    @branding_require_forward.setter
    def branding_require_forward(self, val: Any) -> None:
        self._set_branding_key("require_forward", bool(val))

    @property
    def branding_force_forward(self) -> bool:
        """Stage 2 sends a native forward of C — the only way B can show C."""
        return bool(self._branding_key("force_forward"))

    @branding_force_forward.setter
    def branding_force_forward(self, val: Any) -> None:
        self._set_branding_key("force_forward", bool(val))

    @property
    def content_policy(self) -> "ContentPolicy":
        """The task-level policy, including the legacy ``block_links`` flag."""
        from .content_policy import ContentPolicy

        cfg = self.content_policy_config
        if not cfg.get("block_links") and getattr(self, "block_links", False):
            cfg = {**cfg, "block_links": True}
        if not cfg.get("remove_links") and getattr(self, "remove_links", False):
            cfg = {**cfg, "remove_links": True}
        # Pre-policy rows kept the word lists in their own metadata keys. Fold
        # them in (only when the policy has none) so a task configured before
        # the content policy existed is still filtered instead of silently
        # forwarding everything it used to block.
        legacy_bl = self.metadata.get("blacklist") if isinstance(self.metadata, dict) else None
        if not cfg.get("blacklist_words") and isinstance(legacy_bl, list) and legacy_bl:
            cfg = {**cfg, "blacklist_words": [str(w) for w in legacy_bl]}
        legacy_wl = self.metadata.get("whitelist") if isinstance(self.metadata, dict) else None
        if not cfg.get("whitelist_words") and isinstance(legacy_wl, list) and legacy_wl:
            cfg = {**cfg, "whitelist_words": [str(w) for w in legacy_wl]}
        return ContentPolicy.from_dict(cfg, source="task")

    def policy_chain(
        self,
        *,
        source_chat_id: Optional[str] = None,
        target_chat_id: Optional[str] = None,
    ) -> List["ContentPolicy"]:
        """Policies that apply to one (source, target) pair, task first.

        Keys are matched on the normalised chat id (int/str forms of the same id
        are the same chat) so a UI that sends ``-100123`` and a client that
        sends ``-100123`` as a number do not create two different overrides.
        """
        from .content_policy import ContentPolicy

        chain = [self.content_policy]
        src_key = _chat_key(source_chat_id)
        tgt_key = _chat_key(target_chat_id)
        if src_key:
            for key, raw in self.source_policies.items():
                if _chat_key(key) == src_key:
                    chain.append(ContentPolicy.from_dict(raw, source=f"source:{src_key}"))
                    break
        if tgt_key:
            for key, raw in self.target_policies.items():
                if _chat_key(key) == tgt_key:
                    chain.append(ContentPolicy.from_dict(raw, source=f"target:{tgt_key}"))
                    break
        return chain

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

    # --- blocked / required words -----------------------------------------
    # One authoritative home: the content policy
    # (`metadata["content_policy"]["blacklist_words"] / ["whitelist_words"]`).
    # That is what `policy_chain()` hands the forwarding engine, what
    # `rule_settings.py` exposes to the settings surface, and what backup
    # carries. These two pairs are the compatibility view the Telegram menus
    # have always written through: a write lands in the policy, a read falls
    # back to the pre-policy keys so a task configured before the policy
    # existed keeps its line instead of silently forwarding everything.

    @property
    def blacklist(self) -> List[str]:
        words = list(self.blacklist_words)
        if words:
            return words
        if not isinstance(self.metadata, dict):
            return []
        legacy = self.metadata.get("blacklist", [])
        return [str(w) for w in legacy] if isinstance(legacy, list) else []

    @blacklist.setter
    def blacklist(self, val: List[str]) -> None:
        self.blacklist_words = list(val or [])
        if isinstance(self.metadata, dict):
            self.metadata.pop("blacklist", None)

    @property
    def whitelist(self) -> List[str]:
        words = list(self.whitelist_words)
        if words:
            return words
        if not isinstance(self.metadata, dict):
            return []
        legacy = self.metadata.get("whitelist", [])
        return [str(w) for w in legacy] if isinstance(legacy, list) else []

    @whitelist.setter
    def whitelist(self, val: List[str]) -> None:
        self.whitelist_words = list(val or [])
        if isinstance(self.metadata, dict):
            self.metadata.pop("whitelist", None)

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
    def ai_rewrite_active(self) -> bool:
        """Single authority for "does this task rewrite text with an LLM?".

        Three surfaces write AI state: the AI menu (``ai_config_id`` +
        ``smart_ai_rewrite``) and the task panel's paid-addon switch
        (``paid_addons["ai_rewrite"]``). The pipeline used to gate on
        ``ai_config_id`` alone, so a task whose panel read "AI Rewrite: Off"
        still had every message rewritten. Every reader — pipeline and UI —
        now asks this property instead.
        """
        if not self.ai_config_id:
            return False
        # `ai_config_id` alone keeps meaning "rewrite with this config" —
        # API clients, backups and the AI menu have always relied on that.
        # The task panel's paid-addon switch is the explicit veto on top.
        if isinstance(self.metadata, dict):
            addons = self.metadata.get("paid_addons")
            if isinstance(addons, dict) and "ai_rewrite" in addons:
                return bool(addons["ai_rewrite"])
        return True

    @property
    def news_digest(self) -> bool:
        return bool(self.metadata.get("news_digest", False)) if isinstance(self.metadata, dict) else False

    @news_digest.setter
    def news_digest(self, val: bool) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["news_digest"] = bool(val)

    @property
    def prompt_id(self) -> Optional[str]:
        return str(self.metadata["prompt_id"]) if (isinstance(self.metadata, dict) and self.metadata.get("prompt_id")) else None

    @prompt_id.setter
    def prompt_id(self, val: Optional[str]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        if val:
            self.metadata["prompt_id"] = str(val)
        else:
            self.metadata.pop("prompt_id", None)

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
            return clamp_delay_seconds(self.delay_seconds)
        raw = self.metadata.get("forward_delay_seconds", self.delay_seconds)
        return clamp_delay_seconds(raw if raw not in (None, "") else self.delay_seconds)

    @forward_delay_seconds.setter
    def forward_delay_seconds(self, val: Optional[float]) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["forward_delay_seconds"] = clamp_delay_seconds(val)

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

    @link_replacement.setter
    def link_replacement(self, val: str) -> None:
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        self.metadata["link_replacement"] = str(val or "")

    @property
    def enabled(self) -> bool:
        return bool(self.is_active)

    @property
    def source_chat_ids(self) -> List[str]:
        s_ids = self.detection_criteria.get("source_chat_ids", []) if isinstance(self.detection_criteria, dict) else []
        if not s_ids and self.source_chat_id:
            return [str(self.source_chat_id)]
        return [str(s) for s in s_ids]

    @source_chat_ids.setter
    def source_chat_ids(self, val) -> None:
        """Set the full source list for this task.

        Multi-source is stored in detection_criteria (an existing JSON column)
        so no schema migration is needed. The first entry is mirrored into
        `source_chat_id` because the dispatcher, the duplicate guard and older
        API clients still read the primary source.
        """
        if not isinstance(self.detection_criteria, dict):
            self.detection_criteria = {}
        seen: set = set()
        uniq: List[str] = []
        for raw in val or []:
            s = str(raw).strip()
            if s and s not in seen:
                seen.add(s)
                uniq.append(s)
        self.detection_criteria["source_chat_ids"] = uniq
        if uniq:
            self.source_chat_id = uniq[0]

    @property
    def source_chat_names(self) -> List[str]:
        """Display names aligned with `source_chat_ids` (may be shorter)."""
        if not isinstance(self.detection_criteria, dict):
            return []
        return [str(x) for x in (self.detection_criteria.get("source_chat_names") or [])]

    @source_chat_names.setter
    def source_chat_names(self, val) -> None:
        if not isinstance(self.detection_criteria, dict):
            self.detection_criteria = {}
        self.detection_criteria["source_chat_names"] = [
            str(x) for x in (val or []) if str(x).strip()
        ]

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
        """The effective destination: the primary, or the list's first entry.

        The list and its mirror are two spellings of one fact, and every write
        path here keeps them in step (set_targets, apply_draft, __post_init__).
        This still falls back to the list, because a caller that assigns
        `target_chat_ids` directly — a bare Python assignment cannot be
        intercepted on a plain dataclass field — must not end up with a
        destination-less task that the dispatcher happily reads the list for.
        """
        return self.target_chat_id or (self.target_chat_ids[0] if self.target_chat_ids else "")

    @property
    def intermediate_chat_id(self) -> str:
        return self.intermediate_channel_id

    @property
    def delivery_mode(self) -> str:
        return getattr(self.forward_mode, "value", str(self.forward_mode))

    def matches_source(self, chat_id: str) -> bool:
        """True when ``chat_id`` is one of this task's sources.

        A task may route several sources to several targets (x,y,z -> a,b), and
        the extra sources live in ``detection_criteria["source_chat_ids"]``
        while ``source_chat_id`` mirrors the first one for legacy clients. Both
        are checked here: matching only the mirrored field silently starved
        every source but the first.
        """
        if not self.is_active:
            return False
        cid = str(chat_id).strip()
        if not cid:
            return False

        def _same(a: Any, b: str) -> bool:
            key_a, key_b = _chat_key(a), _chat_key(b)
            if key_a and key_a == key_b:
                return True
            # Usernames arrive with and without the @, and case never matters.
            return str(a).strip().lstrip("@").lower() == b.lstrip("@").lower()

        if self.source_chat_id and _same(self.source_chat_id, cid):
            return True
        for candidate in (self.source_chat_ids or []):
            if _same(candidate, cid):
                return True
        if self.source_chat_name and _same(self.source_chat_name, cid):
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
            # Delivery route travels with the draft too: reopening the editor
            # must not quietly drop the attached bot or its buttons.
            "delivery_transport": str(self.delivery_transport),
            "bot_token_id": str(self.bot_token_id),
            "inline_buttons": self.inline_buttons_raw,
            "header": str(self.header),
            "footer": str(self.footer),
            "custom_header": str(self.custom_header),
            "custom_footer": str(self.custom_footer),
            "replacements": dict(self.replacements),
            # Per-task content rules travel with the draft: they are part of the
            # task, and every surface (bot, web, app, CLI) edits through it.
            "content_policy": self.content_policy_config,
            "content_policy_enabled": self.content_policy_enabled,
            "blacklist_words": list(self.blacklist_words),
            "whitelist_words": list(self.whitelist_words),
            "whitelist_mode": self.whitelist_mode,
            "strip_words": list(self.strip_words),
            "regex_block": list(self._policy_key("regex_block", []) or []),
            "regex_require": list(self._policy_key("regex_require", []) or []),
            "source_policies": dict(self.source_policies),
            "target_policies": dict(self.target_policies),
            "version": int(getattr(self, "version", 1)),
            # AI Rewrite settings (pipeline stage, off by default)
            "ai_config_id": str(self.ai_config_id or ""),
            "ai_fallback_policy": getattr(getattr(self, "ai_fallback_policy", None), "value", str(getattr(self, "ai_fallback_policy", "DROP"))),
            "ai_prompt_version": int(getattr(self, "ai_prompt_version", 0) or 0),
            "ai_timeout_seconds": float(getattr(self, "ai_timeout_seconds", 15.0) or 15.0),
            "prompt_id": str((self.metadata or {}).get("prompt_id", "") or ""),
            # The two switches ai_rewrite_active also consults: without them a
            # draft round-trip would show and save an AI state that differs
            # from what the pipeline sees.
            "smart_ai_rewrite": bool(self.smart_ai_rewrite),
            "paid_addons": dict(self.paid_addons or {}),
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

        # Multi-target list (canonical fan-out). The legacy `target_chat_id`
        # above stays the mirror of the first entry, so both a 1-element list
        # and an N-element list round-trip through the same draft.
        if "target_chat_ids" in draft and draft["target_chat_ids"] is not None:
            seen_t: set = set()
            uniq_t: List[str] = []
            for raw_t in draft["target_chat_ids"] or []:
                tid = str(raw_t).strip()
                if tid and tid not in seen_t:
                    seen_t.add(tid)
                    uniq_t.append(tid)
            self.target_chat_ids = uniq_t
            if uniq_t and not draft.get("target_chat_id"):
                self.target_chat_id = uniq_t[0]
                if not self.target_chat_name:
                    self.target_chat_name = uniq_t[0]

        # Multi-source list: stored in detection_criteria (see the setter), the
        # primary `source_chat_id` mirrors the first entry.
        if isinstance(self.detection_criteria, dict) and self.detection_criteria.get("source_chat_ids"):
            first_src = str(self.detection_criteria["source_chat_ids"][0]).strip()
            if first_src and not draft.get("source_chat_id"):
                self.source_chat_id = first_src

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

        if "delivery_transport" in draft and draft["delivery_transport"] is not None:
            self.delivery_transport = str(draft["delivery_transport"])
        if "bot_token_id" in draft and draft["bot_token_id"] is not None:
            self.bot_token_id = str(draft["bot_token_id"])
        if "inline_buttons" in draft:
            self.inline_buttons_raw = draft["inline_buttons"]

        if "smart_ai_rewrite" in draft:
            self.smart_ai_rewrite = bool(draft["smart_ai_rewrite"])

        if "paid_addons" in draft and isinstance(draft["paid_addons"], dict):
            self.paid_addons = dict(draft["paid_addons"])

        if "album_mode" in draft:
            self.metadata["album_mode"] = str(draft["album_mode"])

        if "header" in draft:
            self.metadata["header"] = str(draft["header"] or "")

        if "footer" in draft:
            self.metadata["footer"] = str(draft["footer"] or "")

        if "replacements" in draft and isinstance(draft["replacements"], dict):
            self.metadata["replacements"] = dict(draft["replacements"])

        # Per-task content rules. Persisted as one blob so a task carries its
        # own rules through the API, the bot, the app and backup/restore; the
        # setters are the same ones the settings registry writes through.
        if "content_policy" in draft and isinstance(draft["content_policy"], dict):
            cfg = dict(draft["content_policy"])
            self.content_policy_config = cfg
        for _key in ("content_policy_enabled", "blacklist_words", "whitelist_words",
                     "whitelist_mode", "strip_words", "regex_block", "regex_require",
                     "source_policies", "target_policies"):
            if _key in draft and draft[_key] is not None:
                setattr(self, _key, draft[_key])

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
      ``remove_phone_numbers`` strips contact numbers (a leading "+" or a
      separator-bearing number with 10+ digits; a bare id is left alone),
      ``remove_empty_lines`` removes blank lines, and ``strip_buttons`` tells the
      pipeline to drop the message's inline keyboard (the only option that acts on
      the message markup rather than its text).
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
    remove_phone_numbers: bool = False
    remove_empty_lines: bool = False
    strip_buttons: bool = False
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
        if self.remove_phone_numbers:
            out = _strip_phone_numbers(out)
        # Collapse the whitespace holes the removals punched out so the
        # rendered message does not show "word  word" artifacts.
        out = re.sub(r"[ \t]{2,}", " ", out)
        if self.remove_empty_lines:
            out = _CLEANER_BLANK_LINES_RE.sub("\n", out)
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
    # What this job delivers. ``"forward"`` is the primary copy of the source
    # message; anything else (``"keyword_reply"``) is an additional deliverable
    # to the same target for the same event. It is part of the persisted
    # deliverable key, so one source message can carry a forward *and* a reply.
    job_kind: str = "forward"
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)



@dataclass
class UserTask:
    """A durable, tenant-owned unit of work surfaced through the API.

    Task states: PENDING -> RUNNING -> SUCCEEDED | RETRY_WAIT -> PENDING
    (retry) | FAILED | CANCELLED. The state lives in the authoritative
    database next to every other tenant resource; there is no second copy to
    invalidate.
    """

    id: str = field(default_factory=_new_id)
    owner_user_id: int = 0
    kind: str = ""
    status: str = "PENDING"
    idempotency_key: str = ""
    attempt: int = 0
    max_attempts: int = 3
    # Claim machinery for the Core worker pool (bounded leases, like the
    # delivery queue). Not visible through the API.
    lease_token: str = ""
    lease_until: int = 0
    run_at: int = 0
    payload_json: str = "{}"
    result_json: str = ""
    error: str = ""
    error_code: str = ""
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)
    started_at: int = 0
    finished_at: int = 0

    def mark_updated(self) -> None:
        self.updated_at = _now()
