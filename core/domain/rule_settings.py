"""Declarative registry of per-task (per-rule) feature settings.

The Telegram bot edits these through hand-written panels; the REST/gRPC control
plane needs the same reach. A registry — instead of one proto field per setting —
is what keeps the two in sync: adding an entry here is what makes a setting
readable and writable through the API, and it gives the dashboard the metadata
(kind, default, bounds) it needs to render a control instead of hard-coding one.

Every entry is read and written through the *entity property*, never by poking
`metadata` directly, so a write here behaves exactly like the same change made
from the bot, and defaults/normalisation stay in one place.

Settings that already have a first-class `ForwardRule` field (forward_mode,
content_mode, delay_seconds, sync_edits, prompt_id, ...) are deliberately NOT
listed: they belong on the rule itself, and a second write path could disagree
with the first.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SettingSpec",
    "SettingError",
    "SETTING_SPECS",
    "SCHEMA_VERSION",
    "coerce_value",
    "read_settings",
    "apply_settings",
    "schema",
]

SCHEMA_VERSION = 1


class SettingError(ValueError):
    """A setting name is unknown, or its value failed validation."""

    def __init__(self, code: str, message: str, key: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.key = key


@dataclass(frozen=True)
class SettingSpec:
    """One controllable per-task setting."""

    kind: str  # bool | int | float | str | list | dict
    default: Any
    group: str = "general"
    label: str = ""
    help: str = ""
    choices: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    nullable: bool = False
    item_kind: str = "any"  # for kind == "list"
    #: May a bot default carry this setting for every task that uses the bot?
    #: False for anything that names a chat, a route or a schedule: those say
    #: *which* messages the task handles and *when*, which is never a property of
    #: the bot credential. Inheriting them would point other tasks' forwards at
    #: someone else's chats.
    inheritable: bool = True

    def to_dict(self, key: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "key": key,
            "kind": self.kind,
            "default": self.default,
            "group": self.group,
            "label": self.label or key.replace("_", " ").title(),
            "nullable": self.nullable,
            "inheritable": self.inheritable,
        }
        if self.help:
            out["help"] = self.help
        if self.choices:
            out["choices"] = list(self.choices)
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.kind == "list":
            out["item_kind"] = self.item_kind
        return out


def _s(**kw: Any) -> SettingSpec:
    return SettingSpec(**kw)


# --------------------------------------------------------------------------- #
# The registry. Keys are the entity property names.
# --------------------------------------------------------------------------- #
SETTING_SPECS: Dict[str, SettingSpec] = {
    # ---- content cleanup -------------------------------------------------
    "header": _s(kind="str", default="", group="cleanup",
                 help="Text prepended to every forwarded message."),
    "footer": _s(kind="str", default="", group="cleanup",
                 help="Text appended to every forwarded message."),
    "replacements": _s(kind="dict", default={}, group="cleanup",
                       help="Literal find/replace pairs applied to the text."),
    "link_replacement": _s(kind="str", default="", group="cleanup"),
    "clean_usernames": _s(kind="bool", default=False, group="cleanup"),
    "clean_hashtags": _s(kind="bool", default=False, group="cleanup"),
    "clean_phones": _s(kind="bool", default=False, group="cleanup"),
    "clean_buttons": _s(kind="bool", default=False, group="cleanup"),
    "clean_empty_lines": _s(kind="bool", default=False, group="cleanup"),
    "clean_custom_words": _s(kind="list", default=[], group="cleanup",
                             item_kind="str",
                             help="Words stripped from the text."),
    "remove_emojis": _s(kind="bool", default=False, group="cleanup"),
    "cleaner_enabled": _s(kind="bool", default=False, group="cleanup",
                          help="Master switch for the cleaner pipeline."),
    "cleaner_config": _s(kind="dict", default={}, group="cleanup",
                         help="Per-rule cleaner rules (which patterns to strip)."),

    # ---- media filtering -------------------------------------------------
    "block_text": _s(kind="bool", default=False, group="media"),
    "block_voice": _s(kind="bool", default=False, group="media"),
    "block_audio": _s(kind="bool", default=False, group="media"),
    "block_video": _s(kind="bool", default=False, group="media"),
    "block_stickers": _s(kind="bool", default=False, group="media"),
    "block_links": _s(kind="bool", default=False, group="media",
                      help="Drop messages that contain a link."),
    # The allow side of the same gate. Stored in its own forward_rules column
    # (not metadata) because backups and the REST contract already carry it;
    # non-empty means "forward only these types", which is a stricter statement
    # than any single block flag and is evaluated first by the engine.
    "allowed_media_types": _s(kind="list", item_kind="str", default=[], group="media",
                              help="Allow-list: when non-empty, only these message types are forwarded."),
    "album_mode": _s(kind="str", default="album", group="media",
                     help="How grouped albums are delivered."),
    "watermark": _s(kind="str", default="", group="media"),
    "watermark_text": _s(kind="str", default="", group="media"),

    # ---- sending behaviour ----------------------------------------------
    "duplicate_filters": _s(kind="bool", default=False, group="sending",
                            help="Suppress messages already forwarded before."),
    "max_time_edit": _s(kind="int", default=86400, group="sending",
                        minimum=0, maximum=2592000,
                        help="Seconds after which an edit is no longer mirrored."),
    "edit_time_limit_seconds": _s(kind="int", default=0, group="sending",
                                  minimum=0, maximum=2592000),
    "forward_delay_seconds": _s(kind="float", default=0.0, group="sending",
                                minimum=0, maximum=86400),
    "auto_delete_seconds": _s(kind="int", default=0, group="sending",
                              minimum=0, maximum=604800,
                              help="Delete the forwarded copy after N seconds (0 = keep)."),
    # Counts only messages that survived every filter, and treats one album as
    # one message. The counter lives in `rule_counters`, so this value is a pure
    # policy: 1 (the default) forwards everything and is what an older task with
    # no metadata behaves like.
    "forward_every_n": _s(kind="int", default=1, group="sending",
                          minimum=1, maximum=1000,
                          help="Forward only every Nth message that passes this task's filters "
                               "(1 = every message, 1000 = one in a thousand)."),
    "debug_mode": _s(kind="bool", default=False, group="sending",
                     help="Log every decision for this task."),
    "sync_replies": _s(kind="bool", default=False, group="sending",
                       help="Keep replies attached to the forwarded copy."),
    "process_raw_text": _s(kind="bool", default=False, group="sending",
                           help="Run the pipeline on raw text before any parsing."),
    "feature_group": _s(kind="str", default="all", group="sending"),
    "sender_type": _s(kind="str", default="Personal Telegram Account", group="sending"),

    # ---- keywords / replies ---------------------------------------------
    "keyword_confirmation": _s(kind="bool", default=False, group="keywords"),
    "keyword_reply": _s(kind="str", default="", group="keywords"),
    "translate_lang": _s(kind="str", default="", group="keywords",
                         help="Target language for auto-translation ('' = off)."),

    # ---- audience filters ------------------------------------------------
    "blacklist": _s(kind="list", default=[], group="audience", item_kind="str",
                    help="Substrings that block a message."),
    "whitelist": _s(kind="list", default=[], group="audience", item_kind="str",
                    help="If set, only matching messages are forwarded."),
    "blacklist_users": _s(kind="list", default=[], group="audience",
                          item_kind="str",
                          help="Sender ids/usernames never forwarded."),
    "whitelist_users": _s(kind="list", default=[], group="audience",
                          item_kind="str"),

    # ---- AI --------------------------------------------------------------
    "ai_creativity": _s(kind="float", default=0.7, group="ai",
                        minimum=0, maximum=2,
                        help="Sampling temperature used for this task's rewrites."),
    "ai_max_tokens": _s(kind="int", default=600, group="ai",
                        minimum=1, maximum=100000),
    "ocr_image_ai": _s(kind="bool", default=False, group="ai",
                       help="Run OCR on images and forward the extracted text."),
    "news_digest": _s(kind="bool", default=False, group="ai"),
    "smart_ai_rewrite": _s(kind="bool", default=False, group="ai",
                           help="Let the AI decide whether a message needs rewriting."),
    "crypto_mode": _s(kind="bool", default=False, group="ai"),
    "paid_addons": _s(kind="dict", default={}, group="ai"),

    # ---- bot route -------------------------------------------------------
    # Two keys, one decision: who the copy is sent as. They are applied as a
    # pair (see apply_settings) because the entity refuses BOT without a token
    # — a half-applied pair would silently leave the task on the account route.
    "delivery_transport": _s(inheritable=False, 
        kind="str", default="USER", group="route", choices=("USER", "BOT"),
        help="USER = this task's account forwards; BOT = the attached bot posts."),
    "bot_token_id": _s(inheritable=False, 
        kind="str", default="", group="route",
        help="Id of an attached bot token ('' = none). Tokens are attached in the"
             " Telegram bot's task screen, where the pasted message is deleted and"
             " the token is stored encrypted; the secret itself is never exposed here."),

    # ---- automation ------------------------------------------------------
    "auto_post_scheduler": _s(inheritable=False, kind="dict", default={"enabled": False},
                              group="automation"),
    "auto_posting": _s(inheritable=False, kind="bool", default=False, group="automation",
                       help="Convenience view of auto_post_scheduler.enabled."),
    "power_control": _s(inheritable=False, kind="bool", default=False, group="automation",
                        help="Convenience view of power_schedule.enabled."),
    "power_schedule": _s(inheritable=False, kind="dict", default={"enabled": False},
                         group="automation"),
    "button_to_text": _s(kind="bool", default=False, group="automation"),
    "link_buttons": _s(inheritable=False, kind="list", default=[], group="automation",
                       item_kind="str"),
    "affiliate_kit": _s(inheritable=False, kind="dict", default={}, group="automation"),

    # ---- per-task content rules (blacklist / whitelist-only / replace) -----
    # Read and written through the ForwardRule properties, which keep the whole
    # policy in one metadata blob (`content_policy`) so a task carries its own
    # rules with it through backup/restore and the API's settings surface.
    "content_policy_enabled": _s(
        kind="bool", default=False, group="content",
        help="Master switch for this task's content rules."),
    "blacklist_words": _s(
        kind="list", default=[], group="content", item_kind="str",
        help="Never forward a message that contains one of these words."),
    "whitelist_words": _s(
        kind="list", default=[], group="content", item_kind="str",
        help="Forward only when the message contains these words (see whitelist_mode)."),
    "whitelist_mode": _s(
        kind="str", default="any", group="content", choices=("any", "all", "phrase"),
        help="any = at least one word, all = every word, phrase = the words in order."),
    # NB: "block_links" is registered once, under the media group (see above):
    # it is the same entity property, and the content policy folds it in.
    "remove_links": _s(
        kind="bool", default=False, group="content",
        help="Forward, but strip the links out of the text."),
    "strip_words": _s(
        kind="list", default=[], group="content", item_kind="str",
        help="Forward, but remove these words from the text."),
    "regex_block": _s(
        kind="list", default=[], group="content", item_kind="str",
        help="Never forward a message matching one of these patterns."),
    "regex_require": _s(
        kind="list", default=[], group="content", item_kind="str",
        help="Forward only a message matching one of these patterns."),
    "source_policies": _s(inheritable=False, 
        kind="dict", default={}, group="content",
        help="Per-source overrides, keyed by source chat id."),
    "target_policies": _s(inheritable=False, 
        kind="dict", default={}, group="content",
        help="Per-target overrides, keyed by target chat id."),
    # --- Branding hop (A -> C -> B) --------------------------------------- #
    # The enable switch and the channel itself (`use_intermediate`,
    # `intermediate_channel_id`) are first-class rule fields, not settings —
    # exactly one write path keeps the two from disagreeing. What is registered
    # here is the *trigger* policy: which messages earn the branded hop.
    "branding_triggers_enabled": _s(
        kind="bool", default=False, group="branding",
        help="When off every matched message takes the hop via C; when on, the triggers below decide."),
    "branding_origin_titles": _s(
        kind="list", default=[], group="branding", item_kind="str",
        help="Hop only when the message was forwarded from one of these channel names."),
    "branding_origin_ids": _s(
        kind="list", default=[], group="branding", item_kind="str",
        help="Hop only when the message was forwarded from one of these chat ids/@usernames."),
    "branding_keywords": _s(
        kind="list", default=[], group="branding", item_kind="str",
        help="Hop only when the message text contains one of these words."),
    "branding_match_mode": _s(
        kind="str", default="any", group="branding", choices=("any", "all"),
        help="any = one trigger is enough, all = every configured trigger must match."),
    "branding_require_forward": _s(
        kind="bool", default=False, group="branding",
        help="Refuse the hop unless the message is itself a forward."),
    "branding_force_forward": _s(
        kind="bool", default=True, group="branding",
        help="Send the final step as a native forward of C, so B's header names C."),
}

_GROUP_ORDER = ("general", "cleanup", "content", "media", "route", "branding", "sending",
                "keywords", "audience", "ai", "automation")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def coerce_value(key: str, value: Any) -> Any:
    """Validate and normalise one setting value, or raise :class:`SettingError`.

    Validation is the point of this layer: the bot's widgets enforce these rules
    in the UI, so an API that trusted the wire would be able to store values the
    bot itself would refuse.
    """
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise SettingError("unknown_setting", f"unknown setting {key!r}", key)

    if value is None:
        if spec.nullable:
            return None
        raise SettingError("invalid_type", f"{key} may not be null", key)

    kind = spec.kind
    # bool must be checked before int: in Python `True` is an `int`.
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise SettingError("invalid_type", f"{key} expects a boolean", key)

    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingError("invalid_type", f"{key} expects an integer", key)
        return _bounded(key, value, spec)

    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingError("invalid_type", f"{key} expects a number", key)
        return _bounded(key, float(value), spec)

    if kind == "str":
        if not isinstance(value, str):
            raise SettingError("invalid_type", f"{key} expects a string", key)
        value = value.strip()
        if spec.choices and value and value not in spec.choices:
            raise SettingError(
                "invalid_choice",
                f"{key} must be one of {', '.join(spec.choices)}",
                key,
            )
        return value

    if kind == "list":
        if not isinstance(value, (list, tuple)):
            raise SettingError("invalid_type", f"{key} expects a list", key)
        if spec.item_kind == "str":
            out: List[str] = []
            for item in value:
                if not isinstance(item, str):
                    raise SettingError("invalid_type",
                                       f"{key} expects a list of strings", key)
                s = item.strip()
                if s:
                    out.append(s)
            return out
        return list(value)

    if kind == "dict":
        if not isinstance(value, dict):
            raise SettingError("invalid_type", f"{key} expects an object", key)
        # JSON keys are always strings; normalising here keeps equality checks
        # between a read and a re-write from failing on int keys.
        return {str(k): v for k, v in value.items()}

    raise SettingError("invalid_spec", f"{key} has an unsupported kind", key)


def _bounded(key: str, value: Any, spec: SettingSpec) -> Any:
    if spec.minimum is not None and value < spec.minimum:
        raise SettingError("out_of_range",
                           f"{key} must be >= {spec.minimum}", key)
    if spec.maximum is not None and value > spec.maximum:
        raise SettingError("out_of_range",
                           f"{key} must be <= {spec.maximum}", key)
    return value


# --------------------------------------------------------------------------- #
# Read / write
# --------------------------------------------------------------------------- #
#: Metadata key holding the settings a task pinned that are stored as real
#: columns. Those leave no other trace of the choice, so the choice is noted by
#: name (see :func:`apply_settings`).
PIN_INDEX = "_pinned_settings"


def _pin_index(meta: Any) -> list:
    """The recorded pin list, reading anything unusable as empty."""
    if not isinstance(meta, dict):
        return []
    raw = meta.get(PIN_INDEX)
    if not isinstance(raw, list):
        return []
    return [str(k) for k in raw if isinstance(k, (str, bytes))]


def pinned_keys(rule: Any) -> set:
    """Settings this task chose itself, rather than inheriting.

    A setting counts as chosen when the task's own value differs from the
    registry default — that single test covers both storage shapes, because
    :func:`read_settings` reads the column-backed and metadata-backed settings
    the same way.

    The gap it leaves is a value a task chose that is *exactly* the default,
    with a bot default now competing for it (turning a bot's link-cleaning back
    off, for instance). Those are recorded by name when the write moves the
    effective value, and this is where they are read back; see
    :data:`PIN_INDEX`.
    """
    meta = getattr(rule, "metadata", None)
    pinned = {k for k in _pin_index(meta) if k in SETTING_SPECS}
    values = read_settings(rule)
    for key, spec in SETTING_SPECS.items():
        if key in pinned:
            continue
        if _chosen_not_default(values.get(key), spec.default):
            pinned.add(key)
    return pinned


def _chosen_not_default(value: Any, default: Any) -> bool:
    """Did a task choose this value, judged by it differing from the default?

    Only used for settings that are stored as columns, where there is no other
    trace of the choice. An unset mapping column reads back as ``{}`` where the
    registry spells out the full shape (``{"enabled": False}``); both mean
    "off", so an empty mapping is compared against whether the default asserts
    anything.
    """
    if value == default:
        return False
    if isinstance(value, dict) and isinstance(default, dict) and not value:
        return any(bool(v) for v in default.values())
    return True


def clear_setting(rule: Any, key: str) -> bool:
    """Return one setting to its inherited value. True when it was pinned.

    Deleting the metadata entry is what actually hands the setting back to the
    bot default: writing the default *value* instead would leave the key pinned
    to that value and the task would stop tracking the bot.
    """
    if key not in SETTING_SPECS:
        raise SettingError("unknown_setting", f"unknown setting {key!r}", key)
    spec = SETTING_SPECS[key]
    meta = getattr(rule, "metadata", None)
    was_pinned = key in pinned_keys(rule)
    # Column-backed settings have no metadata entry; writing the default is how
    # they are cleared.
    setattr(rule, key, spec.default)
    if isinstance(meta, dict):
        meta.pop(key, None)
        recorded = _pin_index(meta)
        if key in recorded:
            recorded = [k for k in recorded if k != key]
            if recorded:
                meta[PIN_INDEX] = recorded
            else:
                meta.pop(PIN_INDEX, None)
    return was_pinned


def read_settings(rule: Any) -> Dict[str, Any]:
    """Current effective value of every registered setting on ``rule``."""
    out: Dict[str, Any] = {}
    for key in SETTING_SPECS:
        try:
            out[key] = getattr(rule, key)
        except Exception:
            # A malformed value in stored metadata must not blank the whole
            # settings view; fall back to the declared default.
            out[key] = SETTING_SPECS[key].default
    return out


def apply_settings(
    rule: Any,
    values: Dict[str, Any],
    replace_all: bool = False,
    effective_before: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Apply ``values`` to ``rule``; returns the keys whose value moved.

    Unknown keys and invalid values raise :class:`SettingError` *before* any
    mutation, so a rejected request never leaves the rule half-updated.

    ``effective_before`` is the mapping the forwarder was using *before* this
    write — a caller that knows which bot the task inherits from passes
    ``core.domain.bot_defaults.effective(...)``. Two things depend on it, and
    both are wrong without it:

      * a merge that contains every registered key (what a form posts) would
        otherwise look like a change to every key;
      * a task overriding an inherited value with the registry's own value
        (turning a bot's "clean links on" back off) would otherwise look like no
        change at all, and the override would be dropped.
    """
    if not isinstance(values, dict):
        raise SettingError("invalid_type", "settings must be an object")

    if replace_all:
        unknown = [k for k in values if k not in SETTING_SPECS]
        if unknown:
            raise SettingError("unknown_setting",
                               f"unknown setting {unknown[0]!r}", unknown[0])
        target: Dict[str, Any] = {}
        for key, spec in SETTING_SPECS.items():
            target[key] = values[key] if key in values else spec.default
    else:
        target = {}
        for key, raw in values.items():
            if key not in SETTING_SPECS:
                raise SettingError("unknown_setting", f"unknown setting {key!r}", key)
            target[key] = raw

    # Validate everything first: an invalid entry must not leave a partial write.
    cleaned = {key: coerce_value(key, raw) for key, raw in target.items()}

    current = read_settings(rule)
    reference = effective_before if isinstance(effective_before, dict) else current
    # The route is a pair, so validate the *resulting* state, not just the keys
    # the request happened to mention. Both halves of the failure would be
    # silent otherwise: switching to BOT with no token is dropped by the entity
    # setter (a no-op success), and clearing the token while still on BOT drops
    # the task back to the account route behind the owner's back.
    effective_transport = cleaned.get("delivery_transport", current.get("delivery_transport", "USER"))
    effective_token = cleaned.get("bot_token_id", current.get("bot_token_id", ""))
    if str(effective_transport or "").upper() == "BOT" and not str(effective_token or "").strip():
        raise SettingError(
            "invalid_state",
            "the bot route needs an attached bot_token_id — send delivery_transport='USER'"
            " with the removal, or attach a token",
            "bot_token_id" if "bot_token_id" in cleaned else "delivery_transport",
        )

    # Apply the token before the transport: a single request may attach and
    # switch at once, and the setter only accepts BOT once a token is present.
    ordered = sorted(cleaned.items(), key=lambda kv: 0 if kv[0] == "bot_token_id" else 1)

    changed: List[str] = []
    for key, value in ordered:
        if reference.get(key) != value:
            changed.append(key)
        setattr(rule, key, value)

    # Record the keys this write moved, by name. A setting whose chosen value is
    # exactly the registry default is otherwise indistinguishable from one the
    # task never touched — a column leaves no other trace, and a metadata entry
    # holding the default says nothing either — so a bot default would quietly
    # win over the user's choice. `clear_setting` removes the note.
    #
    # Only *moved* keys are noted, so a client that posts a whole settings object
    # does not pin the settings it merely rendered.
    meta = getattr(rule, "metadata", None)
    if isinstance(meta, dict):
        recorded = _pin_index(meta)
        added = [k for k in changed if k not in recorded]
        if added:
            meta[PIN_INDEX] = sorted(recorded + added)
    return changed


def schema() -> Dict[str, Any]:
    """Machine-readable description of every setting, for building a UI."""
    ordered = sorted(
        SETTING_SPECS.items(),
        key=lambda kv: (_GROUP_ORDER.index(kv[1].group)
                        if kv[1].group in _GROUP_ORDER else len(_GROUP_ORDER),
                        kv[0]),
    )
    return {
        "version": SCHEMA_VERSION,
        "count": len(ordered),
        "groups": [g for g in _GROUP_ORDER
                   if any(s.group == g for s in SETTING_SPECS.values())],
        "settings": [spec.to_dict(key) for key, spec in ordered],
    }
