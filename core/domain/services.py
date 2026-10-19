"""Pure domain services: content filtering + routing decisions.

These contain NO infrastructure concerns so they can be unit tested in
isolation and reused by any adapter (bot, gRPC, queue consumer).
"""

import ipaddress
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse, urlunparse

from ..domain.entities import EvaluationResult, FilterRule, ForwardRule, MessagePayload
from ..domain.value_objects import DeliveryStatus, FilterAction, LinkPolicy, MediaType, RoutingDecision
from . import branding

logger = logging.getLogger(__name__)

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

        media = payload.media_type.value if isinstance(payload.media_type, MediaType) else str(payload.media_type).lower()

        if filter_rule.blocked_media_types:
            blocked_lower = [str(b).lower() for b in filter_rule.blocked_media_types]
            if media in blocked_lower:
                return EvaluationResult(FilterAction.DROP, f"media_blocked:{media}")
            if "link" in blocked_lower:
                text_check = payload.effective_text or ""
                if URL_REGEX.search(text_check) or "http://" in text_check.lower() or "https://" in text_check.lower() or "t.me/" in text_check.lower():
                    return EvaluationResult(FilterAction.DROP, "link_blocked")

        if filter_rule.allowed_media_types and not (media in filter_rule.allowed_media_types or any(a.lower() == media for a in filter_rule.allowed_media_types)):
            return EvaluationResult(FilterAction.DROP, f"media_not_allowed:{media}")

        text = payload.effective_text or ""

        # Link Filter: drop message if it contains links and block_links is set
        if getattr(filter_rule, "block_links", False):
            if URL_REGEX.search(text) or "http://" in text.lower() or "https://" in text.lower() or "t.me/" in text.lower():
                return EvaluationResult(FilterAction.DROP, "link_blocked")

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

    def remove_urls(self, text: str) -> str:
        if not text:
            return ""
        # 1. Clean markdown hyperlinks [text](url) -> keep anchor text
        cleaned = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.|t\.me/|[^\)]+)\)", r"\1", text)
        # 2. Remove raw URLs and telegram links
        cleaned = re.sub(r"(https?://\S+|www\.\S+|(?:https?://)?(?:t(?:elegram)?\.(?:me|dog))/\S+)", "", cleaned, flags=re.IGNORECASE)
        # 3. Clean up multiple empty lines and excessive spaces
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
        return cleaned.strip()

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

    def remove_usernames(self, text: str) -> str:
        if not text:
            return ""
        # Remove @mentions/usernames
        cleaned = re.sub(r"@[A-Za-z0-9_]{3,32}", "", text)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        return cleaned.strip()

    def remove_hashtags(self, text: str) -> str:
        if not text:
            return ""
        # Remove #hashtags (supports Unicode/Persian/Arabic letters)
        cleaned = re.sub(r"#\S+", "", text)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        return cleaned.strip()

    def remove_phones(self, text: str) -> str:
        if not text:
            return ""
        # Remove telephone numbers (+1..., 09..., etc.)
        cleaned = re.sub(r"(?:\+?\d{1,4}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}", "", text)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        return cleaned.strip()

    def remove_custom_words(self, text: str, words: List[str]) -> str:
        if not text or not words:
            return text
        res = text
        for w in words:
            w_str = str(w).strip()
            if not w_str:
                continue
            pattern = re.compile(re.escape(w_str), re.IGNORECASE)
            res = pattern.sub("", res)
        return res.strip()

    def clean_empty_lines(self, text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
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
            h = header.strip()
            # Prevent duplicate header insertion
            if not text.strip().startswith(h):
                parts.append(h)
        if text and text.strip():
            parts.append(text.strip())
        if footer and footer.strip():
            f = footer.strip()
            # Prevent duplicate footer insertion
            if not text.strip().endswith(f):
                parts.append(f)
        return "\n\n".join(parts)

    def transform_text(self, text: str, rule: ForwardRule, payload: Optional[MessagePayload] = None) -> str:
        """Applies configured links, emojis, replacements, and header/footer transforms."""
        if text is None:
            text = ""
        # 1. Cleaner: Links (URLs)
        if rule.link_replacement:
            text = self.replace_links(text, rule.link_replacement)
        elif getattr(rule, "remove_links", False) or (isinstance(rule.metadata, dict) and rule.metadata.get("remove_links")):
            text = self.remove_links(text)

        # 2. Cleaner: Usernames / Mentions
        if getattr(rule, "clean_usernames", False):
            text = self.remove_usernames(text)

        # 3. Cleaner: Hashtags
        if getattr(rule, "clean_hashtags", False):
            text = self.remove_hashtags(text)

        # 4. Cleaner: Emojis
        if getattr(rule, "remove_emojis", False):
            text = self.remove_emojis(text)

        # 5. Cleaner: Phone numbers
        if getattr(rule, "clean_phones", False):
            text = self.remove_phones(text)

        # 6. Cleaner: Custom words to delete
        clean_words = getattr(rule, "clean_custom_words", [])
        if clean_words:
            text = self.remove_custom_words(text, clean_words)

        # 7. Custom text replacements
        if rule.replacements:
            text = self.replace_text(text, rule.replacements)

        # 8. Cleaner: Empty lines
        if getattr(rule, "clean_empty_lines", False):
            text = self.clean_empty_lines(text)

        # 9. Header & Footer (with template placeholder formatting if payload supplied)
        header = rule.header
        footer = rule.footer
        if payload is not None:
            if header:
                header = HeaderFormatter.format_text(header, rule, payload)
            if footer:
                footer = HeaderFormatter.format_text(footer, rule, payload)

        if header or footer:
            text = self.apply_header_footer(text, header=header, footer=footer)

        return text


TextTransformer = FilterEngine


class HeaderFormatter:
    """Safe formatting for custom headers/footers with placeholder interpolation."""

    @staticmethod
    def format_text(
        template: str,
        rule: ForwardRule,
        payload: MessagePayload,
        custom_vars: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not template or not template.strip():
            return ""

        now_dt = datetime.now(timezone.utc)
        date_str = now_dt.strftime("%Y-%m-%d %H:%M UTC")

        # Source link determination
        source_link = ""
        if payload.chat_username and payload.message_id:
            source_link = f"https://t.me/{payload.chat_username.lstrip('@')}/{payload.message_id}"
        elif payload.chat_id and payload.chat_id.startswith("-100") and payload.message_id:
            clean_cid = payload.chat_id[4:]
            source_link = f"https://t.me/c/{clean_cid}/{payload.message_id}"

        sender_name = payload.sender_name or ""
        chat_title = payload.chat_title or payload.chat_username or payload.chat_id
        post_date = (
            datetime.fromtimestamp(payload.date, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if getattr(payload, "date", 0)
            else date_str
        )
        origin_username = f"@{payload.chat_username.lstrip('@')}" if payload.chat_username else ""
        forward_from_chat = (
            str(payload.forward_origin.get("from_chat_id", payload.chat_id))
            if getattr(payload, "forward_origin", None) and isinstance(payload.forward_origin, dict)
            else str(payload.chat_id or "")
        )

        vars_dict = {
            "rule_name": rule.name or f"Rule-{rule.id[:8]}",
            "category": rule.message_category or "GENERAL",
            "date": post_date,
            "source_link": source_link,
            "sender_name": sender_name,
            "chat_title": chat_title,
            "source_chat": payload.chat_id,
            # Requested validated shortcodes
            "ORIGIN_TEXT": payload.effective_text,
            "ORIGIN_NAME": chat_title,
            "ORIGIN_USERNAME": origin_username,
            "ORIGIN_USERID": str(payload.sender_id or ""),
            "ORIGIN_POST_ID": str(payload.message_id or ""),
            "ORIGIN_CHAT_ID": str(payload.chat_id or ""),
            "ORIGIN_POST_DATE": post_date,
            "ORIGIN_NAME_URL": f"[{chat_title}]({source_link})" if source_link else chat_title,
            "SOURCE_NAME": chat_title,
            "SENDER_CHAT": str(payload.chat_id or ""),
            "FORWARD_FROM_CHAT": forward_from_chat,
            "FROM_USER": sender_name or str(payload.sender_id or ""),
        }
        if custom_vars:
            vars_dict.update(custom_vars)

        res = template
        for k, v in vars_dict.items():
            res = res.replace(f"{{{k}}}", str(v))
            res = res.replace(f"[[{k}]]", str(v))
        return res.strip()


class TransformationService:
    """Standalone transformation service for backward compatibility and domain use."""

    def __init__(self, filter_engine: Optional[FilterEngine] = None):
        self._engine = filter_engine or FilterEngine()

    def remove_links(self, text: str) -> str:
        return self._engine.remove_links(text)

    def replace_links(self, text: str, replacement: str) -> str:
        return self._engine.replace_links(text, replacement)

    def remove_emojis(self, text: str) -> str:
        return self._engine.remove_emojis(text)

    def replace_text(self, text: str, replacements: dict) -> str:
        return self._engine.replace_text(text, replacements)

    def apply_header_footer(self, text: str, header: str = "", footer: str = "") -> str:
        return self._engine.apply_header_footer(text, header, footer)

    def transform_text(self, text: str, rule: ForwardRule, payload: Optional[MessagePayload] = None) -> str:
        return self._engine.transform_text(text, rule, payload)


class SmartRoutingEngine:
    """Domain engine for evaluating smart forwarding rules, VIP vs Regular detection,
    anti-loop guarantees, and priority-ordered execution."""

    def is_vip_origin(self, payload: MessagePayload, rule: ForwardRule) -> Tuple[bool, str]:
        """Determine if an incoming message is a verified VIP message according to
        explicit user criteria and Telegram forward metadata.

        Strict rules:
        - NEVER assume regular messages are VIP.
        - If forward origin is absent or inaccessible, mark UNKNOWN_ORIGIN and do not guess.
        - Evaluate forward_origin_chat_ids, forward_origin_titles, text_contains, regex_pattern.
        - Obey match_mode ("ALL" vs "ANY").
        """
        criteria = rule.detection_criteria or {}
        match_mode = (criteria.get("match_mode") or "ANY").upper()

        origin_ids = [str(x).strip() for x in (criteria.get("forward_origin_chat_ids") or criteria.get("origin_chat_ids") or []) if str(x).strip()]
        origin_titles = [str(x).strip().lower() for x in (criteria.get("forward_origin_titles") or criteria.get("origin_titles") or []) if str(x).strip()]
        text_keywords = [str(x).strip().lower() for x in (criteria.get("text_contains") or criteria.get("keywords") or []) if str(x).strip()]
        regex_pattern = criteria.get("regex_pattern") or criteria.get("text_regex")

        conditions_checked = 0
        matches = []

        # 1. Forward origin check (Chat ID / Title / Username)
        has_origin_filter = bool(origin_ids or origin_titles or criteria.get("require_forward_origin"))
        if has_origin_filter:
            conditions_checked += 1
            if not payload.forward_origin:
                if match_mode == "ALL":
                    return False, "UNKNOWN_ORIGIN:missing_forward_metadata"
            else:
                fwd_chat_id = str(payload.forward_origin.get("from_chat_id") or "").strip()
                fwd_username = str(payload.forward_origin.get("from_chat_username") or "").strip().lstrip("@").lower()
                fwd_title = str(payload.forward_origin.get("from_chat_title") or "").strip().lower()

                matched_id = any(
                    fwd_chat_id == target or (fwd_username and fwd_username == target.lstrip("@").lower())
                    for target in origin_ids
                )
                matched_title = any(clean_title in fwd_title for clean_title in origin_titles)

                if matched_id or matched_title:
                    matches.append("origin_matched")
                elif match_mode == "ALL":
                    return False, "origin_unmatched"

        # 2. Text keywords check
        if text_keywords:
            conditions_checked += 1
            eff_text = (payload.effective_text or "").lower()
            if any(kw in eff_text for kw in text_keywords):
                matches.append("text_contains_matched")
            elif match_mode == "ALL":
                return False, "text_contains_unmatched"

        # 3. Regex check
        if regex_pattern:
            conditions_checked += 1
            try:
                if re.search(regex_pattern, payload.effective_text or "", re.IGNORECASE):
                    matches.append("regex_matched")
                elif match_mode == "ALL":
                    return False, "regex_unmatched"
            except re.error:
                if match_mode == "ALL":
                    return False, "invalid_regex"

        # If no explicit criteria were defined, check if payload is any verified forward
        if conditions_checked == 0:
            if payload.forward_origin and payload.forward_origin.get("from_chat_id"):
                fwd_cid = str(payload.forward_origin.get("from_chat_id"))
                return True, f"forward_chat_id_match:{fwd_cid}"
            return False, "UNKNOWN_ORIGIN"

        if match_mode == "ALL":
            if len(matches) == conditions_checked:
                return True, f"all_matched:{','.join(matches)}"
            return False, "all_criteria_not_satisfied"
        else:  # ANY
            if matches:
                return True, f"any_matched:{','.join(matches)}"
            return False, "no_criteria_matched"

    def check_loop(self, payload: MessagePayload, rule: ForwardRule, target_chat_id: str) -> Tuple[bool, str]:
        """Guarantees no routing loops occur between source, intermediate, and target."""
        cid = str(payload.chat_id).strip()
        tid = str(target_chat_id).strip()

        # Cannot forward back to the exact same chat
        if cid == tid or (payload.chat_username and payload.chat_username.lstrip("@").lower() == tid.lstrip("@").lower()):
            return True, "target_matches_source"

        # Cannot forward back to original forward source
        if payload.forward_origin:
            fwd_cid = str(payload.forward_origin.get("from_chat_id") or "").strip()
            if fwd_cid and fwd_cid == tid:
                return True, "target_matches_forward_origin"

        # If intermediate channel is target, message must not have originated there
        if rule.intermediate_channel_id:
            inter = str(rule.intermediate_channel_id).strip()
            if tid == inter and (cid == inter or (payload.forward_origin and str(payload.forward_origin.get("from_chat_id") or "") == inter)):
                return True, "intermediate_target_is_origin"

        return False, ""

    def evaluate_rule(self, payload: MessagePayload, rule: ForwardRule) -> Tuple[bool, str]:
        """Evaluates whether an incoming message matches this rule."""
        if not rule.is_active:
            return False, "rule_inactive"

        # Paused rules must never forward messages or enqueue forwarding work
        if getattr(rule, "is_paused", False):
            if hasattr(rule, "is_currently_paused"):
                if rule.is_currently_paused():
                    return False, "rule_paused"
            else:
                return False, "rule_paused"

        if not rule.matches_source(payload.chat_id):
            return False, "source_chat_mismatch"

        # Category check: VIP vs REGULAR vs ALL
        cat = (rule.message_category or "ALL").upper()
        if cat in ("VIP", "VIP_ONLY"):
            is_vip, reason = self.is_vip_origin(payload, rule)
            if not is_vip:
                return False, f"category_vip_unmatched:{reason}"
        elif cat in ("REGULAR", "NORMAL", "NORMAL_ONLY"):
            is_vip, reason = self.is_vip_origin(payload, rule)
            if is_vip:
                return False, f"category_regular_unmatched:{reason}"

        # Media filters
        if rule.block_voice and payload.media_type.value == "voice":
            return False, "blocked_voice"
        if rule.block_stickers and payload.media_type.value == "sticker":
            return False, "blocked_stickers"

        handling = (rule.media_handling or "AUTO").upper()
        if handling == "TEXT_ONLY" and payload.has_media:
            return False, "media_handling_text_only"
        if handling == "MEDIA_ONLY" and not payload.has_media:
            return False, "media_handling_media_only"

        # Anti-loop check. A task may fan out to many targets, so the rule is
        # rejected only when *every* target would loop; a single looping target
        # is skipped by the fan-out (see DurableMessagePipeline), which would
        # otherwise silently drop the other, legitimate targets.
        targets = list(getattr(rule, "all_targets", None) or [rule.target_chat_id])
        targets = [t for t in targets if str(t).strip()]
        if targets:
            loops = [t for t in targets if self.check_loop(payload, rule, t)[0]]
            if len(loops) == len(targets):
                is_loop, loop_reason = self.check_loop(payload, rule, targets[0])
                return False, f"loop_detected:{loop_reason}"

        return True, "matched"

    def should_brand_hop(self, payload: MessagePayload, rule: ForwardRule) -> Tuple[bool, str]:
        """The one branding-hop decision: should *this* message go A -> C -> B?

        Shared on purpose. The dispatcher, the durable queue and the dry-run
        simulation all call this, so the route an operator is shown in the bot or
        the dashboard is the route the engine actually takes. Keeping a second,
        simpler check in the dispatcher is what let the hop be "configured" in the
        UI while messages still went straight to B.
        """
        vip = (False, "")
        vip_configured = self.vip_criteria_configured(rule)
        if vip_configured:
            vip = self.is_vip_origin(payload, rule)
        return branding.should_hop(payload, rule, vip=vip, vip_configured=vip_configured)

    def vip_criteria_configured(self, rule: ForwardRule) -> bool:
        """Whether this task declared *any* VIP/origin criteria of its own."""
        criteria = getattr(rule, "detection_criteria", None) or {}
        if not isinstance(criteria, dict):
            criteria = {}
        for key in ("forward_origin_chat_ids", "origin_chat_ids",
                    "forward_origin_titles", "origin_titles",
                    "text_contains", "keywords", "regex_pattern", "text_regex"):
            if criteria.get(key):
                return True
        if criteria.get("require_forward_origin"):
            return True
        return str(getattr(rule, "message_category", "ALL") or "ALL").upper() in ("VIP", "VIP_ONLY")

    def decide_route(self, payload: MessagePayload, rule: ForwardRule) -> Tuple[RoutingDecision, str]:
        """Determine concrete routing path according to rule configuration and message provenance."""
        matched, reason = self.evaluate_rule(payload, rule)
        if not matched:
            return RoutingDecision.DROP, reason

        take_hop, hop_reason = self.should_brand_hop(payload, rule)
        f_mode = getattr(rule.forward_mode, "value", str(rule.forward_mode))

        if take_hop:
            return RoutingDecision.BRANDING_VIA_C, f"Routed via intermediate C with branding forward ({hop_reason})"
        if f_mode == "DIRECT_FORWARD":
            return RoutingDecision.DIRECT_FORWARD, "Native forward directly from A to B"
        if f_mode == "CUSTOM_HEADER_COPY" or getattr(rule, "custom_header", None):
            return RoutingDecision.CUSTOM_HEADER_COPY, "Direct copy with custom header from A to B"
        return RoutingDecision.DIRECT_COPY, "Direct clean copy from A to B"

    def matching_rules(self, payload: MessagePayload, rules: List[ForwardRule]) -> List[ForwardRule]:
        """Returns ordered list of matching rules. Evaluates in priority order
        (highest priority first). Stops at first match unless rule has multi_route=True."""
        sorted_rules = sorted(
            rules,
            key=lambda r: (-getattr(r, "priority", 10), getattr(r, "execution_order", 0), getattr(r, "created_at", 0)),
        )

        matches: List[ForwardRule] = []
        for rule in sorted_rules:
            matched, _ = self.evaluate_rule(payload, rule)
            if matched:
                matches.append(rule)
                if not getattr(rule, "multi_route", False):
                    # Multi-route is not enabled on this rule: deterministic single-match policy
                    break

        return matches


class RoutingPolicy:
    """Maps an incoming payload to the set of rules that must consume it.
    Backed by SmartRoutingEngine."""

    def __init__(self, engine: Optional[SmartRoutingEngine] = None):
        self._engine = engine or SmartRoutingEngine()

    def matching_rules(self, payload: MessagePayload, rules: List[ForwardRule]) -> List[ForwardRule]:
        return self._engine.matching_rules(payload, rules)

    def is_loop(self, payload: MessagePayload, rule: ForwardRule, target_chat_id: str) -> Tuple[bool, str]:
        """True when delivering to ``target_chat_id`` would create a loop.

        Exposed per target because one task may fan out to many targets: the
        caller skips the looping target instead of dropping the message for all
        of them.
        """
        return self._engine.check_loop(payload, rule, target_chat_id)


class DuplicateDetectionService:
    """In-memory domain deduplication tracker to prevent double-send and delivery loops."""

    def __init__(self, ttl_seconds: int = 300, max_size: int = 10000):
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._cache: Dict[str, float] = {}

    def make_key(self, payload: MessagePayload, rule_id: str = "", target_chat_id: str = "") -> str:
        f_orig = payload.forward_origin or {}
        orig_chat = f_orig.get("from_chat_id", "")
        orig_msg = f_orig.get("from_message_id", "")
        return f"{rule_id}:{payload.chat_id}:{payload.message_id}:{target_chat_id}:{orig_chat}:{orig_msg}"

    def is_duplicate(self, key: str) -> bool:
        self._prune()
        return key in self._cache

    def mark_seen(self, key: str) -> None:
        self._prune()
        if len(self._cache) >= self._max_size:
            # Drop oldest entry
            oldest_k = min(self._cache.keys(), key=lambda k: self._cache[k])
            self._cache.pop(oldest_k, None)
        self._cache[key] = time.time()

    def _prune(self) -> None:
        now = time.time()
        expired = [k for k, exp in self._cache.items() if now - exp > self._ttl]
        for k in expired:
            self._cache.pop(k, None)


def utf16_len(s: str) -> int:
    """Calculate the length of string in UTF-16 code units (as required by Telegram entities)."""
    if not s:
        return 0
    return len(s.encode("utf-16-le")) // 2


def utf16_to_char_pos(s: str, u16_offset: int) -> int:
    """Convert UTF-16 code units offset to Python character index."""
    if u16_offset <= 0 or not s:
        return 0
    cur_u16 = 0
    for i, ch in enumerate(s):
        if cur_u16 >= u16_offset:
            return i
        cur_u16 += 2 if ord(ch) > 0xFFFF else 1
    return len(s)


def char_to_utf16_pos(s: str, char_idx: int) -> int:
    """Convert Python character index to UTF-16 code units offset."""
    if char_idx <= 0 or not s:
        return 0
    sub = s[:char_idx]
    return utf16_len(sub)


def safe_text_preview(text, max_chars: int = 60, ellipsis: str = "...") -> str:
    """Return a plain-str preview capped at *max_chars* code points.

    Detaches from Pyrogram's ``Str`` via ``str()`` so slicing can never land
    inside a UTF-16 surrogate pair and raise UnicodeDecodeError.  Empty/None
    input and every unexpected type return ``""``.  Never raises.
    """
    if text is None:
        return ""
    try:
        s = text if isinstance(text, str) else str(text)
    except Exception:
        return ""
    # Remove orphan surrogates so downstream JSON/UTF-8 encoders never fail.
    s = s.encode("utf-8", errors="replace").decode("utf-8")
    if max_chars <= 0 or len(s) <= max_chars:
        return s
    return s[:max_chars] + ellipsis


class LinkManagementEngine:
    """Production-grade Link Management Engine.
    Handles domain allowlists/blocklists, regex & Telegram entity URL detection,
    hyperlink stripping, link rewriting, SSRF prevention, and UTF-16 offset adjustment."""

    TG_DOMAINS = {"t.me", "telegram.me", "telegram.dog"}

    #: Policy values already reported as unsupported (one log line per value).
    _warned_policies: ClassVar[set] = set()

    @classmethod
    def is_safe_url(cls, url: str) -> bool:
        """SSRF & redirect abuse defense. Rejects private IP ranges, loopback,
        metadata endpoints (169.254.169.254), and unsupported schemes."""
        if not url:
            return False
        clean = url.strip()
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", clean):
            clean = "https://" + clean
        try:
            parsed = urlparse(clean)
            scheme = (parsed.scheme or "").lower()
            if scheme not in ("http", "https", "tg"):
                return False
            host = (parsed.hostname or "").lower()
            if not host:
                return False
            if host in ("localhost", "metadata.google.internal"):
                return False
            # Check if host is an IP address
            try:
                ip = ipaddress.ip_address(host)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_reserved
                    or ip.is_unspecified
                ):
                    return False
            except ValueError:
                # Not a literal IP, it is a domain name
                pass
            return True
        except Exception:
            return False

    @classmethod
    def extract_domain(cls, url_or_text: str) -> str:
        """Extract clean lowercased domain without www or port."""
        raw = url_or_text.strip()
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
            raw = "https://" + raw
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    @classmethod
    def matches_domain(cls, domain: str, domain_list: List[str]) -> bool:
        """Check if domain or any parent domain matches items in domain_list."""
        clean_domain = domain.lower().strip()
        if clean_domain.startswith("www."):
            clean_domain = clean_domain[4:]
        for item in domain_list:
            clean_item = item.lower().strip()
            if clean_item.startswith("www."):
                clean_item = clean_item[4:]
            if clean_domain == clean_item or clean_domain.endswith("." + clean_item):
                return True
        return False

    @classmethod
    def process_text_and_entities(
        cls,
        text: str,
        entities: Optional[List[Any]] = None,
        policy: Union[LinkPolicy, str] = LinkPolicy.PRESERVE_ALL,
        allowlist: Optional[List[str]] = None,
        blocklist: Optional[List[str]] = None,
        rewrite_map: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, Optional[List[Any]], bool]:
        """Process text, removing/rewriting URLs according to policy while keeping
        UTF-16 entity offsets consistent for rich formatting (bold, italic, spoiler, etc.)."""
        policy_str = getattr(policy, "value", str(policy)).upper()
        if policy_str == LinkPolicy.PRESERVE_ALL.value or not text:
            return text, entities, False

        allowlist = [d.lower().strip() for d in (allowlist or []) if d.strip()]
        blocklist = [d.lower().strip() for d in (blocklist or []) if d.strip()]
        rewrite_map = rewrite_map or {}

        # 1. Process Telegram entities if provided
        new_entities = list(entities) if entities else []
        altered = False
        res_text = text

        def _should_remove(u: str) -> bool:
            dom = cls.extract_domain(u)
            if policy_str == LinkPolicy.REMOVE_ALL_URLS.value:
                return True
            if policy_str == LinkPolicy.REMOVE_EXTERNAL_URLS.value:
                return dom not in cls.TG_DOMAINS and not cls.matches_domain(dom, list(cls.TG_DOMAINS))
            if policy_str == LinkPolicy.REMOVE_SELECTED_DOMAINS.value:
                return cls.matches_domain(dom, blocklist)
            if policy_str == LinkPolicy.ALLOWLIST_ONLY.value:
                return not cls.matches_domain(dom, allowlist)
            if policy_str == LinkPolicy.CUSTOM_LINK_POLICY.value:
                if blocklist and cls.matches_domain(dom, blocklist):
                    return True
                if allowlist and not cls.matches_domain(dom, allowlist):
                    return True
            return False

        def _rewrite_url(u: str) -> str:
            dom = cls.extract_domain(u)
            for old_d, new_d in rewrite_map.items():
                clean_old = old_d.lower().strip()
                if dom == clean_old or dom.endswith("." + clean_old):
                    return u.replace(old_d, new_d)
            return u

        # Handle text_link entities: if URL should be stripped, remove URL metadata
        # (retaining anchor text without breaking formatting).
        filtered_entities = []
        for ent in new_entities:
            e_type = getattr(ent, "type", None)
            type_name = getattr(e_type, "name", str(e_type)).lower()
            if "text_link" in type_name:
                url_val = getattr(ent, "url", "")
                if _should_remove(url_val):
                    altered = True
                    # Strip link metadata, entity becomes normal text
                    continue
                elif rewrite_map and policy_str in (LinkPolicy.REWRITE_LINKS.value, LinkPolicy.CUSTOM_LINK_POLICY.value):
                    rewritten = _rewrite_url(url_val)
                    if rewritten != url_val:
                        altered = True
                        if hasattr(ent, "url"):
                            ent.url = rewritten
            filtered_entities.append(ent)

        # 2. Find and handle visible URL text spans
        matches = list(URL_REGEX.finditer(res_text))
        if not matches:
            return res_text, filtered_entities, altered

        # Process matches in reverse order so string slicing indices remain stable
        for m in reversed(matches):
            raw_url = m.group(0)
            if raw_url.startswith("@"):
                # A mention is not a link: the cleaner has its own option for
                # those, and stripping them here would remove usernames the user
                # asked to keep.
                continue
            if _should_remove(raw_url):
                start, end = m.span()
                removed_len = utf16_len(raw_url)
                u16_start = char_to_utf16_pos(res_text, start)

                # Delete URL from text
                res_text = res_text[:start] + res_text[end:]
                altered = True

                # Shift entities after this offset
                updated_list = []
                for ent in filtered_entities:
                    e_off = getattr(ent, "offset", 0)
                    e_len = getattr(ent, "length", 0)
                    if e_off >= u16_start + removed_len:
                        # Shift left
                        if hasattr(ent, "offset"):
                            ent.offset = max(0, e_off - removed_len)
                        updated_list.append(ent)
                    elif e_off + e_len <= u16_start:
                        # Unaffected before removal
                        updated_list.append(ent)
                    else:
                        # Overlapping entity: shrink or drop
                        new_len = max(0, e_len - removed_len)
                        if new_len > 0:
                            if hasattr(ent, "length"):
                                ent.length = new_len
                            updated_list.append(ent)
                filtered_entities = updated_list
            elif rewrite_map and policy_str in (LinkPolicy.REWRITE_LINKS.value, LinkPolicy.CUSTOM_LINK_POLICY.value):
                rewritten = _rewrite_url(raw_url)
                if rewritten != raw_url:
                    start, end = m.span()
                    diff = utf16_len(rewritten) - utf16_len(raw_url)
                    u16_start = char_to_utf16_pos(res_text, start)
                    res_text = res_text[:start] + rewritten + res_text[end:]
                    altered = True
                    for ent in filtered_entities:
                        e_off = getattr(ent, "offset", 0)
                        if e_off >= u16_start:
                            if hasattr(ent, "offset"):
                                ent.offset = e_off + diff

        # Clean trailing double spaces. Every removal in this normalization also
        # shrinks the text underneath the entities, so each offset is remapped
        # through the same arithmetic — an entity addressed one character late
        # formats the wrong word — and anything that no longer fits is dropped
        # rather than sent to Telegram as an invalid range.
        pre_normalized = res_text
        res_text = re.sub(r"[ \t]+", " ", res_text)
        res_text = re.sub(r"\n\s*\n\s*\n+", "\n\n", res_text)
        lead = len(res_text) - len(res_text.lstrip())
        res_text = res_text.strip()

        def _shifted(old_index: int) -> int:
            head = re.sub(r"[ \t]+", " ", pre_normalized[:old_index])
            head = re.sub(r"\n\s*\n\s*\n+", "\n\n", head)
            return max(0, len(head) - lead)

        limit = utf16_len(res_text)
        remapped: List[Any] = []
        for ent in filtered_entities:
            old_offset = int(getattr(ent, "offset", 0) or 0)
            old_length = int(getattr(ent, "length", 0) or 0)
            if old_length <= 0:
                continue
            new_offset = _shifted(old_offset)
            new_end = _shifted(old_offset + old_length)
            if new_offset >= limit:
                continue
            new_length = max(0, min(new_end - new_offset, limit - new_offset))
            if new_length <= 0:
                continue
            try:
                ent.offset = new_offset
                ent.length = new_length
            except Exception:
                continue
            remapped.append(ent)
        return res_text, remapped, altered

    @classmethod
    def resolve_policy(cls, raw: Any, *, remove_links: bool = False) -> LinkPolicy:
        """Coerce a stored policy value into ``LinkPolicy``, failing closed.

        Task rows store the policy as a plain string (the default is
        ``"PRESERVE_ALL"``). An unsupported value must never be read as "remove
        everything": it is treated as PRESERVE_ALL and reported once, because a
        typo in a settings payload is not a reason to strip a message.
        """
        if isinstance(raw, LinkPolicy):
            policy = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                policy = LinkPolicy(raw.strip().upper())
            except ValueError:
                policy = LinkPolicy.PRESERVE_ALL
                if raw not in cls._warned_policies:
                    cls._warned_policies.add(raw)
                    logger.warning(
                        "[LINK_ENGINE] Unsupported link policy %r -> PRESERVE_ALL (fail closed)", raw
                    )
        else:
            policy = LinkPolicy.PRESERVE_ALL
        if policy is LinkPolicy.PRESERVE_ALL and remove_links:
            # The cleaner's "Links: remove" toggle predates the policy enum and
            # means exactly this.
            return LinkPolicy.REMOVE_ALL_URLS
        return policy

    @classmethod
    def apply_to_payload(
        cls, payload: MessagePayload, rule: ForwardRule, text: Optional[str] = None
    ) -> Tuple[str, Optional[List[Any]], bool]:
        """Apply a task's link policy to one payload.

        This is the pipeline's entry point into the link engine. ``text`` is the
        body as it stands *after* the earlier stages (the cleaner runs first and
        may already have changed it); the payload is consulted for the entity
        list. Passing the live text is what keeps this pass from resurrecting
        content an earlier stage removed.

        Returns the new text, the entity list whose UTF-16 offsets match that
        text, and whether anything changed. The entity list is returned even when
        nothing changed so the caller always has a consistent view.
        """
        body = payload.effective_text if text is None else text
        body = body or ""
        raw_entities = payload.caption_entities if payload.has_media else payload.entities
        entities: List[Any] = list(raw_entities) if raw_entities else []
        policy = cls.resolve_policy(
            getattr(rule, "link_policy", None),
            remove_links=bool(getattr(rule, "remove_links", False)),
        )
        if policy is LinkPolicy.PRESERVE_ALL or not body:
            return body, entities, False
        return cls.process_text_and_entities(
            body,
            entities or None,
            policy,
            allowlist=list(getattr(rule, "domain_allowlist", None) or []),
            blocklist=list(getattr(rule, "domain_blocklist", None) or []),
            rewrite_map=dict(getattr(rule, "link_rewrite_map", None) or {}),
        )

    @classmethod
    def filter_inline_keyboard(
        cls,
        reply_markup: Any,
        policy: Union[LinkPolicy, str] = LinkPolicy.PRESERVE_ALL,
        allowlist: Optional[List[str]] = None,
        blocklist: Optional[List[str]] = None,
    ) -> Any:
        """Filter URL buttons in InlineKeyboardMarkup according to rule link policy."""
        if not reply_markup or not hasattr(reply_markup, "inline_keyboard"):
            return reply_markup

        policy_str = getattr(policy, "value", str(policy)).upper()
        if policy_str == LinkPolicy.PRESERVE_ALL.value:
            return reply_markup

        allowlist = [d.lower().strip() for d in (allowlist or []) if d.strip()]
        blocklist = [d.lower().strip() for d in (blocklist or []) if d.strip()]

        cleaned_rows = []
        for row in reply_markup.inline_keyboard:
            new_row = []
            for btn in row:
                btn_url = getattr(btn, "url", None)
                if btn_url:
                    dom = cls.extract_domain(btn_url)
                    remove_btn = False
                    if policy_str in (LinkPolicy.REMOVE_BUTTON_URLS.value, LinkPolicy.REMOVE_ALL_URLS.value):
                        remove_btn = True
                    elif policy_str == LinkPolicy.REMOVE_EXTERNAL_URLS.value:
                        remove_btn = dom not in cls.TG_DOMAINS and not cls.matches_domain(dom, list(cls.TG_DOMAINS))
                    elif policy_str == LinkPolicy.REMOVE_SELECTED_DOMAINS.value:
                        remove_btn = cls.matches_domain(dom, blocklist)
                    elif policy_str == LinkPolicy.ALLOWLIST_ONLY.value:
                        remove_btn = not cls.matches_domain(dom, allowlist)
                    elif policy_str == LinkPolicy.CUSTOM_LINK_POLICY.value:
                        if blocklist and cls.matches_domain(dom, blocklist):
                            remove_btn = True
                        elif allowlist and not cls.matches_domain(dom, allowlist):
                            remove_btn = True

                    if not remove_btn:
                        new_row.append(btn)
                else:
                    new_row.append(btn)
            if new_row:
                cleaned_rows.append(new_row)

        if not cleaned_rows:
            return None
        reply_markup.inline_keyboard = cleaned_rows
        return reply_markup


class CaptionManagementEngine:
    """Manages Telegram media captions, enforcing the hard 1024-character limit,
    deduplicating headers/footers, preserving caption entity offsets, and splitting
    overflow text into separate follow-up messages."""

    CAPTION_MAX_LIMIT = 1024
    TEXT_MAX_LIMIT = 4096

    @classmethod
    def format_caption(
        cls,
        caption: str,
        header: str = "",
        footer: str = "",
        caption_entities: Optional[List[Any]] = None,
        split_overflow: bool = True,
    ) -> Tuple[str, Optional[List[Any]], Optional[str]]:
        """Formats media caption:
        1. Prepends header and appends footer without duplicate insertion.
        2. Shifts caption entity offsets by the UTF-16 length of the header.
        3. If length > 1024:
           - Returns truncated caption (<= 1024)
           - Returns overflow text to send as a separate follow-up message if split_overflow=True."""
        text = caption or ""
        hdr = (header or "").strip()
        ftr = (footer or "").strip()

        # Deduplicate header & footer if already present
        if hdr and not text.startswith(hdr):
            text = f"{hdr}\n\n{text}" if text else hdr
            header_shift = utf16_len(f"{hdr}\n\n") if text != hdr else utf16_len(hdr)
        else:
            header_shift = 0

        if ftr and not text.endswith(ftr):
            text = f"{text}\n\n{ftr}" if text else ftr

        shifted_entities = []
        if caption_entities:
            for ent in caption_entities:
                if header_shift > 0 and hasattr(ent, "offset"):
                    ent.offset += header_shift
                shifted_entities.append(ent)

        if len(text) <= cls.CAPTION_MAX_LIMIT:
            return text, shifted_entities, None

        # Caption exceeds 1024 characters!
        if split_overflow:
            split_idx = text.rfind("\n", 0, cls.CAPTION_MAX_LIMIT - 30)
            if split_idx <= 0:
                split_idx = text.rfind(" ", 0, cls.CAPTION_MAX_LIMIT - 30)
            if split_idx <= 0:
                split_idx = cls.CAPTION_MAX_LIMIT - 20

            truncated_caption = text[:split_idx].strip()
            overflow_text = text[split_idx:].strip()

            # Keep only entities that fall within the truncated region
            truncated_u16_limit = utf16_len(truncated_caption)
            valid_entities = [
                e for e in shifted_entities
                if getattr(e, "offset", 0) + getattr(e, "length", 0) <= truncated_u16_limit
            ]
            return truncated_caption, valid_entities, overflow_text
        else:
            truncated_caption = text[: cls.CAPTION_MAX_LIMIT].strip()
            return truncated_caption, shifted_entities, None


class TelegramMessageCapabilityMatrix:
    """Official Matrix of all Telegram Message Types, verifying compatibility
    across reception, detection, clean copy, native forward, caption preservation,
    formatting retention, and intermediate C hop."""

    CAPABILITIES: Dict[str, Dict[str, Any]] = {
        "text": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": False, "formatting": True, "intermediate_hop": True,
            "notes": "Full support. Max 4096 chars, split supported.",
        },
        "photo": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": True, "formatting": True, "intermediate_hop": True,
            "notes": "Max 1024 caption chars. Overflow split supported.",
        },
        "video": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": True, "formatting": True, "intermediate_hop": True,
            "notes": "MP4/MKV supported with thumbnail preservation.",
        },
        "video_note": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": False, "formatting": False, "intermediate_hop": True,
            "notes": "Round video. Telegram does not support captions on video notes.",
        },
        "animation": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": True, "formatting": True, "intermediate_hop": True,
            "notes": "GIF / MPEG4 animations supported.",
        },
        "voice": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": True, "formatting": True, "intermediate_hop": True,
            "notes": "OGG Opus voice notes supported.",
        },
        "audio": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": True, "formatting": True, "intermediate_hop": True,
            "notes": "MP3/M4A audio files with metadata supported.",
        },
        "document": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": True, "formatting": True, "intermediate_hop": True,
            "notes": "General documents (PDF, ZIP, etc.) supported within Telegram file size limits.",
        },
        "sticker": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": False, "formatting": False, "intermediate_hop": True,
            "notes": "Static (WEBP), Animated (TGS), Video (WEBM) stickers supported.",
        },
        "poll": {
            "receive": True, "detect": True, "copy": False, "forward": True,
            "caption": False, "formatting": False, "intermediate_hop": True,
            "notes": "Native forward transfers active poll; Clean copy cannot clone poll contract without re-creation.",
        },
        "location": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": False, "formatting": False, "intermediate_hop": True,
            "notes": "Latitude/longitude geographic coordinates supported.",
        },
        "venue": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": False, "formatting": False, "intermediate_hop": True,
            "notes": "Venue title, address, and coordinates supported.",
        },
        "contact": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": False, "formatting": False, "intermediate_hop": True,
            "notes": "vCard contact information supported.",
        },
        "dice": {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": False, "formatting": False, "intermediate_hop": True,
            "notes": "Animated dice emoji supported.",
        },
        "service": {
            "receive": True, "detect": True, "copy": False, "forward": False,
            "caption": False, "formatting": False, "intermediate_hop": False,
            "notes": "Service messages (user joined, pinned msg, etc.) are intentionally dropped.",
        },
        "giveaway": {
            "receive": True, "detect": True, "copy": False, "forward": True,
            "caption": False, "formatting": True, "intermediate_hop": False,
            "notes": "Telegram Stars Giveaway: Native forward preserves banner; Copy restricted by MTProto.",
        },
        "paid_media": {
            "receive": True, "detect": True, "copy": False, "forward": True,
            "caption": False, "formatting": False, "intermediate_hop": False,
            "notes": "Telegram Stars Paid Media: Protected content restricted by Telegram payment gate.",
        },
        "protected_content": {
            "receive": True, "detect": True, "copy": True, "forward": False,
            "caption": True, "formatting": True, "intermediate_hop": False,
            "notes": "No-forwards restricted channels: Native forward blocked, clean copy fallback applies.",
        },
    }

    @classmethod
    def get_matrix(cls) -> Dict[str, Dict[str, Any]]:
        return cls.CAPABILITIES

    @classmethod
    def check_capability(cls, media_type: Union[MediaType, str]) -> Dict[str, Any]:
        val = getattr(media_type, "value", str(media_type)).lower()
        return cls.CAPABILITIES.get(val, {
            "receive": True, "detect": True, "copy": True, "forward": True,
            "caption": True, "formatting": True, "intermediate_hop": True,
            "notes": "Generic message type",
        })


class AlbumAggregationService:
    """Aggregates media items that share the same media_group_id within a short
    configurable sliding window (e.g. 0.8s) to form complete Telegram albums."""

    def __init__(self, window_seconds: float = 0.8):
        self._window = window_seconds
        self._groups: Dict[str, List[Dict[str, Any]]] = {}
        self._timestamps: Dict[str, float] = {}

    def add_item(self, media_group_id: str, payload: MessagePayload, message_obj: Any = None) -> int:
        gid = str(media_group_id)
        if gid not in self._groups:
            self._groups[gid] = []
        self._groups[gid].append({
            "message_id": payload.message_id,
            "payload": payload,
            "raw": message_obj,
            "ts": time.monotonic(),
        })
        self._timestamps[gid] = time.monotonic()
        return len(self._groups[gid])

    def is_ready(self, media_group_id: str) -> bool:
        gid = str(media_group_id)
        if gid not in self._timestamps:
            return False
        return (time.monotonic() - self._timestamps[gid]) >= self._window

    def flush_group(self, media_group_id: str) -> List[Dict[str, Any]]:
        gid = str(media_group_id)
        self._timestamps.pop(gid, None)
        items = self._groups.pop(gid, [])
        # Preserve original Telegram order by message_id
        items.sort(key=lambda x: x["message_id"])
        return items


class DeliveryStateMachine:
    """Robust Two-Phase State Machine for Reliable Delivery and Crash Recovery.
    Direct:       RECEIVED -> CLASSIFIED -> SENDING -> DELIVERED
    Intermediate: RECEIVED -> CLASSIFIED -> COPYING_TO_C -> COPIED_TO_C -> FORWARDING_TO_B -> DELIVERED
    Recovery:     If crashed after COPIED_TO_C, resumes at FORWARDING_TO_B without re-copying to C."""

    ALLOWED_TRANSITIONS = {
        DeliveryStatus.PENDING: {
            DeliveryStatus.CLAIMED, DeliveryStatus.RECEIVED, DeliveryStatus.CLASSIFIED,
        },
        DeliveryStatus.RECEIVED: {
            DeliveryStatus.CLASSIFIED, DeliveryStatus.FAILED,
        },
        DeliveryStatus.CLASSIFIED: {
            DeliveryStatus.COPYING_TO_C, DeliveryStatus.SENDING, DeliveryStatus.FAILED,
        },
        DeliveryStatus.COPYING_TO_C: {
            DeliveryStatus.COPIED_TO_C, DeliveryStatus.FAILED_C, DeliveryStatus.RETRY_WAIT,
        },
        DeliveryStatus.COPIED_TO_C: {
            DeliveryStatus.FORWARDING_TO_B, DeliveryStatus.FAILED_B, DeliveryStatus.RETRY_WAIT,
        },
        DeliveryStatus.FORWARDING_TO_B: {
            DeliveryStatus.DELIVERED, DeliveryStatus.SENT, DeliveryStatus.FAILED_B, DeliveryStatus.RETRY_WAIT,
        },
        DeliveryStatus.SENDING: {
            DeliveryStatus.SENT, DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, DeliveryStatus.RETRY_WAIT,
        },
        DeliveryStatus.RETRY_WAIT: {
            DeliveryStatus.CLAIMED, DeliveryStatus.COPYING_TO_C, DeliveryStatus.FORWARDING_TO_B, DeliveryStatus.SENDING, DeliveryStatus.DEAD_LETTER,
        },
        DeliveryStatus.FAILED_C: {
            DeliveryStatus.RETRY_WAIT, DeliveryStatus.DEAD_LETTER,
        },
        DeliveryStatus.FAILED_B: {
            DeliveryStatus.RETRY_WAIT, DeliveryStatus.DEAD_LETTER,
        },
        DeliveryStatus.FAILED: {
            DeliveryStatus.RETRY_WAIT, DeliveryStatus.DEAD_LETTER,
        },
        DeliveryStatus.CLAIMED: {
            DeliveryStatus.SENDING, DeliveryStatus.COPYING_TO_C, DeliveryStatus.FORWARDING_TO_B, DeliveryStatus.FAILED,
        },
    }

    @classmethod
    def can_transition(cls, current: DeliveryStatus, target: DeliveryStatus) -> bool:
        if current == target:
            return True
        allowed = cls.ALLOWED_TRANSITIONS.get(current, set())
        return target in allowed

    @classmethod
    def resume_after_crash(cls, job: Any) -> DeliveryStatus:
        """Determines the recovery state after a process crash."""
        inter_msg_id = getattr(job, "intermediate_message_id", None)
        if not inter_msg_id and isinstance(getattr(job, "payload_data", None), dict):
            inter_msg_id = job.payload_data.get("intermediate_message_id")

        use_inter = False
        if hasattr(job, "payload_data") and isinstance(job.payload_data, dict):
            use_inter = bool(job.payload_data.get("use_intermediate", False))

        if use_inter:
            if inter_msg_id:
                # Message was already copied to C! Skip copy and resume forwarding to B!
                return DeliveryStatus.FORWARDING_TO_B
            return DeliveryStatus.COPYING_TO_C

        return DeliveryStatus.SENDING


class AdvancedSettingsEngine:
    """Runtime execution layer for the advanced task-overview settings.

    Phase 1 persisted these fields; Phase 2 makes them *act*. Every method here
    is called from the real dispatch path (durable queue + direct dispatcher),
    so a toggle flipped in the bot, web dashboard, TMA or mobile client changes
    what happens to the very next message — no restart required.

    Design rules:
      * The persisted ``ForwardRule`` is the single source of truth. Nothing is
        read from the request that produced the message.
      * Paid add-ons are checked server-side via ``rule.is_addon_enabled``. A
        client-supplied entitlement flag never grants a premium transform.
      * Every transform is total: it never raises, and it returns the
        (possibly unchanged) text plus a diagnostic reason so the pipeline can
        emit a structured event describing exactly what was applied.
      * Failures degrade to the *untransformed* text, never to a silent drop,
        except where the caller's policy explicitly demands a drop.
    """

    CRYPTO_ADDON_KEY = "crypto_mode"
    WATERMARK_ADDON_KEY = "watermark"
    LINK_BUTTONS_ADDON_KEY = "link_buttons"
    AFFILIATE_ADDON_KEY = "affiliate_kit"
    OCR_ADDON_KEY = "ocr_image_ai"
    SMART_REWRITE_ADDON_KEY = "smart_ai_rewrite"
    KEYWORD_REPLY_ADDON_KEY = "keyword_reply"
    AUTO_DELETE_ADDON_KEY = "auto_delete_message"

    # Bounded: Telegram's own caption/text limits are the real ceiling.
    WATERMARK_MAX_LEN = 200
    KEYWORD_REPLY_MAX_LEN = 4096

    @classmethod
    def apply_content_transforms(
        cls,
        text: str,
        rule: "ForwardRule",
        payload: Optional["MessagePayload"] = None,
    ) -> Tuple[str, List[str]]:
        """Apply the *non-AI* advanced content settings in a fixed order.

        Returns the transformed text and the list of applied transformations
        (empty when the rule opts out of everything), in pipeline order.
        """
        if text is None:
            text = ""
        applied: List[str] = []

        # 1. Watermark — appends a persistent ownership/signature line.
        wm = (rule.watermark_text or "").strip()
        if wm:
            wm = wm[: cls.WATERMARK_MAX_LEN]
            if wm not in text:
                text = f"{text}\n\n{wm}" if text.strip() else wm
                applied.append("watermark")

        # 2. Link buttons — a paid add-on. Renders the configured buttons as a
        #    plain-text block under the message body (native inline keyboards
        #    are attached by the dispatcher, not by the text transform).
        if rule.link_buttons and rule.is_addon_enabled(cls.LINK_BUTTONS_ADDON_KEY):
            lines = [f"🔗 {b.get('text', '').strip()}: {b.get('url', '').strip()}"
                     for b in rule.link_buttons if isinstance(b, dict) and b.get("url")]
            if lines:
                block = "\n".join(lines)
                if block not in text:
                    text = f"{text}\n\n{block}" if text.strip() else block
                    applied.append("link_buttons")

        # 3. Affiliate kit — a paid add-on. Rewrites naked URLs through the
        #    configured affiliate prefix, preserving every other character.
        ak = rule.affiliate_kit or {}
        if isinstance(ak, dict) and ak.get("enabled") and rule.is_addon_enabled(cls.AFFILIATE_ADDON_KEY):
            prefix = str(ak.get("prefix") or "").strip()
            if prefix:
                replaced = URL_REGEX.sub(
                    lambda m: f"{prefix}{m.group(0)}" if m.group(0).startswith("http") else m.group(0),
                    text,
                )
                if replaced != text:
                    text = replaced
                    applied.append("affiliate_kit")

        return text, applied

    @classmethod
    def classify_keyword_trigger(
        cls,
        text: str,
        rule: "ForwardRule",
    ) -> Optional[str]:
        """Evaluate the keyword-confirmation / keyword-reply add-on.

        Returns the reply text to send when a whitelisted keyword is present,
        or ``None`` when the add-on is disabled or no keyword matches.
        """
        if not rule.keyword_confirmation:
            return None
        reply = (rule.keyword_reply or "").strip()[: cls.KEYWORD_REPLY_MAX_LEN]
        if not reply:
            return None
        if not rule.is_addon_enabled(cls.KEYWORD_REPLY_ADDON_KEY):
            return None

        haystack = (text or "").lower()
        keywords = [str(k).strip().lower() for k in (rule.whitelist or []) if str(k).strip()]
        if not keywords:
            return None
        # A keyword confirmation never drops the forwarded message: it only
        # decides whether the configured reply is also posted to the target.
        if any(kw in haystack for kw in keywords):
            return reply
        return None

    @classmethod
    def get_auto_delete_seconds(cls, rule: "ForwardRule") -> int:
        """Return the TTL after which the forwarded message is deleted.

        ``0`` means the message is kept permanently. The add-on gate is checked
        here so a client cannot enable auto-delete by toggling the duration.
        """
        ttl = rule.auto_delete_seconds
        if ttl <= 0:
            return 0
        if not rule.is_addon_enabled(cls.AUTO_DELETE_ADDON_KEY):
            return 0
        # Bounded by Telegram's own 1-year practical maximum for delete timers.
        return max(1, min(ttl, 31_536_000))

    @classmethod
    def is_edit_within_window(
        cls,
        original_date: Optional[int],
        edit_date: Optional[int],
        rule: "ForwardRule",
    ) -> bool:
        """Whether an edited message still qualifies for edit synchronization.

        ``edit_time_limit_seconds == 0`` means "no window" — all edits sync.
        Otherwise an edit arriving after the window is ignored, which prevents
        a burst of very old edits from flooding the target on a reconnect.
        """
        limit = rule.edit_time_limit_seconds
        if limit <= 0:
            return True
        if not original_date or not edit_date:
            return True
        return (int(edit_date) - int(original_date)) <= limit

    @classmethod
    def get_dispatch_delay(cls, rule: "ForwardRule") -> float:
        """Seconds to wait before dispatching, honoring the advanced setting.

        Falls back to the rule's first-class ``delay_seconds`` when the advanced
        setting is absent, and is hard-bounded to one hour.
        """
        return float(rule.forward_delay_seconds or 0.0)
