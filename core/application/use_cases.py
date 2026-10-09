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

    def validate_endpoints(self, source_chat_id: str, target_chat_id: str) -> tuple[bool, str]:
        """Validate source and target chat IDs to prevent loops and empty routing."""
        s = str(source_chat_id or "").strip()
        t = str(target_chat_id or "").strip()
        if not s:
            return False, "source_empty"
        if not t:
            return False, "target_empty"
        if s == t:
            return False, "loop_detected"
        return True, "ok"

    async def create(self, rule: ForwardRule) -> ForwardRule:
        ok, err = self.validate_endpoints(rule.source_chat_id, rule.target_chat_id)
        if not ok:
            raise ValueError(f"Invalid rule endpoints: {err}")
        return await self._repo.add(rule)

    async def update(self, rule: ForwardRule, expected_version: Optional[int] = None) -> ForwardRule:
        ok, err = self.validate_endpoints(rule.source_chat_id, rule.target_chat_id)
        if not ok:
            raise ValueError(f"Invalid rule endpoints: {err}")
        rule.mark_updated()
        return await self._repo.update(rule, expected_version=expected_version)

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

    def provider_names(self) -> list:
        """Provider keys the UI can offer (falls back to enum members)."""
        factory = self._factory
        if factory is not None and hasattr(factory, "providers"):
            try:
                return list(factory.providers())
            except Exception:
                pass
        from ..domain.value_objects import AIProviderType
        return [p.value for p in AIProviderType]

    def models_for(self, provider: str) -> list:
        """Curated model list for a provider (falls back to free text)."""
        factory = self._factory
        if factory is not None and hasattr(factory, "models_for"):
            try:
                return list(factory.models_for(provider))
            except Exception:
                pass
        return []

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
# API credentials + bot tokens (live config, encrypted at rest)
# --------------------------------------------------------------------------- #

class ApiCredentialUseCases:
    def __init__(self, repo) -> None:
        self._repo = repo

    async def create(self, label: str, api_id: int, api_hash: str,
                     proxy: str = "", is_default: bool = False):
        import uuid as _uuid, time as _time
        from .repositories import ApiCredential
        cred = ApiCredential(
            id=str(_uuid.uuid4()), label=label, api_id=int(api_id),
            api_hash=api_hash, proxy=proxy or "", is_default=is_default,
            created_at=int(_time.time()), updated_at=int(_time.time()),
        )
        return await self._repo.add(cred)

    async def get(self, cred_id: str):
        return await self._repo.get_by_id(cred_id)

    async def list_all(self):
        return await self._repo.list_all()

    async def get_default(self):
        return await self._repo.get_default()

    async def set_default(self, cred_id: str):
        creds = await self._repo.list_all()
        for c in creds:
            want = (c.id == cred_id)
            if c.is_default != want:
                c.is_default = want
                await self._repo.update(c)
        return await self._repo.get_by_id(cred_id)

    async def delete(self, cred_id: str) -> bool:
        return await self._repo.delete(cred_id)


class BotTokenUseCases:
    def __init__(self, repo) -> None:
        self._repo = repo

    async def create(self, label: str, token: str):
        import uuid as _uuid, time as _time
        from .repositories import BotToken
        tok = BotToken(
            id=str(_uuid.uuid4()), label=label, token=token.strip(),
            is_active=True, created_at=int(_time.time()), updated_at=int(_time.time()),
        )
        return await self._repo.add(tok)

    async def get(self, token_id: str):
        return await self._repo.get_by_id(token_id)

    async def list_all(self):
        return await self._repo.list_all()

    async def list_active(self):
        return await self._repo.list_active()

    async def delete(self, token_id: str) -> bool:
        return await self._repo.delete(token_id)


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
    duplicates_skipped: int = 0
    edits_forwarded: int = 0
    started_at: int = field(default_factory=lambda: int(time.time()))


class MessageForwardingUseCase:
    """End-to-end pipeline: match rules -> dedupe -> filter -> AI rewrite -> dispatch.

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
        processed_repo=None,
    ) -> None:
        self._rule_repo = rule_repo
        self._filter_repo = filter_repo
        self._ai_repo = ai_repo
        self._factory = provider_factory
        self._filter_engine = filter_engine or FilterEngine()
        self._routing = routing_policy or RoutingPolicy()
        self._processed = processed_repo
        self.sender = None  # async callable: (payload, rule, text, eval_result) -> bool
        self.stats = ForwardingStats()

    async def process_message(
        self, payload: MessagePayload, trigger: str = "NEW_MESSAGE"
    ) -> PipelineOutcome:
        self.stats.processed += 1
        if payload.is_service:
            self.stats.filtered += 1
            return PipelineOutcome(False, "service_message")

        rules = await self._rule_repo.list_active_by_source(payload.chat_id)
        rules = self._routing.matching_rules(payload, rules)
        # Only rules subscribed to this Telegram event fire.
        rules = [r for r in rules if trigger in (getattr(r, "trigger_events", None) or ["NEW_MESSAGE"])]
        if not rules:
            return PipelineOutcome(False, "no_matching_rules")

        any_forwarded = False
        for rule in rules:
            outcome = await self._process_rule(payload, rule, trigger=trigger)
            if outcome.forwarded:
                any_forwarded = True
                self.stats.forwarded += 1
                if payload.is_edit:
                    self.stats.edits_forwarded += 1
            else:
                if outcome.reason in ("duplicate", "too_old", "edit_ignored"):
                    self.stats.duplicates_skipped += 1
                else:
                    self.stats.filtered += 1
        return PipelineOutcome(any_forwarded, "forwarded" if any_forwarded else "filtered")

    async def _process_rule(
        self, payload: MessagePayload, rule: ForwardRule, trigger: str = "NEW_MESSAGE"
    ) -> PipelineOutcome:
        # --- time floor: skip backlog / old messages ------------------
        since = getattr(rule, "since_ts", 0) or 0
        if since and payload.date and payload.date < since:
            return PipelineOutcome(False, "too_old")
        # --- edits -----------------------------------------------------
        if payload.is_edit and getattr(rule, "ignore_edits", False):
            return PipelineOutcome(False, "edit_ignored")
        # --- restart-safe dedupe --------------------------------------
        if not payload.is_edit and self._processed is not None and getattr(rule, "skip_history", True):
            seen = await self._processed.was_processed(rule.id, payload.chat_id, payload.message_id)
            if seen:
                return PipelineOutcome(False, "duplicate")

        filter_rule = (
            await self._filter_repo.get_by_id(rule.filter_rule_id)
            if rule.filter_rule_id
            else None
        )
        evaluation = self._filter_engine.evaluate(payload, filter_rule)

        if evaluation.action.name == "DROP":
            return PipelineOutcome(False, evaluation.reason, evaluation)

        # --- media blocks (voice / stickers) --------------------------
        m_type = str(getattr(payload.media_type, "value", payload.media_type)).lower()
        if rule.block_voice and m_type in ("voice", "audio"):
            return PipelineOutcome(False, "voice_blocked", evaluation)
        if rule.block_stickers and m_type in ("sticker",):
            return PipelineOutcome(False, "sticker_blocked", evaluation)

        text = payload.effective_text
        if rule.link_replacement:
            text = self._filter_engine.replace_links(text, rule.link_replacement)
            evaluation.links_removed = True
        elif rule.remove_links or (isinstance(rule.metadata, dict) and rule.metadata.get("remove_links")):
            text = self._filter_engine.remove_links(text)
            evaluation.links_removed = True

        if rule.remove_emojis:
            text = self._filter_engine.remove_emojis(text)

        if rule.replacements:
            text = self._filter_engine.replace_text(text, rule.replacements)

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

        if rule.header or rule.footer:
            text = self._filter_engine.apply_header_footer(text, header=rule.header, footer=rule.footer)

        if not self.sender:
            return PipelineOutcome(False, "no_sender_registered", evaluation)

        try:
            ok = await self.sender(payload, rule, text, evaluation)
        except Exception as exc:
            self.stats.errors += 1
            logger.exception("Send failed: %s", exc)
            return PipelineOutcome(False, f"send_error:{exc}", evaluation)

        if ok and self._processed is not None:
            try:
                await self._processed.mark_processed(
                    rule.id, payload.chat_id, payload.message_id,
                    payload.media_group_id or "",
                )
            except Exception:
                logger.debug("dedupe mark failed", exc_info=True)
        return PipelineOutcome(ok, "sent" if ok else "send_rejected", evaluation)
