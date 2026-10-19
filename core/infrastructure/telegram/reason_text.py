"""Why the routing engine did what it did, in the user's language.

``MessageRoutingEngine`` answers every decision with a reason string, and those
strings are its own closed vocabulary: ``matched``, ``rule_paused``,
``category_vip_unmatched:origin_unmatched``,
``branding_triggers_unmatched:keywords,none``, and four English sentences from
``decide_route`` (``"Direct clean copy from A to B"``). The dry-run screen
printed whichever of them came back, so the one screen that exists to answer
"why did this message not go through?" answered in English, in ten locales —
including a full English sentence wrapped inside a Persian one.

The engine keeps its tokens (the dispatcher, the queue and the API all compare
them, and they are the honest thing to log). Rendering happens here:

* a token with no ``:`` is a whole sentence of its own (``PLAIN``);
* a token with a ``:`` is a family plus its detail — ``reason=`` for a nested
  engine token (rendered recursively, so ``loop_detected:target_matches_source``
  reads as one sentence) or ``words=`` for a comma-separated list of matched
  conditions (``all_matched:origin_matched,regex_matched``);
* the four route sentences are matched whole (``SENTENCES``);
* anything this table does not know is shown as the engine wrote it, with an
  ERROR line: an unknown token on a diagnostics screen is a smaller failure than
  a blank line, and the log says which token to add.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict

logger = logging.getLogger("atf.botui.reason")

#: The engine's own word for a decision, and the sentence that says it.
PLAIN: Dict[str, str] = {
    "matched": "ui_reason_matched",
    "rule_inactive": "ui_reason_rule_inactive",
    "rule_paused": "ui_reason_rule_paused",
    "source_chat_mismatch": "ui_reason_source_chat_mismatch",
    "blocked_voice": "ui_reason_blocked_voice",
    "blocked_stickers": "ui_reason_blocked_stickers",
    "media_handling_text_only": "ui_reason_media_handling_text_only",
    "media_handling_media_only": "ui_reason_media_handling_media_only",
    "origin_unmatched": "ui_reason_origin_unmatched",
    "text_contains_unmatched": "ui_reason_text_contains_unmatched",
    "regex_unmatched": "ui_reason_regex_unmatched",
    "invalid_regex": "ui_reason_invalid_regex",
    "all_criteria_not_satisfied": "ui_reason_all_criteria_not_satisfied",
    "no_criteria_matched": "ui_reason_no_criteria_matched",
    "target_matches_source": "ui_reason_target_matches_source",
    "target_matches_forward_origin": "ui_reason_target_matches_forward_origin",
    "intermediate_target_is_origin": "ui_reason_intermediate_target_is_origin",
    "branding_always": "ui_reason_branding_always",
    "branding_requires_forward_origin": "ui_reason_branding_requires_forward_origin",
    "branding_no_triggers_configured": "ui_reason_branding_no_triggers_configured",
    "UNKNOWN_ORIGIN": "ui_reason_unknown_origin",
}

#: ``family:detail`` — the sentence takes ``{reason}`` (a nested token, rendered
#: the same way) or ``{words}`` (a list of matched conditions).
FAMILIES: Dict[str, str] = {
    "loop_detected": "ui_reason_loop_detected",
    "category_vip_unmatched": "ui_reason_category_vip_unmatched",
    "category_regular_unmatched": "ui_reason_category_regular_unmatched",
    "forward_chat_id_match": "ui_reason_forward_chat_id_match",
    "all_matched": "ui_reason_all_matched",
    "any_matched": "ui_reason_any_matched",
    "unknown_origin": "ui_reason_unknown_origin_detail",
    "branding_disabled": "ui_reason_branding_disabled",
    "branding_vip_matched": "ui_reason_branding_vip_matched",
    "branding_vip_unmatched": "ui_reason_branding_vip_unmatched",
    "branding_triggers_matched": "ui_reason_branding_triggers_matched",
    "branding_triggers_unmatched": "ui_reason_branding_triggers_unmatched",
}

#: The families whose detail is a list of conditions, not a nested token.
WORD_FAMILIES = frozenset({
    "all_matched", "any_matched",
    "branding_triggers_matched", "branding_triggers_unmatched",
})

#: One condition, or one sub-token, named on its own.
WORDS: Dict[str, str] = {
    "origin_matched": "ui_reason_word_origin",
    "origin": "ui_reason_word_origin",
    "text_contains_matched": "ui_reason_word_keywords",
    "keywords": "ui_reason_word_keywords",
    "regex_matched": "ui_reason_word_regex",
    "vip": "ui_reason_word_vip",
    "missing_forward_metadata": "ui_reason_word_missing_forward_metadata",
    "no_channel": "ui_reason_word_no_channel",
    "flag_off": "ui_reason_word_flag_off",
    "no_match": "ui_reason_word_no_match",
    "none": "ui_reason_word_none",
}

#: `decide_route` answers with a sentence, not a token. Matched whole: the
#: engine's wording is the key, and the test that reads the domain module fails
#: if a sentence is reworded without its translation.
SENTENCES: Dict[str, str] = {
    "Routed via intermediate C with branding forward": "ui_reason_route_branding",
    "Native forward directly from A to B": "ui_reason_route_direct_forward",
    "Direct copy with custom header from A to B": "ui_reason_route_custom_header",
    "Direct clean copy from A to B": "ui_reason_route_direct_copy",
}


def reason_label(reason: str, t: Callable[..., str]) -> str:
    """The engine's reason, as one sentence in the user's language.

    ``t`` is the render callable already bound to the user's language
    (``ProBotUI._t``).
    """
    text = str(reason or "").strip()
    if not text:
        return ""
    # A route sentence arrives with its own parenthetical detail
    # ("Routed via intermediate C with branding forward (branding_always)"), so
    # the sentence is matched by prefix and the detail rendered separately.
    for sentence, key in SENTENCES.items():
        if text.startswith(sentence):
            detail = text[len(sentence):].strip().strip("()").strip()
            if not detail:
                return t(key)
            return f"{t(key)} ({reason_label(detail, t)})"

    key = PLAIN.get(text)
    if key:
        return t(key)

    family, sep, detail = text.partition(":")
    slug = family.strip().lower()
    family_key = FAMILIES.get(slug) if sep else None
    if family_key is None:
        logger.error("no catalog entry for the %r routing reason", text)
        return text
    detail = detail.strip()
    if slug in WORD_FAMILIES:
        words = ", ".join(word_label(item, t) for item in detail.split(",") if item.strip())
        return t(family_key, words=words)
    return t(family_key, reason=detail_label(detail, t))


def detail_label(detail: str, t: Callable[..., str]) -> str:
    """A nested detail: one of our condition words, or another engine reason.

    `category_vip_unmatched:origin_unmatched` names a condition word;
    `loop_detected:target_matches_source` names another reason; and
    `branding_vip_unmatched:any_matched:regex_matched` names a whole reason of
    its own. The first is a word, so it is looked up before recursing.
    """
    token = str(detail or "").strip()
    if token in WORDS:
        return word_label(token, t)
    return reason_label(token, t)


def word_label(item: str, t: Callable[..., str]) -> str:
    """One matched condition, said in the user's language."""
    token = str(item or "").strip()
    key = WORDS.get(token)
    if key is None:
        logger.error("no catalog entry for the %r condition word", token)
        return token
    return t(key)
