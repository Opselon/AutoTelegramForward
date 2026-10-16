"""gRPC servicers implementing the shared proto contract over the use cases."""

import time

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import grpc

logger = logging.getLogger(__name__)

from ...proto import pb, pb_grpc  # noqa: F401
from ...application.backup_service import (  # noqa: E402
    decrypt_bundle,
    deserialize_bundle,
    encrypt_bundle,
    serialize_bundle,
)
from ...application.log_policy import LogPolicyError  # noqa: E402


def _status(success: bool, message: str = "", error_code: str = "") -> pb.StatusResponse:
    return pb.StatusResponse(success=success, message=message, error_code=error_code)


def _session_info(s) -> pb.SessionInfo:
    return pb.SessionInfo(
        id=s.id, phone_number=s.phone_number, user_id=s.user_id,
        username=s.username, first_name=s.first_name,
        is_active=s.is_active, is_authorized=s.is_authorized,
        created_at=s.created_at, updated_at=s.updated_at,
        owner_user_id=int(getattr(s, "owner_user_id", 0) or 0),
    )


# --------------------------------------------------------------------------- #
# v17: complete per-task control surface.
#
# The tables below are the single definition of which proto fields map to which
# entity attribute, and how each one is converted. `_rule_pb` (read) and
# `_apply_masked_update` (write) both walk them, so a field can never be
# readable but silently unwritable, or cast one way on read and another on write.
# --------------------------------------------------------------------------- #
_V17_BOOLS = (
    ("skip_history", "skip_history"),
    ("ignore_edits", "ignore_edits"),
    ("sync_edits", "sync_edits"),
    ("sync_deletes", "sync_deletes"),
    ("split_long_caption", "split_long_caption"),
)
_V17_INTS = (("since_ts", "since_ts"),)
_V17_FLOATS = (
    ("delay_seconds", "delay_seconds"),
    ("album_aggregation_window_seconds", "album_aggregation_window_seconds"),
)
_V17_STRS = (
    ("topic_id", "topic_id"),
    ("ai_secondary_config_id", "ai_secondary_config_id"),
    ("prompt_id", "prompt_id"),
    ("link_policy", "link_policy"),
    ("intermediate_target_chat_id", "intermediate_target_chat_id"),
    ("intermediate_mode", "intermediate_mode"),
)
_V17_LISTS = (
    ("source_chat_ids", "source_chat_ids"),
    ("source_chat_names", "source_chat_names"),
    ("trigger_events", "trigger_events"),
    ("domain_allowlist", "domain_allowlist"),
    ("domain_blocklist", "domain_blocklist"),
    ("allowed_media_types", "allowed_media_types"),
)
# dict attributes carried over the wire as JSON strings
_V17_JSONS = (
    ("retry_policy_json", "retry_policy"),
    ("proxy_json", "proxy"),
)
# Protocol-buffer repeated fields have no presence marker, so a non-empty list is
# what "provided" means on the legacy path. `content_mode` is excluded from
# _V17_STRS above because it needs enum coercion on both sides.
_V17_READ_EXTRA = ("content_mode",)


def _attr(r, name, default=None):
    """getattr that also treats an unset/None attribute as absent."""
    try:
        v = getattr(r, name)
    except Exception:
        return default
    return default if v is None else v


def _enum_or_str(value, enum_cls):
    """Coerce to an enum member when the name is known, else keep the string."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    key = value.strip().upper()
    if not key:
        return ""
    return enum_cls.__members__.get(key, value.strip())


def _v17_read_values(r) -> dict:
    """Current v17 values for ``_rule_pb``."""
    from ..persistence.coercion import enum_str

    out = {}
    for pb_name, attr in _V17_BOOLS:
        out[pb_name] = bool(_attr(r, attr, False))
    for pb_name, attr in _V17_INTS:
        out[pb_name] = int(_attr(r, attr, 0) or 0)
    for pb_name, attr in _V17_FLOATS:
        out[pb_name] = float(_attr(r, attr, 0.0) or 0.0)
    for pb_name, attr in _V17_STRS:
        out[pb_name] = str(_attr(r, attr, "") or "")
    for pb_name, attr in _V17_LISTS:
        try:
            out[pb_name] = [str(x) for x in (_attr(r, attr, []) or [])]
        except Exception:
            out[pb_name] = []
    for pb_name, attr in _V17_JSONS:
        raw = _attr(r, attr, None)
        # An empty object/list reads as "" so "not configured" is one rule on the
        # wire instead of "" here and "{}" there.
        out[pb_name] = _json_dumps(raw) if raw else ""
    out["content_mode"] = enum_str(_attr(r, "content_mode", ""), "")
    return out


def _json_dumps(obj) -> str:
    import json as _j
    try:
        return _j.dumps(obj)
    except Exception:
        return ""


def _json_loads(raw, default=None):
    import json as _j
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw).strip()
    if not text:
        return default
    try:
        return _j.loads(text)
    except Exception:
        return default


# --- write side ------------------------------------------------------------ #
# Every field UpdateRule can apply, keyed by the name a client puts in
# update_mask. Values are (entity attribute, coercion).
def _coerce_bool(r, pb_name):
    return bool(getattr(r, pb_name))


def _coerce_int(r, pb_name):
    return int(getattr(r, pb_name) or 0)


def _coerce_float(r, pb_name):
    return float(getattr(r, pb_name) or 0.0)


def _coerce_str(r, pb_name):
    return str(getattr(r, pb_name) or "")


def _coerce_list(r, pb_name):
    return [str(x) for x in (getattr(r, pb_name) or [])]


def _coerce_json(r, pb_name):
    return _json_loads(getattr(r, pb_name), None)


def _coerce_routing(r, pb_name):
    from ...domain.value_objects import RoutingType as _RT
    return _RT[routing_name(getattr(r, pb_name))]


def _coerce_forward_mode(r, pb_name):
    from ...domain.value_objects import ForwardMode as _FM
    return _FM[mode_name(getattr(r, pb_name))]


def _coerce_content_mode(r, pb_name):
    from ...domain.value_objects import ContentMode as _CM
    return _enum_or_str(getattr(r, pb_name), _CM)


def _coerce_ai_fallback(r, pb_name):
    from ...domain.entities import AIFallbackPolicy as _AFP
    raw = str(getattr(r, pb_name) or "").strip().upper()
    if not raw:
        return None
    return _AFP.__members__.get(raw, raw)


# name -> (entity attribute, coercion). Names are the ForwardRule proto fields,
# which is what a caller writes in update_mask.
_MASKED_FIELDS = {
    # routing / identity
    "source_chat_id": ("source_chat_id", _coerce_str),
    "target_chat_id": ("target_chat_id", _coerce_str),
    "source_chat_name": ("source_chat_name", _coerce_str),
    "target_chat_name": ("target_chat_name", _coerce_str),
    "routing_type": ("routing_type", _coerce_routing),
    "forward_mode": ("forward_mode", _coerce_forward_mode),
    "name": ("name", _coerce_str),
    "description": ("description", _coerce_str),
    "message_category": ("message_category", _coerce_str),
    "target_chat_ids": ("target_chat_ids", _coerce_list),
    "owner_user_id": ("owner_user_id", _coerce_int),
    # activation
    "is_active": ("is_active", _coerce_bool),
    "is_paused": ("is_paused", _coerce_bool),
    "paused_until": ("paused_until", _coerce_int),
    "priority": ("priority", _coerce_int),
    "execution_order": ("execution_order", _coerce_int),
    "multi_route": ("multi_route", _coerce_bool),
    "version": ("version", _coerce_int),
    # retry / rate limits
    "max_retries": ("max_retries", _coerce_int),
    "retry_backoff_base": ("retry_backoff_base", _coerce_float),
    "rate_limit_per_minute": ("rate_limit_per_minute", _coerce_int),
    "rate_limit_burst": ("rate_limit_burst", _coerce_int),
    # media / delivery
    "media_handling": ("media_handling", _coerce_str),
    "dedupe_policy": ("dedupe_policy", _coerce_str),
    "remove_links": ("remove_links", _coerce_bool),
    "preserve_signature": ("preserve_signature", _coerce_bool),
    "caption_max_length": ("caption_max_length", _coerce_int),
    "split_long_caption": ("split_long_caption", _coerce_bool),
    "album_aggregation_window_seconds": ("album_aggregation_window_seconds", _coerce_float),
    "delay_seconds": ("delay_seconds", _coerce_float),
    # templates / header & footer
    "custom_caption_template": ("custom_caption_template", _coerce_str),
    "custom_header": ("custom_header", _coerce_str),
    "custom_footer": ("custom_footer", _coerce_str),
    "header_enabled": ("header_enabled", _coerce_bool),
    "template_text": ("template_text", _coerce_str),
    "template_media": ("template_media", _coerce_str),
    "template_album": ("template_album", _coerce_str),
    # intermediate routing / fallback
    "intermediate_channel_id": ("intermediate_channel_id", _coerce_str),
    "intermediate_channel_name": ("intermediate_channel_name", _coerce_str),
    "intermediate_target_chat_id": ("intermediate_target_chat_id", _coerce_str),
    "intermediate_mode": ("intermediate_mode", _coerce_str),
    "use_intermediate": ("use_intermediate", _coerce_bool),
    "fallback_mode": ("fallback_mode", _coerce_forward_mode),
    "fallback_enabled": ("fallback_enabled", _coerce_bool),
    # detection / triggers
    "detection_criteria_json": ("detection_criteria", _coerce_json),
    "trigger_events": ("trigger_events", _coerce_list),
    "skip_history": ("skip_history", _coerce_bool),
    "since_ts": ("since_ts", _coerce_int),
    "ignore_edits": ("ignore_edits", _coerce_bool),
    "sync_edits": ("sync_edits", _coerce_bool),
    "sync_deletes": ("sync_deletes", _coerce_bool),
    "topic_id": ("topic_id", _coerce_str),
    # content policy
    "content_mode": ("content_mode", _coerce_content_mode),
    "link_policy": ("link_policy", _coerce_str),
    "domain_allowlist": ("domain_allowlist", _coerce_list),
    "domain_blocklist": ("domain_blocklist", _coerce_list),
    "allowed_media_types": ("allowed_media_types", _coerce_list),
    "retry_policy_json": ("retry_policy", _coerce_json),
    "proxy_json": ("proxy", _coerce_json),
    # multi-source
    "source_chat_ids": ("source_chat_ids", _coerce_list),
    "source_chat_names": ("source_chat_names", _coerce_list),
    # AI
    "ai_config_id": ("ai_config_id", _coerce_str),
    "ai_secondary_config_id": ("ai_secondary_config_id", _coerce_str),
    "prompt_id": ("prompt_id", _coerce_str),
    "ai_fallback_policy": ("ai_fallback_policy", _coerce_ai_fallback),
    "ai_prompt_version": ("ai_prompt_version", _coerce_int),
    "ai_timeout_seconds": ("ai_timeout_seconds", _coerce_float),
    # escape hatch
    "custom_metadata_json": ("metadata", _coerce_json),
}


def _apply_masked_update(existing, r, mask) -> list:
    """Apply exactly the fields named in ``mask``.

    This is the path that makes a true partial update possible: a name in the
    mask is applied even when its value is zero/false/empty, which the legacy
    present-only chain cannot express. Returns the field names actually applied
    and raises ValueError for an unknown mask entry so a typo cannot silently
    no-op a request the caller believed had taken effect.
    """
    applied = []
    for name in mask:
        entry = _MASKED_FIELDS.get(name)
        if entry is None:
            raise ValueError(f"unknown update_mask field: {name}")
        attr, coerce = entry
        try:
            value = coerce(r, name)
        except Exception as exc:
            raise ValueError(f"invalid value for {name}: {exc}") from exc
        if attr == "metadata":
            if isinstance(value, dict):
                if not isinstance(existing.metadata, dict):
                    existing.metadata = {}
                existing.metadata.update({str(k): v for k, v in value.items()})
                applied.append(name)
            continue
        if attr == "ai_config_id":
            existing.ai_config_id = value or None
        else:
            setattr(existing, attr, value)
        applied.append(name)
    return applied


# The two spellings of one route: (legacy scalar, authoritative list). The
# scalar is a mirror of the list's *first* entry.
_MIRRORED_ENDPOINT_PAIRS = (
    ("source_chat_id", "source_chat_ids"),
    ("target_chat_id", "target_chat_ids"),
)


def _sync_mirrored_endpoints(existing, applied) -> list[str]:
    """Re-mirror a list whose legacy scalar was updated without its list.

    ``target_chat_id`` is the mirror of ``target_chat_ids[0]`` (and likewise for
    sources). A client that names only the scalar in its update mask — the web
    'Edit Target' quick-prompt, and older bot/mobile builds — would otherwise
    leave the superseded entry sitting first in the list, so ``all_targets``
    fans the message out to it as well while the dispatcher reads a different
    destination. A client that names the list is authoritative and needs no
    repair. Returns the names of the lists that were repaired.
    """
    applied = set(applied or [])
    repaired: list[str] = []
    for scalar_attr, list_attr in _MIRRORED_ENDPOINT_PAIRS:
        if scalar_attr not in applied or list_attr in applied:
            continue
        primary = str(getattr(existing, scalar_attr, "") or "").strip()
        if not primary:
            continue
        current = list(getattr(existing, list_attr, []) or [])
        if not current:
            continue
        if str(current[0]).strip() == primary:
            continue
        # Put the new primary first, keep the rest in order, and drop a
        # duplicate of it (assigning the list back is idempotent: the source
        # setter mirrors the scalar it now starts with).
        rest = [str(x).strip() for x in current[1:]
                if str(x).strip() and str(x).strip() != primary]
        setattr(existing, list_attr, [primary, *rest])
        repaired.append(list_attr)
    return repaired


def _v17_create_apply(entity, r) -> None:
    """Apply v17 fields whose proto value is present (the no-mask path).

    Repeated fields have no presence marker in proto3, so an empty list means
    "not provided" here; a caller that needs to clear a list sends update_mask.
    """
    for pb_name, attr in _V17_BOOLS:
        if getattr(r, pb_name, False):
            setattr(entity, attr, True)
    for pb_name, attr in _V17_INTS:
        if getattr(r, pb_name, 0):
            setattr(entity, attr, int(getattr(r, pb_name)))
    for pb_name, attr in _V17_FLOATS:
        if getattr(r, pb_name, 0.0):
            setattr(entity, attr, float(getattr(r, pb_name)))
    for pb_name, attr in _V17_STRS:
        if getattr(r, pb_name, ""):
            setattr(entity, attr, str(getattr(r, pb_name)))
    for pb_name, attr in _V17_LISTS:
        values = list(getattr(r, pb_name, []) or [])
        if values:
            setattr(entity, attr, [str(v) for v in values])
    for pb_name, attr in _V17_JSONS:
        raw = str(getattr(r, pb_name, "") or "").strip()
        if raw:
            setattr(entity, attr, _json_loads(raw, None))
    if getattr(r, "content_mode", ""):
        try:
            from ...domain.value_objects import ContentMode as _CM
            entity.content_mode = _enum_or_str(r.content_mode, _CM)
        except Exception:
            pass


def _rule_settings():
    """Lazy import of the settings registry (kept out of the module header so
    importing the servicer never drags in domain defaults)."""
    from ...domain import rule_settings as _RS
    return _RS


def _pb_routing(value) -> int:
    """Domain RoutingType -> proto enum value.

    The domain enum names its zero member `UNKNOWN` while the proto calls it
    `ROUTING_UNKNOWN`; without the translation a read of an unset routing type
    raised ValueError out of _rule_pb.
    """
    name = getattr(value, "name", None)
    if not name:
        return pb.RoutingType.CHANNEL_TO_CHANNEL
    if name == "UNKNOWN":
        name = "ROUTING_UNKNOWN"
    try:
        return pb.RoutingType.Value(name)
    except ValueError:
        return pb.RoutingType.CHANNEL_TO_CHANNEL


def _rule_pb(r) -> pb.ForwardRule:
    import json as _json
    from ...domain.value_objects import ForwardMode as _FM

    def _mode_val(v):
        if v is None:
            return pb.ForwardMode.FORWARD_MODE_UNKNOWN
        name = getattr(v, "name", None)
        if name and _FM.__members__.get(name) is not None:
            try:
                return pb.ForwardMode.Value(name)
            except ValueError:
                pass
        raw = str(getattr(v, "value", v))
        mapping = {
            "DIRECT_FORWARD": pb.ForwardMode.DIRECT_FORWARD,
            "COPY_MESSAGE": pb.ForwardMode.COPY_MESSAGE,
            "CUSTOM_HEADER_COPY": pb.ForwardMode.CUSTOM_HEADER_COPY,
        }
        return mapping.get(raw, pb.ForwardMode.COPY_MESSAGE)

    criteria = getattr(r, "detection_criteria", None)
    criteria_json = _json.dumps(criteria) if isinstance(criteria, dict) else "{}"

    try:
        tgt_ids = list(getattr(r, "target_chat_ids", []) or [])
    except Exception:
        tgt_ids = []

    return pb.ForwardRule(
        id=r.id, session_id=r.session_id,
        source_chat_id=r.source_chat_id, source_chat_name=r.source_chat_name,
        target_chat_id=r.target_chat_id, target_chat_name=r.target_chat_name,
        routing_type=_pb_routing(r.routing_type),
        forward_mode=_mode_val(getattr(r, "forward_mode", None)),
        is_active=r.is_active, filter_rule_id=r.filter_rule_id or "",
        ai_config_id=r.ai_config_id or "", remove_links=r.remove_links,
        custom_caption_template=r.custom_caption_template,
        created_at=r.created_at, updated_at=r.updated_at,
        # Smart Forwarding Rules (v8)
        name=getattr(r, "name", "") or "",
        description=getattr(r, "description", "") or "",
        target_chat_ids=tgt_ids,
        message_category=getattr(r, "message_category", "ALL") or "ALL",
        intermediate_channel_id=getattr(r, "intermediate_channel_id", "") or "",
        intermediate_channel_name=getattr(r, "intermediate_channel_name", "") or "",
        use_intermediate=bool(getattr(r, "use_intermediate", False)),
        fallback_mode=_mode_val(getattr(r, "fallback_mode", None)),
        fallback_enabled=bool(getattr(r, "fallback_enabled", True)),
        detection_criteria_json=criteria_json,
        priority=int(getattr(r, "priority", 10) or 10),
        execution_order=int(getattr(r, "execution_order", 0) or 0),
        multi_route=bool(getattr(r, "multi_route", False)),
        media_handling=getattr(r, "media_handling", "AUTO") or "AUTO",
        dedupe_policy=getattr(r, "dedupe_policy", "STRICT") or "STRICT",
        max_retries=int(getattr(r, "max_retries", 3) or 3),
        retry_backoff_base=float(getattr(r, "retry_backoff_base", 2.0) or 2.0),
        rate_limit_per_minute=int(getattr(r, "rate_limit_per_minute", 0) or 0),
        rate_limit_burst=int(getattr(r, "rate_limit_burst", 0) or 0),
        custom_header=getattr(r, "custom_header", "") or "",
        custom_footer=getattr(r, "custom_footer", "") or "",
        header_enabled=bool(getattr(r, "header_enabled", False)),
        template_text=getattr(r, "template_text", "") or "",
        template_media=getattr(r, "template_media", "") or "",
        template_album=getattr(r, "template_album", "") or "",
        caption_max_length=int(getattr(r, "caption_max_length", 0) or 0),
        preserve_signature=bool(getattr(r, "preserve_signature", False)),
        is_paused=bool(getattr(r, "is_paused", False)),
        paused_until=int(getattr(r, "paused_until", 0) or 0),
        version=int(getattr(r, "version", 1) or 1),
        owner_user_id=int(getattr(r, "owner_user_id", 0) or 0),
        custom_metadata_json=_json.dumps(getattr(r, "metadata", {}) or {}),
        ai_fallback_policy=getattr(getattr(r, "ai_fallback_policy", None), "value", str(getattr(r, "ai_fallback_policy", "DROP"))),
        ai_prompt_version=int(getattr(r, "ai_prompt_version", 0) or 0),
        ai_timeout_seconds=float(getattr(r, "ai_timeout_seconds", 15.0) or 15.0),
        # v17: per-task control surface + full settings snapshot
        **_v17_read_values(r),
        settings_json=_json.dumps(_rule_settings().read_settings(r)),
    )


def _filter_pb(f) -> pb.FilterRule:
    return pb.FilterRule(
        id=f.id, name=f.name,
        whitelist_keywords=f.whitelist_keywords,
        blacklist_keywords=f.blacklist_keywords,
        regex_patterns=f.regex_patterns,
        allowed_media_types=f.allowed_media_types,
        blocked_media_types=f.blocked_media_types,
        drop_service_messages=f.drop_service_messages,
        min_message_length=f.min_message_length,
        max_message_length=f.max_message_length,
        owner_user_id=int(getattr(f, "owner_user_id", 0) or 0),
    )


def _ai_pb(c, mask_key: bool = True) -> pb.AIConfig:
    key = ("***" + c.api_key[-4:]) if (mask_key and c.api_key) else c.api_key
    return pb.AIConfig(
        id=c.id, name=c.name, provider=c.provider.value, model=c.model,
        api_key=key, base_url=c.base_url, system_prompt=c.system_prompt,
        user_prompt_template=c.user_prompt_template, temperature=c.temperature,
        is_enabled=c.is_enabled, target_language=c.target_language,
        owner_user_id=int(getattr(c, "owner_user_id", 0) or 0),
    )


# --------------------------------------------------------------------------- #
# Chat listing (ListChats)
#
# The dialog snapshot is bounded on purpose: one request against an account in
# thousands of chats must not allocate an unbounded list or pin a gRPC worker
# for the whole fetch. Telegram returns dialogs newest-first, which is the order
# a picker wants anyway, and `truncated` flags when the cap was hit so the
# client knows there is more.
# --------------------------------------------------------------------------- #
_CHAT_LIST_MAX_DIALOGS = 1000

# Page size used when the caller supplies no limit. A proto3 scalar cannot
# distinguish "unset" from 0, so the core must treat 0 as "unset" and fall back
# to this default — faulting 0 to a single row would silently look like an
# account that owns exactly one chat.
_DEFAULT_CHAT_LIMIT = 100


def _chat_type_of(chat) -> str:
    """Normalise a chat kind to the lowercase wire vocabulary.

    pyrogram reports the kind either as an enum member (``ChatType.SUPERGROUP``)
    or, on lighter previews, a plain string; both collapse to one stable value
    (``supergroup``) so the mobile client can branch on a single set.
    """
    raw = getattr(chat, "type", None)
    if raw is None:
        return type(chat).__name__.lower()
    return str(getattr(raw, "value", raw)).lower()


def _chat_members(chat) -> int:
    """Participant count when the peer exposes it, else 0 (unknown, not an error).

    Telegram omits the count for private chats and small groups on the dialog
    preview, so a missing value must not fail the row — it is advisory only.
    """
    for attr in ("members_count", "participants_count"):
        val = getattr(chat, attr, None)
        if val:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return 0


def _chat_summary_from_dialog(dialog) -> "pb.ChatSummary":
    """Map one dialog to a ChatSummary, raising so the caller can skip the row.

    ``dialog.chat`` is accessed directly rather than with a default: a malformed
    dialog then raises here, where the caller logs the count and continues,
    instead of silently emitting an empty entry the UI cannot act on.
    """
    chat = dialog.chat
    username = getattr(chat, "username", "") or ""
    title = (
        getattr(chat, "title", "")
        or getattr(chat, "first_name", "")
        or username
        or str(getattr(chat, "id", ""))
    )
    return pb.ChatSummary(
        chat_id=str(chat.id),
        title=str(title),
        username=username,
        chat_type=_chat_type_of(chat),
        members=_chat_members(chat),
        is_forum=bool(getattr(chat, "is_forum", False)),
    )


def _chat_matches_query(summary, query: str) -> bool:
    """Case-insensitive substring match on the title or the @username."""
    if not query:
        return True
    needle = query.lower()
    return (
        needle in (summary.title or "").lower()
        or needle in (summary.username or "").lower()
    )


def _clamp_chat_limit(value) -> int:
    """Resolve the requested page size into the documented 1..200 window.

    A positive value is clamped so a direct gRPC caller cannot drain the whole
    snapshot in one reply. A zero/absent value (which proto3 cannot tell apart)
    takes the default page size rather than faulting to one row, so an omitted
    limit behaves the same here as it does at the API boundary.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_CHAT_LIMIT
    if n <= 0:
        return _DEFAULT_CHAT_LIMIT
    return min(200, n)


class SessionControlServicer(pb_grpc.SessionControlServiceServicer):
    def __init__(self, sessions, pool) -> None:
        self._sessions = sessions
        self._pool = pool

    async def ListSessions(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ListSessionsResponse(sessions=[])
        rows = await self._sessions.list_by_owner(owner)
        return pb.ListSessionsResponse(sessions=[_session_info(s) for s in rows])

    async def StartLogin(self, request, context):
        # Interactive login lives in the bot; gRPC returns guidance.
        return pb.StartLoginResponse(
            success=True,
            message="Use the Telegram bot /login command for interactive login.",
        )

    async def SubmitCode(self, request, context):
        return pb.SubmitCodeResponse(success=False, message="Interactive login is handled by the bot.")

    async def SubmitPassword(self, request, context):
        return pb.SubmitPasswordResponse(success=False, message="Interactive login is handled by the bot.")

    async def BackupSession(self, request, context):
        # v12.2: the exported blob is full account-takeover material — only
        # the session's owner may retrieve it. Fail closed on missing/zero identity.
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.BackupSessionResponse(success=False, message="unauthorized")
        s = await self._sessions.get_owned(request.session_id, owner)
        if not s:
            return pb.BackupSessionResponse(success=False, message="session_not_found")
        return pb.BackupSessionResponse(success=True, encrypted_session_data=s.session_string_encrypted)

    async def RestoreSession(self, request, context):
        # v12.2: bind the restored session to the importing account so it
        # cannot be dropped into the shared/legacy scope and later picked up
        # by another tenant's rules.
        try:
            owner = int(request.owner_user_id or 0)
            if not owner:
                return pb.RestoreSessionResponse(success=False, message="unauthorized")
            s = await self._sessions.create(
                phone_number="",
                session_string_encrypted=request.encrypted_session_data,
                owner_user_id=owner,
            )
            s.activate()
            await self._sessions._repo.update(s)
            return pb.RestoreSessionResponse(success=True, session_id=s.id)
        except Exception as exc:
            return pb.RestoreSessionResponse(success=False, message=str(exc))

    async def TerminateSession(self, request, context):
        # v12.2: deleting a session also tears down its live Telegram client —
        # require ownership first. A foreign delete raises PermissionError,
        # which we surface as a denial rather than a not_found so the caller
        # cannot use the response to probe for session existence.
        owner = int(request.owner_user_id or 0)
        if not owner:
            return _status(False, "unauthorized")
        try:
            ok = await self._sessions.delete_owned(request.session_id, owner)
        except PermissionError:
            logger.warning(
                "TerminateSession: owner=%s denied termination of session=%s",
                owner, request.session_id,
            )
            return _status(False, "denied")
        if ok:
            if self._pool is not None:
                await self._pool.stop(request.session_id)
        return _status(ok, "terminated" if ok else "not_found")

    async def ResolveChat(self, request, context):
        """Resolve a human handle (username / t.me link / numeric id) to a
        canonical numeric chat id using a live Telegram session. The dispatcher
        only understands numeric ids, so the API layer calls this before saving
        a rule to guarantee no username ever lands in chat_id columns."""
        import re as _re

        handle = (request.handle or "").strip()
        session_id = (request.session_id or "").strip()
        if not handle:
            return pb.ResolveChatResponse(found=False, error="handle_empty")

        # Normalize the handle: links and @-prefixes down to a bare username/id.
        raw = handle
        raw = _re.sub(r"^https?://t\.me/", "", raw, flags=_re.IGNORECASE)
        raw = _re.sub(r"^t\.me/", "", raw, flags=_re.IGNORECASE)
        raw = raw.lstrip("@").strip()
        if not raw:
            return pb.ResolveChatResponse(found=False, error="handle_empty")

        # Numeric ids (with optional -100 channel prefix) pass through.
        if _re.fullmatch(r"-?\d+", raw):
            return pb.ResolveChatResponse(
                found=True, chat_id=str(int(raw)), title=handle, chat_type="numeric"
            )

        # Pick the requested session, or the first active+authorized one owned
        # by the caller. Fail closed: no arbitrary tenant client driving.
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ResolveChatResponse(found=False, error="unauthorized")
        if session_id:
            session = await self._sessions.get_owned(session_id, owner)
        else:
            sessions = await self._sessions.list_by_owner(owner)
            active = [s for s in sessions if s.is_active and s.is_authorized]
            session = active[0] if active else (sessions[0] if sessions else None)
        if not session:
            return pb.ResolveChatResponse(found=False, error="no_active_session")

        client = self._pool.get(session.id)
        if client is None:
            return pb.ResolveChatResponse(
                found=False, error="session_not_connected"
            )

        try:
            chat = await client.get_chat(raw)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
            return pb.ResolveChatResponse(
                found=False, error=f"{type(exc).__name__}: {exc}"
            )

        chat_type = type(chat).__name__.lower()
        if hasattr(chat, "type") and chat.type is not None:
            chat_type = getattr(chat.type, "value", str(chat.type)).lower()
        username = getattr(chat, "username", "") or ""
        title = getattr(chat, "title", "") or getattr(chat, "first_name", "") or username
        return pb.ResolveChatResponse(
            found=True,
            chat_id=str(chat.id),
            title=str(title),
            username=username,
            chat_type=chat_type,
        )

    async def ListChats(self, request, context):
        """List the live dialogs (channels/groups/chats) of one owned session.

        This is the browse surface behind the mobile source/target pickers, so
        the tenant boundary and the Telegram client are resolved here rather
        than trusted from the caller. Every decision fails closed:
          * no owner  -> the request is unauthenticated (the API always copies
            the owner from the verified JWT, so 0 only reaches the core from a
            direct, unauthenticated gRPC caller);
          * unknown OR foreign session -> an identical NOT_FOUND, so the reply
            cannot be used to probe for another tenant's session ids;
          * no live client -> FAILED_PRECONDITION, not an empty list: an empty
            page would be indistinguishable from "this account really has no
            chats" and would silently break the picker.
        A read path must not spawn a Telegram client as a side effect, so a
        disconnected session is reported, never reconnected here.
        """
        owner = int(request.owner_user_id or 0)
        session_id = (request.session_id or "").strip()
        query = (request.query or "").strip()
        limit = _clamp_chat_limit(request.limit)
        offset = max(int(request.offset or 0), 0)

        if not owner:
            logger.warning("[PIPELINE:CHAT_LIST:ERROR] refused: owner_user_id missing")
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "owner_user_id is required")
            return pb.ListChatsResponse()
        if not session_id:
            logger.warning("[PIPELINE:CHAT_LIST:ERROR] refused: session_id missing owner=%s", owner)
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "session_id is required")
            return pb.ListChatsResponse()

        session = await self._sessions.get_owned(session_id, owner)
        if session is None:
            # Foreign and missing answer identically on purpose (see docstring).
            logger.warning(
                "[PIPELINE:CHAT_LIST:ERROR] session lookup denied owner=%s", owner,
            )
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("session_not_found")
            return pb.ListChatsResponse()

        client = self._pool.get(session.id) if self._pool is not None else None
        if client is None:
            logger.warning(
                "[PIPELINE:CHAT_LIST:ERROR] no live client owner=%s session=%s",
                owner, session.id[:8],
            )
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("session_not_connected")
            return pb.ListChatsResponse()

        logger.info(
            "[PIPELINE:CHAT_LIST:START] owner=%s session=%s query_len=%d limit=%d offset=%d",
            owner, session.id[:8], len(query), limit, offset,
        )

        # Build the whole snapshot first, then filter and page it in memory:
        # offset beyond the end must yield an empty page, which is only possible
        # if the total is known before slicing.
        snapshot: List[Any] = []
        skipped = 0
        try:
            async for dialog in client.get_dialogs(limit=_CHAT_LIST_MAX_DIALOGS):
                try:
                    snapshot.append(_chat_summary_from_dialog(dialog))
                except Exception as exc:  # noqa: BLE001 - one bad row must not kill the page
                    skipped += 1
                    logger.warning(
                        "[PIPELINE:CHAT_LIST:DIALOG_SKIPPED] reason=%s skipped=%d",
                        type(exc).__name__, skipped,
                    )
        except Exception as exc:  # noqa: BLE001 - surface a soft failure to the UI
            logger.error(
                "[PIPELINE:CHAT_LIST:ERROR] dialog fetch failed owner=%s reason=%s",
                owner, type(exc).__name__,
            )
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details(f"dialog_fetch_failed: {type(exc).__name__}")
            return pb.ListChatsResponse()

        matched = [c for c in snapshot if _chat_matches_query(c, query)]
        total = len(matched)
        page = matched[offset:offset + limit]
        # The snapshot hit the fetch cap, so there may be dialogs we never saw;
        # Telegram returns dialogs newest-first, which is the order a picker wants.
        truncated = len(snapshot) >= _CHAT_LIST_MAX_DIALOGS

        logger.info(
            "[PIPELINE:CHAT_LIST:COMPLETE] owner=%s session=%s fetched=%d matched=%d "
            "page=%d skipped=%d truncated=%s",
            owner, session.id[:8], len(snapshot), total, len(page), skipped, truncated,
        )
        return pb.ListChatsResponse(
            session_id=session.id,
            chats=page,
            count=len(page),
            total=total,
            truncated=truncated,
        )


class ForwardRuleControlServicer(pb_grpc.ForwardRuleControlServiceServicer):
    def __init__(self, rules, queue=None) -> None:
        self._rules = rules
        self._queue = queue

    async def ListRules(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ListRulesResponse(rules=[])
        if request.session_id:
            rows = await self._rules.list_by_session(request.session_id, owner)
        else:
            rows = await self._rules.list_by_owner(owner)
        return pb.ListRulesResponse(rules=[_rule_pb(r) for r in rows])

    async def GetRule(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            # Fail-closed: an unscoped single-row read must never return a row.
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("owner_user_id is required")
            return pb.ForwardRule()
        get_owned = getattr(self._rules, "get_owned", None)
        if get_owned is None:
            entity = await self._rules.get_by_id(request.rule_id)
            if entity is None or int(getattr(entity, "owner_user_id", 0) or 0) != owner:
                entity = None
        else:
            entity = await get_owned(request.rule_id, owner)
        if entity is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("rule not found")
            return pb.ForwardRule()
        return _rule_pb(entity)

    async def CreateRule(self, request, context):
        from ...domain.entities import ForwardRule as Entity
        from ...domain.value_objects import ForwardMode as FM, RoutingType as RT
        import json as _json
        r = request.rule
        entity = Entity(
            session_id=r.session_id, source_chat_id=r.source_chat_id,
            source_chat_name=r.source_chat_name, target_chat_id=r.target_chat_id,
            target_chat_name=r.target_chat_name,
            routing_type=RT[routing_name(r.routing_type)],
            forward_mode=FM[mode_name(r.forward_mode)],
            is_active=r.is_active or True,
            remove_links=r.remove_links,
            custom_caption_template=r.custom_caption_template,
            ai_config_id=r.ai_config_id or None,
            ai_fallback_policy=r.ai_fallback_policy or "DROP",
            ai_prompt_version=int(r.ai_prompt_version or 0),
            ai_timeout_seconds=float(r.ai_timeout_seconds or 15.0),
            # Smart fields
            name=r.name or "",
            description=r.description or "",
            target_chat_ids=list(r.target_chat_ids or []),
            message_category=r.message_category or "ALL",
            intermediate_channel_id=r.intermediate_channel_id or "",
            intermediate_channel_name=r.intermediate_channel_name or "",
            use_intermediate=bool(r.use_intermediate),
            fallback_mode=FM[mode_name(r.fallback_mode)] if r.fallback_mode else FM.COPY_MESSAGE,
            fallback_enabled=bool(r.fallback_enabled) if r.fallback_enabled else True,
            detection_criteria=_json.loads(r.detection_criteria_json or "{}") or {},
            priority=int(r.priority or 10),
            execution_order=int(r.execution_order or 0),
            multi_route=bool(r.multi_route),
            media_handling=r.media_handling or "AUTO",
            dedupe_policy=r.dedupe_policy or "STRICT",
            max_retries=int(r.max_retries or 3),
            retry_backoff_base=float(r.retry_backoff_base or 2.0),
            rate_limit_per_minute=int(r.rate_limit_per_minute or 0),
            rate_limit_burst=int(r.rate_limit_burst or 0),
            custom_header=r.custom_header or "",
            custom_footer=r.custom_footer or "",
            header_enabled=bool(r.header_enabled),
            template_text=r.template_text or "",
            template_media=r.template_media or "",
            template_album=r.template_album or "",
            caption_max_length=int(r.caption_max_length or 0),
            preserve_signature=bool(r.preserve_signature),
            is_paused=bool(r.is_paused),
            paused_until=int(r.paused_until or 0),
            owner_user_id=int(r.owner_user_id or 0),
        )
        # v17: apply the new per-task fields (multi-source, trigger events,
        # content policy, per-task delay, ...). Setting them through the public
        # attributes keeps defaults and normalisation in the entity.
        _v17_create_apply(entity, r)
        if r.custom_metadata_json:
            try:
                merged = _json.loads(r.custom_metadata_json)
                if isinstance(merged, dict):
                    if not isinstance(entity.metadata, dict):
                        entity.metadata = {}
                    entity.metadata.update(merged)
                    if not entity.ai_config_id and merged.get("ai_config_id"):
                        entity.ai_config_id = str(merged["ai_config_id"])
                    if merged.get("prompt_id"):
                        entity.prompt_id = str(merged["prompt_id"])
            except Exception:
                pass
        try:
            created = await self._rules.create(entity)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return pb.ForwardRule()
        return _rule_pb(created)

    async def UpdateRule(self, request, context):
        import json as _json
        from ...domain.value_objects import ForwardMode as FM
        # v12.2: verify the caller owns the rule before mutating it.
        caller = int(request.rule.owner_user_id or 0)
        if not caller:
            return pb.ForwardRule()
        existing = await self._rules.get_owned(request.rule.id, caller)
        if not existing:
            return pb.ForwardRule()
        r = request.rule
        mask = [m.strip() for m in (request.update_mask or []) if m.strip()]
        if mask:
            # Exact partial update: only the named fields are touched, and a
            # named field is applied even when its value is zero/false/empty.
            try:
                applied = _apply_masked_update(existing, r, mask)
            except ValueError as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                return pb.ForwardRule()
            # A scalar named alone leaves the authoritative list pointing at a
            # superseded entry; re-mirror it here so the two spellings cannot
            # disagree about where the message goes.
            _sync_mirrored_endpoints(existing, applied)
            existing.mark_updated()
            try:
                updated = await self._rules.update(existing)
            except ValueError as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                return pb.ForwardRule()
            return _rule_pb(updated)
        # ---- legacy present-only path (no mask): unchanged behaviour ----
        _v17_create_apply(existing, r)
        if r.source_chat_id:
            existing.source_chat_id = r.source_chat_id
        if r.target_chat_id:
            existing.target_chat_id = r.target_chat_id
        if r.source_chat_name:
            existing.source_chat_name = r.source_chat_name
        if r.target_chat_name:
            existing.target_chat_name = r.target_chat_name
        existing.is_active = r.is_active
        existing.remove_links = r.remove_links
        if r.custom_caption_template:
            existing.custom_caption_template = r.custom_caption_template

        # Smart fields (only applied when present in the request)
        if r.name:
            existing.name = r.name
        if r.description:
            existing.description = r.description
        if r.target_chat_ids:
            existing.target_chat_ids = list(r.target_chat_ids)
        if r.message_category:
            existing.message_category = r.message_category
        if r.intermediate_channel_id:
            existing.intermediate_channel_id = r.intermediate_channel_id
        if r.intermediate_channel_name:
            existing.intermediate_channel_name = r.intermediate_channel_name
        if r.use_intermediate:
            existing.use_intermediate = bool(r.use_intermediate)
        if r.fallback_mode:
            try:
                existing.fallback_mode = FM[mode_name(r.fallback_mode)]
            except KeyError:
                existing.fallback_mode = FM.COPY_MESSAGE
        existing.fallback_enabled = bool(r.fallback_enabled) if r.fallback_enabled else existing.fallback_enabled
        if r.detection_criteria_json:
            try:
                existing.detection_criteria = _json.loads(r.detection_criteria_json) or {}
            except Exception:
                pass
        if r.priority:
            existing.priority = int(r.priority)
        if r.execution_order:
            existing.execution_order = int(r.execution_order)
        if r.multi_route:
            existing.multi_route = bool(r.multi_route)
        if r.media_handling:
            existing.media_handling = r.media_handling
        if r.dedupe_policy:
            existing.dedupe_policy = r.dedupe_policy
        if r.max_retries:
            existing.max_retries = int(r.max_retries)
        if r.retry_backoff_base:
            existing.retry_backoff_base = float(r.retry_backoff_base)
        if r.rate_limit_per_minute:
            existing.rate_limit_per_minute = int(r.rate_limit_per_minute)
        if r.rate_limit_burst:
            existing.rate_limit_burst = int(r.rate_limit_burst)
        if r.custom_header:
            existing.custom_header = r.custom_header
        if r.custom_footer:
            existing.custom_footer = r.custom_footer
        if r.header_enabled:
            existing.header_enabled = bool(r.header_enabled)
        if r.template_text:
            existing.template_text = r.template_text
        if r.template_media:
            existing.template_media = r.template_media
        if r.template_album:
            existing.template_album = r.template_album
        if r.caption_max_length:
            existing.caption_max_length = int(r.caption_max_length)
        if r.preserve_signature:
            existing.preserve_signature = bool(r.preserve_signature)
        if r.is_paused:
            existing.is_paused = bool(r.is_paused)
        if r.paused_until:
            existing.paused_until = int(r.paused_until)
        merged = {}
        if r.custom_metadata_json:
            try:
                merged = _json.loads(r.custom_metadata_json)
                if isinstance(merged, dict):
                    if not isinstance(existing.metadata, dict):
                        existing.metadata = {}
                    existing.metadata.update(merged)
                    if "ai_config_id" in merged:
                        existing.ai_config_id = str(merged["ai_config_id"] or "") or None
                    if "prompt_id" in merged:
                        if merged["prompt_id"]:
                            existing.prompt_id = str(merged["prompt_id"])
                        else:
                            existing.metadata.pop("prompt_id", None)
            except Exception:
                pass

        # AI Rewrite configuration
        if r.ai_config_id:
            existing.ai_config_id = r.ai_config_id
        elif r.ai_config_id == "" and (r.custom_metadata_json and "ai_config_id" in merged):
            existing.ai_config_id = None
        if r.ai_fallback_policy:
            try:
                from ...domain.entities import AIFallbackPolicy
                existing.ai_fallback_policy = AIFallbackPolicy(str(r.ai_fallback_policy).upper())
            except Exception:
                pass
        if r.ai_prompt_version:
            existing.ai_prompt_version = int(r.ai_prompt_version)
        if r.ai_timeout_seconds and r.ai_timeout_seconds > 0:
            existing.ai_timeout_seconds = float(r.ai_timeout_seconds)

        existing.mark_updated()
        try:
            updated = await self._rules.update(existing)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return pb.ForwardRule()
        return _rule_pb(updated)

    # ------------------------------------------------------------- settings
    # Schema-driven per-task settings. One registry entry makes a setting
    # controllable here, so the API reaches every knob the Telegram bot edits
    # without a new proto field per setting.
    async def GetRuleSettings(self, request, context):
        import json as _json
        from ...domain import rule_settings as RS
        owner = int(request.owner_user_id or 0)
        if not owner:
            # Fail-closed: an unscoped read must never return another tenant's data.
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("owner_user_id is required")
            return pb.RuleSettingsResponse(success=False, error_code="unauthorized")
        entity = await self._rules.get_owned(request.rule_id, owner)
        if entity is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("rule not found")
            return pb.RuleSettingsResponse(success=False, error_code="not_found")
        return pb.RuleSettingsResponse(
            success=True,
            settings_json=_json.dumps(RS.read_settings(entity)),
            schema_json=_json.dumps(RS.schema()),
        )

    async def UpdateRuleSettings(self, request, context):
        import json as _json
        from ...domain import rule_settings as RS
        owner = int(request.owner_user_id or 0)
        if not owner:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("owner_user_id is required")
            return pb.RuleSettingsResponse(success=False, error_code="unauthorized")
        entity = await self._rules.get_owned(request.rule_id, owner)
        if entity is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("rule not found")
            return pb.RuleSettingsResponse(success=False, error_code="not_found")
        try:
            values = _json.loads(request.settings_json or "{}")
        except Exception:
            return pb.RuleSettingsResponse(
                success=False, error_code="invalid_json",
                message="settings_json is not valid JSON",
            )
        if not isinstance(values, dict):
            return pb.RuleSettingsResponse(
                success=False, error_code="invalid_type",
                message="settings_json must be a JSON object",
            )
        try:
            changed = RS.apply_settings(entity, values, replace_all=bool(request.replace_all))
        except RS.SettingError as exc:
            # Nothing was written: apply_settings validates before it mutates.
            return pb.RuleSettingsResponse(
                success=False, error_code=exc.code, message=str(exc),
                settings_json=_json.dumps(RS.read_settings(entity)),
            )
        if changed:
            entity.mark_updated()
            try:
                await self._rules.update(entity)
            except ValueError as exc:
                return pb.RuleSettingsResponse(
                    success=False, error_code="persist_failed", message=str(exc),
                    settings_json=_json.dumps(RS.read_settings(entity)),
                )
        return pb.RuleSettingsResponse(
            success=True,
            message=f"{len(changed)} setting(s) updated",
            settings_json=_json.dumps(RS.read_settings(entity)),
            schema_json=_json.dumps(RS.schema()),
        )

    async def GetRuleSettingsSchema(self, request, context):
        import json as _json
        from ...domain import rule_settings as RS
        return pb.RuleSettingsResponse(
            success=True, schema_json=_json.dumps(RS.schema()),
        )

    async def DeleteRule(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return _status(False, "unauthorized")
        try:
            ok = await self._rules.delete_owned(request.id, owner)
        except PermissionError:
            logger.warning(
                "DeleteRule: owner=%s denied delete of rule=%s",
                owner, request.id,
            )
            return _status(False, "denied")
        return _status(ok, "deleted" if ok else "not_found")

    async def TestRule(self, request, context):
        """Dry-run a rule against a sample message without sending anything."""
        import json as _json
        from ...domain.entities import MessagePayload, MediaType
        from ...domain.services import SmartRoutingEngine, TransformationService

        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.TestRuleResponse(matched=False, match_reason="unauthorized")
        rule = await self._rules.get_owned(request.rule_id, owner)
        if rule is None:
            return pb.TestRuleResponse(matched=False, match_reason="rule_not_found")

        media_raw = (request.sample_media_type or "text").lower()
        try:
            media_type = MediaType(media_raw)
        except ValueError:
            media_type = MediaType.TEXT

        forward_origin = None
        if request.forward_origin_chat_id or request.forward_origin_username or request.forward_origin_title:
            forward_origin = {
                "type": "channel",
                "from_chat_id": request.forward_origin_chat_id or "",
                "from_chat_username": request.forward_origin_username or "",
                "from_chat_title": request.forward_origin_title or "",
                "from_message_id": 0,
                "date": 0,
                "sender_name": request.forward_origin_username or request.forward_origin_title or "",
            }

        payload = MessagePayload(
            message_id=1,
            chat_id=request.sample_chat_id or rule.source_chat_id or "-1000",
            chat_type="channel",
            sender_id=request.sample_sender_id or None,
            text=request.sample_text or "",
            caption=request.sample_text or "",
            media_type=media_type,
            has_media=bool(request.sample_has_media),
            forward_origin=forward_origin,
        )

        engine = SmartRoutingEngine()
        matched, reason = engine.evaluate_rule(payload, rule)
        is_vip, vip_reason = engine.is_vip_origin(payload, rule)
        # Report the same verdict delivery reaches. A task fans out to every
        # target, so a loop only rejects the message when *every* target would
        # loop — one looping target is skipped at fan-out (see
        # SmartRoutingEngine.evaluate_rule). Checking the primary alone made the
        # dry-run contradict the dispatcher on exactly those tasks, telling the
        # operator a message would be dropped that would in fact be delivered.
        _dests = [t for t in (getattr(rule, "all_targets", None) or [rule.target_chat_id])
                  if str(t).strip()]
        _hits = [engine.check_loop(payload, rule, t) for t in _dests]
        if _dests and all(hit[0] for hit in _hits):
            looped, loop_reason = _hits[0]
        else:
            looped, loop_reason = False, ""

        rendered = ""
        header_rendered = ""
        footer_rendered = ""
        if matched:
            svc = TransformationService()
            rendered = svc.transform_text(payload.effective_text, rule, payload)
            header_rendered = svc.apply_header_footer(
                "", header=rule.header, footer="",
            ) if rule.header else ""
            footer_rendered = rule.footer or ""

        return pb.TestRuleResponse(
            matched=matched,
            match_reason=reason,
            rule_name=rule.name or f"Rule-{rule.id[:8]}",
            message_category=rule.message_category or "ALL",
            is_vip=is_vip,
            vip_reason=vip_reason,
            loop_detected=looped,
            loop_reason=loop_reason,
            forward_mode=str(getattr(rule.forward_mode, "value", rule.forward_mode)),
            fallback_mode=str(getattr(rule.fallback_mode, "value", rule.fallback_mode)),
            rendered_text=rendered,
            rendered_header=header_rendered,
            rendered_footer=footer_rendered,
            delivery_targets=list(rule.all_targets),
            intermediate_channel_id=rule.intermediate_channel_id or "",
            use_intermediate=bool(rule.use_intermediate),
        )

    async def PauseRule(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.RuleActionResponse(success=False, message="unauthorized", error_code="unauthorized")
        rule = await self._rules.get_owned(request.rule_id, owner)
        if rule is None:
            return pb.RuleActionResponse(success=False, message="rule_not_found", error_code="not_found")
        rule.is_paused = True
        rule.paused_until = int(request.until or 0)
        rule.mark_updated()
        await self._rules.update(rule)
        return pb.RuleActionResponse(success=True, message="paused")

    async def ResumeRule(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.RuleActionResponse(success=False, message="unauthorized", error_code="unauthorized")
        rule = await self._rules.get_owned(request.rule_id, owner)
        if rule is None:
            return pb.RuleActionResponse(success=False, message="rule_not_found", error_code="not_found")
        rule.is_paused = False
        rule.paused_until = 0
        rule.mark_updated()
        await self._rules.update(rule)
        if self._queue is not None:
            try:
                if hasattr(self._queue, "_db") and not hasattr(self._queue, "_db_path"):
                    from ..persistence.postgres_repositories import _execute_db
                    await _execute_db(
                        self._queue._db,
                        "UPDATE delivery_jobs SET next_retry_at = 0 WHERE rule_id = %s AND status = 'RETRY_WAIT'",
                        (rule.id,),
                    )
                elif hasattr(self._queue, "_db") and hasattr(self._queue._db, "execute"):
                    self._queue._db.execute(
                        "UPDATE delivery_jobs SET next_retry_at = 0 WHERE rule_id = ? AND status = 'RETRY_WAIT'",
                        (rule.id,),
                    )
            except Exception:
                pass
        return pb.RuleActionResponse(success=True, message="resumed")


class DeliveryControlServicer(pb_grpc.DeliveryControlServiceServicer):
    """Live delivery stats, dead-letter queue inspection and replay."""

    def __init__(
        self, db, rule_repo, pipeline=None, queue_repo=None, message_map_repo=None
    ) -> None:
        self._db = db
        self._rules = rule_repo
        self._pipeline = pipeline
        self._queue_repo = queue_repo
        self._message_map_repo = message_map_repo

    def _q(self, sql: str, params: tuple = ()):
        try:
            cur = self._db.connection.cursor()
            cur.execute(sql, params)
            return cur.fetchall()
        except Exception:
            return []

    async def _query_queue(self, sql: str, params: tuple = ()) -> list:
        """Execute a query against delivery_jobs or dead_letter_queue across PostgreSQL and SQLite."""
        if self._queue_repo is not None and hasattr(self._queue_repo, "_fetch_all"):
            pg_sql = sql.replace("?", "%s")
            return await self._queue_repo._fetch_all(pg_sql, params)
        return self._q(sql, params)

    async def _execute_queue(self, sql: str, params: tuple = ()) -> Any:
        """Execute an insert/update/delete on delivery_jobs or dead_letter_queue across backends."""
        if self._queue_repo is not None and hasattr(self._queue_repo, "_db") and not hasattr(self._queue_repo, "_db_path"):
            from ..persistence.postgres_repositories import _execute_db
            pg_sql = sql.replace("?", "%s")
            if "INSERT OR IGNORE" in pg_sql:
                pg_sql = pg_sql.replace("INSERT OR IGNORE", "INSERT")
                if "VALUES" in pg_sql and "ON CONFLICT" not in pg_sql:
                    pg_sql += " ON CONFLICT DO NOTHING"
            return await _execute_db(self._queue_repo._db, pg_sql, params)
        if hasattr(self._db, "execute"):
            return self._db.execute(sql, params)
        cur = self._db.connection.cursor()
        cur.execute(sql, params)
        self._db.connection.commit()
        return cur

    async def _owner_rule_ids(self, owner_user_id: int) -> Optional[set]:
        """v12: resolve the rule ids a tenant may see. None = unscoped (admin)."""
        if not owner_user_id:
            return None
        try:
            owned = await self._rules.list_by_owner(int(owner_user_id))
        except Exception:
            return set()
        return {r.id for r in owned}

    async def GetDeliveryStats(self, request, context):
        # v12: a tenant only sees stats for its own rules, queue depth and
        # errors included. Without this GET /api/queue exposed every tenant's
        # rule names, error text and dead-letter backlog.
        scope = await self._owner_rule_ids(int(request.owner_user_id or 0))
        owner_clause = ""
        owner_params: tuple = ()
        if scope is not None:
            if not scope:
                return pb.DeliveryStatsResponse(rules=[], errors=[])
            placeholders = ",".join("?" for _ in scope)
            owner_clause = f" AND rule_id IN ({placeholders})"
            owner_params = tuple(scope)

        processed = forwarded = failed = dedup = filtered = in_queue = retries = 0
        for row in self._q(f"SELECT forwarded, filtered, errors FROM rule_stats WHERE 1=1{owner_clause}", owner_params):
            forwarded += int(row[0] or 0)
            filtered += int(row[1] or 0)
            failed += int(row[2] or 0)
        for row in self._q(f"SELECT COALESCE(SUM(forwarded),0), COALESCE(SUM(filtered),0), COALESCE(SUM(errors),0) FROM metrics_hourly WHERE 1=1{owner_clause}", owner_params):
            forwarded += int(row[0] or 0)
            filtered += int(row[1] or 0)
            failed += int(row[2] or 0)

        # Active jobs in queue: PENDING, CLAIMED, RETRY_WAIT
        queue_sql = f"SELECT COUNT(*) FROM delivery_jobs WHERE status IN ('PENDING', 'CLAIMED', 'RETRY_WAIT'){owner_clause}"
        for row in await self._query_queue(queue_sql, owner_params):
            val = row[0] if isinstance(row, (tuple, list)) else list(row.values())[0] if hasattr(row, "values") else (row["count"] if isinstance(row, dict) else 0)
            in_queue = int(val or 0)

        retries_sql = f"SELECT COALESCE(SUM(attempts),0) FROM delivery_jobs WHERE attempts > 1{owner_clause}"
        for row in await self._query_queue(retries_sql, owner_params):
            val = row[0] if isinstance(row, (tuple, list)) else list(row.values())[0] if hasattr(row, "values") else (row["coalesce"] if isinstance(row, dict) else 0)
            retries = int(val or 0)

        processed = forwarded + filtered + failed

        rules = []
        try:
            all_rules = await self._rules.list_all()
        except Exception:
            all_rules = []
        for r in all_rules:
            if scope is not None and r.id not in scope:
                continue
            stat = self._q(
                "SELECT forwarded, filtered, errors, last_forward_ts FROM rule_stats WHERE rule_id=?",
                (r.id,),
            )
            if stat:
                s = stat[0]
                rules.append(pb.RuleLiveStat(
                    rule_id=r.id, rule_name=r.name or f"Rule-{r.id[:8]}",
                    is_active=bool(r.is_active), is_paused=bool(getattr(r, "is_paused", False)),
                    forwarded=int(s[0] or 0), filtered=int(s[1] or 0), errors=int(s[2] or 0),
                    last_forward_ts=int(s[3] or 0),
                ))
            else:
                rules.append(pb.RuleLiveStat(
                    rule_id=r.id, rule_name=r.name or f"Rule-{r.id[:8]}",
                    is_active=bool(r.is_active), is_paused=bool(getattr(r, "is_paused", False)),
                ))

        errors = []
        for row in self._q(
            "SELECT ts, rule_id, category, error_name, severity, detail, chat_id "
            "FROM error_log WHERE 1=1" + owner_clause + " ORDER BY ts DESC LIMIT 25",
            owner_params,
        ):
            errors.append(pb.RecentError(
                ts=int(row[0] or 0), rule_id=str(row[1] or ""), category=str(row[2] or ""),
                error_name=str(row[3] or ""), severity=str(row[4] or ""),
                detail=str(row[5] or "")[:500], chat_id=str(row[6] or ""),
            ))

        dlq_total = 0
        dlq_sql = "SELECT COUNT(*) FROM dead_letter_queue WHERE 1=1" + owner_clause
        for row in await self._query_queue(dlq_sql, owner_params):
            val = row[0] if isinstance(row, (tuple, list)) else list(row.values())[0] if hasattr(row, "values") else (row["count"] if isinstance(row, dict) else 0)
            dlq_total = int(val or 0)

        return pb.DeliveryStatsResponse(
            processed_total=processed, forwarded_total=forwarded, failed_total=failed,
            dedup_skipped_total=dedup, filtered_total=filtered, in_queue=in_queue,
            dead_lettered_total=dlq_total, retry_total=retries,
            rules=rules, errors=errors,
        )

    async def ListDeadLetter(self, request, context):
        limit = int(request.limit or 50)
        if limit <= 0 or limit > 500:
            limit = 50
        params: tuple = ()
        sql = (
            "SELECT id, rule_id, source_chat_id, source_message_id, target_chat_id, "
            "attempts, last_error, error_category, dead_lettered_at "
            "FROM dead_letter_queue"
        )
        where = []
        if request.rule_id:
            where.append("rule_id=?")
            params = (request.rule_id,)
        if request.since:
            where.append("dead_lettered_at>=?")
            params = params + (int(request.since),)
        # v12: scope to the caller's rules.
        scope = await self._owner_rule_ids(int(request.owner_user_id or 0))
        if scope is not None:
            if not scope:
                return pb.ListDeadLetterResponse(entries=[], total=0)
            placeholders = ",".join("?" for _ in scope)
            where.append(f"rule_id IN ({placeholders})")
            params = params + tuple(scope)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY dead_lettered_at DESC LIMIT ?"
        params = params + (limit,)

        entries = []
        for r in await self._query_queue(sql, params):
            if isinstance(r, (tuple, list)):
                entries.append(pb.DeadLetterEntry(
                    id=str(r[0]), rule_id=str(r[1]), source_chat_id=str(r[2]),
                    source_message_id=int(r[3] or 0), target_chat_id=str(r[4]),
                    attempts=int(r[5] or 0), last_error=str(r[6] or "")[:500],
                    error_category=str(r[7] or ""), dead_lettered_at=int(r[8] or 0),
                ))
            elif hasattr(r, "keys"):
                d = dict(r)
                entries.append(pb.DeadLetterEntry(
                    id=str(d.get("id", "")), rule_id=str(d.get("rule_id", "")),
                    source_chat_id=str(d.get("source_chat_id", "")),
                    source_message_id=int(d.get("source_message_id", 0) or 0),
                    target_chat_id=str(d.get("target_chat_id", "")),
                    attempts=int(d.get("attempts", 0) or 0),
                    last_error=str(d.get("last_error") or d.get("error_message") or "")[:500],
                    error_category=str(d.get("error_category", "")),
                    dead_lettered_at=int(d.get("dead_lettered_at", 0) or 0),
                ))
        return pb.ListDeadLetterResponse(entries=entries, total=len(entries))

    async def ReplayDeadLetter(self, request, context):
        rows = await self._query_queue("SELECT * FROM dead_letter_queue WHERE id=?", (request.id,))
        if not rows:
            return pb.RuleActionResponse(success=False, message="not_found", error_code="not_found")
        r = rows[0]
        if isinstance(r, (tuple, list)):
            row_id = r[0]
            rule_id = r[2] if len(r) > 2 else ""
            src_chat = r[3] if len(r) > 3 else ""
            src_msg = r[4] if len(r) > 4 else 0
            tgt_chat = r[5] if len(r) > 5 else ""
            payload_json = r[6] if len(r) > 6 else "{}"
        else:
            d = dict(r) if hasattr(r, "keys") else r
            row_id = d.get("id", "")
            rule_id = d.get("rule_id", "")
            src_chat = d.get("source_chat_id", "")
            src_msg = d.get("source_message_id", 0)
            tgt_chat = d.get("target_chat_id", "")
            payload_json = d.get("payload_json", "{}")

        # v12: a tenant may only replay entries whose rule it owns.
        scope = await self._owner_rule_ids(int(request.owner_user_id or 0))
        if scope is not None and rule_id not in scope:
            logger.warning(
                "ReplayDeadLetter: owner=%s denied replay of entry=%s rule=%s",
                request.owner_user_id, request.id, rule_id,
            )
            return pb.RuleActionResponse(success=False, message="not_found", error_code="not_found")
        try:
            import json as _json
            import time as _time
            payload_str = payload_json if isinstance(payload_json, str) else _json.dumps(payload_json or {})
            now = int(_time.time())
            await self._execute_queue(
                "INSERT OR IGNORE INTO delivery_jobs "
                "(id, rule_id, source_chat_id, source_message_id, target_chat_id, payload_json, "
                "status, attempts, max_attempts, next_retry_at, lease_until, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, 3, 0, 0, ?, ?)",
                (f"replay-{row_id}", str(rule_id), str(src_chat),
                 int(src_msg or 0), str(tgt_chat), payload_str, now, now),
            )
            await self._execute_queue("DELETE FROM dead_letter_queue WHERE id=?", (request.id,))
            return pb.RuleActionResponse(success=True, message="requeued")
        except Exception as exc:
            return pb.RuleActionResponse(success=False, message=str(exc)[:200], error_code="replay_failed")

    async def PurgeDeadLetter(self, request, context):
        params: tuple = ()
        sql = "DELETE FROM dead_letter_queue"
        where = []
        if request.rule_id:
            where.append("rule_id=?")
            params = (request.rule_id,)
        if request.older_than:
            where.append("dead_lettered_at<?")
            params = params + (int(request.older_than),)
        # v12: purge only the caller's own rules.
        scope = await self._owner_rule_ids(int(request.owner_user_id or 0))
        if scope is not None:
            if not scope:
                return pb.RuleActionResponse(success=True, message="purged 0")
            placeholders = ",".join("?" for _ in scope)
            where.append(f"rule_id IN ({placeholders})")
            params = params + tuple(scope)
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            await self._execute_queue(sql, params)
            return pb.RuleActionResponse(success=True, message="purged")
        except Exception as exc:
            return pb.RuleActionResponse(success=False, message=str(exc)[:200], error_code="purge_failed")


def routing_name(value) -> str:
    """Proto routing enum -> *domain* RoutingType member name.

    The proto spells its zero value `ROUTING_UNKNOWN` while the domain enum calls
    it `UNKNOWN`, so an unset routing_type (the proto default) used to raise
    KeyError out of CreateRule instead of creating the rule.
    """
    try:
        name = pb.RoutingType.Name(value)
    except ValueError:
        return "CHANNEL_TO_CHANNEL"
    return "UNKNOWN" if name == "ROUTING_UNKNOWN" else name


def mode_name(value) -> str:
    """Proto forward-mode enum -> *domain* ForwardMode member name.

    Same zero-value mismatch as routing_name: `FORWARD_MODE_UNKNOWN` has no
    domain member, so it maps to the default COPY_MESSAGE.
    """
    try:
        name = pb.ForwardMode.Name(value)
    except ValueError:
        return "COPY_MESSAGE"
    return "COPY_MESSAGE" if name == "FORWARD_MODE_UNKNOWN" else name


class FilterControlServicer(pb_grpc.FilterControlServiceServicer):
    def __init__(self, filters_uc) -> None:
        self._filters = filters_uc

    async def ListFilters(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ListFiltersResponse(filters=[])
        rows = await self._filters.list_by_owner(owner)
        return pb.ListFiltersResponse(filters=[_filter_pb(f) for f in rows])

    async def CreateFilter(self, request, context):
        from ...domain.entities import FilterRule as Entity
        f = request.filter
        entity = Entity(
            name=f.name,
            whitelist_keywords=list(f.whitelist_keywords),
            blacklist_keywords=list(f.blacklist_keywords),
            regex_patterns=list(f.regex_patterns),
            allowed_media_types=list(f.allowed_media_types),
            blocked_media_types=list(f.blocked_media_types),
            drop_service_messages=f.drop_service_messages,
            min_message_length=f.min_message_length,
            max_message_length=f.max_message_length,
            owner_user_id=int(f.owner_user_id or 0),
        )
        created = await self._filters.create(entity)
        return _filter_pb(created)

    async def UpdateFilter(self, request, context):
        # v12.2: ownership guard at the use-case boundary.
        caller = int(request.filter.owner_user_id or 0)
        if not caller:
            return pb.FilterRule()
        existing = await self._filters.get_owned(request.filter.id, caller)
        if not existing:
            return pb.FilterRule()
        f = request.filter
        existing.name = f.name or existing.name
        existing.whitelist_keywords = list(f.whitelist_keywords)
        existing.blacklist_keywords = list(f.blacklist_keywords)
        existing.regex_patterns = list(f.regex_patterns)
        existing.allowed_media_types = list(f.allowed_media_types)
        existing.blocked_media_types = list(f.blocked_media_types)
        existing.drop_service_messages = f.drop_service_messages
        existing.min_message_length = f.min_message_length
        existing.max_message_length = f.max_message_length
        existing.owner_user_id = int(f.owner_user_id or existing.owner_user_id or 0)
        updated = await self._filters.update(existing)
        return _filter_pb(updated)

    async def DeleteFilter(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return _status(False, "unauthorized")
        try:
            ok = await self._filters.delete_owned(request.id, owner)
        except PermissionError:
            logger.warning(
                "DeleteFilter: owner=%s denied delete of filter=%s",
                owner, request.id,
            )
            return _status(False, "denied")
        return _status(ok, "deleted" if ok else "not_found")


class AIControlServicer(pb_grpc.AIControlServiceServicer):
    def __init__(self, ai_uc, prompt_service=None, ai_transformer=None) -> None:
        self._ai = ai_uc
        self._prompts = prompt_service
        self._transformer = ai_transformer

    async def ListAIConfigs(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.ListAIConfigsResponse(configs=[])
        rows = await self._ai.list_by_owner(owner)
        return pb.ListAIConfigsResponse(configs=[_ai_pb(c) for c in rows])

    async def CreateAIConfig(self, request, context):
        from ...domain.entities import AIConfig as Entity
        from ...domain.value_objects import AIProviderType as PT
        c = request.config
        entity = Entity(
            name=c.name, provider=PT(c.provider.lower()) if c.provider else PT.OPENAI,
            model=c.model or "gpt-4o-mini", api_key=c.api_key, base_url=c.base_url,
            system_prompt=c.system_prompt, user_prompt_template=c.user_prompt_template or "{text}",
            temperature=c.temperature or 0.7, is_enabled=c.is_enabled or True,
            target_language=c.target_language or "en",
            owner_user_id=int(c.owner_user_id or 0),
        )
        created = await self._ai.create(entity)
        return _ai_pb(created)

    async def UpdateAIConfig(self, request, context):
        # v12.2: ownership guard — a forged config id must not let a tenant
        # overwrite another account's provider key.
        caller = int(request.config.owner_user_id or 0)
        if not caller:
            return pb.AIConfig()
        existing = await self._ai.get_owned(request.config.id, caller)
        if not existing:
            return pb.AIConfig()
        c = request.config
        if c.model:
            existing.model = c.model
        if c.api_key and not c.api_key.startswith("***"):
            existing.api_key = c.api_key
        if c.base_url:
            existing.base_url = c.base_url
        if c.system_prompt:
            existing.system_prompt = c.system_prompt
        if c.user_prompt_template:
            existing.user_prompt_template = c.user_prompt_template
        existing.temperature = c.temperature or existing.temperature
        existing.is_enabled = c.is_enabled
        if c.target_language:
            existing.target_language = c.target_language
        existing.owner_user_id = int(c.owner_user_id or existing.owner_user_id or 0)
        updated = await self._ai.update(existing)
        return _ai_pb(updated)

    async def DeleteAIConfig(self, request, context):
        owner = int(request.owner_user_id or 0)
        if not owner:
            return _status(False, "unauthorized")
        try:
            ok = await self._ai.delete_owned(request.id, owner)
        except PermissionError:
            logger.warning(
                "DeleteAIConfig: owner=%s denied delete of config=%s",
                owner, request.id,
            )
            return _status(False, "denied")
        return _status(ok, "deleted" if ok else "not_found")

    async def TestAIRewrite(self, request, context):
        # v12.2: a rewrite burns the provider's quota — refuse to run it
        # against a config the caller does not own.
        owner = int(request.owner_user_id or 0)
        if not owner:
            return pb.TestAIRewriteResponse(success=False, error_message="unauthorized")
        ok, text = await self._ai.test_rewrite_owned(
            request.ai_config_id, request.sample_text, owner
        )
        return pb.TestAIRewriteResponse(success=ok, rewritten_text=text if ok else "", error_message="" if ok else text)

    # ------------------------------------------------------------------
    # Prompt templates (AI Rewrite) — shared by bot & web dashboard
    # ------------------------------------------------------------------

    @staticmethod
    def _prompt_pb(t) -> pb.PromptTemplate:
        return pb.PromptTemplate(
            id=t.id, name=t.name, description=t.description or "",
            system_prompt=t.system_prompt,
            user_prompt_template=t.user_prompt_template or "{text}",
            target_language=t.target_language or "en",
            current_version=t.current_version,
            is_system=bool(t.is_system),
            created_at=int(t.created_at or 0), updated_at=int(t.updated_at or 0),
        )

    async def ListPromptTemplates(self, request, context):
        if self._prompts is None:
            return pb.ListPromptTemplatesResponse()
        # v12: scope to the caller. An empty owner returns the shared system
        # prompts only; list_templates() (everything) is reserved for the
        # unscoped admin path in BackupService.
        owner = int(request.owner_user_id or 0)
        if owner:
            rows = await self._prompts.list_templates_by_owner(owner)
        else:
            rows = [t for t in await self._prompts.list_templates() if t.is_system]
        return pb.ListPromptTemplatesResponse(templates=[self._prompt_pb(t) for t in rows])

    async def SavePromptTemplate(self, request, context):
        if self._prompts is None:
            return pb.PromptTemplate()
        c = request.template
        owner = int(request.owner_user_id or 0)
        try:
            if c.id:
                existing = await self._prompts.get_template(c.id)
                if existing and existing.is_system:
                    return pb.PromptTemplate()  # system prompts are immutable
                # v12: a tenant may only overwrite its own (non-system) prompts.
                if existing and owner and int(getattr(existing, "owner_user_id", 0) or 0) not in (0, owner):
                    logger.warning(
                        "SavePromptTemplate: owner=%s denied write to prompt=%s owned_by=%s",
                        owner, c.id, getattr(existing, "owner_user_id", 0),
                    )
                    return pb.PromptTemplate()
                if existing and request.expected_version and existing.current_version != request.expected_version:
                    return pb.PromptTemplate()  # OCC conflict
                updated = await self._prompts.update_template(
                    c.id,
                    name=c.name or existing.name,
                    system_prompt=c.system_prompt,
                    user_prompt_template=c.user_prompt_template or "{text}",
                    description=c.description or "",
                    expected_version=request.expected_version or None,
                )
                if updated is False or updated is None:
                    return pb.PromptTemplate()
                fresh = await self._prompts.get_template(c.id)
                return self._prompt_pb(fresh) if fresh else pb.PromptTemplate()
            created = await self._prompts.create_template(
                name=c.name or "Untitled prompt",
                system_prompt=c.system_prompt,
                user_prompt_template=c.user_prompt_template or "{text}",
                description=c.description or "",
                target_language=c.target_language or "auto",
                owner_user_id=owner,
            )
            return self._prompt_pb(created)
        except Exception as exc:
            logger.warning("SavePromptTemplate failed: %s", exc)
            return pb.PromptTemplate()

    async def DeletePromptTemplate(self, request, context):
        if self._prompts is None:
            return _status(False, "prompt_service_unavailable")
        existing = await self._prompts.get_template(request.id)
        if not existing:
            return _status(False, "not_found")
        if existing.is_system:
            return _status(False, "system_prompt_immutable")
        # v12: a tenant may only delete its own prompts.
        owner = int(request.owner_user_id or 0)
        if owner and int(getattr(existing, "owner_user_id", 0) or 0) not in (0, owner):
            logger.warning(
                "DeletePromptTemplate: owner=%s denied delete of prompt=%s owned_by=%s",
                owner, request.id, getattr(existing, "owner_user_id", 0),
            )
            return _status(False, "not_found")
        ok = await self._prompts.delete_template(request.id)
        return _status(ok, "deleted" if ok else "not_found")

    async def TestPrompt(self, request, context):
        """Runs the real transform path in dry-run — no message is sent."""
        if self._prompts is None or self._transformer is None:
            return pb.TestPromptResponse(success=False, error_code="service_unavailable")
        # v12.2: the transform consumes the AI config's quota — only allow a
        # config the caller owns (shared is_system configs stay available to
        # everyone, as on the read path).
        owner = int(request.owner_user_id or 0)
        if owner and request.ai_config_id:
            cfg = await self._ai.get_owned(request.ai_config_id, owner) if hasattr(self._ai, "get_owned") else None
            if cfg is None:
                logger.warning(
                    "TestPrompt: owner=%s denied use of ai_config=%s",
                    owner, request.ai_config_id,
                )
                return pb.TestPromptResponse(success=False, error_code="config_not_found")
        try:
            result = await self._prompts.test_prompt(
                request.prompt_id, request.sample_text,
                request.ai_config_id, self._transformer,
                version=request.version or None,
            )
            return pb.TestPromptResponse(
                success=bool(result.get("success")),
                transformed_text=result.get("transformed_text") or "",
                error_message=result.get("error_message") or "",
                error_code=result.get("error_code") or "",
                execution_duration_ms=result.get("execution_duration_ms") or 0.0,
                provider_used=result.get("provider_used") or "",
                model_used=result.get("model_used") or "",
                prompt_version_used=int(result.get("prompt_version_used") or 0),
                circuit_state=result.get("circuit_state") or "",
            )
        except Exception as exc:
            logger.warning("TestPrompt failed: %s", exc)
            return pb.TestPromptResponse(success=False, error_code=str(type(exc).__name__), error_message=str(exc))


class SystemStatusControlServicer(pb_grpc.SystemStatusControlServiceServicer):
    def __init__(self, sessions, rules, pipeline, bot_username: str, version: str,
                 log_policy=None, log_policy_store=None, log_client=None) -> None:
        self._sessions = sessions
        self._rules = rules
        self._pipeline = pipeline
        self._bot_username = bot_username
        self._version = version
        # The producer-side collection policy lives in this process, so only it
        # can answer "what is Core actually collecting?". Optional to keep the
        # servicer constructible in tests that do not boot the logging stack;
        # a missing policy is reported as unavailable rather than as a zero.
        self._log_policy = log_policy
        # Where a change is persisted, so it outlives a restart. Absent means
        # this build holds the policy in memory, which a change reports as
        # persisted=false rather than pretending otherwise.
        self._log_policy_store = log_policy_store
        # The same client the rest of Core emits through, used for the audit
        # record of a change: the trail goes through the policy being changed.
        self._log_client = log_client

    def attach_log_client(self, log_client) -> None:
        """Late-bind the Logger client used for the audit record of a change.

        The servicer is registered before the client is built (the client needs
        the policy this servicer reports), so the wiring is completed here rather
        than by rebuilding the servicer and re-registering it.
        """
        self._log_client = log_client

    async def GetSystemStats(self, request, context):
        s = self._pipeline.stats
        owner = int(request.owner_user_id or 0)
        if owner:
            rules_q = await self._rules.list_by_owner(owner)
        else:
            rules_q = []
        active = [r for r in rules_q if r.is_active]
        return pb.SystemStatsResponse(
            core_running=True,
            uptime_seconds=int(time.time()) - s.started_at,
            active_sessions_count=len(active),
            active_rules_count=len(active),
            total_messages_processed=s.processed,
            total_messages_forwarded=s.forwarded,
            total_messages_filtered=s.filtered,
            total_messages_rewritten=s.rewritten,
            bot_username=self._bot_username,
            version=self._version,
        )

    async def GetLogPolicy(self, request, context):
        """Report the collection policy this process enforces, honestly.

        The control plane used to hard-code this scope as unavailable because
        nothing here could be read. The policy is per producer and decided from
        this process's own environment, so the real answer has to come from
        inside Core — never estimated by the API, and never zero-filled: a zero
        would read as "nothing was suppressed", the opposite of the truth.
        """
        policy = self._log_policy
        if policy is None:
            return pb.LogPolicyResponse(
                available=False,
                reason="no collection policy attached to this core process",
            )
        return self._policy_response(policy)

    async def SetLogPolicy(self, request, context):
        """Change the policy Core enforces, or refuse the request intact.

        The change is validated and persisted *here*, by the process that has to
        live with the value: a control plane cannot know whether a level name
        means what its caller thinks it means, and a policy the producer
        interpreted differently would be worse than no policy at all. A rejected
        request changes nothing, and the answer is the policy that is in force
        afterwards — read back from the very instance the emitter consults — plus
        whether that change is durable, because "applied but lost on restart" and
        "saved" are different promises.
        """
        policy = self._log_policy
        if policy is None:
            return pb.LogPolicyResponse(
                available=False,
                reason="no collection policy attached to this core process",
            )

        actor = (request.actor or "").strip()
        correlation = (request.correlation_id or "").strip()
        try:
            policy.reconfigure(
                levels=list(request.levels) if request.levels else None,
                # Presence, not truthiness: an explicit 0 removes the ceiling,
                # while omitting the field leaves whatever ceiling is in force.
                # Reading 0 as "not mentioned" is what made "unlimited" an
                # impossible request for an operator to make.
                verbose_rate_per_second=(
                    float(request.verbose_rate_per_second)
                    if request.HasField("verbose_rate_per_second")
                    else None
                ),
                diagnostic_seconds=int(request.diagnostic_seconds or 0) or None,
                disable_diagnostics=bool(request.disable_diagnostics),
            )
        except LogPolicyError as exc:
            self._audit_policy_change(
                actor, applied=False, detail={"error": str(exc)}, correlation_id=correlation
            )
            response = self._policy_response(policy)
            response.applied = False
            response.persisted = False
            response.reason = str(exc)
            return response

        persisted = True
        if self._log_policy_store is None:
            # No store attached means this build keeps the policy in memory:
            # report it as volatile rather than claiming durability it lacks.
            persisted = False
        else:
            persisted = bool(self._log_policy_store.save(policy.export_state()))

        response = self._policy_response(policy)
        response.applied = True
        response.persisted = persisted
        self._audit_policy_change(
            actor,
            applied=True,
            detail={
                "configured_levels": list(response.configured_levels),
                "verbose_rate_per_second": response.verbose_rate_per_second,
                "diagnostic_active": response.diagnostic_active,
                "persisted": persisted,
            },
            correlation_id=correlation,
        )
        return response

    def _policy_response(self, policy) -> "pb.LogPolicyResponse":
        """The enforced policy, as the control plane and the audit trail see it."""
        snap = policy.snapshot()
        return pb.LogPolicyResponse(
            available=True,
            configured_levels=list(snap.get("configured_levels", [])),
            effective_levels=list(snap.get("levels", [])),
            diagnostic_active=bool(snap.get("diagnostic_active")),
            diagnostic_until=int(snap.get("diagnostic_until") or 0),
            verbose_rate_per_second=float(snap.get("verbose_rate_per_second") or 0.0),
            emitted=int(snap.get("emitted") or 0),
            emitted_security=int(snap.get("emitted_security") or 0),
            suppressed_level=int(snap.get("suppressed_level") or 0),
            suppressed_rate=int(snap.get("suppressed_rate") or 0),
            diagnostic_activations=int(snap.get("diagnostic_activations") or 0),
        )

    def _audit_policy_change(
        self, actor: str, applied: bool, detail: dict, correlation_id: str = ""
    ) -> None:
        """Record an administrative policy change through the policy it changed.

        The event type starts with `admin.`, which the policy never suppresses:
        a threshold is a noise control, and "who turned collection off" is
        exactly the kind of fact it must not be able to hide.
        """
        log_client = self._log_client
        if log_client is None:
            return
        payload = {"actor": actor or "unattributed", "applied": applied}
        payload.update(detail)
        try:
            log_client.log(
                "WARN",
                "core",
                "admin",
                f"log collection policy change {'applied' if applied else 'rejected'}"
                + (f" by {actor}" if actor else ""),
                payload,
                correlation_id=correlation_id,
                # Naming the event keeps it outside every threshold: the policy
                # must not be able to suppress the record of its own change.
                event_type="admin.log_policy.changed",
                reason_code="ADMIN_REQUIRED",
            )
        except Exception as exc:  # never fail a policy change on audit trouble
            logger.warning("[PIPELINE:log_policy] audit emit failed: %s", exc)


class BackupControlServicer(pb_grpc.BackupControlServiceServicer):
    """Server migration: export/import the whole install or one tenant.

    The bundle carries live credentials, so the caller should pass a passphrase
    for anything that leaves the box.
    """

    def __init__(self, backup_service) -> None:
        self._backup = backup_service

    async def BackupExport(self, request, context):
        try:
            owner = int(request.owner_user_id or 0)
            doc = await self._backup.create_snapshot(owner or None)
            text = serialize_bundle(doc)
            if request.passphrase:
                text = encrypt_bundle(text, request.passphrase)
            return pb.BackupExportResponse(
                success=True,
                bundle=text,
                counts={k: int(v) for k, v in (doc.get("counts") or {}).items()},
            )
        except Exception as exc:
            logger.warning("BackupExport failed: %s", exc)
            return pb.BackupExportResponse(success=False, error=str(exc))

    async def BackupImport(self, request, context):
        try:
            text = decrypt_bundle(request.bundle, request.passphrase)
            doc = deserialize_bundle(text)
            target = int(request.target_owner_user_id or 0)
            stats = await self._backup.restore_snapshot(
                doc,
                target or None,
                overwrite_existing=bool(request.overwrite_existing),
            )
            return pb.BackupImportResponse(
                success=True,
                stats={
                    k: pb.EntityImportStats(
                        imported=int(v.get("imported", 0)),
                        skipped=int(v.get("skipped", 0)),
                        failed=int(v.get("failed", 0)),
                    )
                    for k, v in stats.items()
                },
            )
        except Exception as exc:
            logger.warning("BackupImport failed: %s", exc)
            return pb.BackupImportResponse(success=False, error=str(exc))


class AuditControlServicer(pb_grpc.AuditControlServiceServicer):
    """Tenant-scoped audit event queries backed by the PostgreSQL audit store
    (mission Phase Three). The Go API resolves the authorized tenant from the
    authenticated principal and passes it as owner_user_id — this servicer
    never trusts client-side tenant claims beyond that contract."""

    def __init__(self, audit_repo):
        self._audit = audit_repo

    async def QueryAuditEvents(self, request, context):
        if self._audit is None:
            return pb.QueryAuditEventsResponse(events=[], total=0)
        rows = await self._audit.query(
            tenant_id=str(request.owner_user_id) if request.owner_user_id else "",
            correlation_id=request.correlation_id or "",
            operation_id=request.operation_id or "",
            feature_id=request.feature_id or "",
            service=request.service or "",
            severity=request.severity or "",
            event_category=request.event_category or "",
            outcome=request.outcome or "",
            since=request.since or 0,
            until=request.until or 0,
            limit=request.limit or 100,
            offset=request.offset or 0,
        )

        def _s(v):
            return "" if v is None else str(v)

        events = [
            pb.AuditEventEntry(
                event_id=_s(r.get("event_id")),
                schema_version=_s(r.get("schema_version")),
                event_type=_s(r.get("event_type")),
                event_category=_s(r.get("event_category")),
                severity=_s(r.get("severity")),
                outcome=_s(r.get("outcome")),
                ts_utc=int(r.get("ts_utc") or 0),
                service=_s(r.get("service")),
                correlation_id=_s(r.get("correlation_id")),
                operation_id=_s(r.get("operation_id")),
                tenant_id=_s(r.get("tenant_id")),
                actor_type=_s(r.get("actor_type")),
                actor_id=_s(r.get("actor_id")),
                feature_id=_s(r.get("feature_id")),
                action=_s(r.get("action")),
                resource_type=_s(r.get("resource_type")),
                resource_id=_s(r.get("resource_id")),
                rule_id=_s(r.get("rule_id")),
                queue_job_id=_s(r.get("queue_job_id")),
                reason_code=_s(r.get("reason_code")),
                duration_ms=int(r.get("duration_ms") or 0),
                error_code=_s(r.get("error_code")),
                error_class=_s(r.get("error_class")),
                retryable=bool(r.get("retryable") or False),
                result_summary=_s(r.get("result_summary")),
                metadata_json=_s(r.get("metadata")),
            )
            for r in rows
        ]
        return pb.QueryAuditEventsResponse(events=events, total=len(events))
