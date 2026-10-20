"""The inbound decision path.

Runs on the dispatcher's hot path, so it does the minimum: pure decisions plus
one indexed read per gate and a single insert. The model call never happens
here — that belongs to the worker, which is what makes a slow provider unable
to stall message ingestion.

Order of gates (first failure wins, and the reason is logged):

1. message shape      — private, not self, not a bot, has text
2. account switches   — enabled, active day, quiet hours
3. targeting          — mode + per-chat switch
4. rules              — ranked deterministically; a SKIP rule stops here
5. pacing             — cooldown, per-chat daily cap, per-account rate limit
6. idempotency        — one job per inbound message, enforced by a unique index
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from ...domain.autoreply import (
    AutoReplyJob,
    AutoReplyPrompt,
    AutoReplyRule,
    AutoReplySettings,
    ContactTarget,
    IncomingMessage,
    JobStatus,
    rank_rules,
)
from .context import redact_for_prompt
from .targeting import is_targeted

logger = logging.getLogger("atf.autoreply.engine")


def _new_correlation_id() -> str:
    return f"ar-{uuid.uuid4().hex[:16]}"


@dataclass
class AutoReplyDecision:
    """The outcome of running one inbound message through the gates."""

    reply: bool = False
    reason: str = ""
    rule: Optional[AutoReplyRule] = None
    prompt_id: Optional[str] = None
    ai_config_id: Optional[str] = None
    cooldown_seconds: int = 0
    correlation_id: str = ""
    job_id: str = ""
    duplicate: bool = False
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reply": self.reply,
            "reason": self.reason,
            "rule_id": self.rule.id if self.rule else "",
            "prompt_id": self.prompt_id or "",
            "ai_config_id": self.ai_config_id or "",
            "cooldown_seconds": self.cooldown_seconds,
            "correlation_id": self.correlation_id,
            "job_id": self.job_id,
            "duplicate": self.duplicate,
            "latency_ms": self.latency_ms,
        }


def decide(settings: AutoReplySettings, targets: Sequence[ContactTarget],
           rules: Sequence[AutoReplyRule], message: IncomingMessage, *,
           history_count: int = 0, last_sent_at: int = 0, sent_today: int = 0,
           now: Optional[float] = None) -> Tuple[bool, str, Optional[AutoReplyRule]]:
    """Pure gate evaluation — no I/O, no clock reads beyond ``now``.

    Kept separate from :class:`AutoReplyEngine` so every branch (quiet hours,
    cooldown, rule precedence) is unit-testable without a database.
    """
    ts = time.time() if now is None else now
    if not settings.enabled:
        return False, "disabled", None
    if message.is_self:
        return False, "from_self", None
    if message.is_bot and settings.ignore_bots:
        return False, "from_bot", None
    if message.chat_type != "private":
        return False, "not_private", None
    text = (message.text or "").strip()
    if not text:
        return False, "empty_text", None
    if message.is_command:
        return False, "command", None
    if not settings.is_active_day(ts):
        return False, "inactive_day", None
    if settings.is_quiet_now(ts):
        return False, "quiet_hours", None

    allowed, target_reason = is_targeted(settings.target_mode, targets, message)
    if not allowed:
        return False, target_reason, None

    matched_rule: Optional[AutoReplyRule] = None
    matched = rank_rules(rules, message, history_count)
    if matched:
        matched_rule = matched[0]
        if matched_rule.action.value == "SKIP":
            return False, f"rule_skip:{matched_rule.id}", matched_rule

    cooldown = (matched_rule.cooldown_seconds
                if matched_rule is not None and matched_rule.cooldown_seconds >= 0
                else settings.cooldown_seconds)
    if cooldown > 0 and last_sent_at and ts - last_sent_at < cooldown:
        return False, "cooldown", matched_rule
    if settings.daily_limit_per_chat > 0 and sent_today >= settings.daily_limit_per_chat:
        return False, "daily_limit", matched_rule

    return True, "eligible", matched_rule


class AutoReplyEngine:
    """Decides and enqueues; never calls a model and never sends a message."""

    def __init__(self, repo: Any, *, log_sink: Any = None, clock=time.time) -> None:
        self._repo = repo
        self._log = log_sink
        self._clock = clock
        self._recent: Dict[int, Deque[float]] = {}
        self._recent_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    def _emit(self, level: str, event: str, message: str, **fields: Any) -> None:
        detail = {"event": event, **{k: v for k, v in fields.items() if v not in (None, "")}}
        try:
            if self._log is not None:
                self._log.log(
                    level, "autoreply", "auto_reply",
                    message,
                    detail=detail,
                    rule_id=str(fields.get("rule_id") or ""),
                    user_id=int(fields.get("owner_user_id") or 0),
                    correlation_id=str(fields.get("correlation_id") or ""),
                    event_type=f"autoreply.{event}",
                )
                return
        except Exception:
            pass
        getattr(logger, level.lower(), logger.info)(
            "[AUTOREPLY:%s] %s %s", event, message, detail
        )

    # ------------------------------------------------------------------ #
    # Rate limiting (per account, sliding one-minute window)
    # ------------------------------------------------------------------ #
    def attach_worker(self, worker: Any) -> None:
        """Bind the queue consumer so the dispatcher can register transports."""
        self._worker = worker

    def attach_log(self, log_sink: Any) -> None:
        """Bind the structured sink once it exists (it is built after this graph)."""
        self._log = log_sink
    def _allow_rate(self, owner_user_id: int, per_minute: int, now: float) -> bool:
        if per_minute <= 0:
            return True
        window_start = now - 60.0
        with self._recent_lock:
            bucket = self._recent.setdefault(int(owner_user_id), deque())
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= per_minute:
                return False
            bucket.append(now)
            return True

    def reset_rate(self, owner_user_id: Optional[int] = None) -> None:
        with self._recent_lock:
            if owner_user_id is None:
                self._recent.clear()
            else:
                self._recent.pop(int(owner_user_id), None)

    # ------------------------------------------------------------------ #
    # Hot path
    # ------------------------------------------------------------------ #
    async def handle_incoming(self, message: IncomingMessage) -> AutoReplyDecision:
        started = self._clock()
        correlation_id = _new_correlation_id()
        owner = int(getattr(message, "owner_user_id", 0) or 0)
        # No linked account means no tenant to attribute a reply (or a job row)
        # to: fail closed before reading anything owner-scoped.
        if owner <= 0:
            decision = AutoReplyDecision(
                reply=False, reason="no_owner", correlation_id=correlation_id,
            )
            decision.latency_ms = (self._clock() - started) * 1000
            return decision
        settings = self._repo.get_settings(owner)
        targets = self._repo.list_targets(owner)
        rules = self._repo.list_rules(owner)

        history_count = self._repo.count_messages(owner, message.chat_id)
        last_sent_at = self._repo.last_sent_at(owner, message.chat_id)
        sent_today = self._repo.sent_since(owner, message.chat_id, _start_of_local_day(
            self._clock(), settings.timezone_offset_minutes))

        allowed, reason, rule = decide(
            settings, targets, rules, message,
            history_count=history_count, last_sent_at=last_sent_at,
            sent_today=sent_today, now=self._clock(),
        )
        decision = AutoReplyDecision(
            reply=False, reason=reason, rule=rule, correlation_id=correlation_id,
            prompt_id=settings.prompt_id, ai_config_id=settings.ai_config_id,
        )
        if not allowed:
            self._emit("debug", "skipped", "auto-reply skipped", owner_user_id=owner,
                       chat_id=message.chat_id, message_id=message.message_id,
                       reason=reason, correlation_id=correlation_id)
            decision.latency_ms = (self._clock() - started) * 1000
            return decision

        if not self._allow_rate(owner, settings.rate_limit_per_minute, self._clock()):
            decision.reason = "rate_limited"
            self._emit("warning", "rate_limited", "auto-reply rate limit reached",
                       owner_user_id=owner, chat_id=message.chat_id,
                       correlation_id=correlation_id)
            decision.latency_ms = (self._clock() - started) * 1000
            return decision

        # The rule may override the prompt, provider, and cooldown for this chat.
        target = self._repo.get_target_by_chat(message.chat_id, owner)
        prompt_id = (target.prompt_id if target and target.prompt_id else
                     (rule.prompt_id if rule and rule.prompt_id else settings.prompt_id))
        ai_config_id = (rule.ai_config_id if rule and rule.ai_config_id else settings.ai_config_id)
        cooldown = (rule.cooldown_seconds if rule is not None and rule.cooldown_seconds >= 0
                    else settings.cooldown_seconds)
        decision.prompt_id = prompt_id
        decision.ai_config_id = ai_config_id
        decision.cooldown_seconds = cooldown

        # Context is stored only for conversations that are actually answered:
        # an ignored chat must not accumulate a transcript.
        try:
            self._repo.append_message(
                owner, message.chat_id, "user", redact_for_prompt(message.text),
                message.message_id,
            )
        except Exception:
            logger.debug("autoreply context append failed", exc_info=True)

        job = AutoReplyJob(
            owner_user_id=owner,
            session_id=message.session_id,
            chat_id=message.chat_id,
            chat_title=message.sender_name or "",
            source_message_id=message.message_id,
            status=JobStatus.PENDING,
            max_attempts=settings.max_attempts,
            next_attempt_at=int(self._clock()),
            prompt_id=prompt_id,
            rule_id=rule.id if rule else None,
            ai_config_id=ai_config_id,
            inbound_text=redact_for_prompt(message.text)[:4000],
            correlation_id=correlation_id,
        )
        created, stored = self._repo.enqueue(job)
        decision.reply = True
        decision.duplicate = not created
        decision.job_id = (stored.id if stored else job.id)
        decision.reason = "enqueued" if created else "duplicate"
        self._emit(
            "info", "enqueued" if created else "duplicate", "auto-reply job enqueued",
            owner_user_id=owner, chat_id=message.chat_id, message_id=message.message_id,
            job_id=decision.job_id, rule_id=rule.id if rule else "",
            prompt_id=prompt_id, correlation_id=correlation_id,
        )
        decision.latency_ms = (self._clock() - started) * 1000
        return decision


def _start_of_local_day(now: float, tz_offset_minutes: int) -> int:
    """Epoch seconds of local midnight, for the per-chat daily cap."""
    local = now + tz_offset_minutes * 60
    day_start_local = int(local // 86400) * 86400
    return int(day_start_local - tz_offset_minutes * 60)


__all__ = ["AutoReplyDecision", "AutoReplyEngine", "decide"]
