"""Application use cases: orchestrate domain services + repository ports.

SRP: each class owns one business capability; CQRS-ish split per aggregate.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

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

    async def create(self, phone_number: str, session_string_encrypted: str = "",
                     owner_user_id: int = 0) -> TelegramSession:
        existing = await self._repo.get_by_phone(phone_number)
        if existing:
            existing.session_string_encrypted = session_string_encrypted
            if owner_user_id and not getattr(existing, "owner_user_id", 0):
                existing.owner_user_id = int(owner_user_id)
            return await self._repo.update(existing)
        return await self._repo.add(
            TelegramSession(phone_number=phone_number,
                            session_string_encrypted=session_string_encrypted,
                            owner_user_id=int(owner_user_id or 0))
        )

    def create_sync(self, phone_number: str, session_string_encrypted: str = "") -> TelegramSession:
        import asyncio
        return asyncio.run(self.create(phone_number, session_string_encrypted))

    async def list_by_owner(self, owner_user_id: int) -> List[TelegramSession]:
        if hasattr(self._repo, "list_by_owner"):
            return await self._repo.list_by_owner(owner_user_id)
        return [s for s in await self._repo.list_all() if getattr(s, "owner_user_id", 0) == owner_user_id]

    async def list_all(self) -> List[TelegramSession]:
        return await self._repo.list_all()

    async def list_active(self) -> List[TelegramSession]:
        return await self._repo.list_active()

    async def get(self, session_id: str) -> Optional[TelegramSession]:
        return await self._repo.get_by_id(session_id)

    def _owns(self, entity_owner: int, owner_user_id: Optional[int]) -> bool:
        # owner_user_id=None keeps admin/system scope; zero or empty fails closed.
        if owner_user_id is None:
            return True
        if not owner_user_id:
            return False
        return int(entity_owner or 0) == int(owner_user_id)

    async def get_owned(self, session_id: str, owner_user_id: Optional[int]) -> Optional[TelegramSession]:
        s = await self._repo.get_by_id(session_id)
        if s is None or not self._owns(getattr(s, "owner_user_id", 0), owner_user_id):
            return None
        return s

    async def delete_owned(self, session_id: str, owner_user_id: Optional[int]) -> bool:
        existing = await self._repo.get_by_id(session_id)
        if existing is None:
            return False
        if not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        return await self._repo.delete(session_id)

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
    def __init__(self, repo: IForwardRuleRepository, session_repo: Optional[Any] = None) -> None:
        self._repo = repo
        self._session_repo = session_repo

    @staticmethod
    def _normalize_chat_id(val: str) -> str:
        s = str(val or "").strip().lower()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
            if s.startswith(prefix):
                s = s[len(prefix):]
        return s.strip()

    @staticmethod
    def _is_numeric_chat_id(val: str) -> bool:
        """True when the id is a numeric Telegram id (channel/group/user).
        The dispatcher converts chat ids with int() before sending, so a
        username in a chat_id column breaks delivery — reject non-numeric ids
        unless an explicit opt-out flag marks them as unresolved placeholders."""
        s = str(val or "").strip()
        try:
            int(s)
            return True
        except (TypeError, ValueError):
            return False

    def validate_endpoints(
        self,
        source_chat_id: str,
        target_chat_id: str,
        intermediate_channel_id: str = "",
        use_intermediate: bool = False,
    ) -> tuple[bool, str]:
        """Validate source and target chat IDs to prevent loops and empty routing."""
        s = str(source_chat_id or "").strip()
        t = str(target_chat_id or "").strip()
        if not s:
            return False, "source_empty"
        if not t:
            return False, "target_empty"
        if not self._is_numeric_chat_id(s):
            return False, "source_not_numeric"
        if not self._is_numeric_chat_id(t):
            return False, "target_not_numeric"
        sn = self._normalize_chat_id(s)
        tn = self._normalize_chat_id(t)
        if sn == tn:
            return False, "loop_detected"
        if use_intermediate and intermediate_channel_id:
            inter = self._normalize_chat_id(intermediate_channel_id)
            if inter and (inter == sn or inter == tn):
                return False, "loop_detected"
        return True, "ok"

    async def create(self, rule: ForwardRule) -> ForwardRule:
        ok, err = self.validate_endpoints(
            rule.source_chat_id,
            rule.target_chat_id,
            getattr(rule, "intermediate_channel_id", "") or "",
            bool(getattr(rule, "use_intermediate", False)),
        )
        if not ok:
            raise ValueError(f"Invalid rule endpoints: {err}")
        if self._session_repo and rule.session_id and getattr(rule, "owner_user_id", 0):
            sess = await self._session_repo.get_by_id(rule.session_id)
            if sess and getattr(sess, "owner_user_id", 0) and int(sess.owner_user_id) != int(rule.owner_user_id):
                raise PermissionError(f"Session {rule.session_id} not owned by {rule.owner_user_id}")
        return await self._repo.add(rule)

    async def update(self, rule: ForwardRule, expected_version: Optional[int] = None) -> ForwardRule:
        ok, err = self.validate_endpoints(
            rule.source_chat_id,
            rule.target_chat_id,
            getattr(rule, "intermediate_channel_id", "") or "",
            bool(getattr(rule, "use_intermediate", False)),
        )
        if not ok:
            raise ValueError(f"Invalid rule endpoints: {err}")
        if self._session_repo and rule.session_id and getattr(rule, "owner_user_id", 0):
            sess = await self._session_repo.get_by_id(rule.session_id)
            if sess and getattr(sess, "owner_user_id", 0) and int(sess.owner_user_id) != int(rule.owner_user_id):
                raise PermissionError(f"Session {rule.session_id} not owned by {rule.owner_user_id}")
        rule.mark_updated()
        return await self._repo.update(rule, expected_version=expected_version)

    async def get(self, rule_id: str) -> Optional[ForwardRule]:
        return await self._repo.get_by_id(rule_id)

    async def list_by_session(self, session_id: str, owner_user_id: int = 0) -> List[ForwardRule]:
        return await self._repo.list_by_session(session_id, owner_user_id)

    async def list_by_owner(self, owner_user_id: int) -> List[ForwardRule]:
        if hasattr(self._repo, "list_by_owner"):
            return await self._repo.list_by_owner(owner_user_id)
        return [r for r in await self._repo.list_all() if getattr(r, "owner_user_id", 0) == owner_user_id]

    async def list_all(self) -> List[ForwardRule]:
        return await self._repo.list_all()

    async def toggle(self, rule_id: str) -> Optional[ForwardRule]:
        r = await self._repo.get_by_id(rule_id)
        if r:
            r.is_active = not r.is_active
            r.mark_updated()
            return await self._repo.update(r)
        return None

    # -- v12.1: owner-guarded mutations -------------------------------------
    # The Telegram bot resolves entities by callback id, so an IDOR guard must
    # live here (one enforcement point for every caller) rather than only in
    # the HTTP/gRPC layer. `owner_user_id=None` keeps the legacy/admin path;
    # any other value must match the entity or the call is refused.

    def _owns(self, entity_owner: int, owner_user_id: Optional[int]) -> bool:
        # owner_user_id=None keeps admin/system scope; zero or empty fails closed.
        if owner_user_id is None:
            return True
        if not owner_user_id:
            return False
        return int(entity_owner or 0) == int(owner_user_id)

    async def get_owned(self, rule_id: str, owner_user_id: Optional[int]) -> Optional[ForwardRule]:
        r = await self._repo.get_by_id(rule_id)
        if r is None or not self._owns(getattr(r, "owner_user_id", 0), owner_user_id):
            return None
        return r

    async def update_owned(self, rule: ForwardRule, owner_user_id: Optional[int],
                           expected_version: Optional[int] = None) -> ForwardRule:
        existing = await self._repo.get_by_id(rule.id)
        if existing is None or not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        return await self.update(rule, expected_version=expected_version)

    async def delete_owned(self, rule_id: str, owner_user_id: Optional[int]) -> bool:
        existing = await self._repo.get_by_id(rule_id)
        if existing is None:
            return False
        if not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        return await self._repo.delete(rule_id)

    async def toggle_owned(self, rule_id: str, owner_user_id: Optional[int]) -> Optional[ForwardRule]:
        r = await self._repo.get_by_id(rule_id)
        if r is None or not self._owns(getattr(r, "owner_user_id", 0), owner_user_id):
            return None
        r.is_active = not r.is_active
        r.mark_updated()
        return await self._repo.update(r)

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

    async def list_by_owner(self, owner_user_id: int) -> List[FilterRule]:
        return await self._repo.list_by_owner(owner_user_id)

    def _owns(self, entity_owner: int, owner_user_id: Optional[int]) -> bool:
        # owner_user_id=None keeps admin/system scope; zero or empty fails closed.
        if owner_user_id is None:
            return True
        if not owner_user_id:
            return False
        return int(entity_owner or 0) == int(owner_user_id)

    async def get_owned(self, filter_id: str, owner_user_id: Optional[int]) -> Optional[FilterRule]:
        f = await self._repo.get_by_id(filter_id)
        if f is None or not self._owns(getattr(f, "owner_user_id", 0), owner_user_id):
            return None
        return f

    async def delete_owned(self, filter_id: str, owner_user_id: Optional[int]) -> bool:
        existing = await self._repo.get_by_id(filter_id)
        if existing is None:
            return False
        if not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        return await self._repo.delete(filter_id)

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

    async def update_owned(self, cfg: AIConfig, owner_user_id: Optional[int]) -> AIConfig:
        # v12.1: bot edit-steps resolve the config id from callback state —
        # refuse a forged id belonging to another tenant.
        existing = await self._repo.get_by_id(cfg.id)
        if existing is None:
            raise KeyError("config_not_found")
        if not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        return await self._repo.update(cfg)

    async def get(self, config_id: str) -> Optional[AIConfig]:
        return await self._repo.get_by_id(config_id)

    async def list_all(self) -> List[AIConfig]:
        return await self._repo.list_all()

    async def list_by_owner(self, owner_user_id: int) -> List[AIConfig]:
        return await self._repo.list_by_owner(owner_user_id)

    def _owns(self, entity_owner: int, owner_user_id: Optional[int]) -> bool:
        # owner_user_id=None keeps admin/system scope; zero or empty fails closed.
        if owner_user_id is None:
            return True
        if not owner_user_id:
            return False
        return int(entity_owner or 0) == int(owner_user_id)

    async def get_owned(self, config_id: str, owner_user_id: Optional[int]) -> Optional[AIConfig]:
        c = await self._repo.get_by_id(config_id)
        if c is None or not self._owns(getattr(c, "owner_user_id", 0), owner_user_id):
            return None
        return c

    async def delete_owned(self, config_id: str, owner_user_id: Optional[int]) -> bool:
        existing = await self._repo.get_by_id(config_id)
        if existing is None:
            return False
        if not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        return await self._repo.delete(config_id)

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

    async def test_rewrite_owned(self, config_id: str, sample_text: str,
                                 owner_user_id: Optional[int]) -> tuple[bool, str]:
        # v12.1: executing a rewrite consumes another tenant's API key/quota —
        # refuse a forged config id before touching the provider.
        cfg = await self._repo.get_by_id(config_id)
        if cfg is None or not self._owns(getattr(cfg, "owner_user_id", 0), owner_user_id):
            return False, "config_not_found"
        return await self.test_rewrite(config_id, sample_text)

    async def health_check(self, config_id: str) -> tuple[bool, str, float]:
        """Runs health check against provider, returns (is_healthy, response_or_error, latency_ms)."""
        cfg = await self._repo.get_by_id(config_id)
        if not cfg:
            return False, "config_not_found", 0.0
        try:
            provider = self._factory.get(cfg)
            if hasattr(provider, "check_health"):
                return await provider.check_health(cfg)
            import time
            t0 = time.perf_counter()
            res = await provider.rewrite(cfg, "ping")
            elapsed = (time.perf_counter() - t0) * 1000
            return True, res, elapsed
        except Exception as exc:  # pragma: no cover - network path
            return False, f"{type(exc).__name__}: {exc}", 0.0

    async def health_check_owned(self, config_id: str,
                                 owner_user_id: Optional[int]) -> tuple[bool, str, float]:
        # v12.1: same guard as test_rewrite_owned — a health check hits the
        # provider with the stored key, so a forged id must stop here.
        cfg = await self._repo.get_by_id(config_id)
        if cfg is None or not self._owns(getattr(cfg, "owner_user_id", 0), owner_user_id):
            return False, "config_not_found", 0.0
        return await self.health_check(config_id)


# --------------------------------------------------------------------------- #
# API credentials + bot tokens (live config, encrypted at rest)
# --------------------------------------------------------------------------- #

class ApiCredentialUseCases:
    def __init__(self, repo) -> None:
        self._repo = repo

    async def create(self, label: str, api_id: int, api_hash: str,
                     proxy: str = "", is_default: bool = False,
                     owner_user_id: int = 0):
        import uuid as _uuid, time as _time
        from .repositories import ApiCredential
        cred = ApiCredential(
            id=str(_uuid.uuid4()), label=label, api_id=int(api_id),
            api_hash=api_hash, proxy=proxy or "", is_default=is_default,
            created_at=int(_time.time()), updated_at=int(_time.time()),
            # v12: stamp the creating tenant; 0 = legacy/system scope.
            owner_user_id=int(owner_user_id or 0),
        )
        return await self._repo.add(cred)

    async def get(self, cred_id: str):
        return await self._repo.get_by_id(cred_id)

    def _owns(self, entity_owner: int, owner_user_id):
        # owner_user_id=None keeps admin/system scope; zero or empty fails closed.
        if owner_user_id is None:
            return True
        if not owner_user_id:
            return False
        return int(entity_owner or 0) == int(owner_user_id)

    async def get_owned(self, cred_id: str, owner_user_id):
        c = await self._repo.get_by_id(cred_id)
        if c is None or not self._owns(getattr(c, "owner_user_id", 0), owner_user_id):
            return None
        return c

    async def list_by_owner(self, owner_user_id: int):
        if hasattr(self._repo, "list_by_owner"):
            return await self._repo.list_by_owner(int(owner_user_id or 0))
        return [c for c in await self._repo.list_all()
                if int(getattr(c, "owner_user_id", 0) or 0) == int(owner_user_id or 0)]

    async def set_default_owned(self, cred_id: str, owner_user_id):
        # v12.1: a forged credential id must not flip another tenant's default.
        existing = await self._repo.get_by_id(cred_id)
        if existing is None:
            return None
        if not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        scope = None if owner_user_id is None else await self.list_by_owner(owner_user_id)
        creds = await self._repo.list_all() if scope is None else scope
        for c in creds:
            want = (c.id == cred_id)
            if c.is_default != want:
                c.is_default = want
                await self._repo.update(c)
        return await self._repo.get_by_id(cred_id)

    async def delete_owned(self, cred_id: str, owner_user_id) -> bool:
        existing = await self._repo.get_by_id(cred_id)
        if existing is None:
            return False
        if not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        return await self._repo.delete(cred_id)

    async def list_all(self):
        return await self._repo.list_all()

    async def get_default(self):
        return await self._repo.get_default()

    async def get_default_by_owner(self, owner_user_id: int):
        # v12: prefer the caller's own credential; falls back to the shared
        # system scope on a legacy single-tenant install.
        return await self._repo.get_default_by_owner(int(owner_user_id or 0))

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

    async def create(self, label: str, token: str, owner_user_id: int = 0):
        import uuid as _uuid, time as _time
        from .repositories import BotToken
        tok = BotToken(
            id=str(_uuid.uuid4()), label=label, token=token.strip(),
            is_active=True, created_at=int(_time.time()), updated_at=int(_time.time()),
            owner_user_id=int(owner_user_id or 0),
        )
        return await self._repo.add(tok)

    async def get(self, token_id: str):
        """Delivery resolves a token by id: the worker has no tenant context."""
        return await self._repo.get_by_id(token_id)

    async def get_owned(self, token_id: str, owner_user_id: int):
        return await self._repo.get_owned(token_id, int(owner_user_id or 0))

    async def list_all(self):
        return await self._repo.list_all()

    async def list_by_owner(self, owner_user_id: int):
        return await self._repo.list_by_owner(int(owner_user_id or 0))

    async def list_active(self):
        return await self._repo.list_active()

    async def delete(self, token_id: str) -> bool:
        return await self._repo.delete(token_id)

    async def delete_owned(self, token_id: str, owner_user_id: int) -> bool:
        return await self._repo.delete_owned(token_id, int(owner_user_id or 0))

    async def get_defaults(self, token_id: str, owner_user_id: int) -> dict:
        """The defaults this owner's bot carries, or {} (never another owner's).

        Reading is not an error when the bot is absent or foreign: a settings
        screen shows the registry defaults, which is also what the forwarder
        would use.
        """
        record = await self.get_owned(token_id, owner_user_id)
        stored = getattr(record, "defaults", None) if record is not None else None
        return dict(stored) if isinstance(stored, dict) else {}

    async def set_defaults(self, token_id: str, owner_user_id: int, defaults: dict) -> bool:
        """Replace this owner's defaults for this bot; False when not theirs.

        Validation happens in the domain (`core.domain.bot_defaults`) before
        anything gets here, so this is only the owner-scoped write.
        """
        return await self._repo.set_defaults(token_id, int(owner_user_id or 0), dict(defaults or {}))


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
            logger.info("Use case: Rule %s skipped msg %s: too_old (date=%s < since=%s)", rule.id[:8], payload.message_id, payload.date, since)
            return PipelineOutcome(False, "too_old")
        # --- edits -----------------------------------------------------
        if payload.is_edit and getattr(rule, "ignore_edits", False):
            logger.info("Use case: Rule %s skipped edit on msg %s (ignore_edits=True)", rule.id[:8], payload.message_id)
            return PipelineOutcome(False, "edit_ignored")
        # --- restart-safe dedupe --------------------------------------
        if not payload.is_edit and self._processed is not None and getattr(rule, "skip_history", True):
            seen = await self._processed.was_processed(rule.id, payload.chat_id, payload.message_id)
            if seen:
                logger.info("Use case: Rule %s skipped msg %s: already processed (duplicate)", rule.id[:8], payload.message_id)
                return PipelineOutcome(False, "duplicate")

        filter_rule = (
            await self._filter_repo.get_by_id(rule.filter_rule_id)
            if rule.filter_rule_id
            else None
        )
        evaluation = self._filter_engine.evaluate(payload, filter_rule)

        if evaluation.action.name == "DROP":
            logger.warning("Use case: Rule %s DROPPED msg %s by filter rule %s: %s", rule.id[:8], payload.message_id, rule.filter_rule_id or "none", evaluation.reason)
            return PipelineOutcome(False, evaluation.reason, evaluation)

        # --- media blocks (voice / stickers) --------------------------
        m_type = str(getattr(payload.media_type, "value", payload.media_type)).lower()
        if rule.block_voice and m_type in ("voice", "audio"):
            logger.warning("Use case: Rule %s DROPPED msg %s: voice/audio blocked", rule.id[:8], payload.message_id)
            return PipelineOutcome(False, "voice_blocked", evaluation)
        if rule.block_stickers and m_type in ("sticker",):
            logger.warning("Use case: Rule %s DROPPED msg %s: sticker blocked", rule.id[:8], payload.message_id)
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
            logger.error("Use case: No sender registered on use case — cannot forward msg %s", payload.message_id)
            return PipelineOutcome(False, "no_sender_registered", evaluation)

        try:
            ok = await self.sender(payload, rule, text, evaluation)
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Use case: Send failed for rule %s msg %s: %s", rule.id[:8], payload.message_id, exc, exc_info=True)
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
