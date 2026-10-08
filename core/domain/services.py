"""Pure domain services: content filtering + routing decisions.

These contain NO infrastructure concerns so they can be unit tested in
isolation and reused by any adapter (bot, gRPC, queue consumer).
"""

import re
from typing import List, Optional

from ..domain.entities import EvaluationResult, FilterRule, ForwardRule, MessagePayload
from ..domain.value_objects import FilterAction, MediaType

URL_REGEX = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|@[A-Za-z0-9_]{5,})",
    re.IGNORECASE,
)


class FilterEngine:
    """Evaluates a MessagePayload against a FilterRule.

    Order of checks (fail fast):
      1. service messages      2. media type allow/deny
      3. length bounds         4. blacklist keywords
      5. whitelist keywords    6. regex patterns
    """

    def evaluate(self, payload: MessagePayload, filter_rule: Optional[FilterRule]) -> EvaluationResult:
        if payload.is_service:
            if filter_rule is not None and filter_rule.drop_service_messages:
                return EvaluationResult(FilterAction.DROP, "service_message_dropped")
            if filter_rule is None:
                return EvaluationResult(FilterAction.DROP, "service_message_dropped")

        if filter_rule is None:
            return EvaluationResult(FilterAction.ALLOW, "no_filter")

        media = payload.media_type.value if isinstance(payload.media_type, MediaType) else str(payload.media_type)

        if filter_rule.blocked_media_types and media in filter_rule.blocked_media_types:
            return EvaluationResult(FilterAction.DROP, f"media_blocked:{media}")

        if filter_rule.allowed_media_types and media not in filter_rule.allowed_media_types:
            return EvaluationResult(FilterAction.DROP, f"media_not_allowed:{media}")

        text = payload.effective_text or ""

        if filter_rule.min_message_length and len(text) < filter_rule.min_message_length:
            return EvaluationResult(FilterAction.DROP, "too_short")

        if filter_rule.max_message_length and len(text) > filter_rule.max_message_length:
            return EvaluationResult(FilterAction.DROP, "too_long")

        lowered = text.lower()
        if any(self._kw(lowered, kw) for kw in filter_rule.blacklist_keywords):
            return EvaluationResult(FilterAction.DROP, "blacklist_keyword")

        if filter_rule.whitelist_keywords and not any(
            self._kw(lowered, kw) for kw in filter_rule.whitelist_keywords
        ):
            return EvaluationResult(FilterAction.DROP, "whitelist_mismatch")

        if filter_rule.regex_patterns:
            any_valid = False
            for pattern in filter_rule.regex_patterns:
                try:
                    compiled = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
                except re.error:
                    continue  # invalid pattern: ignore it, never crash or drop
                any_valid = True
                if compiled.search(text):
                    break
            else:
                if any_valid:
                    return EvaluationResult(FilterAction.DROP, "regex_mismatch")

        return EvaluationResult(FilterAction.ALLOW, "filter_passed")

    @staticmethod
    def _kw(lowered_text: str, keyword: str) -> bool:
        return keyword.lower() in lowered_text

    def remove_links(self, text: str) -> str:
        return URL_REGEX.sub("", text).strip()


class RoutingPolicy:
    """Maps an incoming payload to the set of rules that must consume it."""

    def matching_rules(self, payload: MessagePayload, rules: List[ForwardRule]) -> List[ForwardRule]:
        return [r for r in rules if r.matches_source(payload.chat_id)]
