"""Application use cases: orchestrate domain services + repository ports.

SRP: each class owns one business capability; CQRS-ish split per aggregate.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from ..domain.entities import (
    AIConfig,
    EvaluationResult,
    FilterRule,
    ForwardRule,
    MessagePayload,
    TelegramSession,
)
from ..domain.services import FilterEngine, RoutingPolicy
from .repositories import (
    IAIConfigRepository,
    IFilterRuleRepository,
    IForwardRuleRepository,
    ISessionRepository,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

class SessionUseCases:
    def __init__(self, repo: ISessionRepository) -> None:
        self._repo = repo

    async def create(self, phone_number: str, session_string_encrypted: str = "") -> TelegramSession:
        existing = await self._repo.get_by_phone(phone_number)
        if existing:
            existing.session_string_encrypted = session_string_encrypted
            return await self._repo.update(existing)
        return await self._repo.add(
            TelegramSession(phone_number=phone_number, session_string_encrypted=session_string_encrypted)
        )

    def create_sync(self, phone_number: str, session_string_encrypted: str = "") -> TelegramSession:
        import asyncio
        return asyncio.run(self.create(phone_number, session_string_encrypted))

    async def list_all(self) -> List[TelegramSession]:
        return await self._repo.list_all()

    async def list_active(self) -> List[TelegramSession]:
        return await self._repo.list_active()

    async def get(self, session_id: str) -> Optional[TelegramSession]:
        return await self._repo.get_by_id(session_id)

    async def activate(self, session_id: str) -> Optional[TelegramSession]:
        s = await self._repo.get_by_id(session_id)
        if s:
            s.activate()
            return await self._repo.update(s)
        return None

    async def deactivate(self, session_id: str) -> Optional[TelegramSession]:
        s = await self._repo.get_by_id(session_id)
        if s:
            s.deactivate()
            return await self._repo.update(s)
        return None

    async def delete(self, session_id: str) -> bool:
        return await self._repo.delete(session_id)


# --------------------------------------------------------------------------- #
# Forward rules
# --------------------------------------------------------------------------- #

class ForwardRuleUseCases:
    def __init__(self, repo: IForwardRuleRepository) -> None:
        self._repo = repo

    async def create(self, rule: ForwardRule) -> ForwardRule:
        return await self._repo.add(rule)

    async def update(self, rule: ForwardRule) -> ForwardRule:
        rule.mark_updated()
        return await self._repo.update(rule)

    async def get(self, rule_id: str) -> Optional[ForwardRule]:
        return await self._repo.get_by_id(rule_id)

    async def list_by_session(self, session_id: str) -> List[ForwardRule]:
        return await self._repo.list_by_session(session_id)

    async def list_all(self) -> List[ForwardRule]:
        return await self._repo.list_all()

    async def toggle(self, rule_id: str) -> Optional[ForwardRule]:
        r = await self._repo.get_by_id(rule_id)
        if r:
            r.is_active = not r.is_active
            r.mark_updated()
            return await self._repo.update(r)
        return None

    async def delete(self, rule_id: str) -> bool:
        return await self._repo.delete(rule_id)


# --------------------------------------------------------------------------- #
# Filter rules
# --------------------------------------------------------------------------- #

class FilterRuleUseCases:
    def __init__(self, repo: IFilterRuleRepository) -> None:
        self._repo = repo

    async def create(self, fr: FilterRule) -> FilterRule:
        return await self._repo.add(fr)

    async def update(self, fr: FilterRule) -> FilterRule:
        return await self._repo.update(fr)

    async def get(self, filter_id: str) -> Optional[FilterRule]:
        return await self._repo.get_by_id(filter_id)

    async def list_all(self) -> List[FilterRule]:
        return await self._repo.list_all()

    async def delete(self, filter_id: str) -> bool:
        return await self._repo.delete(filter_id)


# --------------------------------------------------------------------------- #
# AI configs
# --------------------------------------------------------------------------- #

class AIConfigUseCases:
    def __init__(self, repo: IAIConfigRepository, provider_factory) -> None:
        self._repo = repo
        self._factory = provider_factory

    async def create(self, cfg: AIConfig) -> AIConfig:
        return await self._repo.add(cfg)

    async def update(self, cfg: AIConfig) -> AIConfig:
        return await self._repo.update(cfg)

    async def get(self, config_id: str) -> Optional[AIConfig]:
        return await self._repo.get_by_id(config_id)

    async def list_all(self) -> List[AIConfig]:
        return await self._repo.list_all()

    async def delete(self, config_id: str) -> bool:
        return await self._repo.delete(config_id)

    async def test_rewrite(self, config_id: str, sample_text: str) -> tuple[bool, str]:
        cfg = await self._repo.get_by_id(config_id)
        if not cfg:
            return False, "config_not_found"
        try:
            provider = self._factory.get(cfg)
            return True, await provider.rewrite(cfg, sample_text)
        except Exception as exc:  # pragma: no cover - network path
            return False, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Message pipeline (the heart of the forwarder)
# --------------------------------------------------------------------------- #

@dataclass
class PipelineOutcome:
    forwarded: bool = False
    reason: str = ""
    evaluation: Optional[EvaluationResult] = None


@dataclass
class ForwardingStats:
    processed: int = 0
    forwarded: int = 0
    filtered: int = 0
    rewritten: int = 0
    errors: int = 0
    started_at: int = field(default_factory=lambda: int(time.time()))


class MessageForwardingUseCase:
    """End-to-end pipeline: match rules -> filter -> AI rewrite -> dispatch.

    The actual Telegram send is delegated to a `sender` callable (port) so
    this use case is fully unit-testable without any network.
    """

    def __init__(
        self,
        rule_repo: IForwardRuleRepository,
        filter_repo: IFilterRuleRepository,
        ai_repo: IAIConfigRepository,
        provider_factory,
        filter_engine: Optional[FilterEngine] = None,
        routing_policy: Optional[RoutingPolicy] = None,
    ) -> None:
        self._rule_repo = rule_repo
        self._filter_repo = filter_repo
        self._ai_repo = ai_repo
        self._factory = provider_factory
        self._filter_engine = filter_engine or FilterEngine()
        self._routing = routing_policy or RoutingPolicy()
        self.sender = None  # async callable: (payload, rule, text, eval_result) -> bool
        self.stats = ForwardingStats()

    async def process_message(self, payload: MessagePayload) -> PipelineOutcome:
        self.stats.processed += 1
        if payload.is_service:
            self.stats.filtered += 1
            return PipelineOutcome(False, "service_message")

        rules = await self._rule_repo.list_active_by_source(payload.chat_id)
        rules = self._routing.matching_rules(payload, rules)
        if not rules:
            return PipelineOutcome(False, "no_matching_rules")

        any_forwarded = False
        for rule in rules:
            outcome = await self._process_rule(payload, rule)
            if outcome.forwarded:
                any_forwarded = True
                self.stats.forwarded += 1
            else:
                self.stats.filtered += 1
        return PipelineOutcome(any_forwarded, "forwarded" if any_forwarded else "filtered")

    async def _process_rule(self, payload: MessagePayload, rule: ForwardRule) -> PipelineOutcome:
        filter_rule = (
            await self._filter_repo.get_by_id(rule.filter_rule_id)
            if rule.filter_rule_id
            else None
        )
        evaluation = self._filter_engine.evaluate(payload, filter_rule)

        if evaluation.action.name == "DROP":
            return PipelineOutcome(False, evaluation.reason, evaluation)

        text = payload.effective_text
        if rule.remove_links:
            text = self._filter_engine.remove_links(text)
            evaluation.links_removed = True

        ai_cfg = (
            await self._ai_repo.get_by_id(rule.ai_config_id)
            if rule.ai_config_id
            else None
        )
        if ai_cfg and ai_cfg.is_enabled:
            try:
                provider = self._factory.get(ai_cfg)
                rewritten = await provider.rewrite(ai_cfg, text)
                text = rewritten
                evaluation.rewritten_text = rewritten
                self.stats.rewritten += 1
            except Exception as exc:
                logger.warning("AI rewrite failed, sending original: %s", exc)

        if not self.sender:
            return PipelineOutcome(False, "no_sender_registered", evaluation)

        try:
            ok = await self.sender(payload, rule, text, evaluation)
        except Exception as exc:
            self.stats.errors += 1
            logger.exception("Send failed: %s", exc)
            return PipelineOutcome(False, f"send_error:{exc}", evaluation)

        return PipelineOutcome(ok, "sent" if ok else "send_rejected", evaluation)
