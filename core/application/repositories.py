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
