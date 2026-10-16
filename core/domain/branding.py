"""Branding hop (A -> C -> B): per-task configuration and its trigger decision.

A task can deliver its matched messages *through* one of our own channels (C)
before the final target (B) sees them: the message is copied into C first (so C
looks like the publisher) and only then forwarded from C to B, which makes B's
forward header name C instead of the original source channel. That is the point
of the feature — branding channel C.

This module owns exactly two things:

* **where the configuration lives** — ``metadata["branding_hop"]``, a JSON blob,
  so it needs no schema migration and every existing path (create/update,
  backup/restore, gRPC, REST, bot, mobile) carries it for free, and
* **whether one message should take the hop** — :func:`should_hop`.

The decision is deliberately a single shared function: the live dispatcher, the
durable queue, the dry-run simulation and the bot preview all call it, so what
an operator is shown is what the engine will actually do.

Trigger semantics (all optional; *no* configured trigger means "always hop"):

* ``origin_ids`` / ``origin_titles`` match the **forward origin channel** of the
  incoming message (Telegram's "Forwarded from <channel>" header — the channel
  name, not text inside the message body).
* ``keywords`` match the message text.
* ``match_mode`` picks ``any`` (at least one group matches) or ``all``.
* ``require_forward`` is a *gate*, not a check: it refuses a message that is not
  itself a forward, regardless of match_mode.
* ``force_forward`` (default true) is what makes stage 2 a native forward from
  C, which is the only way B can show C in its header.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "BRANDING_META_KEY",
    "MATCH_MODES",
    "DEFAULTS",
    "read_config",
    "write_config",
    "channel_id",
    "enabled",
    "force_forward",
    "match_origin",
    "match_keywords",
    "should_hop",
]

BRANDING_META_KEY = "branding_hop"

MATCH_MODES = ("any", "all")

#: Legacy spelling of "route through C" that older rules/clients still carry.
_LEGACY_HOP_MODES = ("VIA_INTERMEDIATE", "MASK_ORIGIN")

DEFAULTS: Dict[str, Any] = {
    # When False the hop is unconditional: every matched message goes A -> C -> B.
    "triggers_enabled": False,
    "origin_ids": [],
    "origin_titles": [],
    "keywords": [],
    "match_mode": "any",
    "require_forward": False,
    "force_forward": True,
}

_LIST_KEYS = ("origin_ids", "origin_titles", "keywords")
_BOOL_KEYS = ("triggers_enabled", "require_forward", "force_forward")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _as_str_list(value: Any) -> List[str]:
    """Coerce anything list-ish into a de-duplicated list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    try:
        items = list(value)
    except TypeError:
        items = [value]
    out: List[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def read_config(rule: Any) -> Dict[str, Any]:
    """Normalised branding config for ``rule`` — never None, never shared."""
    meta = getattr(rule, "metadata", None)
    raw = meta.get(BRANDING_META_KEY) if isinstance(meta, dict) else None
    cfg: Dict[str, Any] = {key: (list(val) if isinstance(val, list) else val)
                           for key, val in DEFAULTS.items()}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in DEFAULTS:
                cfg[key] = value
    for key in _LIST_KEYS:
        cfg[key] = _as_str_list(cfg[key])
    for key in _BOOL_KEYS:
        cfg[key] = bool(cfg[key])
    mode = str(cfg.get("match_mode") or "any").strip().lower()
    cfg["match_mode"] = mode if mode in MATCH_MODES else "any"
    return cfg


def write_config(rule: Any, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge ``values`` into the stored config and return the new state."""
    cfg = read_config(rule)
    for key, value in (values or {}).items():
        if key in _LIST_KEYS:
            cfg[key] = _as_str_list(value)
        elif key in _BOOL_KEYS:
            cfg[key] = bool(value)
        elif key == "match_mode":
            mode = str(value or "any").strip().lower()
            cfg[key] = mode if mode in MATCH_MODES else "any"
    if not isinstance(getattr(rule, "metadata", None), dict):
        rule.metadata = {}
    rule.metadata[BRANDING_META_KEY] = cfg
    return cfg


# --------------------------------------------------------------------------- #
# Enablement
# --------------------------------------------------------------------------- #
def channel_id(rule: Any) -> str:
    """The configured branding channel C (numeric id, @username or link)."""
    return str(getattr(rule, "intermediate_channel_id", "") or "").strip()


def enabled(rule: Any) -> bool:
    """True when this task routes through C: an enable flag *and* a channel.

    Both spellings count: the first-class ``use_intermediate`` flag (what the
    dashboard, bot and mobile write) and the legacy ``intermediate_mode`` value
    (``VIA_INTERMEDIATE``/``MASK_ORIGIN``) that older rules carry. Without this,
    a rule that was switched on through one spelling silently delivered
    directly — the hop looked configured but never happened.
    """
    if not channel_id(rule):
        return False
    if bool(getattr(rule, "use_intermediate", False)):
        return True
    mode = str(getattr(rule, "intermediate_mode", "NONE") or "NONE").upper()
    return mode in _LEGACY_HOP_MODES


def force_forward(rule: Any) -> bool:
    """Whether stage 2 must be a native forward from C (the branded header)."""
    return bool(read_config(rule)["force_forward"])


# --------------------------------------------------------------------------- #
# Trigger matching
# --------------------------------------------------------------------------- #
def _text_of(payload: Any) -> str:
    """Message text, preferring the effective (post-processing) text."""
    for attr in ("effective_text", "text", "raw_text", "caption"):
        value = getattr(payload, attr, None)
        if value:
            return str(value)
    return ""


def _origin_of(payload: Any) -> Dict[str, Any]:
    origin = getattr(payload, "forward_origin", None)
    return origin if isinstance(origin, dict) else {}


def match_origin(payload: Any, origin_ids: List[str], origin_titles: List[str]) -> bool:
    """Match the *forward origin channel* (Telegram's "Forwarded from" header).

    Compares the origin chat id, its @username, and its channel **name/title**
    (substring, case-insensitive) — the operator names channels, not ids.
    """
    origin = _origin_of(payload)
    if not origin:
        return False
    fwd_id = str(origin.get("from_chat_id") or "").strip()
    fwd_username = str(origin.get("from_chat_username") or "").strip().lstrip("@").lower()
    fwd_title = str(origin.get("from_chat_title") or "").strip().lower()

    for candidate in origin_ids:
        wanted = str(candidate).strip()
        if not wanted:
            continue
        if fwd_id == wanted or (fwd_username and fwd_username == wanted.lstrip("@").lower()):
            return True
    for candidate in origin_titles:
        wanted = str(candidate).strip().lower()
        if wanted and (wanted in fwd_title or fwd_title in wanted):
            return True
    return False


def match_keywords(text: str, keywords: List[str]) -> bool:
    """True when any keyword appears in ``text``.

    Folding is delegated to the content engine's :func:`fold_text` so a keyword
    written with an Arabic yeh/kaf matches the Persian spelling in the message
    (and harakat, tatweel and zero-width joiners are ignored) — exactly the same
    equivalence the content rules already apply. Imported lazily: content_policy
    imports the entity module, which imports this one.
    """
    if not text:
        return False
    try:
        from .content_policy import fold_text  # local import breaks the cycle
    except Exception:  # pragma: no cover - content_policy is always importable
        fold_text = lambda value: str(value).casefold()  # noqa: E731
    folded = fold_text(text)
    for word in keywords:
        needle = str(word).strip()
        if needle and fold_text(needle) in folded:
            return True
    return False


def should_hop(
    payload: Any,
    rule: Any,
    vip: Tuple[bool, str] = (False, ""),
    vip_configured: bool = False,
) -> Tuple[bool, str]:
    """Decide whether *this* message takes the branding hop.

    Returns ``(take_hop, reason)``. ``vip``/``vip_configured`` are the optional
    result of the task's own VIP-origin criteria (``message_category=VIP`` or
    explicit origin filters), so a rule that already declared "only VIP" keeps
    gating the hop exactly as before without registering a second trigger set.
    """
    if not channel_id(rule):
        return False, "branding_disabled:no_channel"
    if not enabled(rule):
        return False, "branding_disabled:flag_off"

    cfg = read_config(rule)
    if not cfg["triggers_enabled"]:
        # Legacy behaviour, preserved: a task with VIP criteria hops only for a
        # message that matched them; without criteria the hop is unconditional.
        if vip_configured:
            if vip and vip[0]:
                return True, f"branding_vip_matched:{vip[1]}"
            return False, f"branding_vip_unmatched:{vip[1] if vip else 'no_match'}"
        return True, "branding_always"

    # A "require" is a gate and must hold on its own, whatever match_mode says.
    if cfg["require_forward"] and not _origin_of(payload):
        return False, "branding_requires_forward_origin"

    conditions: List[str] = []
    matched: List[str] = []
    if cfg["origin_ids"] or cfg["origin_titles"]:
        conditions.append("origin")
        if match_origin(payload, cfg["origin_ids"], cfg["origin_titles"]):
            matched.append("origin")
    if cfg["keywords"]:
        conditions.append("keywords")
        if match_keywords(_text_of(payload), cfg["keywords"]):
            matched.append("keywords")
    if vip_configured:
        conditions.append("vip")
        if vip and vip[0]:
            matched.append("vip")

    if not conditions:
        # Triggers were switched on but nothing was configured to test.
        return True, "branding_no_triggers_configured"

    satisfied = len(matched) == len(conditions) if cfg["match_mode"] == "all" else bool(matched)
    if satisfied:
        return True, "branding_triggers_matched:" + ",".join(matched)
    detail = ",".join(matched) if matched else "none"
    return False, "branding_triggers_unmatched:" + detail
