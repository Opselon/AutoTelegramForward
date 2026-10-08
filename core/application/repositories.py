"""Repository port interfaces (DIP: domain owns the abstraction)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from ..domain.entities import AIConfig, FilterRule, ForwardRule, TelegramSession


@dataclass
class ApiCredential:
    """An api_id/api_hash pair (optionally with a proxy), stored encrypted."""

    id: str
    label: str = ""
    api_id: int = 0
    api_hash: str = ""
    proxy: str = ""
    is_default: bool = False
    created_at: int = 0
    updated_at: int = 0


@dataclass
class BotToken:
    """A bot token, stored encrypted at rest."""

    id: str
    label: str = ""
    token: str = ""
    is_active: bool = True
    created_at: int = 0
    updated_at: int = 0


class ISessionRepository(ABC):
    @abstractmethod
    async def add(self, session: TelegramSession) -> TelegramSession:
        ...

    @abstractmethod
    async def update(self, session: TelegramSession) -> TelegramSession:
        ...

    @abstractmethod
    async def get_by_id(self, session_id: str) -> Optional[TelegramSession]:
        ...

    @abstractmethod
    async def get_by_phone(self, phone: str) -> Optional[TelegramSession]:
        ...

    @abstractmethod
    async def list_all(self) -> List[TelegramSession]:
        ...

    @abstractmethod
    async def list_active(self) -> List[TelegramSession]:
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        ...


class IForwardRuleRepository(ABC):
    @abstractmethod
    async def add(self, rule: ForwardRule) -> ForwardRule:
        ...

    @abstractmethod
    async def update(self, rule: ForwardRule) -> ForwardRule:
        ...

    @abstractmethod
    async def get_by_id(self, rule_id: str) -> Optional[ForwardRule]:
        ...

    @abstractmethod
    async def list_by_session(self, session_id: str) -> List[ForwardRule]:
        ...

    @abstractmethod
    async def list_all(self) -> List[ForwardRule]:
        ...

    @abstractmethod
    async def list_active_by_source(self, source_chat_id: str) -> List[ForwardRule]:
        ...

    @abstractmethod
    async def delete(self, rule_id: str) -> bool:
        ...


class IFilterRuleRepository(ABC):
    @abstractmethod
    async def add(self, filter_rule: FilterRule) -> FilterRule:
        ...

    @abstractmethod
    async def update(self, filter_rule: FilterRule) -> FilterRule:
        ...

    @abstractmethod
    async def get_by_id(self, filter_id: str) -> Optional[FilterRule]:
        ...

    @abstractmethod
    async def list_all(self) -> List[FilterRule]:
        ...

    @abstractmethod
    async def delete(self, filter_id: str) -> bool:
        ...


class IAIConfigRepository(ABC):
    @abstractmethod
    async def add(self, config: AIConfig) -> AIConfig:
        ...

    @abstractmethod
    async def update(self, config: AIConfig) -> AIConfig:
        ...

    @abstractmethod
    async def get_by_id(self, config_id: str) -> Optional[AIConfig]:
        ...

    @abstractmethod
    async def list_all(self) -> List[AIConfig]:
        ...

    @abstractmethod
    async def delete(self, config_id: str) -> bool:
        ...


class IApiCredentialRepository(ABC):
    @abstractmethod
    async def add(self, cred: ApiCredential) -> ApiCredential:
        ...

    @abstractmethod
    async def update(self, cred: ApiCredential) -> ApiCredential:
        ...

    @abstractmethod
    async def get_by_id(self, cred_id: str) -> Optional[ApiCredential]:
        ...

    @abstractmethod
    async def get_default(self) -> Optional[ApiCredential]:
        ...

    @abstractmethod
    async def list_all(self) -> List[ApiCredential]:
        ...

    @abstractmethod
    async def delete(self, cred_id: str) -> bool:
        ...


class IBotTokenRepository(ABC):
    @abstractmethod
    async def add(self, token: BotToken) -> BotToken:
        ...

    @abstractmethod
    async def get_by_id(self, token_id: str) -> Optional[BotToken]:
        ...

    @abstractmethod
    async def list_all(self) -> List[BotToken]:
        ...

    @abstractmethod
    async def list_active(self) -> List[BotToken]:
        ...

    @abstractmethod
    async def delete(self, token_id: str) -> bool:
        ...


class IProcessedMessageRepository(ABC):
    """Restart-safe dedupe store: which messages a rule already forwarded."""

    @abstractmethod
    async def was_processed(self, rule_id: str, chat_id: str, message_id: int) -> bool:
        ...

    @abstractmethod
    async def mark_processed(
        self, rule_id: str, chat_id: str, message_id: int, media_group_id: str = ""
    ) -> None:
        ...

    @abstractmethod
    async def purge_before(self, rule_id: str, ts: int) -> int:
        ...


class IUiStateRepository(ABC):
    """Durable per-user UI flow state — survives restarts."""

    @abstractmethod
    async def get(self, user_id: int) -> Optional[dict]:
        ...

    @abstractmethod
    async def save(self, user_id: int, step: str, buffer: dict) -> None:
        ...

    @abstractmethod
    async def clear(self, user_id: int) -> None:
        ...


class IErrorLogRepository(ABC):
    """Structured Telegram error log (exact RPC error name + severity)."""

    @abstractmethod
    async def record(
        self, category: str, error_name: str, severity: str, detail: str = "",
        recoverable: bool = False, user_id: Optional[int] = None,
        session_id: Optional[str] = None, rule_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        ...

    @abstractmethod
    async def recent(self, limit: int = 10, severity: Optional[str] = None) -> List[dict]:
        ...

    @abstractmethod
    async def counts_by_severity(self, since_ts: int = 0) -> dict:
        ...


class IMetricsRepository(ABC):
    """Rolling throughput counters for the dashboard."""

    @abstractmethod
    async def bump(self, forwarded: int = 0, filtered: int = 0, errors: int = 0) -> None:
        ...

    @abstractmethod
    async def hourly(self, hours: int = 24) -> List[dict]:
        ...

    @abstractmethod
    async def totals(self) -> dict:
        ...

    @abstractmethod
    async def prune(self, keep_hours: int = 168) -> int:
        ...


class IRuleStatsRepository(ABC):
    """Per-rule live counters on the hot path."""

    @abstractmethod
    async def bump(self, rule_id: str, forwarded: int = 0, filtered: int = 0, errors: int = 0) -> None:
        ...

    @abstractmethod
    async def get(self, rule_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    async def top_rules(self, limit: int = 10) -> List[dict]:
        ...


class IUserRepository(ABC):
    """Per-user preferences, plan and quota."""

    @abstractmethod
    async def get_or_create(self, user_id: int, language: str = "") -> dict:
        ...

    @abstractmethod
    async def set_language(self, user_id: int, language: str) -> None:
        ...

    @abstractmethod
    async def bump_quota(self, user_id: int, by: int = 1) -> None:
        ...
