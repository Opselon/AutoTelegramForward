"""Pure domain services: content filtering + routing decisions.

These contain NO infrastructure concerns so they can be unit tested in
isolation and reused by any adapter (bot, gRPC, queue consumer).
"""

import re
from typing import List, Optional

from ..domain.entities import EvaluationResult, FilterRule, ForwardRule, MessagePayload
from ..domain.value_objects import FilterAction, MediaType

URL_REGEX = re.compile(
    r"(https?://[^\s]+|www\.[^\s]+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/[^\s]+|@[A-Za-z0-9_]{3,32})",
    re.IGNORECASE,
)

EMOJI_REGEX = re.compile(
    r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\ufe00-\ufe0f]",
    flags=re.UNICODE,
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
        if not text:
            return ""
        # 1. Clean markdown hyperlinks [text](url) -> keep anchor text
        cleaned = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.|t\.me/|[^\)]+)\)", r"\1", text)
        # 2. Remove raw URLs, telegram links, and @usernames
        cleaned = URL_REGEX.sub("", cleaned)
        # 3. Clean up multiple empty lines and excessive spaces
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
        return cleaned.strip()

    def replace_links(self, text: str, replacement: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.|t\.me/|[^\)]+)\)", replacement, text)
        cleaned = URL_REGEX.sub(replacement, cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
        return cleaned.strip()

    def remove_emojis(self, text: str) -> str:
        if not text:
            return ""
        return EMOJI_REGEX.sub("", text).strip()

    def replace_text(self, text: str, replacements: dict) -> str:
        if not text or not replacements:
            return text
        res = text
        for old, new in replacements.items():
            if not old:
                continue
            old_str = str(old)
            new_str = str(new)
            # Exact match first
            if old_str in res:
                res = res.replace(old_str, new_str)
            else:
                # Case-insensitive match for handles or words
                pattern = re.compile(re.escape(old_str), re.IGNORECASE)
                res = pattern.sub(new_str, res)
        return res

    def apply_header_footer(self, text: str, header: str = "", footer: str = "") -> str:
        parts = []
        if header and header.strip():
            parts.append(header.strip())
        if text and text.strip():
            parts.append(text.strip())
        if footer and footer.strip():
            parts.append(footer.strip())
        return "\n\n".join(parts)

    def transform_text(self, text: str, rule: ForwardRule) -> str:
        """Applies configured links, emojis, replacements, and header/footer transforms."""
        if text is None:
            text = ""
        # 1. Link replacement or removal
        if rule.link_replacement:
            text = self.replace_links(text, rule.link_replacement)
        elif rule.remove_links or (isinstance(rule.metadata, dict) and rule.metadata.get("remove_links")):
            text = self.remove_links(text)

        # 2. Emoji removal
        if rule.remove_emojis:
            text = self.remove_emojis(text)

        # 3. Custom text replacements
        if rule.replacements:
            text = self.replace_text(text, rule.replacements)

        # 4. Header & Footer
        if rule.header or rule.footer:
            text = self.apply_header_footer(text, header=rule.header, footer=rule.footer)

        return text


class RoutingPolicy:
    """Maps an incoming payload to the set of rules that must consume it."""

    def matching_rules(self, payload: MessagePayload, rules: List[ForwardRule]) -> List[ForwardRule]:
        return [r for r in rules if r.matches_source(payload.chat_id)]
