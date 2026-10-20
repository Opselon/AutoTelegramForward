"""Prompt assembly for the auto-reply engine.

The system prompt is built from four inputs, in this order of precedence:

1. the assigned personal prompt (per-chat override → matching rule → the
   account default → the built-in persona),
2. the effective tone and response length,
3. the language (explicit, or detected from what the contact just wrote),
4. what the responder remembers about that conversation.

The built prompt is bounded: a chat message can be long, a user's prompt can be
long, but the request that leaves this process must stay inside a sane budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ...domain.autoreply import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_TEMPLATE,
    LANGUAGE_NAMES,
    LENGTH_DIRECTIVES,
    TONE_DIRECTIVES,
    AutoReplyPrompt,
    AutoReplyRule,
    AutoReplySettings,
    ResponseLength,
    Tone,
    detect_language,
)

#: Hard ceiling for the assembled system prompt (characters).
MAX_SYSTEM_PROMPT_CHARS = 9000
#: Hard ceiling for one inbound text carried into the prompt.
MAX_INBOUND_CHARS = 4000


@dataclass
class PromptContext:
    """Everything the prompt needs, resolved once per reply."""

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tone: str = Tone.FRIENDLY.value
    response_length: str = ResponseLength.SHORT.value
    language: str = "en"
    prompt_id: Optional[str] = None
    ai_config_id: Optional[str] = None
    rule_id: Optional[str] = None
    cooldown_seconds: int = 0
    temperature: float = 0.7
    max_tokens: int = 300
    max_reply_chars: int = 1200
    user_template: str = DEFAULT_USER_TEMPLATE
    history: List[Dict[str, str]] = field(default_factory=list)
    memory: List[str] = field(default_factory=list)
    correlation_id: str = ""
    inbound: str = ""
    sender: str = ""


def resolve_language(settings: AutoReplySettings, prompt: Optional[AutoReplyPrompt],
                     rule: Optional[AutoReplyRule], text: str) -> str:
    """Effective reply language: rule → prompt → settings → detected."""
    for candidate in (
        getattr(rule, "language", "") if rule else "",
        getattr(prompt, "language", "") if prompt else "",
        settings.language,
    ):
        value = str(candidate or "").strip().lower()
        if value:
            return value
    return detect_language(text)


def _identity_block(settings: AutoReplySettings, prompt: Optional[AutoReplyPrompt]) -> str:
    base = (prompt.system_prompt if prompt and prompt.system_prompt.strip()
            else DEFAULT_SYSTEM_PROMPT).strip()
    return base


def _directive_block(tone: str, length: str, language: str,
                     memory: Sequence[str], extra: str) -> List[str]:
    parts: List[str] = []
    tone_line = TONE_DIRECTIVES.get(tone)
    if tone_line:
        parts.append(f"Tone: {tone_line}")
    length_line = LENGTH_DIRECTIVES.get(length)
    if length_line:
        parts.append(f"Length: {length_line}")
    language_name = LANGUAGE_NAMES.get(language, language or "English")
    parts.append(f"Write the reply in {language_name}.")
    if memory:
        # Bounded, and only what the tenant stored: never model-invented facts.
        remembered = "\n".join(f"- {fact}" for fact in list(memory)[:20])
        parts.append("Things you remember about this conversation:\n" + remembered)
    extra = (extra or "").strip()
    if extra:
        parts.append("Owner instructions (highest priority after safety):\n" + extra)
    return parts


def build_system_prompt(settings: AutoReplySettings, prompt: Optional[AutoReplyPrompt] = None,
                        rule: Optional[AutoReplyRule] = None, memory: Sequence[str] = (),
                        language: str = "", text: str = "") -> str:
    """Assemble the system prompt for one reply."""
    eff_language = language or resolve_language(settings, prompt, rule, text)
    tone = (rule.tone.value if (rule and rule.tone) else
            prompt.tone.value if (prompt and prompt.tone) else settings.tone.value)
    length = (rule.response_length.value if (rule and rule.response_length) else
              prompt.response_length.value if (prompt and prompt.response_length) else
              settings.response_length.value)
    blocks = [_identity_block(settings, prompt)]
    blocks.extend(_directive_block(tone, length, eff_language, memory, settings.extra_instructions))
    out = "\n\n".join(block for block in blocks if block)
    if len(out) > MAX_SYSTEM_PROMPT_CHARS:
        # Keep the head (identity + tone) and the tail (language + owner rules):
        # both ends carry instruction the model must see.
        head = out[: MAX_SYSTEM_PROMPT_CHARS // 2]
        tail = out[-MAX_SYSTEM_PROMPT_CHARS // 2:]
        out = f"{head}\n[…]\n{tail}"
    return out


def render_user_template(template: str, text: str, sender: str = "") -> str:
    """Fill the prompt's user template without letting it remove the message."""
    raw = (template or DEFAULT_USER_TEMPLATE)
    if "{text}" not in raw:
        raw = raw.rstrip() + "\n\n{text}"
    safe_text = (text or "")[:MAX_INBOUND_CHARS]
    out = raw.replace("{text}", safe_text)
    out = out.replace("{sender}", sender or "")
    return out


def build_messages(system_prompt: str, history: Sequence[Dict[str, str]],
                   inbound_text: str, template: str = DEFAULT_USER_TEMPLATE,
                   sender: str = "") -> List[Dict[str, str]]:
    """Chat-completion messages: system, bounded history, then the new message.

    ``history`` is expected oldest-first and already free of the inbound message;
    the caller passes the rolling window from the database. Roles are
    normalised because a prompt template is text, not a message role.
    """
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for item in history or []:
        role = str(item.get("role") or "user").lower()
        if role not in ("user", "assistant"):
            role = "user"
        content = str(item.get("content") or "").strip()
        if content:
            messages.append({"role": role, "content": content[:MAX_INBOUND_CHARS]})
    messages.append({"role": "user", "content": render_user_template(template, inbound_text, sender)})
    return messages


def redact_for_prompt(text: str) -> str:
    """Strip obvious credentials from text that will leave the process.

    A contact can paste a token or a card number into a chat; the responder
    should answer the message, not forward the secret to a third-party model.
    """
    if not text:
        return ""
    out = re.sub(r"\b(?:sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    out = re.sub(r"\b\d{13,19}\b", "[redacted-number]", out)
    out = re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._-]{12,}", r"\1[redacted]", out)
    return out


__all__ = [
    "MAX_INBOUND_CHARS",
    "MAX_SYSTEM_PROMPT_CHARS",
    "PromptContext",
    "build_messages",
    "build_system_prompt",
    "redact_for_prompt",
    "render_user_template",
    "resolve_language",
]
