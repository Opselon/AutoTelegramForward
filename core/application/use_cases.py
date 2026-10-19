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
from ..domain.media_filter import (
    link_block_reason,
    media_block_reason,
    normalise_media_type,
)
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
        """Legacy admin flip: read the state now, then transition atomically.

        Returns ``None`` when the task moved between the read and the write
        (instead of the pre-fix behaviour of writing whatever the stale read
        implied) — callers that need to tell those apart use `set_status`.
        """
        r = await self._repo.get_by_id(rule_id)
        if r is None:
            return None
        outcome, fresh = await self.set_status(rule_id, r.status_state(), None)
        return fresh if outcome == self.STATUS_OK else None

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
        """Legacy owner-scoped flip, now an atomic transition (see `set_status`)."""
        r = await self.get_owned(rule_id, owner_user_id)
        if r is None:
            return None
        outcome, fresh = await self.set_status(rule_id, r.status_state(), owner_user_id)
        return fresh if outcome == self.STATUS_OK else None

    #: Outcomes of `set_status`, so a caller can answer honestly instead of
    #: guessing from a ``None``: the task moved, the caller's screen was stale,
    #: or the task is not theirs / does not exist.
    STATUS_OK = "ok"
    STATUS_STALE = "stale"
    STATUS_NOT_FOUND = "not_found"

    async def set_status(
        self,
        rule_id: str,
        expected_state: str,
        owner_user_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> tuple:
        """Turn a task on if it is off, off if it is running, atomically.

        ``expected_state`` is what the caller's screen showed
        (`ForwardRule.status_state()`). The transition is conditional on the
        stored state still being that, so a stale screen (the task was paused from
        the dashboard, or a second device changed it) is refused with
        ``STATUS_STALE`` and the caller is handed the *current* task to re-render
        — never a silent flip of a state the user is not looking at.
        """
        from ..domain.entities import STATUS_RUNNING

        r = await self._repo.get_by_id(rule_id)
        if r is None or not self._owns(getattr(r, "owner_user_id", 0), owner_user_id):
            return self.STATUS_NOT_FOUND, None
        moved = await self._repo.set_task_status(
            rule_id,
            target_active=expected_state != STATUS_RUNNING,
            expected_state=expected_state,
            owner_user_id=owner_user_id,
            now=now,
        )
        fresh = await self._repo.get_by_id(rule_id)
        return (self.STATUS_OK if moved else self.STATUS_STALE), fresh

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

    #: display names for the providers the factory knows.  English only — a
    #: client localizes from its own catalog when it has keys for them.
    PROVIDER_LABELS = {
        "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google Gemini",
        "groq": "Groq", "deepseek": "DeepSeek", "openrouter": "OpenRouter",
        "9router": "9Router", "ninerouter": "9Router", "xai": "xAI",
        "mistral": "Mistral", "together": "Together AI", "fireworks": "Fireworks",
        "custom": "Custom (OpenAI-compatible)",
    }

    def provider_catalog(self) -> List[dict]:
        """Providers this deployment can call, with their default endpoints.

        The catalogue comes from the same registry the rewrite path resolves a
        provider through, so a client never keeps a second copy that can drift
        — a provider that can be chosen here is one that can actually be called.
        """
        from ..infrastructure.ai.providers import AIProviderFactory, OpenAICompatibleProvider

        factory = self._factory if self._factory is not None else AIProviderFactory()
        try:
            names = list(factory.providers())
        except Exception:  # pragma: no cover - a broken factory must not blank the UI
            names = list(AIProviderFactory().providers())
        catalog = []
        for name in names:
            try:
                models = list(factory.models_for(name) or [])
            except Exception:  # pragma: no cover - defensive
                models = []
            catalog.append({
                "id": name,
                "label": self.PROVIDER_LABELS.get(name, name),
                "default_base_url": OpenAICompatibleProvider.default_base_urls.get(name, ""),
                "models": models,
            })
        return catalog

    def models_for(self, provider: str) -> list:
        """Curated model list for a provider (falls back to free text)."""
        factory = self._factory
        if factory is not None and hasattr(factory, "models_for"):
            try:
                return list(factory.models_for(provider))
            except Exception:
                pass
        return []

    @staticmethod
    def _guard_endpoint(cfg: AIConfig) -> None:
        """Refuse an endpoint this server must not be made to call.

        The guard sits at the write, not only at the probe: a stored endpoint is
        replayed on every rewrite, so an address checked only when a key is
        tested would still be called forever afterwards. Empty means "the
        provider's own default" and is left alone.
        """
        from ..infrastructure.ai.providers import endpoint_guard

        url = str(getattr(cfg, "base_url", "") or "").strip()
        if not url:
            return
        problem = endpoint_guard(url)
        if problem and problem != "missing_endpoint":
            raise ValueError(f"endpoint refused: {problem}")

    async def create(self, cfg: AIConfig) -> AIConfig:
        self._guard_endpoint(cfg)
        return await self._repo.add(cfg)

    async def update(self, cfg: AIConfig) -> AIConfig:
        self._guard_endpoint(cfg)
        return await self._repo.update(cfg)

    async def update_owned(self, cfg: AIConfig, owner_user_id: Optional[int]) -> AIConfig:
        # v12.1: bot edit-steps resolve the config id from callback state —
        # refuse a forged id belonging to another tenant.
        existing = await self._repo.get_by_id(cfg.id)
        if existing is None:
            raise KeyError("config_not_found")
        if not self._owns(getattr(existing, "owner_user_id", 0), owner_user_id):
            raise PermissionError("not_owner")
        # Same gate as update(): this is the path the bot's edit steps take, and
        # a URL must not become storable by walking through a wizard.
        self._guard_endpoint(cfg)
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

    async def check_provider_key_owned(self, *, provider: str = "", model: str = "",
                                       base_url: str = "", api_key: str = "",
                                       config_id: str = "",
                                       owner_user_id: Optional[int] = None):
        """Probe an endpoint + key the tenant wants to add, before it is saved.

        `config_id` names a config already stored: whatever the caller leaves
        blank is filled from it, so a UI can re-check a saved key without ever
        receiving it.  A masked key (`***abcd`) is treated as "unchanged", the
        same convention the update path uses.

        The probe spends the tenant's own credentials against the tenant's own
        endpoint, so ownership is required in both directions: a foreign
        `config_id` and a missing owner are the same refusal.
        """
        from ..infrastructure.ai.providers import ProbeResult, probe_credentials

        if not owner_user_id:
            # No tenant named: the probe would be nobody's. Same answer as a
            # config that is not theirs, so the caller learns nothing extra.
            return ProbeResult(False, False, 0.0, "", "config_not_found")
        stored = None
        if config_id:
            stored = await self.get_owned(config_id, owner_user_id)
            if stored is None:
                return ProbeResult(False, False, 0.0, "", "config_not_found")

        def _provider_of(value) -> str:
            # AIProviderType is an enum: its `.value` is what the wire carries.
            return str(getattr(value, "value", value) or "").strip()

        def _value(given: str, field: str) -> str:
            if given and given.strip():
                return given.strip()
            if stored is None:
                return ""
            if field == "provider":
                return _provider_of(getattr(stored, field, ""))
            return str(getattr(stored, field, "") or "").strip()

        provider_id = _value(provider, "provider")
        model_id = _value(model, "model")
        endpoint = _value(base_url, "base_url")
        # A masked key is the value we sent to the client, not the key itself.
        key_value = api_key.strip() if api_key and not api_key.startswith("***") else ""
        if not key_value:
            key_value = str(getattr(stored, "api_key", "") or "") if stored else ""
        return await probe_credentials(provider_id, model_id, endpoint, key_value)

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
        bot_tokens=None,
    ) -> None:
        self._rule_repo = rule_repo
        self._filter_repo = filter_repo
        self._ai_repo = ai_repo
        self._factory = provider_factory
        self._filter_engine = filter_engine or FilterEngine()
        self._routing = routing_policy or RoutingPolicy()
        self._processed = processed_repo
        # Per-bot defaults, resolved per message (see core.application
        # .effective_settings). The resolved values travel with the rule this
        # use case hands to its sender, so nothing downstream has to know.
        self._bot_tokens = bot_tokens
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
        # The rule as the forwarder should see it: the attached bot's defaults
        # filled in for everything this task does not set itself.
        from .effective_settings import materialize
        rule = await materialize(rule, self._bot_tokens)
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

        # --- media & content blocks (voice / audio / video / stickers / text /
        # link) — the same decision the durable pipeline applies, so a reply or
        # PV delivery cannot forward a type the task blocks.
        m_type = normalise_media_type(payload.media_type)
        media_reason = media_block_reason(rule, m_type) or link_block_reason(
            rule, payload.effective_text
        )
        if media_reason:
            logger.warning(
                "Use case: Rule %s DROPPED msg %s: %s",
                rule.id[:8], payload.message_id, media_reason,
            )
            return PipelineOutcome(False, media_reason, evaluation)

        text = payload.effective_text
        # The per-task cleaner must run on *every* forwarding path. It was only
        # applied by the durable pipeline, so a reply/PV delivery forwarded the
        # raw text with links, @mentions and custom words the owner had asked to
        # strip. Same cleaner, same order (before link/text transforms).
        cleaner = rule.build_cleaner() if hasattr(rule, "build_cleaner") else None
        if cleaner is not None:
            try:
                cleaned = cleaner.clean(text)
                if cleaned != text:
                    logger.info(
                        "[CLEANER:USE_CASE] Rule %s cleaned msg %s: len %d -> %d",
                        rule.id[:8], payload.message_id, len(text), len(cleaned),
                    )
                    text = cleaned
                if getattr(cleaner, "strip_buttons", False) and getattr(payload, "reply_markup", None) is not None:
                    payload.reply_markup = None
                    logger.info("[CLEANER:USE_CASE] Rule %s removed the inline keyboard of msg %s",
                                rule.id[:8], payload.message_id)
            except Exception as exc:
                # Never drop a message because the cleaner failed.
                logger.warning(
                    "[CLEANER:USE_CASE] Rule %s cleaner failed (%s) — delivering uncleaned text",
                    rule.id[:8], exc,
                )
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

        # Owner-scoped for the same reason as the pipeline: a config id in a
        # rule row is a selector, never proof that the rule's owner may spend it.
        ai_cfg = (
            await self._ai_repo.get_owned(rule.ai_config_id, getattr(rule, "owner_user_id", 0) or 0)
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
