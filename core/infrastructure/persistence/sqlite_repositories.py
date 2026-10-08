"""SQLite repository implementations of the application repository ports.

A future PostgreSQL/MySQL backend implements the same interfaces and is
swapped in the DI container — domain and application layers are untouched.
"""

import json
import time
import uuid
from typing import List, Optional

from ...application.repositories import (
    ApiCredential,
    BotToken,
    IAIConfigRepository,
    IApiCredentialRepository,
    IBotTokenRepository,
    IFilterRuleRepository,
    IForwardRuleRepository,
    IProcessedMessageRepository,
    ISessionRepository,
)
from ...domain.entities import AIConfig, FilterRule, ForwardRule, TelegramSession
from ...domain.value_objects import (
    AIProviderType,
    ContentMode,
    ForwardMode,
    RoutingType,
    TriggerEvent,
)
from ..security.crypto import CryptoService
from .database import SqliteDatabase


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


def _safe_list(raw, default=None):
    try:
        val = json.loads(raw) if raw else default
        return val if isinstance(val, list) else (default or [])
    except (json.JSONDecodeError, TypeError):
        return default or []


def _safe_dict(raw, default=None):
    try:
        val = json.loads(raw) if raw else default
        return val if isinstance(val, dict) else (default or {})
    except (json.JSONDecodeError, TypeError):
        return default or {}


class SqliteSessionRepository(ISessionRepository):
    def __init__(self, db: SqliteDatabase, crypto: CryptoService) -> None:
        self._db = db
        self._crypto = crypto

    async def add(self, session: TelegramSession) -> TelegramSession:
        enc = self._crypto.encrypt(session.session_string_encrypted) if session.session_string_encrypted else ""
        self._db.execute(
            "INSERT INTO sessions (id, phone_number, session_string_encrypted, user_id, username,"
            " first_name, is_active, is_authorized, api_credential_id, proxy,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.id, session.phone_number, enc, session.user_id, session.username,
                session.first_name, int(session.is_active), int(session.is_authorized),
                session.api_credential_id, json.dumps(session.proxy or {}),
                session.created_at, session.updated_at,
            ),
        )
        return session

    async def update(self, session: TelegramSession) -> TelegramSession:
        enc = self._crypto.encrypt(session.session_string_encrypted) if session.session_string_encrypted else ""
        self._db.execute(
            "UPDATE sessions SET phone_number=?, session_string_encrypted=?, user_id=?, username=?,"
            " first_name=?, is_active=?, is_authorized=?, api_credential_id=?, proxy=?,"
            " updated_at=? WHERE id=?",
            (
                session.phone_number, enc, session.user_id, session.username,
                session.first_name, int(session.is_active), int(session.is_authorized),
                session.api_credential_id, json.dumps(session.proxy or {}),
                session.updated_at, session.id,
            ),
        )
        return session

    def _row_to_entity(self, row) -> TelegramSession:
        enc = row["session_string_encrypted"]
        cols = set(row.keys())
        return TelegramSession(
            id=row["id"],
            phone_number=row["phone_number"],
            session_string_encrypted=self._crypto.decrypt(enc) if enc else "",
            user_id=row["user_id"],
            username=row["username"],
            first_name=row["first_name"],
            is_active=bool(row["is_active"]),
            is_authorized=bool(row["is_authorized"]),
            api_credential_id=row["api_credential_id"] if "api_credential_id" in cols else "default",
            proxy=_safe_dict(row["proxy"], {}) if "proxy" in cols and row["proxy"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_by_id(self, session_id: str) -> Optional[TelegramSession]:
        row = self._db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
        return self._row_to_entity(row) if row else None

    async def get_by_phone(self, phone: str) -> Optional[TelegramSession]:
        row = self._db.query_one("SELECT * FROM sessions WHERE phone_number=?", (phone,))
        return self._row_to_entity(row) if row else None

    async def list_all(self) -> List[TelegramSession]:
        rows = self._db.query_all("SELECT * FROM sessions ORDER BY created_at")
        return [self._row_to_entity(r) for r in rows]

    async def list_active(self) -> List[TelegramSession]:
        rows = self._db.query_all("SELECT * FROM sessions WHERE is_active=1 AND is_authorized=1")
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, session_id: str) -> bool:
        cur = self._db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0


class SqliteForwardRuleRepository(IForwardRuleRepository):
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def add(self, rule: ForwardRule) -> ForwardRule:
        self._db.execute(
            "INSERT INTO forward_rules (id, session_id, source_chat_id, source_chat_name, target_chat_id,"
            " target_chat_name, target_chat_ids, routing_type, forward_mode, is_active, filter_rule_id,"
            " ai_config_id, remove_links, custom_caption_template, delay_seconds, skip_history,"
            " since_ts, ignore_edits, trigger_events, content_mode, metadata, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule.id, rule.session_id, rule.source_chat_id, rule.source_chat_name,
                rule.target_chat_id, rule.target_chat_name, json.dumps(rule.target_chat_ids),
                rule.routing_type.value, rule.forward_mode.value, int(rule.is_active),
                rule.filter_rule_id, rule.ai_config_id, int(rule.remove_links),
                rule.custom_caption_template, rule.delay_seconds, int(rule.skip_history),
                rule.since_ts, int(rule.ignore_edits),
                json.dumps(getattr(rule, "trigger_events", [TriggerEvent.NEW_MESSAGE.value])
                           if isinstance(getattr(rule, "trigger_events", None), list)
                           else [getattr(rule, "trigger_events", TriggerEvent.NEW_MESSAGE.value)]),
                getattr(getattr(rule, "content_mode", ContentMode.AUTO), "value", ContentMode.AUTO.value),
                json.dumps(rule.metadata or {}), rule.created_at, rule.updated_at,
            ),
        )
        return rule

    async def update(self, rule: ForwardRule) -> ForwardRule:
        self._db.execute(
            "UPDATE forward_rules SET session_id=?, source_chat_id=?, source_chat_name=?, target_chat_id=?,"
            " target_chat_name=?, target_chat_ids=?, routing_type=?, forward_mode=?, is_active=?,"
            " filter_rule_id=?, ai_config_id=?, remove_links=?, custom_caption_template=?,"
            " delay_seconds=?, skip_history=?, since_ts=?, ignore_edits=?, trigger_events=?,"
            " content_mode=?, metadata=?, updated_at=? WHERE id=?",
            (
                rule.session_id, rule.source_chat_id, rule.source_chat_name,
                rule.target_chat_id, rule.target_chat_name, json.dumps(rule.target_chat_ids),
                rule.routing_type.value, rule.forward_mode.value, int(rule.is_active),
                rule.filter_rule_id, rule.ai_config_id, int(rule.remove_links),
                rule.custom_caption_template, rule.delay_seconds, int(rule.skip_history),
                rule.since_ts, int(rule.ignore_edits),
                json.dumps(getattr(rule, "trigger_events", [TriggerEvent.NEW_MESSAGE.value])
                           if isinstance(getattr(rule, "trigger_events", None), list)
                           else [getattr(rule, "trigger_events", TriggerEvent.NEW_MESSAGE.value)]),
                getattr(getattr(rule, "content_mode", ContentMode.AUTO), "value", ContentMode.AUTO.value),
                json.dumps(rule.metadata or {}), rule.updated_at, rule.id,
            ),
        )
        return rule

    @staticmethod
    def _row_to_entity(row) -> ForwardRule:
        cols = set(row.keys())
        trigger_raw = row["trigger_events"] if "trigger_events" in cols else "NEW_MESSAGE"
        triggers = _safe_list(trigger_raw, [TriggerEvent.NEW_MESSAGE.value])
        if isinstance(triggers, str):
            triggers = [triggers]
        content_raw = row["content_mode"] if "content_mode" in cols else ContentMode.AUTO.value
        try:
            cm = ContentMode(content_raw)
        except ValueError:
            cm = ContentMode.AUTO
        return ForwardRule(
            id=row["id"], session_id=row["session_id"],
            source_chat_id=row["source_chat_id"], source_chat_name=row["source_chat_name"],
            target_chat_id=row["target_chat_id"], target_chat_name=row["target_chat_name"],
            target_chat_ids=_safe_list(row["target_chat_ids"] if "target_chat_ids" in cols else "[]", []),
            routing_type=RoutingType(row["routing_type"]),
            forward_mode=ForwardMode(row["forward_mode"]),
            is_active=bool(row["is_active"]), filter_rule_id=row["filter_rule_id"],
            ai_config_id=row["ai_config_id"], remove_links=bool(row["remove_links"]),
            custom_caption_template=row["custom_caption_template"],
            delay_seconds=float(row["delay_seconds"]) if "delay_seconds" in cols else 0.0,
            skip_history=bool(row["skip_history"]) if "skip_history" in cols else True,
            since_ts=int(row["since_ts"] or 0) if "since_ts" in cols else 0,
            ignore_edits=bool(row["ignore_edits"]) if "ignore_edits" in cols else False,
            trigger_events=triggers,
            content_mode=cm,
            metadata=_safe_dict(row["metadata"] if "metadata" in cols else "{}", {}),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def get_by_id(self, rule_id: str) -> Optional[ForwardRule]:
        row = self._db.query_one("SELECT * FROM forward_rules WHERE id=?", (rule_id,))
        return self._row_to_entity(row) if row else None

    async def list_by_session(self, session_id: str) -> List[ForwardRule]:
        rows = self._db.query_all(
            "SELECT * FROM forward_rules WHERE session_id=? ORDER BY created_at", (session_id,)
        )
        return [self._row_to_entity(r) for r in rows]

    async def list_all(self) -> List[ForwardRule]:
        rows = self._db.query_all("SELECT * FROM forward_rules ORDER BY created_at")
        return [self._row_to_entity(r) for r in rows]

    async def list_active_by_source(self, source_chat_id: str) -> List[ForwardRule]:
        rows = self._db.query_all(
            "SELECT * FROM forward_rules WHERE source_chat_id=? AND is_active=1",
            (str(source_chat_id),),
        )
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, rule_id: str) -> bool:
        cur = self._db.execute("DELETE FROM forward_rules WHERE id=?", (rule_id,))
        return cur.rowcount > 0


class SqliteFilterRuleRepository(IFilterRuleRepository):
    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    @staticmethod
    def _cols(fr: FilterRule):
        return (
            fr.id, fr.name,
            json.dumps(fr.whitelist_keywords), json.dumps(fr.blacklist_keywords),
            json.dumps(fr.regex_patterns), json.dumps(fr.allowed_media_types),
            json.dumps(fr.blocked_media_types), int(fr.drop_service_messages),
            fr.min_message_length, fr.max_message_length,
        )

    async def add(self, filter_rule: FilterRule) -> FilterRule:
        self._db.execute(
            "INSERT INTO filter_rules (id, name, whitelist_keywords, blacklist_keywords, regex_patterns,"
            " allowed_media_types, blocked_media_types, drop_service_messages, min_message_length,"
            " max_message_length) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._cols(filter_rule),
        )
        return filter_rule

    async def update(self, filter_rule: FilterRule) -> FilterRule:
        self._db.execute(
            "UPDATE filter_rules SET name=?, whitelist_keywords=?, blacklist_keywords=?, regex_patterns=?,"
            " allowed_media_types=?, blocked_media_types=?, drop_service_messages=?, min_message_length=?,"
            " max_message_length=? WHERE id=?",
            (*self._cols(filter_rule)[1:], filter_rule.id),
        )
        return filter_rule

    @staticmethod
    def _row_to_entity(row) -> FilterRule:
        return FilterRule(
            id=row["id"], name=row["name"],
            whitelist_keywords=json.loads(row["whitelist_keywords"]),
            blacklist_keywords=json.loads(row["blacklist_keywords"]),
            regex_patterns=json.loads(row["regex_patterns"]),
            allowed_media_types=json.loads(row["allowed_media_types"]),
            blocked_media_types=json.loads(row["blocked_media_types"]),
            drop_service_messages=bool(row["drop_service_messages"]),
            min_message_length=row["min_message_length"],
            max_message_length=row["max_message_length"],
        )

    async def get_by_id(self, filter_id: str) -> Optional[FilterRule]:
        row = self._db.query_one("SELECT * FROM filter_rules WHERE id=?", (filter_id,))
        return self._row_to_entity(row) if row else None

    async def list_all(self) -> List[FilterRule]:
        rows = self._db.query_all("SELECT * FROM filter_rules ORDER BY name")
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, filter_id: str) -> bool:
        cur = self._db.execute("DELETE FROM filter_rules WHERE id=?", (filter_id,))
        return cur.rowcount > 0


class SqliteAIConfigRepository(IAIConfigRepository):
    def __init__(self, db: SqliteDatabase, crypto: CryptoService) -> None:
        self._db = db
        self._crypto = crypto

    async def add(self, config: AIConfig) -> AIConfig:
        self._db.execute(
            "INSERT INTO ai_configs (id, name, provider, model, api_key_encrypted, base_url, system_prompt,"
            " user_prompt_template, temperature, is_enabled, target_language)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                config.id, config.name, config.provider.value, config.model,
                self._crypto.encrypt(config.api_key) if config.api_key else "",
                config.base_url, config.system_prompt, config.user_prompt_template,
                config.temperature, int(config.is_enabled), config.target_language,
            ),
        )
        return config

    async def update(self, config: AIConfig) -> AIConfig:
        self._db.execute(
            "UPDATE ai_configs SET name=?, provider=?, model=?, api_key_encrypted=?, base_url=?,"
            " system_prompt=?, user_prompt_template=?, temperature=?, is_enabled=?, target_language=?"
            " WHERE id=?",
            (
                config.name, config.provider.value, config.model,
                self._crypto.encrypt(config.api_key) if config.api_key else "",
                config.base_url, config.system_prompt, config.user_prompt_template,
                config.temperature, int(config.is_enabled), config.target_language, config.id,
            ),
        )
        return config

    def _row_to_entity(self, row) -> AIConfig:
        enc = row["api_key_encrypted"]
        return AIConfig(
            id=row["id"], name=row["name"], provider=AIProviderType(row["provider"]),
            model=row["model"],
            api_key=self._crypto.decrypt(enc) if enc else "",
            base_url=row["base_url"], system_prompt=row["system_prompt"],
            user_prompt_template=row["user_prompt_template"],
            temperature=row["temperature"], is_enabled=bool(row["is_enabled"]),
            target_language=row["target_language"],
        )

    async def get_by_id(self, config_id: str) -> Optional[AIConfig]:
        row = self._db.query_one("SELECT * FROM ai_configs WHERE id=?", (config_id,))
        return self._row_to_entity(row) if row else None

    async def list_all(self) -> List[AIConfig]:
        rows = self._db.query_all("SELECT * FROM ai_configs ORDER BY name")
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, config_id: str) -> bool:
        cur = self._db.execute("DELETE FROM ai_configs WHERE id=?", (config_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Credential vault (api_id/api_hash) + bot tokens + processed-message dedupe
# --------------------------------------------------------------------------- #

class SqliteApiCredentialRepository(IApiCredentialRepository):
    def __init__(self, db: SqliteDatabase, crypto: CryptoService) -> None:
        self._db = db
        self._crypto = crypto

    async def add(self, cred: ApiCredential) -> ApiCredential:
        self._db.execute(
            "INSERT INTO api_credentials (id, label, api_id, api_hash_encrypted, proxy,"
            " is_default, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cred.id, cred.label, cred.api_id,
                self._crypto.encrypt(cred.api_hash) if cred.api_hash else "",
                cred.proxy, int(cred.is_default), cred.created_at, cred.updated_at,
            ),
        )
        return cred

    async def update(self, cred: ApiCredential) -> ApiCredential:
        self._db.execute(
            "UPDATE api_credentials SET label=?, api_id=?, api_hash_encrypted=?, proxy=?,"
            " is_default=?, updated_at=? WHERE id=?",
            (
                cred.label, cred.api_id,
                self._crypto.encrypt(cred.api_hash) if cred.api_hash else "",
                cred.proxy, int(cred.is_default), cred.updated_at, cred.id,
            ),
        )
        return cred

    def _row_to_entity(self, row) -> ApiCredential:
        enc = row["api_hash_encrypted"]
        return ApiCredential(
            id=row["id"], label=row["label"], api_id=row["api_id"],
            api_hash=self._crypto.decrypt(enc) if enc else "",
            proxy=row["proxy"], is_default=bool(row["is_default"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def get_by_id(self, cred_id: str) -> Optional[ApiCredential]:
        row = self._db.query_one("SELECT * FROM api_credentials WHERE id=?", (cred_id,))
        return self._row_to_entity(row) if row else None

    async def get_default(self) -> Optional[ApiCredential]:
        row = self._db.query_one("SELECT * FROM api_credentials WHERE is_default=1 LIMIT 1")
        return self._row_to_entity(row) if row else None

    async def list_all(self) -> List[ApiCredential]:
        rows = self._db.query_all("SELECT * FROM api_credentials ORDER BY label")
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, cred_id: str) -> bool:
        cur = self._db.execute("DELETE FROM api_credentials WHERE id=?", (cred_id,))
        return cur.rowcount > 0


class SqliteBotTokenRepository(IBotTokenRepository):
    def __init__(self, db: SqliteDatabase, crypto: CryptoService) -> None:
        self._db = db
        self._crypto = crypto

    async def add(self, token: BotToken) -> BotToken:
        self._db.execute(
            "INSERT INTO bot_tokens (id, label, token_encrypted, is_active, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                token.id, token.label,
                self._crypto.encrypt(token.token) if token.token else "",
                int(token.is_active), token.created_at, token.updated_at,
            ),
        )
        return token

    def _row_to_entity(self, row) -> BotToken:
        enc = row["token_encrypted"]
        return BotToken(
            id=row["id"], label=row["label"],
            token=self._crypto.decrypt(enc) if enc else "",
            is_active=bool(row["is_active"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def get_by_id(self, token_id: str) -> Optional[BotToken]:
        row = self._db.query_one("SELECT * FROM bot_tokens WHERE id=?", (token_id,))
        return self._row_to_entity(row) if row else None

    async def list_all(self) -> List[BotToken]:
        rows = self._db.query_all("SELECT * FROM bot_tokens ORDER BY created_at")
        return [self._row_to_entity(r) for r in rows]

    async def list_active(self) -> List[BotToken]:
        rows = self._db.query_all("SELECT * FROM bot_tokens WHERE is_active=1 ORDER BY created_at")
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, token_id: str) -> bool:
        cur = self._db.execute("DELETE FROM bot_tokens WHERE id=?", (token_id,))
        return cur.rowcount > 0


class SqliteProcessedMessageRepository(IProcessedMessageRepository):
    """Restart-safe dedupe: remembers (rule, chat, message) triples."""

    RETENTION_SECONDS = 60 * 60 * 24 * 30  # keep 30 days of history

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def was_processed(self, rule_id: str, chat_id: str, message_id: int) -> bool:
        row = self._db.query_one(
            "SELECT 1 FROM processed_messages WHERE rule_id=? AND chat_id=? AND message_id=?",
            (rule_id, chat_id, message_id),
        )
        return row is not None

    async def mark_processed(
        self, rule_id: str, chat_id: str, message_id: int, media_group_id: str = ""
    ) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO processed_messages (rule_id, chat_id, message_id,"
            " media_group_id, ts) VALUES (?, ?, ?, ?, ?)",
            (rule_id, chat_id, message_id, media_group_id or "", _now()),
        )

    async def purge_before(self, rule_id: str, ts: int) -> int:
        cur = self._db.execute(
            "DELETE FROM processed_messages WHERE rule_id=? AND ts < ?", (rule_id, ts)
        )
        return cur.rowcount

    async def cleanup(self) -> int:
        cutoff = _now() - self.RETENTION_SECONDS
        cur = self._db.execute("DELETE FROM processed_messages WHERE ts < ?", (cutoff,))
        return cur.rowcount
