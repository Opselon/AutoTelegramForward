"""AI Output Validator and Sanitizer.

Validates and sanitizes AI transform outputs to guarantee:
1. Strict adherence to Telegram character caps (4096 text / 1024 caption).
2. Defense against Prompt Injection leaks, system instruction echoes, and delimiter breaches.
3. Rejection of empty or invalid output.
4. Stripping of hazardous tags (<script>, <style>, raw execution tags).
5. HTML entity balance and safety.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Optional, Tuple

MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

# Dangerous patterns and tag injections
DANGEROUS_TAGS_PATTERN = re.compile(
    r"<\s*(script|style|iframe|embed|object|applet)[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
UNTRUSTED_DELIMITER_PATTERN = re.compile(
    r"</?(?:untrusted_user_message|system_instructions|admin_prompt)[^>]*>",
    re.IGNORECASE,
)
SYSTEM_PROMPT_LEAK_PATTERN = re.compile(
    r"\b(?:my\s+system\s+prompt|system\s+instructions|system\s+prompt)\s*:",
    re.IGNORECASE,
)
JAVASCRIPT_URI_PATTERN = re.compile(r"javascript\s*:", re.IGNORECASE)


@dataclass
class ValidationResult:
    is_valid: bool
    sanitized_text: str
    error_message: str = ""
    error_code: str = ""  # EMPTY_OUTPUT, EXCEEDS_LENGTH, INJECTION_LEAK, DANGEROUS_CONTENT


class AIOutputValidator:
    """Validates and cleans AI transformation output before forwarding."""

    def __init__(
        self,
        max_text_len: int = MAX_TEXT_LENGTH,
        max_caption_len: int = MAX_CAPTION_LENGTH,
    ) -> None:
        self.max_text_len = max_text_len
        self.max_caption_len = max_caption_len

    def validate(
        self,
        text: Optional[str],
        is_caption: bool = False,
        allow_truncate: bool = True,
    ) -> ValidationResult:
        """Validates AI transformed text.

        Returns ValidationResult with is_valid=True and sanitized_text,
        or is_valid=False with error_code and error_message.
        """
        if text is None:
            return ValidationResult(
                is_valid=False,
                sanitized_text="",
                error_message="AI returned None output",
                error_code="EMPTY_OUTPUT",
            )

        cleaned = text.replace("\x00", "").strip()
        if not cleaned:
            return ValidationResult(
                is_valid=False,
                sanitized_text="",
                error_message="AI returned empty or whitespace-only text",
                error_code="EMPTY_OUTPUT",
            )

        # 1. Defense against injection delimiter leaks
        if UNTRUSTED_DELIMITER_PATTERN.search(cleaned) or SYSTEM_PROMPT_LEAK_PATTERN.search(cleaned):
            return ValidationResult(
                is_valid=False,
                sanitized_text="",
                error_message="AI output leaked internal delimiters or instructions",
                error_code="INJECTION_LEAK",
            )

        # 2. Check for dangerous script/iframe content
        if DANGEROUS_TAGS_PATTERN.search(cleaned) or JAVASCRIPT_URI_PATTERN.search(cleaned):
            cleaned = DANGEROUS_TAGS_PATTERN.sub("", cleaned)
            cleaned = JAVASCRIPT_URI_PATTERN.sub("", cleaned).strip()
            if not cleaned:
                return ValidationResult(
                    is_valid=False,
                    sanitized_text="",
                    error_message="AI output contained disallowed executable tags",
                    error_code="DANGEROUS_CONTENT",
                )

        # 3. Validate length limit (caption vs text)
        limit = self.max_caption_len if is_caption else self.max_text_len
        if len(cleaned) > limit:
            if allow_truncate:
                # Truncate cleanly at word boundary
                truncated = cleaned[:limit - 3]
                last_space = truncated.rfind(" ")
                if last_space > limit - 100 and last_space > 0:
                    truncated = truncated[:last_space]
                cleaned = truncated.rstrip() + "..."
            else:
                return ValidationResult(
                    is_valid=False,
                    sanitized_text="",
                    error_message=f"Output length ({len(cleaned)}) exceeds Telegram limit ({limit})",
                    error_code="EXCEEDS_LENGTH",
                )

        # 4. Check HTML tag balance if HTML formatting is used
        is_balanced, balance_err = self._check_html_tag_balance(cleaned)
        if not is_balanced:
            # Escape HTML to preserve content safely if tags are broken
            cleaned = html.escape(cleaned)

        return ValidationResult(
            is_valid=True,
            sanitized_text=cleaned,
        )

    @staticmethod
    def _check_html_tag_balance(text: str) -> Tuple[bool, str]:
        """Simple validation for standard Telegram HTML tags (<b>, <i>, <a>, <code>, <pre>)."""
        tags = re.findall(r"<(/?[a-zA-Z0-9]+)[^>]*>", text)
        if not tags:
            return True, ""

        stack = []
        void_tags = {"br", "hr", "img"}
        for t in tags:
            tag_name = t.lower()
            if tag_name.startswith("/"):
                closing = tag_name[1:]
                if closing in void_tags:
                    continue
                if not stack or stack[-1] != closing:
                    return False, f"Mismatched closing tag </{closing}>"
                stack.pop()
            else:
                if tag_name not in void_tags:
                    stack.append(tag_name)

        if stack:
            return False, f"Unclosed tags: {stack}"
        return True, ""
