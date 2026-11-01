""" branding decision fixtures: shared between the live Python
``core.domain.branding`` implementation and the Go Worker's port.

Each fixture is one (message, rule, vip, vip_configured) tuple in the exact
shape :func:`core.domain.branding.should_hop` and
:class:`core.domain.services.SmartRoutingEngine` accept, plus the decision
those implementations produce. The Go harness (worker/internal/parity) reads
the same JSON, rebuilds the same inputs, and asserts its own
``branding.ShouldHop`` reproduces the same ``(take_hop, reason)`` pair.

Adding a case here means adding it to BOTH engines: the Python reference is
regenerated with ``scripts/regen_parity_reference.sh`` and CI diffs it, so a
Go port that drifts fails the build instead of silently routing differently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.domain import branding  # noqa: E402
from core.domain.entities import ForwardRule, MessagePayload  # noqa: E402
from core.domain.services import SmartRoutingEngine  # noqa: E402

ROUTING = SmartRoutingEngine()


def _rule(**over) -> ForwardRule:
    """A rule with the fixture's fields set, defaults elsewhere."""
    base = dict(
        id="rule-1",
        session_id="s1",
        source_chat_id="-100111",
        target_chat_id="-100222",
        intermediate_channel_id="",
        use_intermediate=False,
        intermediate_mode="NONE",
        message_category="ALL",
        detection_criteria={},
        metadata={},
        content_mode="AUTO",
        forward_mode="COPY_MESSAGE",
    )
    base.update(over)
    rule = ForwardRule(**{k: v for k, v in base.items() if hasattr(ForwardRule, k) or True})
    # ForwardRule is a dataclass; accept any kwarg by constructing permissive.
    for key, value in over.items():
        if not hasattr(rule, key):
            object.__setattr__(rule, key, value)
    return rule


def _payload(**over) -> MessagePayload:
    base = dict(
        chat_id="-100111",
        message_id=42,
        text="",
        has_media=False,
    )
    base.update(over)
    return MessagePayload(**base)


def case_disabled_no_channel() -> dict:
    rule = _rule()
    payload = _payload(text="hello")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "disabled_no_channel", "take_hop": take, "reason": reason}


def case_disabled_flag_off() -> dict:
    rule = _rule(intermediate_channel_id="-100999")
    payload = _payload(text="hello")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "disabled_flag_off", "take_hop": take, "reason": reason}


def case_flag_via_legacy_mode() -> dict:
    rule = _rule(intermediate_channel_id="-100999", intermediate_mode="VIA_INTERMEDIATE")
    payload = _payload(text="hello")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "flag_via_legacy_mode", "take_hop": take, "reason": reason}


def case_always() -> dict:
    rule = _rule(intermediate_channel_id="-100999", use_intermediate=True)
    payload = _payload(text="hello")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "always", "take_hop": take, "reason": reason}


def case_keyword_match() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["offered", "price"], "match_mode": "any",
        }},
    )
    payload = _payload(text="New product offered today")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "keyword_match", "take_hop": take, "reason": reason}


def case_keyword_miss() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["offered"], "match_mode": "any",
        }},
    )
    payload = _payload(text="nothing to see here")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "keyword_miss", "take_hop": take, "reason": reason}


def case_persian_keyword() -> dict:
    # Arabic yeh in the keyword, Persian yeh in the message: the fold must
    # make them match, exactly as the content rules do.
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["فروش"], "match_mode": "any",
        }},
    )
    payload = _payload(text="فروش ویژه امروز")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "persian_keyword", "take_hop": take, "reason": reason}


def case_persian_keyword_miss() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["خرید"], "match_mode": "any",
        }},
    )
    payload = _payload(text="فروش ویژه امروز")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "persian_keyword_miss", "take_hop": take, "reason": reason}


def case_origin_id_match() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "origin_ids": ["-100555"], "match_mode": "any",
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Channel Five",
                        "from_chat_username": "chanfive"},
    )
    take, reason = branding.should_hop(payload, rule)
    return {"name": "origin_id_match", "take_hop": take, "reason": reason}


def case_origin_username_match() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "origin_ids": ["@chanfive"], "match_mode": "any",
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Channel Five",
                        "from_chat_username": "chanfive"},
    )
    take, reason = branding.should_hop(payload, rule)
    return {"name": "origin_username_match", "take_hop": take, "reason": reason}


def case_origin_title_match() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "origin_titles": ["channel five"], "match_mode": "any",
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Channel Five",
                        "from_chat_username": ""},
    )
    take, reason = branding.should_hop(payload, rule)
    return {"name": "origin_title_match", "take_hop": take, "reason": reason}


def case_origin_title_partial() -> dict:
    # The Python reference matches `wanted in title or title in wanted`, so a
    # configured title that is a substring (or superstring) of the real one
    # also matches. The Go port must do the same, not the stricter exact
    # comparison a naive port would write.
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "origin_titles": ["Five"], "match_mode": "any",
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Channel Five",
                        "from_chat_username": ""},
    )
    take, reason = branding.should_hop(payload, rule)
    return {"name": "origin_title_partial", "take_hop": take, "reason": reason}


def case_origin_missing() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "origin_ids": ["-100555"], "match_mode": "any",
        }},
    )
    payload = _payload(text="hello")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "origin_missing", "take_hop": take, "reason": reason}


def case_require_forward_gate() -> dict:
    # require_forward is a GATE: it refuses a non-forward whatever
    # match_mode says, even when the keyword matches.
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["hello"], "require_forward": True,
        }},
    )
    payload = _payload(text="hello")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "require_forward_gate", "take_hop": take, "reason": reason}


def case_require_forward_satisfied() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["hello"], "require_forward": True,
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""},
    )
    take, reason = branding.should_hop(payload, rule)
    return {"name": "require_forward_satisfied", "take_hop": take, "reason": reason}


def case_match_all_satisfied() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["hello"], "origin_ids": ["-100555"],
            "match_mode": "all",
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""},
    )
    take, reason = branding.should_hop(payload, rule)
    return {"name": "match_all_satisfied", "take_hop": take, "reason": reason}


def case_match_all_one_missing() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["nomatch"], "origin_ids": ["-100555"],
            "match_mode": "all",
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""},
    )
    take, reason = branding.should_hop(payload, rule)
    return {"name": "match_all_one_missing", "take_hop": take, "reason": reason}


def case_match_any_one_of_two() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["nomatch"], "origin_ids": ["-100555"],
            "match_mode": "any",
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""},
    )
    take, reason = branding.should_hop(payload, rule)
    return {"name": "match_any_one_of_two", "take_hop": take, "reason": reason}


def case_no_triggers_configured() -> dict:
    # Triggers switched on but nothing configured: hop.
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {"triggers_enabled": True}},
    )
    payload = _payload(text="hello")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "no_triggers_configured", "take_hop": take, "reason": reason}


def case_invalid_config_degrades() -> dict:
    # An unusable configuration (bad types, unknown keys, too many rows)
    # degrades to the documented defaults rather than raising.
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": "yes", "keywords": [1, 2, "offered"], "match_mode": "bogus",
            "origin_ids": 5, "require_forward": None,
        }},
    )
    payload = _payload(text="offered")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "invalid_config_degrades", "take_hop": take, "reason": reason}


def case_vip_matched() -> dict:
    # A task whose own VIP criteria matched: the hop is taken through the
    # no-triggers legacy path exactly as before.
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        detection_criteria={"forward_origin_chat_ids": ["-100555"]},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""},
    )
    take, reason = ROUTING.should_brand_hop(payload, rule)
    return {"name": "vip_matched", "take_hop": take, "reason": reason}


def case_vip_unmatched() -> dict:
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        detection_criteria={"forward_origin_chat_ids": ["-100555"]},
    )
    payload = _payload(text="hello")
    take, reason = ROUTING.should_brand_hop(payload, rule)
    return {"name": "vip_unmatched", "take_hop": take, "reason": reason}


def case_vip_with_triggers() -> dict:
    # VIP configured AND triggers configured: vip is one of the conditions.
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        detection_criteria={"forward_origin_chat_ids": ["-100555"]},
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["hello"], "match_mode": "all",
        }},
    )
    payload = _payload(
        text="hello",
        forward_origin={"from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""},
    )
    take, reason = ROUTING.should_brand_hop(payload, rule)
    return {"name": "vip_with_triggers", "take_hop": take, "reason": reason}


def case_caption_text() -> dict:
    # _text_of prefers effective_text, then text, then raw_text, then caption.
    rule = _rule(
        intermediate_channel_id="-100999",
        use_intermediate=True,
        metadata={"branding_hop": {
            "triggers_enabled": True, "keywords": ["captioned"], "match_mode": "any",
        }},
    )
    payload = _payload(text="captioned body", caption="not this")
    take, reason = branding.should_hop(payload, rule)
    return {"name": "caption_text", "take_hop": take, "reason": reason}


CASES = [
    case_disabled_no_channel,
    case_disabled_flag_off,
    case_flag_via_legacy_mode,
    case_always,
    case_keyword_match,
    case_keyword_miss,
    case_persian_keyword,
    case_persian_keyword_miss,
    case_origin_id_match,
    case_origin_username_match,
    case_origin_title_match,
    case_origin_title_partial,
    case_origin_missing,
    case_require_forward_gate,
    case_require_forward_satisfied,
    case_match_all_satisfied,
    case_match_all_one_missing,
    case_match_any_one_of_two,
    case_no_triggers_configured,
    case_invalid_config_degrades,
    case_vip_matched,
    case_vip_unmatched,
    case_vip_with_triggers,
    case_caption_text,
]


# The fixture set the Go harness replays. Every case carries its *inputs*
# alongside the decision the live Python implementation made, so a Go port is
# graded on the exact same question the Python engine was asked.
FIXTURE_INPUTS = {
    "disabled_no_channel": {"text": "hello"},
    "disabled_flag_off": {"text": "hello"},
    "flag_via_legacy_mode": {"text": "hello"},
    "always": {"text": "hello"},
    "keyword_match": {"text": "New product offered today"},
    "keyword_miss": {"text": "nothing to see here"},
    "persian_keyword": {"text": "فروش ویژه امروز"},
    "persian_keyword_miss": {"text": "فروش ویژه امروز"},
    "origin_id_match": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Channel Five", "from_chat_username": "chanfive"}},
    "origin_username_match": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Channel Five", "from_chat_username": "chanfive"}},
    "origin_title_match": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Channel Five", "from_chat_username": ""}},
    "origin_title_partial": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Channel Five", "from_chat_username": ""}},
    "origin_missing": {"text": "hello"},
    "require_forward_gate": {"text": "hello"},
    "require_forward_satisfied": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""}},
    "match_all_satisfied": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""}},
    "match_all_one_missing": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""}},
    "match_any_one_of_two": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""}},
    "no_triggers_configured": {"text": "hello"},
    "invalid_config_degrades": {"text": "offered"},
    "vip_matched": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""}},
    "vip_unmatched": {"text": "hello"},
    "vip_with_triggers": {"text": "hello", "forward_origin": {
        "from_chat_id": "-100555", "from_chat_title": "Five", "from_chat_username": ""}},
    "caption_text": {"text": "captioned body", "caption": "not this"},
}

# The rule each case builds, in the plain shape the Go harness reconstructs.
FIXTURE_RULES = {
    "disabled_no_channel": {},
    "disabled_flag_off": {"intermediate_channel_id": "-100999"},
    "flag_via_legacy_mode": {"intermediate_channel_id": "-100999", "intermediate_mode": "VIA_INTERMEDIATE"},
    "always": {"intermediate_channel_id": "-100999", "use_intermediate": True},
    "keyword_match": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                      "branding_hop": {"triggers_enabled": True, "keywords": ["offered", "price"], "match_mode": "any"}},
    "keyword_miss": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                     "branding_hop": {"triggers_enabled": True, "keywords": ["offered"], "match_mode": "any"}},
    "persian_keyword": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                        "branding_hop": {"triggers_enabled": True, "keywords": ["فروش"], "match_mode": "any"}},
    "persian_keyword_miss": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                             "branding_hop": {"triggers_enabled": True, "keywords": ["خرید"], "match_mode": "any"}},
    "origin_id_match": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                        "branding_hop": {"triggers_enabled": True, "origin_ids": ["-100555"], "match_mode": "any"}},
    "origin_username_match": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                              "branding_hop": {"triggers_enabled": True, "origin_ids": ["@chanfive"], "match_mode": "any"}},
    "origin_title_match": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                           "branding_hop": {"triggers_enabled": True, "origin_titles": ["channel five"], "match_mode": "any"}},
    "origin_title_partial": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                             "branding_hop": {"triggers_enabled": True, "origin_titles": ["Five"], "match_mode": "any"}},
    "origin_missing": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                       "branding_hop": {"triggers_enabled": True, "origin_ids": ["-100555"], "match_mode": "any"}},
    "require_forward_gate": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                             "branding_hop": {"triggers_enabled": True, "keywords": ["hello"], "require_forward": True}},
    "require_forward_satisfied": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                                  "branding_hop": {"triggers_enabled": True, "keywords": ["hello"], "require_forward": True}},
    "match_all_satisfied": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                            "branding_hop": {"triggers_enabled": True, "keywords": ["hello"],
                                             "origin_ids": ["-100555"], "match_mode": "all"}},
    "match_all_one_missing": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                              "branding_hop": {"triggers_enabled": True, "keywords": ["nomatch"],
                                               "origin_ids": ["-100555"], "match_mode": "all"}},
    "match_any_one_of_two": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                             "branding_hop": {"triggers_enabled": True, "keywords": ["nomatch"],
                                              "origin_ids": ["-100555"], "match_mode": "any"}},
    "no_triggers_configured": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                               "branding_hop": {"triggers_enabled": True}},
    "invalid_config_degrades": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                                "branding_hop": {"triggers_enabled": "yes", "keywords": [1, 2, "offered"],
                                                 "match_mode": "bogus", "origin_ids": 5, "require_forward": None}},
    "vip_matched": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                    "detection_criteria": {"forward_origin_chat_ids": ["-100555"]}},
    "vip_unmatched": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                      "detection_criteria": {"forward_origin_chat_ids": ["-100555"]}},
    "vip_with_triggers": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                          "detection_criteria": {"forward_origin_chat_ids": ["-100555"]},
                          "branding_hop": {"triggers_enabled": True, "keywords": ["hello"], "match_mode": "all"}},
    "caption_text": {"intermediate_channel_id": "-100999", "use_intermediate": True,
                     "branding_hop": {"triggers_enabled": True, "keywords": ["captioned"], "match_mode": "any"}},
}


def main() -> None:
    """Emit the reference as JSON: the same fixtures the Go harness reads.

    Every entry carries the inputs the Python decision was asked about, so
    the Go port is graded on exactly the same question rather than on a
    re-interpretation of it.
    """
    out = []
    for fn in CASES:
        result = fn()
        name = result["name"]
        message = dict(FIXTURE_INPUTS.get(name, {}))
        ruleIn = dict(FIXTURE_RULES.get(name, {}))
        out.append({
            "name": name,
            "message": message,
            "rule": ruleIn,
            "take_hop": result["take_hop"],
            "reason": result["reason"],
        })
    print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
