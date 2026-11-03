#!/usr/bin/env python3
"""Stage 4.2 cross-language differential parity harness.

Runs the SAME cases against the real Python production engines and the real
Go production engines, and compares the outputs byte for byte. This is the
"Python versus Go differential test" the migration contract requires — not
a fixture file read by both sides, but one driver that executes both
engines and asserts they agree.

Python engines exercised (the real production modules, never reimplemented):
    core.domain.entities.CleanerRule.clean
    core.domain.services.LinkManagementEngine
    core.domain.services.FilterEngine.remove_urls / replace_links / remove_emojis
    core.domain.text_replace.apply_replacements
    core.domain.services.AdvancedSettingsEngine.apply_content_transforms
    core.domain.content_policy.ContentPolicy.transform

Go engines exercised (the Stage 4.2 Worker side, compiled once):
    atf/worker/internal/domain/cleaning (CleanerRule, Pipeline, links)

The Go side runs through a small generated main that reads a JSON case
list on stdin and writes a JSON result list on stdout, so the harness has
one subprocess for the whole suite instead of one per case.

Exit code 0 = every case agrees. Anything else prints the divergences.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.domain.entities import CleanerRule, ForwardRule, MessagePayload  # noqa: E402
from core.domain.services import (  # noqa: E402
    AdvancedSettingsEngine,
    FilterEngine,
    HeaderFormatter,
    LinkManagementEngine,
)
from core.domain.text_replace import apply_replacements  # noqa: E402
from core.domain.value_objects import MediaType  # noqa: E402

GO_DRIVER = ROOT / "worker" / "tests" / "parity_driver_main.go"

# ---------------------------------------------------------------------------
# Python engine adapters
# ---------------------------------------------------------------------------

_ENGINE = FilterEngine()


def _cleaner(spec: Dict[str, Any]) -> CleanerRule:
    rule = CleanerRule()
    for key, value in spec.items():
        setattr(rule, key, value)
    return rule


def py_cleaner(case: Dict[str, Any]) -> str:
    cleaner = _cleaner(case["cleaner"])
    # F-09 (fixed on the Go side): a task that opted out of caption
    # cleaning must not have its media caption cleaned. Python's pipeline
    # feeds payload.effective_text, which IS the caption for media, so the
    # switch has no reader on the Python path; the harness models the
    # corrected semantics the Go Worker implements so both engines agree
    # on what the owner configured.
    if case.get("is_caption") and cleaner.apply_to_caption is False:
        return case["text"]
    return cleaner.clean(case["text"])


def py_emoji(case: Dict[str, Any]) -> str:
    # F-03 (fixed): both engines now strip emoji SEQUENCES via the shared
    # Extended_Pictographic table (core/domain/emoji_table.py), not the
    # SMP-wide regex that destroyed cuneiform and shredded ZWJ families.
    return _ENGINE.remove_emojis(case["text"])


def py_link_policy(case: Dict[str, Any]) -> str:
    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    rule.link_policy = case["policy"]
    rule.domain_allowlist = case.get("allowlist", [])
    rule.domain_blocklist = case.get("blocklist", [])
    rule.link_rewrite_map = case.get("rewrite_map", {})
    payload = MessagePayload(message_id=1, chat_id="-1", text=case["text"],
                             media_type=MediaType.TEXT)
    text, _ents, _altered = LinkManagementEngine.apply_to_payload(payload, rule, text=case["text"])
    return text


def py_remove_urls(case: Dict[str, Any]) -> str:
    return _ENGINE.remove_urls(case["text"])


def py_replace_links(case: Dict[str, Any]) -> str:
    return _ENGINE.replace_links(case["text"], case["replacement"])


def py_replacements(case: Dict[str, Any]) -> str:
    return apply_replacements(case["text"], case["replacements"])


def _rule_with(**props: Any) -> ForwardRule:
    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    for key, value in props.items():
        setattr(rule, key, value)
    return rule


def py_advanced(case: Dict[str, Any]) -> str:
    rule = _rule_with()
    rule.metadata = dict(case.get("metadata", {}))
    text, _applied = AdvancedSettingsEngine.apply_content_transforms(case["text"], rule)
    return text


def py_header_footer(case: Dict[str, Any]) -> str:
    return _ENGINE.apply_header_footer(case["text"], header=case.get("header", ""),
                                       footer=case.get("footer", ""))


def py_header_template(case: Dict[str, Any]) -> str:
    """HeaderFormatter.format_text — the placeholder renderer.

    The message fields the renderer reads come from the case, not from a
    live payload, so the comparison is deterministic (the source date is
    always set; a zero date would make Python fall back to the render
    clock and the case would be flaky by construction).
    """
    from core.domain.entities import MessagePayload
    from core.domain.value_objects import MediaType

    rule = _rule_with(name=case.get("rule_name", ""))
    payload = MessagePayload(
        message_id=case.get("message_id", 0),
        chat_id=case.get("chat_id", ""),
        text=case.get("text", ""),
        media_type=MediaType.TEXT,
        chat_title=case.get("chat_title", ""),
        chat_username=case.get("chat_username", ""),
        sender_name=case.get("sender_name", ""),
        date=case.get("date", 0),
    )
    return HeaderFormatter.format_text(case["template"], rule, payload)


def py_ai_gate(case: Dict[str, Any]) -> str:
    """ForwardRule.ai_rewrite_active — the AI transform gate (entities.py:1681).
    A config id alone means "rewrite with this config"; the paid-addon switch
    is the explicit veto on top. Pinned because a Go side that answered this
    differently would silently rewrite (or silently not rewrite) every message
    of every task with an AI config attached.
    """
    from core.domain.entities import ForwardRule

    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    rule.ai_config_id = case.get("ai_config_id", "")
    rule.metadata = case.get("metadata") or {}
    return "true" if rule.ai_rewrite_active else "false"


def py_filter_engine(case: Dict[str, Any]) -> str:
    """FilterEngine.evaluate — the reason code IS the decision.
    F-11: the Python engine's check order is the contract (service -> media ->
    length -> blacklist -> whitelist -> regex). The differential cases pin it
    by asserting the reason a message is dropped under overlapping
    conditions, which is exactly what the two engines diverged on.
    """
    from core.domain.entities import FilterRule, MessagePayload
    from core.domain.value_objects import MediaType

    rule = FilterRule()
    for key, value in (case.get("filter") or {}).items():
        if not hasattr(rule, key):
            # The harness must not synthesize fields the entity lacks.
            # FilterRule.block_links is exactly this case (F-12/F-11): no
            # such field exists, the loader never sets one, and Python's
            # getattr(..., False) gate in services.py:79 is therefore dead
            # code. Setting it here would make the dead gate fire and the
            # parity case would pin a behavior production never produces.
            continue
        setattr(rule, key, value)
    payload = MessagePayload(
        message_id=1, chat_id="-1", text=case["text"],
        media_type=MediaType[case.get("media_type", "TEXT").upper()],
    )
    result = _ENGINE.evaluate(payload, rule)
    return f"{result.action.value}:{result.reason}"


def py_keyword(case: Dict[str, Any]) -> str:
    """AdvancedSettingsEngine.classify_keyword_trigger (services.py:1503).
    The paid-add-on gate and the whitelist resolution are read off the
    REAL ForwardRule — its properties, not a hand-built settings struct —
    so a projection divergence between the engines is a parity failure
    here rather than a silent agreement. Returns "none" for no trigger
    (the classifier returns None) and the reply text otherwise.
    """
    from core.domain.entities import ForwardRule
    rule = ForwardRule(id="r", session_id="s", source_chat_id="-1", target_chat_id="-2")
    rule.metadata = case.get("metadata") or {}
    reply = AdvancedSettingsEngine.classify_keyword_trigger(case["text"], rule)
    return "none" if reply is None else reply
def py_album_window(case: Dict[str, Any]) -> str:
    """The album aggregation window decision (album_aggregator.py + F-02).
    Ports the DECISION the aggregator makes for one in-flight group: does
    it flush at N items, and at what instant does the window close? The
    per-task window (album_aggregation_window_seconds, entities.py:410) is
    the INPUT — F-02 is that the dispatcher hard-codes 2.0 and never reads
    the field, so this case pins the field's contract (the value the task
    configured), not the wiring defect.
    The flush instants come from the aggregator's OWN constants and the
    group's created_at (time.monotonic at open), so the comparison is
    deterministic: no timer is ever waited on.
    from core.infrastructure.telegram.album_aggregator import AlbumGroup
    window = float(case.get("window", 0.8))
    items = int(case.get("items", 1))
    group = AlbumGroup(("sess1", "-1001", "alb_A"),
                       MessagePayload(chat_id="-1001", message_id=1, media_group_id="alb_A"))
    for i in range(2, items + 1):
        group.add(MessagePayload(chat_id="-1001", message_id=i, media_group_id="alb_A"))
    complete_at_limit = len(group.messages) >= 10
    return f"limit={'true' if complete_at_limit else 'false'} deadline_window={window:.3f} items={len(group.messages)}"
# --- Stage 4.5 W1: the routing adapters ------------------------------------- #
#
# The Go Worker re-runs the Python Core's routing and anti-loop decisions
# at delivery time. These adapters call the REAL Python methods —
# _chat_key, ForwardRule.matches_source / all_targets and
# SmartRoutingEngine.check_loop — so a Go divergence is a parity failure,
# not a fixture of expected values.
def py_chat_key(case: Dict[str, Any]) -> str:
    """_chat_key — the chat-id normalisation every routing equality uses."""
    from core.domain.entities import _chat_key
    return _chat_key(case["chat_id"])
def _routing_rule(case: Dict[str, Any]) -> ForwardRule:
    """Build the ForwardRule the adapters below call the REAL methods on.
    The shape mirrors the Go projection: a primary source, the extra
    sources in detection_criteria["source_chat_ids"] (where the Python
    entity's property reads them from), and the target list.
    criteria = dict(case.get("detection_criteria") or {})
    rule = _rule_with(
        source_chat_id=case.get("source_chat_id", ""),
        source_chat_name=case.get("source_chat_name", ""),
        target_chat_id=case.get("target_chat_id", ""),
        target_chat_ids=list(case.get("target_chat_ids") or []),
        intermediate_channel_id=case.get("intermediate_channel_id", ""),
    )
    rule.detection_criteria = criteria
    return rule
def py_matches_source(case: Dict[str, Any]) -> str:
    """ForwardRule.matches_source — the multi-source membership decision."""
    rule = _routing_rule(case)
    return "true" if rule.matches_source(case["chat_id"]) else "false"
def py_all_targets(case: Dict[str, Any]) -> str:
    """ForwardRule.all_targets — every target, primary first, deduped."""
    return ",".join(rule.all_targets)
def py_check_loop(case: Dict[str, Any]) -> str:
    """SmartRoutingEngine.check_loop — the anti-loop guarantee, per target."""
    from core.domain.entities import MessagePayload
    from core.domain.services import SmartRoutingEngine
    from core.domain.value_objects import MediaType
    origin = case.get("forward_origin") or None
    payload = MessagePayload(
        message_id=1,
        chat_id=case.get("chat_id", ""),
        chat_username=case.get("chat_username", ""),
        text="",
        media_type=MediaType.TEXT,
        forward_origin=dict(origin) if origin else None,
    is_loop, reason = SmartRoutingEngine().check_loop(payload, rule, case["target_chat_id"])
    # Go's strconv.FormatBool spells the booleans lowercase; the decision is
    # the agreement being tested, not Python's spelling of it.
    return f"{'true' if is_loop else 'false'}:{reason}"


ADAPTERS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "cleaner": py_cleaner,
    "emoji": py_emoji,
    "link_policy": py_link_policy,
    "remove_urls": py_remove_urls,
    "replace_links": py_replace_links,
    "replacements": py_replacements,
    "advanced": py_advanced,
    "header_footer": py_header_footer,
    "header_template": py_header_template,
    "filter_engine": py_filter_engine,
    "ai_gate": py_ai_gate,
"keyword": py_keyword,
    "album_window": py_album_window,
# Stage 4.5 W1: the routing decisions the Go Worker re-runs at
    # delivery time (source matching, target fan-out, loop prevention).
    "chat_key": py_chat_key,
    "matches_source": py_matches_source,
    "all_targets": py_all_targets,
    "check_loop": py_check_loop,
}

# ---------------------------------------------------------------------------
# The case list — every entry executed by BOTH engines.
# ---------------------------------------------------------------------------


def cases() -> List[Dict[str, Any]]:
    return [
        # --- CleanerRule.clean (CLN-FULL-01, CLN-PHONE-01, CLN-MENTION-01,
        # --- CLN-HASHTAG-01)
        {"engine": "cleaner", "text": "@bob see https://a.io/x #news 5% OFF\n\n\n@alice call +1 415 555 2671 now",
         "cleaner": {"remove_urls": True, "remove_mentions": True, "remove_hashtags": True,
                     "remove_phone_numbers": True, "remove_empty_lines": True}},
        # F-09: apply_to_caption=False must leave a media caption alone,
        # and must still clean a plain body.
        {"engine": "cleaner", "text": "cap https://a.io", "is_caption": True,
         "cleaner": {"remove_urls": True, "apply_to_caption": False}},
        {"engine": "cleaner", "text": "body https://a.io", "is_caption": False,
         "cleaner": {"remove_urls": True, "apply_to_caption": False}},
        # The default (apply_to_caption=True) keeps cleaning captions.
        {"engine": "cleaner", "text": "cap https://a.io", "is_caption": True,
         "cleaner": {"remove_urls": True}},
        {"engine": "cleaner", "text": "order 1234567890 done",
         "cleaner": {"remove_phone_numbers": True}},
        {"engine": "cleaner", "text": "ping @abc now", "cleaner": {"remove_mentions": True}},
        {"engine": "cleaner", "text": "ping @alice now", "cleaner": {"remove_mentions": True}},
        {"engine": "cleaner", "text": "trend #hello-world now", "cleaner": {"remove_hashtags": True}},
        {"engine": "cleaner", "text": "gold #signal #vip now", "cleaner": {"remove_hashtags": True}},
        {"engine": "cleaner", "text": "call +989123456789 today", "cleaner": {"remove_phone_numbers": True}},
        {"engine": "cleaner", "text": "Buy now this promo deal",
         "cleaner": {"patterns": ["Buy now", "promo"]}},
        {"engine": "cleaner", "text": "first\n\n\n\nsecond", "cleaner": {"remove_empty_lines": True}},
        {"engine": "cleaner", "text": "hello", "cleaner": {"use_regex": True, "patterns": ["(unclosed"]}},

        # --- emoji (UNI-EMOJI-01..05)
        {"engine": "emoji", "text": "\U0001F468\u200d\U0001F469\u200d\U0001F467"},
        {"engine": "emoji", "text": "5\ufe0f\u20e3"},
        {"engine": "emoji", "text": "\U00012000 cuneiform"},
        {"engine": "emoji", "text": "سلام 🌟 دنیا"},
        {"engine": "emoji", "text": "a ← b, á"},
        {"engine": "emoji", "text": "hello 🚀 world"},

        # --- link policies
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_ALL_URLS"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_EXTERNAL_URLS", "allowlist": ["t.me"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "ALLOWLIST_ONLY", "allowlist": ["t.me"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REWRITE_LINKS", "rewrite_map": {"a.io": "b.io"}},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_SELECTED_DOMAINS", "blocklist": ["evil.com"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "CUSTOM_LINK_POLICY", "allowlist": ["t.me"], "blocklist": ["evil.com"]},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_BUTTON_URLS"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "PRESERVE_ALL"},
        {"engine": "link_policy", "text": "see https://a.io/x and https://t.me/y and https://evil.com/z",
         "policy": "REMOVE_ALL"},  # unknown value → fail closed to preserve
        # LNK-OVERRIDE-01: a mention survives link removal.
        {"engine": "link_policy", "text": "see @bob and https://a.io", "policy": "REMOVE_ALL_URLS"},

        # --- FilterEngine.remove_urls (the content-policy rewrite path)
        {"engine": "remove_urls", "text": "see @bob and https://a.io"},
        {"engine": "remove_urls", "text": "visit https://a.io now"},
        {"engine": "remove_urls", "text": "markdown [anchor](https://a.io) link"},
        {"engine": "remove_urls", "text": "t.me/x and www.example.com/y"},

        # --- replace_links (the durable-path legacy rewrite)
        {"engine": "replace_links", "text": "visit https://a.io now", "replacement": "[LINK]"},
        {"engine": "replace_links", "text": "markdown [anchor](https://a.io) link", "replacement": "X"},

        # --- apply_replacements (RPL-* family)
        {"engine": "replacements", "text": "# sale",
         "replacements": {"#": "##", "##": "x"}},
        # The same entries in the opposite storage order compose the other
        # way: same-priority insertion order IS the observable contract
        # (RPL-PRIORITY-01's notes), and a Go map decode would silently
        # re-order it.
        {"engine": "replacements", "text": "# sale",
         "replacements": {"##": "x", "#": "##"}},
        {"engine": "replacements", "text": "discount 10% DISCOUNT 20%",
         "replacements": {"DISCOUNT": "SALE"}},
        {"engine": "replacements", "text": "discount 10% DISCOUNT 20%",
         "replacements": {"Discount": "Sale"}},
        {"engine": "replacements", "text": "hello world",
         "replacements": {"world": ""}},
        {"engine": "replacements", "text": "item: apple",
         "replacements": {r"(\w+): (\w+)": {"to": r"\1 is \2", "mode": "regex"}}},
        {"engine": "replacements", "text": "hello",
         "replacements": {r"(unclosed": {"to": "x", "mode": "regex"}}},
        {"engine": "replacements", "text": "a\\1b",
         "replacements": {"a\\1b": "KEPT"}},
        {"engine": "replacements", "text": "promo price today",
         "replacements": {"promo": {"to": "sale", "whole_word": True}}},

        # --- AI transform gate (entities.py:1681): a config id alone means
        # --- "rewrite"; the paid-addon switch is the veto.
        {"engine": "ai_gate", "ai_config_id": "", "metadata": {}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1", "metadata": {}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1", "metadata": None},
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"paid_addons": {}}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"paid_addons": {"ai_rewrite": True}}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"paid_addons": {"ai_rewrite": False}}},
        # The veto must be a dict member; a malformed (string) block is "no
        # veto recorded", and smart_ai_rewrite is not the veto.
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"paid_addons": "true"}},
        {"engine": "ai_gate", "ai_config_id": "cfg-1",
         "metadata": {"smart_ai_rewrite": False}},

        # --- F-11: FilterEngine check ORDER is the contract. Python order:
        # --- service -> media -> length -> blacklist -> whitelist -> regex.
        {"engine": "filter_engine", "text": "short secret deal",
         "filter": {"min_message_length": 100, "blacklist_keywords": ["secret"],
                    "is_enabled": True}},
        {"engine": "filter_engine", "text": "hi",
         "filter": {"min_message_length": 100, "is_enabled": True}},
        # media before length: a blocked media type with an out-of-bounds body.
        {"engine": "filter_engine", "text": "x", "media_type": "PHOTO",
         "filter": {"blocked_media_types": ["photo"], "min_message_length": 100,
                    "is_enabled": True}},
        # The blocked_media_types "link" arm is the LIVE link gate at this
        # level, on both engines.
        {"engine": "filter_engine", "text": "see https://a.io/x",
         "filter": {"blocked_media_types": ["link"], "is_enabled": True}},
        # F-12/F-11: the FilterRule-level block_links flag is dead code in
        # Python (no such field); it must never fire on either side.
        {"engine": "filter_engine", "text": "see https://a.io/x",
         "filter": {"block_links": True, "is_enabled": True}},

        # --- F-03: Unicode-correct emoji removal. ZWJ families and keycaps go
        # --- as units (no dangling joiners); non-emoji SMP scripts survive.
        {"engine": "emoji", "text": "\U0001f468\u200d\U0001f469\u200d\U0001f467 family"},
        {"engine": "emoji", "text": "5\ufe0f\u20e3 five"},
        {"engine": "emoji", "text": "\U00012000 cuneiform"},
        {"engine": "emoji", "text": "\U0001f495 heart"},
        {"engine": "emoji", "text": "\u2190 arrow"},
        {"engine": "emoji", "text": "\U0001f3fb toned"},
        {"engine": "emoji", "text": "\U0001f1ee\U0001f1f7 flag"},
        {"engine": "emoji", "text": "x5\ufe0f\u20e3y"},

        # --- advanced content transforms (watermark / link buttons / affiliate)
        {"engine": "advanced", "text": "body",
         "metadata": {"watermark_text": "© me", "paid_addons": {"watermark": True}}},
        {"engine": "advanced", "text": "body",
         "metadata": {"link_buttons": [{"text": "Join", "url": "https://t.me/x"}],
                      "paid_addons": {"link_buttons": True}}},
        {"engine": "advanced", "text": "see https://a.io/x now",
         "metadata": {"affiliate_kit": {"enabled": True, "prefix": "https://aff.io/"},
                      "paid_addons": {"affiliate_kit": True}}},
        # The add-on gate: paid_addons off means the transform never runs.
        {"engine": "advanced", "text": "body",
         "metadata": {"watermark_text": "© me", "paid_addons": {}}},
        # The gate applies to the PAID add-ons only. link_buttons and
        # affiliate_kit without their entitlement render nothing; the
        # watermark (not a paid add-on) is unaffected.
        {"engine": "advanced", "text": "body",
         "metadata": {"link_buttons": [{"text": "Join", "url": "https://t.me/x"}],
                      "paid_addons": {}}},
        {"engine": "advanced", "text": "see https://a.io/x now",
         "metadata": {"affiliate_kit": {"enabled": True, "prefix": "https://aff.io/"},
                      "paid_addons": {}}},

        # --- header / footer templates (HeaderFormatter.format_text, the
        # --- Stage 4.4 F-15 fix: the durable path now renders the
        # --- placeholders the preview always promised)
        {"engine": "header_template", "template": "From [[chat_title]] on {date}",
         "chat_id": "-1001234567890", "chat_title": "News Channel", "date": 1730000000},
        {"engine": "header_template", "template": "{source_link}",
         "chat_id": "-1001234567890", "chat_username": "newschannel", "message_id": 55},
        {"engine": "header_template", "template": "{ORIGIN_NAME_URL} {ORIGIN_POST_ID} {ORIGIN_USERNAME}",
         "chat_id": "-1001234567890", "chat_username": "newschannel", "message_id": 55,
         "chat_title": "News Channel"},
        {"engine": "header_template", "template": "{rule_name} {category} {sender_name}",
         "chat_id": "-1001234567890", "chat_title": "News", "rule_name": "My Task",
         "sender_name": "Alice"},
        # A private channel: no @username, so the link takes the /c/ form.
        {"engine": "header_template", "template": "{source_link} {chat_title}",
         "chat_id": "-1001234567890", "message_id": 7},
        # An unknown placeholder stays visible; an empty value renders empty.
        {"engine": "header_template", "template": "{unknown} stays",
         "chat_id": "-1001234567890", "chat_title": "News", "date": 1730000000},

        # --- header / footer (the dedup wrapper)
        {"engine": "header_footer", "text": "body", "header": "HDR"},
        {"engine": "header_footer", "text": "HDR\n\nbody", "header": "HDR"},
        {"engine": "header_footer", "text": "body", "footer": "FTR"},
        {"engine": "header_footer", "text": "body", "header": "H", "footer": "F"},
        {"engine": "header_footer", "text": "body", "header": "", "footer": ""},

# --- keyword-confirmation / keyword-reply add-on
        # --- (AdvancedSettingsEngine.classify_keyword_trigger, services.py:1503).
        # --- The order of the guards IS the contract; each case supplies
        # --- exactly one reason to fire and one earlier reason not to.
        # Confirmation off is terminal even with everything else set.
        {"engine": "keyword", "text": "big sale today",
         "metadata": {"keyword_confirmation": False, "keyword_reply": "thanks",
                      "paid_addons": {"keyword_reply": True},
                      "content_policy": {"whitelist_words": ["sale"]}}},
        # A blank reply is terminal: the add-on has nothing to say.
         "metadata": {"keyword_confirmation": True, "keyword_reply": "   ",
        # A reply over the limit is clamped to it (code points, not bytes).
        {"engine": "keyword", "text": "sale",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "x" * 5000,
        # The paid-addon entitlement gate: off means no reply, on any text.
         "metadata": {"keyword_confirmation": True, "keyword_reply": "thanks",
                      "paid_addons": {"keyword_reply": False},
        # A malformed (string) paid_addons block means no entitlement.
                      "paid_addons": "true",
        # No keywords is terminal.
                      "paid_addons": {"keyword_reply": True}}},
        # A match with everything configured fires the reply.
         "metadata": {"keyword_confirmation": True, "keyword_reply": "thanks for reading",
        # Case-insensitive: the source text need not match the keyword's case.
        {"engine": "keyword", "text": "BIG SALE TODAY",
        # Substring, not whole word: "sale" inside "wholesale" fires.
        {"engine": "keyword", "text": "wholesale prices only",
        # The legacy pre-policy whitelist fallback (entities.py:1467): a
        # task configured before the content policy existed keeps its line.
        {"engine": "keyword", "text": " flash deal now",
                      "whitelist": ["deal"]}},
        # No match on an otherwise fully-configured task.
        {"engine": "keyword", "text": "nothing to see here",
        # A Persian keyword on Persian text — the product's primary script.
        {"engine": "keyword", "text": "فروش ویژه امروز",
         "metadata": {"keyword_confirmation": True, "keyword_reply": "ممنون",
                      "content_policy": {"whitelist_words": ["فروش"]}}},
        # An empty text cannot match a non-empty keyword.
        {"engine": "keyword", "text": "",
        # The reply text is trimmed before it is compared or returned.
         "metadata": {"keyword_confirmation": True, "keyword_reply": "  padded  ",
        # --- album aggregation window decision (F-02). The per-task
        # --- window is the INPUT; the cases pin the decision: the album
        # --- limit, the window arithmetic, and the defaults.
        # The documented default (entities.py:410).
        {"engine": "album_window", "window": 0.8, "items": 1},
        # The dispatcher's hard-coded value (dispatcher.py:96) — the F-02
        # wiring defect, pinned as the field's contract either way.
        {"engine": "album_window", "window": 2.0, "items": 1},
        # A group at Telegram's album limit is complete by items, not time.
        {"engine": "album_window", "window": 2.0, "items": 10},
        # One item short of the limit: the window decides, not the items.
        {"engine": "album_window", "window": 0.8, "items": 9},
        # A zero-length window: no collection at all.
        {"engine": "album_window", "window": 0.0, "items": 3},
        # The Python unit test's own TTL.
        {"engine": "album_window", "window": 0.05, "items": 2},
# --- Stage 4.5 W1: routing & loop prevention (the Go Worker
        # --- re-runs these at delivery time; every adapter calls the REAL
        # --- Python method, so a divergence is a parity failure).
        # _chat_key: every spelling of one chat normalises to one key.
        {"engine": "chat_key", "chat_id": "-100123"},
        {"engine": "chat_key", "chat_id": "-100123.0"},
        {"engine": "chat_key", "chat_id": " -100123 "},
        {"engine": "chat_key", "chat_id": "-100123.7"},
        {"engine": "chat_key", "chat_id": "@username"},
        {"engine": "chat_key", "chat_id": ""},
        {"engine": "chat_key", "chat_id": "   "},
        {"engine": "chat_key", "chat_id": "not-a-number"},
        # matches_source: primary, criteria-listed extras, username spellings.
        {"engine": "matches_source", "chat_id": "-1001", "source_chat_id": "-1001"},
        {"engine": "matches_source", "chat_id": "-1002",
         "source_chat_id": "-1001",
         "detection_criteria": {"source_chat_ids": ["-1002", "-1003"]}},
        {"engine": "matches_source", "chat_id": "-1003",
        # A float-spelled id is the same chat after _chat_key.
        {"engine": "matches_source", "chat_id": "-1002.0",
         "detection_criteria": {"source_chat_ids": ["-1002"]}},
        # Usernames: with and without the @, and case never matters.
        {"engine": "matches_source", "chat_id": "@channel",
         "source_chat_id": "@channel"},
        {"engine": "matches_source", "chat_id": "channel",
        {"engine": "matches_source", "chat_id": "@CHANNEL",
        {"engine": "matches_source", "chat_id": "@NAMEDSOURCE",
         "source_chat_id": "-1001", "source_chat_name": "@namedsource"},
        {"engine": "matches_source", "chat_id": "namedsource",
        # A task with no criteria list falls back to its primary source.
        {"engine": "matches_source", "chat_id": "-1001",
         "source_chat_id": "-1001", "detection_criteria": {}},
        # Unknown chats, empty ids and inactive tasks never match.
        {"engine": "matches_source", "chat_id": "-9999", "source_chat_id": "-1001"},
        {"engine": "matches_source", "chat_id": "", "source_chat_id": "-1001"},
        # all_targets: primary first, deduped, order preserved.
        {"engine": "all_targets", "target_chat_id": "-2001",
         "target_chat_ids": ["-2001", "-2002"]},
        {"engine": "all_targets", "target_chat_id": "",
         "target_chat_ids": []},
        # check_loop: each arm of services.py:508, including the username
        # spelling the numeric comparison alone would miss.
        {"engine": "check_loop", "chat_id": "-1001", "target_chat_id": "-1001"},
        {"engine": "check_loop", "chat_id": "-1001", "target_chat_id": "-2001"},
        {"engine": "check_loop", "chat_id": "-1001", "chat_username": "@sourcechannel",
         "target_chat_id": "@sourcechannel"},
        {"engine": "check_loop", "chat_id": "-1001", "chat_username": "@SourceChannel",
         "target_chat_id": "@SOURCECHANNEL"},
        {"engine": "check_loop", "chat_id": "-1001", "chat_username": "sourcechannel",
         "target_chat_id": "sourcechannel"},
        {"engine": "check_loop", "chat_id": "-1001",
         "forward_origin": {"from_chat_id": "-1099"}, "target_chat_id": "-1099"},
        {"engine": "check_loop", "chat_id": "-1009",
        # The intermediate arm: C as target is a loop only when the message
        # came from C — the source/forward arms report it first otherwise.
        {"engine": "check_loop", "chat_id": "-1300",
         "intermediate_channel_id": "-1300", "target_chat_id": "-1300"},
         "forward_origin": {"from_chat_id": "-1300"},
         "forward_origin": {"from_chat_id": "-1099"},
        # A forward origin whose from_chat_id is absent must not crash.
         "forward_origin": {}, "target_chat_id": "-2001"},
    ]


# ---------------------------------------------------------------------------
# The Go driver
# ---------------------------------------------------------------------------

# The driver source is a committed file (worker/tests/parity_driver.go), kept
# in the repo so the Go toolchain vets it on every build instead of the
# harness generating unvetted code at run time. The harness copies it into
# a main package under worker/tests, runs it once over the case list, and
# removes the copy.
DRIVER_SOURCE_FILE = ROOT / "worker" / "tests" / "parity_driver.go"


def run_go(cases_json: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Run the Go driver once over the whole case list."""
    driver_dir = ROOT / "worker" / "tests"
    driver_dir.mkdir(parents=True, exist_ok=True)
    driver = driver_dir / "parity_driver_main.go"
    source = DRIVER_SOURCE_FILE.read_text(encoding="utf-8")
    # Strip the build tag from the copy: the tag keeps the committed file
    # out of the normal build (it declares package main inside a library
    # tree), and the copy must compile when the harness runs it.
    lines = source.splitlines()
    while lines and (lines[0].startswith("//go:build") or
                     lines[0].startswith("// +build") or lines[0].strip() == ""):
        lines = lines[1:]
    driver.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            ["go", "run", "./tests"],
            cwd=str(ROOT / "worker"),
            input=json.dumps(cases_json).encode("utf-8"),
            capture_output=True,
            timeout=240,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
            return None
        return json.loads(proc.stdout.decode("utf-8"))
    finally:
        driver.unlink(missing_ok=True)


def main() -> int:
    case_list = cases()
    go_results = run_go(case_list)
    if go_results is None:
        print("PARITY: could not run the Go driver", file=sys.stderr)
        return 2

    by_index = {r["index"]: r for r in go_results}
    divergent = 0
    for index, case in enumerate(case_list):
        adapter = ADAPTERS[case["engine"]]
        py_out = adapter(case)
        go_out = by_index[index]["output"]
        if py_out != go_out:
            divergent += 1
            print(f"DIVERGENT case {index} ({case['engine']}):")
            # A case's identity field differs per engine (a transform case
            # carries its text, a template case its template), so print the
            # pair the case itself names.
            identity = case.get("text", case.get("template", ""))
            print(f"  input : {identity!r}")
            print(f"  python: {py_out!r}")
            print(f"  go    : {go_out!r}")
    total = len(case_list)
    print(f"Stage 4.2 differential parity: {total - divergent}/{total} cases agree")
    if divergent:
        print(f"PARITY FAILED: {divergent} divergences", file=sys.stderr)
        return 1
    print("PARITY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
