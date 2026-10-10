"""SQLite repository implementations of the application repository ports.

A future PostgreSQL/MySQL backend implements the same interfaces and is
swapped in the DI container — domain and application layers are untouched.
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from ...application.repositories import (
    ApiCredential,
    BotToken,
    IAIConfigRepository,
    IApiCredentialRepository,
    IBotTokenRepository,
    IDeliveryQueueRepository,
    IErrorLogRepository,
    IFilterRuleRepository,
    IForwardRuleRepository,
    IMessageMapRepository,
    IMetricsRepository,
    IProcessedMessageRepository,
    IPromptRepository,
    IRuleStatsRepository,
    ISessionRepository,
    IUiStateRepository,
    IUserRepository,
    IAuthFlowRepository,
)
from ...domain.entities import (
    AIConfig,
    DeliveryJob,
    DeliveryStatus,
    FilterRule,
    ForwardRule,
    MessageMapping,
    PromptTemplate,
    PromptVersion,
    TelegramSession,
)
from ..telegram.auth_state import ACTIVE_STATES, AuthState
from ...domain.value_objects import (
    AIFallbackPolicy,
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
            " owner_user_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.id, session.phone_number, enc, session.user_id, session.username,
                session.first_name, int(session.is_active), int(session.is_authorized),
                session.api_credential_id, json.dumps(session.proxy or {}),
                int(getattr(session, "owner_user_id", 0) or 0),
                session.created_at, session.updated_at,
            ),
        )
        return session

    async def update(self, session: TelegramSession) -> TelegramSession:
        enc = self._crypto.encrypt(session.session_string_encrypted) if session.session_string_encrypted else ""
        self._db.execute(
            "UPDATE sessions SET phone_number=?, session_string_encrypted=?, user_id=?, username=?,"
            " first_name=?, is_active=?, is_authorized=?, api_credential_id=?, proxy=?,"
            " owner_user_id=?, updated_at=? WHERE id=?",
            (
                session.phone_number, enc, session.user_id, session.username,
                session.first_name, int(session.is_active), int(session.is_authorized),
                session.api_credential_id, json.dumps(session.proxy or {}),
                int(getattr(session, "owner_user_id", 0) or 0),
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
            owner_user_id=int(row["owner_user_id"]) if "owner_user_id" in cols and row["owner_user_id"] else 0,
        )

    async def get_by_id(self, session_id: str) -> Optional[TelegramSession]:
        row = self._db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
        return self._row_to_entity(row) if row else None

    async def get_by_phone(self, phone: str) -> Optional[TelegramSession]:
        row = self._db.query_one("SELECT * FROM sessions WHERE phone_number=?", (phone,))
        return self._row_to_entity(row) if row else None

    async def list_by_owner(self, owner_user_id: int) -> List[TelegramSession]:
        rows = self._db.query_all(
            "SELECT * FROM sessions WHERE owner_user_id=? ORDER BY created_at",
            (int(owner_user_id),)
        )
        return [self._row_to_entity(r) for r in rows]

    async def list_all(self) -> List[TelegramSession]:
        rows = self._db.query_all("SELECT * FROM sessions ORDER BY created_at")
        return [self._row_to_entity(r) for r in rows]

    async def list_by_owner(self, owner_user_id: int) -> List[TelegramSession]:
        rows = self._db.query_all(
            "SELECT * FROM sessions WHERE owner_user_id=? ORDER BY created_at",
            (int(owner_user_id),)
        )
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

    SMART_RULE_COLUMNS = (
        "message_category", "intermediate_channel_id", "intermediate_channel_name",
        "intermediate_target_chat_id", "use_intermediate", "fallback_mode",
        "fallback_enabled", "detection_criteria", "priority", "execution_order",
        "multi_route", "media_handling", "dedupe_policy", "max_retries",
        "retry_backoff_base", "rate_limit_per_minute", "rate_limit_burst",
        "custom_header", "custom_footer", "header_enabled", "template_text",
        "template_media", "template_album", "caption_max_length",
        "preserve_signature", "is_paused", "paused_until",
        "link_policy", "domain_allowlist", "domain_blocklist",
        "link_rewrite_map", "allowed_media_types", "split_long_caption",
    )

    @staticmethod
    def _smart_params(rule: ForwardRule) -> tuple:
        return (
            getattr(rule, "message_category", "ALL") or "ALL",
            getattr(rule, "intermediate_channel_id", "") or "",
            getattr(rule, "intermediate_channel_name", "") or "",
            getattr(rule, "intermediate_target_chat_id", "") or "",
            int(bool(getattr(rule, "use_intermediate", False))),
            getattr(getattr(rule, "fallback_mode", None), "value", "COPY_MESSAGE"),
            int(bool(getattr(rule, "fallback_enabled", True))),
            json.dumps(getattr(rule, "detection_criteria", {}) or {}),
            int(getattr(rule, "priority", 10) or 10),
            int(getattr(rule, "execution_order", 0) or 0),
            int(bool(getattr(rule, "multi_route", False))),
            getattr(rule, "media_handling", "AUTO") or "AUTO",
            getattr(rule, "dedupe_policy", "STRICT") or "STRICT",
            int(getattr(rule, "max_retries", 3) or 3),
            float(getattr(rule, "retry_backoff_base", 2.0) or 2.0),
            int(getattr(rule, "rate_limit_per_minute", 0) or 0),
            int(getattr(rule, "rate_limit_burst", 0) or 0),
            getattr(rule, "custom_header", "") or "",
            getattr(rule, "custom_footer", "") or "",
            int(bool(getattr(rule, "header_enabled", False))),
            getattr(rule, "template_text", "") or "",
            getattr(rule, "template_media", "") or "",
            getattr(rule, "template_album", "") or "",
            int(getattr(rule, "caption_max_length", 0) or 0),
            int(bool(getattr(rule, "preserve_signature", False))),
            int(bool(getattr(rule, "is_paused", False))),
            int(getattr(rule, "paused_until", 0) or 0),
            getattr(rule, "link_policy", "PRESERVE_ALL") or "PRESERVE_ALL",
            json.dumps(getattr(rule, "domain_allowlist", []) or []),
            json.dumps(getattr(rule, "domain_blocklist", []) or []),
            json.dumps(getattr(rule, "link_rewrite_map", {}) or {}),
            json.dumps(getattr(rule, "allowed_media_types", []) or []),
            int(bool(getattr(rule, "split_long_caption", True))),
        )

    @staticmethod
    def _smart_set_clause() -> str:
        return ", ".join(f"{c}=?" for c in SqliteForwardRuleRepository.SMART_RULE_COLUMNS)

    async def add(self, rule: ForwardRule) -> ForwardRule:
        rule_version = int(getattr(rule, "version", 1) or 1)
        ai_fallback = getattr(getattr(rule, "ai_fallback_policy", AIFallbackPolicy.DROP), "value", AIFallbackPolicy.DROP.value)
        ai_prompt_ver = int(getattr(rule, "ai_prompt_version", 0) or 0)
        ai_timeout = float(getattr(rule, "ai_timeout_seconds", 15.0) or 15.0)
        ai_sec = getattr(rule, "ai_secondary_config_id", None)
        # Check if version & AI columns are present
        try:
            self._db.execute(
                "INSERT INTO forward_rules (id, session_id, source_chat_id, source_chat_name, target_chat_id,"
                " target_chat_name, target_chat_ids, routing_type, forward_mode, is_active, filter_rule_id,"
                " ai_config_id, remove_links, custom_caption_template, delay_seconds, skip_history,"
                " since_ts, ignore_edits, trigger_events, content_mode, metadata, version, created_at, updated_at,"
                " ai_prompt_version, ai_fallback_policy, ai_timeout_seconds, ai_secondary_config_id,"
                " owner_user_id,"
                f" {', '.join(self.SMART_RULE_COLUMNS)})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                f" {', '.join('?' * len(self.SMART_RULE_COLUMNS))})",
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
                    json.dumps(rule.metadata or {}), rule_version, rule.created_at, rule.updated_at,
                    ai_prompt_ver, ai_fallback, ai_timeout, ai_sec,
                    int(getattr(rule, "owner_user_id", 0) or 0),
                    *self._smart_params(rule),
                ),
            )
        except Exception as exc:
            # Fallback for old schema without version column
            if "has no column named version" in str(exc) or "no column named version" in str(exc) or "no column named ai_prompt_version" in str(exc):
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
            else:
                raise
        return rule

    async def update(self, rule: ForwardRule, expected_version: Optional[int] = None) -> ForwardRule:
        rule_version = int(getattr(rule, "version", 1) or 1)
        next_version = rule_version + 1

        trigger_json = json.dumps(
            getattr(rule, "trigger_events", [TriggerEvent.NEW_MESSAGE.value])
            if isinstance(getattr(rule, "trigger_events", None), list)
            else [getattr(rule, "trigger_events", TriggerEvent.NEW_MESSAGE.value)]
        )
        content_val = getattr(getattr(rule, "content_mode", ContentMode.AUTO), "value", ContentMode.AUTO.value)
        meta_json = json.dumps(rule.metadata or {})

        ai_fallback = getattr(getattr(rule, "ai_fallback_policy", AIFallbackPolicy.DROP), "value", AIFallbackPolicy.DROP.value)
        ai_prompt_ver = int(getattr(rule, "ai_prompt_version", 0) or 0)
        ai_timeout = float(getattr(rule, "ai_timeout_seconds", 15.0) or 15.0)
        ai_sec = getattr(rule, "ai_secondary_config_id", None)

        params = (
            rule.session_id, rule.source_chat_id, rule.source_chat_name,
            rule.target_chat_id, rule.target_chat_name, json.dumps(rule.target_chat_ids),
            rule.routing_type.value, rule.forward_mode.value, int(rule.is_active),
            rule.filter_rule_id, rule.ai_config_id, int(rule.remove_links),
            rule.custom_caption_template, rule.delay_seconds, int(rule.skip_history),
            rule.since_ts, int(rule.ignore_edits),
            trigger_json, content_val, meta_json, next_version, rule.updated_at,
            ai_prompt_ver, ai_fallback, ai_timeout, ai_sec,
            *self._smart_params(rule),
        )

        from ...application.repositories import ConcurrencyError

        if expected_version is not None:
            cur = self._db.execute(
                "UPDATE forward_rules SET session_id=?, source_chat_id=?, source_chat_name=?, target_chat_id=?,"
                " target_chat_name=?, target_chat_ids=?, routing_type=?, forward_mode=?, is_active=?,"
                " filter_rule_id=?, ai_config_id=?, remove_links=?, custom_caption_template=?,"
                " delay_seconds=?, skip_history=?, since_ts=?, ignore_edits=?, trigger_events=?,"
                " content_mode=?, metadata=?, version=?, updated_at=?,"
                " ai_prompt_version=?, ai_fallback_policy=?, ai_timeout_seconds=?, ai_secondary_config_id=?,"
                f" {self._smart_set_clause()}"
                " WHERE id=? AND version=?",
                (*params, rule.id, int(expected_version)),
            )
            if cur.rowcount == 0:
                # Check if rule exists
                existing = await self.get_by_id(rule.id)
                if not existing:
                    raise ValueError(f"Rule {rule.id} does not exist")
                raise ConcurrencyError(
                    f"Concurrency conflict on rule {rule.id}: expected version {expected_version}, "
                    f"current version is {existing.version}"
                )
        else:
            try:
                cur = self._db.execute(
                    "UPDATE forward_rules SET session_id=?, source_chat_id=?, source_chat_name=?, target_chat_id=?,"
                    " target_chat_name=?, target_chat_ids=?, routing_type=?, forward_mode=?, is_active=?,"
                    " filter_rule_id=?, ai_config_id=?, remove_links=?, custom_caption_template=?,"
                    " delay_seconds=?, skip_history=?, since_ts=?, ignore_edits=?, trigger_events=?,"
                    " content_mode=?, metadata=?, version=?, updated_at=?,"
                    " ai_prompt_version=?, ai_fallback_policy=?, ai_timeout_seconds=?, ai_secondary_config_id=?,"
                    f" {self._smart_set_clause()}"
                    " WHERE id=?",
                    (*params, rule.id),
                )
            except Exception as exc:
                if "no such column: version" in str(exc) or "has no column named version" in str(exc):
                    # Fallback update without version
                    cur = self._db.execute(
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
                            trigger_json, content_val, meta_json, rule.updated_at, rule.id,
                        ),
                    )
                else:
                    raise

        rule.version = next_version
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
        rule_version = int(row["version"]) if ("version" in cols and row["version"] is not None) else 1
        fb_raw = row["ai_fallback_policy"] if "ai_fallback_policy" in cols else AIFallbackPolicy.DROP.value
        try:
            fb_policy = AIFallbackPolicy(fb_raw)
        except ValueError:
            fb_policy = AIFallbackPolicy.DROP
        ai_ver = int(row["ai_prompt_version"]) if ("ai_prompt_version" in cols and row["ai_prompt_version"] is not None) else 0
        ai_timeout = float(row["ai_timeout_seconds"]) if ("ai_timeout_seconds" in cols and row["ai_timeout_seconds"] is not None) else 15.0
        ai_sec = str(row["ai_secondary_config_id"]) if ("ai_secondary_config_id" in cols and row["ai_secondary_config_id"] is not None) else None

        def _col(name, default=None):
            return row[name] if name in cols and row[name] is not None else default

        try:
            fallback_mode = ForwardMode(_col("fallback_mode", ForwardMode.COPY_MESSAGE.value))
        except Exception:
            fallback_mode = ForwardMode.COPY_MESSAGE

        detection_criteria = _safe_dict(_col("detection_criteria", "{}"), {})

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
            ai_fallback_policy=fb_policy,
            ai_prompt_version=ai_ver,
            ai_timeout_seconds=ai_timeout,
            ai_secondary_config_id=ai_sec,
            metadata=_safe_dict(row["metadata"] if "metadata" in cols else "{}", {}),
            version=rule_version,
            created_at=row["created_at"], updated_at=row["updated_at"],
            # Smart Forwarding Rules fields (v8)
            message_category=_col("message_category", "ALL") or "ALL",
            intermediate_channel_id=_col("intermediate_channel_id", "") or "",
            intermediate_channel_name=_col("intermediate_channel_name", "") or "",
            intermediate_target_chat_id=_col("intermediate_target_chat_id", "") or "",
            use_intermediate=bool(_col("use_intermediate", 0)),
            fallback_mode=fallback_mode,
            fallback_enabled=bool(_col("fallback_enabled", 1)),
            detection_criteria=detection_criteria,
            priority=int(_col("priority", 10) or 10),
            execution_order=int(_col("execution_order", 0) or 0),
            multi_route=bool(_col("multi_route", 0)),
            media_handling=_col("media_handling", "AUTO") or "AUTO",
            dedupe_policy=_col("dedupe_policy", "STRICT") or "STRICT",
            max_retries=int(_col("max_retries", 3) or 3),
            retry_backoff_base=float(_col("retry_backoff_base", 2.0) or 2.0),
            rate_limit_per_minute=int(_col("rate_limit_per_minute", 0) or 0),
            rate_limit_burst=int(_col("rate_limit_burst", 0) or 0),
            custom_header=_col("custom_header", "") or "",
            custom_footer=_col("custom_footer", "") or "",
            header_enabled=bool(_col("header_enabled", 0)),
            template_text=_col("template_text", "") or "",
            template_media=_col("template_media", "") or "",
            template_album=_col("template_album", "") or "",
            caption_max_length=int(_col("caption_max_length", 0) or 0),
            preserve_signature=bool(_col("preserve_signature", 0)),
            is_paused=bool(_col("is_paused", 0)),
            paused_until=int(_col("paused_until", 0) or 0),
            link_policy=_col("link_policy", "PRESERVE_ALL") or "PRESERVE_ALL",
            domain_allowlist=_safe_list(_col("domain_allowlist", "[]"), []),
            domain_blocklist=_safe_list(_col("domain_blocklist", "[]"), []),
            link_rewrite_map=_safe_dict(_col("link_rewrite_map", "{}"), {}),
            allowed_media_types=_safe_list(_col("allowed_media_types", "[]"), []),
            split_long_caption=bool(_col("split_long_caption", 1)),
            owner_user_id=int(_col("owner_user_id", 0) or 0),
        )

    async def get_by_id(self, rule_id: str) -> Optional[ForwardRule]:
        row = self._db.query_one("SELECT * FROM forward_rules WHERE id=?", (rule_id,))
        return self._row_to_entity(row) if row else None

    async def list_by_session(self, session_id: str, owner_user_id: int = 0) -> List[ForwardRule]:
        if owner_user_id:
            rows = self._db.query_all(
                "SELECT * FROM forward_rules WHERE session_id=? AND owner_user_id=?"
                " ORDER BY created_at", (session_id, int(owner_user_id))
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM forward_rules WHERE session_id=? ORDER BY created_at", (session_id,)
            )
        return [self._row_to_entity(r) for r in rows]

    async def list_by_owner(self, owner_user_id: int) -> List[ForwardRule]:
        rows = self._db.query_all(
            "SELECT * FROM forward_rules WHERE owner_user_id=? ORDER BY created_at",
            (int(owner_user_id),)
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
            int(getattr(fr, "owner_user_id", 0) or 0),
        )

    async def add(self, filter_rule: FilterRule) -> FilterRule:
        self._db.execute(
            "INSERT INTO filter_rules (id, name, whitelist_keywords, blacklist_keywords, regex_patterns,"
            " allowed_media_types, blocked_media_types, drop_service_messages, min_message_length,"
            " max_message_length, owner_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._cols(filter_rule),
        )
        return filter_rule

    async def update(self, filter_rule: FilterRule) -> FilterRule:
        self._db.execute(
            "UPDATE filter_rules SET name=?, whitelist_keywords=?, blacklist_keywords=?, regex_patterns=?,"
            " allowed_media_types=?, blocked_media_types=?, drop_service_messages=?, min_message_length=?,"
            " max_message_length=?, owner_user_id=? WHERE id=?",
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
            owner_user_id=int(row["owner_user_id"]) if "owner_user_id" in row.keys() and row["owner_user_id"] else 0,
        )

    async def get_by_id(self, filter_id: str) -> Optional[FilterRule]:
        row = self._db.query_one("SELECT * FROM filter_rules WHERE id=?", (filter_id,))
        return self._row_to_entity(row) if row else None

    async def list_all(self) -> List[FilterRule]:
        rows = self._db.query_all("SELECT * FROM filter_rules ORDER BY name")
        return [self._row_to_entity(r) for r in rows]

    async def list_by_owner(self, owner_user_id: int) -> List[FilterRule]:
        rows = self._db.query_all(
            "SELECT * FROM filter_rules WHERE owner_user_id=? ORDER BY name",
            (int(owner_user_id),),
        )
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
            " user_prompt_template, temperature, is_enabled, target_language, owner_user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                config.id, config.name, config.provider.value, config.model,
                self._crypto.encrypt(config.api_key) if config.api_key else "",
                config.base_url, config.system_prompt, config.user_prompt_template,
                config.temperature, int(config.is_enabled), config.target_language,
                int(getattr(config, "owner_user_id", 0) or 0),
            ),
        )
        return config

    async def update(self, config: AIConfig) -> AIConfig:
        self._db.execute(
            "UPDATE ai_configs SET name=?, provider=?, model=?, api_key_encrypted=?, base_url=?,"
            " system_prompt=?, user_prompt_template=?, temperature=?, is_enabled=?, target_language=?,"
            " owner_user_id=? WHERE id=?",
            (
                config.name, config.provider.value, config.model,
                self._crypto.encrypt(config.api_key) if config.api_key else "",
                config.base_url, config.system_prompt, config.user_prompt_template,
                config.temperature, int(config.is_enabled), config.target_language,
                int(getattr(config, "owner_user_id", 0) or 0), config.id,
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
            owner_user_id=int(row["owner_user_id"]) if "owner_user_id" in row.keys() and row["owner_user_id"] else 0,
        )

    async def get_by_id(self, config_id: str) -> Optional[AIConfig]:
        row = self._db.query_one("SELECT * FROM ai_configs WHERE id=?", (config_id,))
        return self._row_to_entity(row) if row else None

    async def list_all(self) -> List[AIConfig]:
        rows = self._db.query_all("SELECT * FROM ai_configs ORDER BY name")
        return [self._row_to_entity(r) for r in rows]

    async def list_by_owner(self, owner_user_id: int) -> List[AIConfig]:
        rows = self._db.query_all(
            "SELECT * FROM ai_configs WHERE owner_user_id=? ORDER BY name",
            (int(owner_user_id),),
        )
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


# --------------------------------------------------------------------------- #
# v3: professional state DB — UI state, error log, metrics, rule stats, users
# --------------------------------------------------------------------------- #

class SqliteAuthFlowRepository(IAuthFlowRepository):
    """Persisted auth state machine rows (audit D1).

    Every write bumps ``version`` — optimistic concurrency, so a late callback
    from a previous flow cannot clobber the current one.
    """

    _COLUMNS = (
        "user_id", "state", "phone_number", "credential_id", "phone_code_hash",
        "code_attempts", "password_attempts", "version", "created_at",
        "updated_at", "expires_at", "last_error", "correlation_id",
    )

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def get(self, user_id: int) -> Optional[dict]:
        return self._db.query_one(
            "SELECT * FROM auth_flows WHERE user_id=?", (int(user_id),)
        )

    async def save(self, row: dict) -> None:
        cols = [c for c in self._COLUMNS if c in row]
        placeholders = ",".join("?" for _ in cols)
        assignments = ",".join(f"{c}=excluded.{c}" for c in cols)
        values = [row[c] for c in cols]
        self._db.execute(
            f"INSERT INTO auth_flows ({','.join(cols)}) VALUES ({placeholders})"
            f" ON CONFLICT(user_id) DO UPDATE SET {assignments}",
            tuple(values),
        )

    async def delete(self, user_id: int) -> None:
        self._db.execute("DELETE FROM auth_flows WHERE user_id=?", (int(user_id),))

    async def expired(self) -> list:
        # only flows that are still waiting on somebody can expire
        return self._db.query_all(
            "SELECT * FROM auth_flows WHERE expires_at < ? AND state NOT IN (?,?,?,?)",
            (
                int(time.time()),
                AuthState.AUTHENTICATED.value,
                AuthState.READY.value,
                AuthState.SESSION_REVOKED.value,
                AuthState.AUTH_EXPIRED.value,
            ),
        )

    async def active(self) -> list:
        # one index seek on idx_auth_flows_state per active state
        marks = ",".join("?" for _ in ACTIVE_STATES)
        return self._db.query_all(
            f"SELECT * FROM auth_flows WHERE state IN ({marks})",
            tuple(s.value for s in ACTIVE_STATES),
        )


class SqliteUiStateRepository(IUiStateRepository):
    """Durable UI flows: a restart mid-login resumes where the user stopped."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def get(self, user_id: int) -> Optional[dict]:
        row = self._db.query_one(
            "SELECT step, buffer FROM ui_states WHERE user_id=?", (int(user_id),)
        )
        if not row:
            return None
        return {"step": row["step"] or "", "buffer": SqliteDatabase.loads(row["buffer"], {})}

    async def save(self, user_id: int, step: str, buffer: dict) -> None:
        self._db.execute(
            "INSERT INTO ui_states (user_id, step, buffer, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET step=excluded.step, buffer=excluded.buffer,"
            " updated_at=excluded.updated_at",
            (int(user_id), step, SqliteDatabase.dumps(buffer or {}), int(time.time())),
        )

    async def clear(self, user_id: int) -> None:
        self._db.execute("DELETE FROM ui_states WHERE user_id=?", (int(user_id),))


class SqliteErrorLogRepository(IErrorLogRepository):
    """Every Telegram RPC error, with the exact exception class kept."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def record(self, category, error_name, severity, detail="",
                     recoverable=False, user_id=None, session_id=None,
                     rule_id=None, chat_id=None) -> None:
        self._db.execute(
            "INSERT INTO error_log (ts, user_id, session_id, rule_id, category, error_name,"
            " severity, detail, recoverable, chat_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()),
             int(user_id) if user_id else None, session_id, rule_id,
             category, error_name, severity, detail[:1000],
             int(bool(recoverable)), chat_id),
        )

    async def recent(self, limit: int = 10, severity: Optional[str] = None) -> List[dict]:
        if severity:
            rows = self._db.query_all(
                "SELECT * FROM error_log WHERE severity=? ORDER BY ts DESC LIMIT ?",
                (severity, int(limit)),
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM error_log ORDER BY ts DESC LIMIT ?", (int(limit),)
            )
        return [dict(r) for r in rows]

    async def counts_by_severity(self, since_ts: int = 0) -> dict:
        rows = self._db.query_all(
            "SELECT severity, COUNT(*) AS n FROM error_log WHERE ts>=?"
            " GROUP BY severity",
            (int(since_ts),),
        )
        return {r["severity"]: r["n"] for r in rows}


class SqliteMetricsRepository(IMetricsRepository):
    """Hourly throughput buckets — cheap writes, rolling window."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def bump(self, forwarded: int = 0, filtered: int = 0, errors: int = 0) -> None:
        hour = (int(time.time()) // 3600) * 3600
        self._db.execute(
            "INSERT INTO metrics_hourly (ts_hour, forwarded, filtered, errors)"
            " VALUES (?,?,?,?) ON CONFLICT(ts_hour) DO UPDATE SET"
            " forwarded=forwarded+excluded.forwarded,"
            " filtered=filtered+excluded.filtered,"
            " errors=errors+excluded.errors",
            (hour, int(forwarded), int(filtered), int(errors)),
        )

    async def hourly(self, hours: int = 24) -> List[dict]:
        since = int(time.time()) - int(hours) * 3600
        rows = self._db.query_all(
            "SELECT ts_hour, forwarded, filtered, errors FROM metrics_hourly"
            " WHERE ts_hour>=? ORDER BY ts_hour ASC",
            (since,),
        )
        return [dict(r) for r in rows]

    async def totals(self) -> dict:
        row = self._db.query_one(
            "SELECT COALESCE(SUM(forwarded),0) AS f, COALESCE(SUM(filtered),0) AS fl,"
            " COALESCE(SUM(errors),0) AS e FROM metrics_hourly"
        )
        return {"forwarded": row["f"], "filtered": row["fl"], "errors": row["e"]}

    async def total_str(self) -> str:
        """Human-readable lifetime totals for the dashboard."""
        t = await self.totals()
        return f"{t['forwarded']} forwarded / {t['filtered']} filtered / {t['errors']} errors"

    async def prune(self, keep_hours: int = 168) -> int:
        cutoff = int(time.time()) - int(keep_hours) * 3600
        cur = self._db.execute("DELETE FROM metrics_hourly WHERE ts_hour<?", (cutoff,))
        return cur.rowcount


class SqliteRuleStatsRepository(IRuleStatsRepository):
    """Per-rule counters bumped on the hot path without touching rule rows."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def bump(self, rule_id: str, forwarded: int = 0, filtered: int = 0, errors: int = 0) -> None:
        self._db.execute(
            "INSERT INTO rule_stats (rule_id, forwarded, filtered, errors, last_forward_ts)"
            " VALUES (?,?,?, ?,?) ON CONFLICT(rule_id) DO UPDATE SET"
            " forwarded=forwarded+excluded.forwarded,"
            " filtered=filtered+excluded.filtered,"
            " errors=errors+excluded.errors,"
            " last_forward_ts=excluded.last_forward_ts",
            (rule_id, int(forwarded), int(filtered), int(errors),
             int(time.time()) if forwarded else 0),
        )

    async def get(self, rule_id: str) -> Optional[dict]:
        row = self._db.query_one(
            "SELECT * FROM rule_stats WHERE rule_id=?", (rule_id,)
        )
        return dict(row) if row else None

    async def top_rules(self, limit: int = 10) -> List[dict]:
        rows = self._db.query_all(
            "SELECT * FROM rule_stats ORDER BY forwarded DESC LIMIT ?", (int(limit),)
        )
        return [dict(r) for r in rows]


class SqliteUserRepository(IUserRepository):
    """Per-user language, plan, quota."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def get_or_create(self, user_id: int, language: str = "") -> dict:
        uid = int(user_id)
        row = self._db.query_one("SELECT * FROM users WHERE user_id=?", (uid,))
        if row:
            return dict(row)
        now = int(time.time())
        self._db.execute(
            "INSERT INTO users (user_id, language, is_admin, plan, quota_forwarded,"
            " quota_reset_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (uid, language, 0, "free", 0, now, now, now),
        )
        return {
            "user_id": uid, "language": language, "is_admin": 0, "plan": "free",
            "quota_forwarded": 0, "quota_reset_at": now,
        }

    async def set_language(self, user_id: int, language: str) -> None:
        self._db.execute(
            "UPDATE users SET language=?, updated_at=? WHERE user_id=?",
            (language, int(time.time()), int(user_id)),
        )

    async def bump_quota(self, user_id: int, by: int = 1) -> None:
        self._db.execute(
            "UPDATE users SET quota_forwarded=quota_forwarded+?, updated_at=? WHERE user_id=?",
            (int(by), int(time.time()), int(user_id)),
        )


# --------------------------------------------------------------------------- #
# v6: P0 Data-Plane Reliability — Message Map & Durable Queue
# --------------------------------------------------------------------------- #

class SqliteMessageMapRepository(IMessageMapRepository):
    """SQLite implementation of the persistent message mapping port."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def _row_to_entity(self, row) -> MessageMapping:
        status_val = row["delivery_status"]
        try:
            status = DeliveryStatus(status_val)
        except (ValueError, TypeError):
            status = DeliveryStatus.PENDING

        return MessageMapping(
            id=row["id"],
            rule_id=row["rule_id"],
            source_chat_id=str(row["source_chat_id"]),
            source_message_id=int(row["source_message_id"]),
            target_chat_id=str(row["target_chat_id"]),
            target_message_id=int(row["target_message_id"]) if row["target_message_id"] is not None else None,
            media_group_id=row["media_group_id"] if row["media_group_id"] else None,
            delivery_status=status,
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    async def add_or_update(self, mapping: MessageMapping) -> MessageMapping:
        now = _now()
        mapping.updated_at = now
        sql = """
        INSERT INTO message_map (
            id, rule_id, source_chat_id, source_message_id, target_chat_id,
            target_message_id, media_group_id, delivery_status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rule_id, source_chat_id, source_message_id, target_chat_id) DO UPDATE SET
            target_message_id = COALESCE(excluded.target_message_id, message_map.target_message_id),
            media_group_id = COALESCE(excluded.media_group_id, message_map.media_group_id),
            delivery_status = excluded.delivery_status,
            updated_at = excluded.updated_at
        """
        status_str = getattr(mapping.delivery_status, "value", str(mapping.delivery_status))
        self._db.execute(
            sql,
            (
                mapping.id,
                mapping.rule_id,
                str(mapping.source_chat_id),
                int(mapping.source_message_id),
                str(mapping.target_chat_id),
                mapping.target_message_id,
                mapping.media_group_id or "",
                status_str,
                mapping.created_at,
                mapping.updated_at,
            ),
        )
        return mapping

    async def get_by_source(self, source_chat_id: str, source_message_id: int) -> List[MessageMapping]:
        rows = self._db.query_all(
            "SELECT * FROM message_map WHERE source_chat_id=? AND source_message_id=?",
            (str(source_chat_id), int(source_message_id)),
        )
        return [self._row_to_entity(r) for r in rows]

    async def get_by_target(self, target_chat_id: str, target_message_id: int) -> Optional[MessageMapping]:
        row = self._db.query_one(
            "SELECT * FROM message_map WHERE target_chat_id=? AND target_message_id=?",
            (str(target_chat_id), int(target_message_id)),
        )
        return self._row_to_entity(row) if row else None

    async def get_by_rule_and_source(
        self, rule_id: str, source_chat_id: str, source_message_id: int
    ) -> List[MessageMapping]:
        rows = self._db.query_all(
            "SELECT * FROM message_map WHERE rule_id=? AND source_chat_id=? AND source_message_id=?",
            (rule_id, str(source_chat_id), int(source_message_id)),
        )
        return [self._row_to_entity(r) for r in rows]

    async def update_delivery(
        self,
        rule_id: str,
        source_chat_id: str,
        source_message_id: int,
        target_chat_id: str,
        target_message_id: Optional[int],
        status: DeliveryStatus,
    ) -> bool:
        now = _now()
        status_str = getattr(status, "value", str(status))
        cur = self._db.execute(
            """
            UPDATE message_map SET
                target_message_id = COALESCE(?, target_message_id),
                delivery_status = ?,
                updated_at = ?
            WHERE rule_id = ? AND source_chat_id = ? AND source_message_id = ? AND target_chat_id = ?
            """,
            (
                target_message_id,
                status_str,
                now,
                rule_id,
                str(source_chat_id),
                int(source_message_id),
                str(target_chat_id),
            ),
        )
        return cur.rowcount > 0

    async def cleanup(self, retention_seconds: int = 60 * 60 * 24 * 30) -> int:
        cutoff = _now() - retention_seconds
        cur = self._db.execute("DELETE FROM message_map WHERE updated_at < ?", (cutoff,))
        return cur.rowcount


class SqliteDeliveryQueueRepository(IDeliveryQueueRepository):
    """SQLite implementation of the durable delivery jobs queue."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def _row_to_entity(self, row) -> DeliveryJob:
        status_val = row["status"]
        try:
            status = DeliveryStatus(status_val)
        except (ValueError, TypeError):
            status = DeliveryStatus.PENDING

        payload = SqliteDatabase.loads(row["payload_json"], {})
        keys = row.keys() if hasattr(row, "keys") else []
        inter_chat = row["intermediate_chat_id"] if "intermediate_chat_id" in keys else None
        inter_msg = row["intermediate_message_id"] if "intermediate_message_id" in keys else None
        deliv_stage = row["delivery_stage"] if "delivery_stage" in keys else "DIRECT"
        return DeliveryJob(
            id=row["id"],
            rule_id=row["rule_id"],
            source_chat_id=str(row["source_chat_id"]),
            source_message_id=int(row["source_message_id"]),
            target_chat_id=str(row["target_chat_id"]),
            payload_data=payload,
            status=status,
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_retry_at=float(row["next_retry_at"]),
            lease_until=float(row["lease_until"]),
            worker_id=row["worker_id"] if row["worker_id"] else None,
            error_detail=row["error_detail"] if row["error_detail"] else None,
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            intermediate_chat_id=str(inter_chat) if inter_chat else None,
            intermediate_message_id=int(inter_msg) if inter_msg is not None else None,
            delivery_stage=str(deliv_stage or "DIRECT"),
        )

    async def enqueue(self, job: DeliveryJob) -> bool:
        payload_str = SqliteDatabase.dumps(job.payload_data)
        status_str = getattr(job.status, "value", str(job.status))
        try:
            cur = self._db.execute(
                """
                INSERT OR IGNORE INTO delivery_jobs (
                    id, rule_id, source_chat_id, source_message_id, target_chat_id,
                    payload_json, status, attempts, max_attempts, next_retry_at,
                    lease_until, worker_id, error_detail, created_at, updated_at,
                    intermediate_chat_id, intermediate_message_id, delivery_stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.rule_id,
                    str(job.source_chat_id),
                    int(job.source_message_id),
                    str(job.target_chat_id),
                    payload_str,
                    status_str,
                    job.attempts,
                    job.max_attempts,
                    job.next_retry_at,
                    job.lease_until,
                    job.worker_id or "",
                    job.error_detail or "",
                    job.created_at,
                    job.updated_at,
                    str(job.intermediate_chat_id or "") if job.intermediate_chat_id else None,
                    job.intermediate_message_id,
                    job.delivery_stage or "DIRECT",
                ),
            )
        except Exception:
            # Fallback for tables without v9 columns
            cur = self._db.execute(
                """
                INSERT OR IGNORE INTO delivery_jobs (
                    id, rule_id, source_chat_id, source_message_id, target_chat_id,
                    payload_json, status, attempts, max_attempts, next_retry_at,
                    lease_until, worker_id, error_detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.rule_id,
                    str(job.source_chat_id),
                    int(job.source_message_id),
                    str(job.target_chat_id),
                    payload_str,
                    status_str,
                    job.attempts,
                    job.max_attempts,
                    job.next_retry_at,
                    job.lease_until,
                    job.worker_id or "",
                    job.error_detail or "",
                    job.created_at,
                    job.updated_at,
                ),
            )
        return cur.rowcount > 0

    async def claim_batch(
        self, worker_id: str, batch_size: int = 5, lease_duration: float = 30.0
    ) -> List[DeliveryJob]:
        now = time.time()
        claimed: List[DeliveryJob] = []
        with self._db._lock:
            # Atomic claim under lock
            rows = self._db._conn.execute(
                """
                SELECT * FROM delivery_jobs
                WHERE (
                    status = 'PENDING'
                    OR status = 'RETRY_WAIT'
                    OR (status = 'CLAIMED' AND lease_until < ?)
                )
                AND next_retry_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (now, now, int(batch_size)),
            ).fetchall()

            if not rows:
                return []

            lease_until = now + lease_duration
            now_int = int(now)
            for r in rows:
                self._db._conn.execute(
                    """
                    UPDATE delivery_jobs
                    SET status = 'CLAIMED', worker_id = ?, lease_until = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (worker_id, lease_until, now_int, r["id"]),
                )
            self._db._conn.commit()

            # Re-fetch claimed items
            for r in rows:
                job = self._row_to_entity(r)
                job.status = DeliveryStatus.CLAIMED
                job.worker_id = worker_id
                job.lease_until = lease_until
                claimed.append(job)

        return claimed

    async def mark_sent(self, job_id: str, target_message_id: Optional[int] = None) -> bool:
        now = _now()
        cur = self._db.execute(
            """
            UPDATE delivery_jobs
            SET status = 'SENT', lease_until = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, job_id),
        )
        return cur.rowcount > 0

    async def mark_retry(self, job_id: str, error: str, backoff_seconds: float) -> bool:
        now = time.time()
        row = self._db.query_one("SELECT attempts, max_attempts FROM delivery_jobs WHERE id = ?", (job_id,))
        if not row:
            return False

        attempts = int(row["attempts"]) + 1
        max_attempts = int(row["max_attempts"])

        if attempts >= max_attempts:
            cur = self._db.execute(
                """
                UPDATE delivery_jobs
                SET status = 'FAILED', attempts = ?, error_detail = ?, lease_until = 0, updated_at = ?
                WHERE id = ?
                """,
                (attempts, error, int(now), job_id),
            )
        else:
            next_retry = now + backoff_seconds
            cur = self._db.execute(
                """
                UPDATE delivery_jobs
                SET status = 'RETRY_WAIT', attempts = ?, next_retry_at = ?,
                    error_detail = ?, lease_until = 0, updated_at = ?
                WHERE id = ?
                """,
                (attempts, next_retry, error, int(now), job_id),
            )
        return cur.rowcount > 0

    async def mark_failed(self, job_id: str, error: str) -> bool:
        now = _now()
        cur = self._db.execute(
            """
            UPDATE delivery_jobs
            SET status = 'FAILED', error_detail = ?, lease_until = 0, updated_at = ?
            WHERE id = ?
            """,
            (error, now, job_id),
        )
        return cur.rowcount > 0

    async def mark_intermediate_copied(self, job_id: str, inter_chat: str, inter_msg_id: int) -> bool:
        now = _now()
        try:
            cur = self._db.execute(
                """
                UPDATE delivery_jobs
                SET status = 'COPIED_TO_C', delivery_stage = 'COPIED_TO_C',
                    intermediate_chat_id = ?, intermediate_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(inter_chat), int(inter_msg_id), now, job_id),
            )
            return cur.rowcount > 0
        except Exception:
            return False

    async def mark_dead_letter(self, job_id: str, error: str, category: str = "DELIVERY_EXHAUSTED") -> bool:
        now = _now()
        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO dead_letter_queue (
                    id, job_id, rule_id, source_chat_id, source_message_id,
                    target_chat_id, payload_json, error_message, error_category, dead_lettered_at
                )
                SELECT ?, id, rule_id, source_chat_id, source_message_id,
                       target_chat_id, payload_json, ?, ?, ?
                FROM delivery_jobs WHERE id = ?
                """,
                (f"dlq_{job_id}", error, category, now, job_id),
            )
        except Exception:
            pass
        cur = self._db.execute(
            """
            UPDATE delivery_jobs
            SET status = 'DEAD_LETTER', error_detail = ?, lease_until = 0, updated_at = ?
            WHERE id = ?
            """,
            (error, now, job_id),
        )
        return cur.rowcount > 0

    async def get_dead_letter_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            rows = self._db.query_all(
                "SELECT * FROM dead_letter_queue ORDER BY dead_lettered_at DESC LIMIT ?", (int(limit),)
            )
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def mark_unknown(self, job_id: str, error: str) -> bool:
        now = _now()
        cur = self._db.execute(
            """
            UPDATE delivery_jobs
            SET status = 'UNKNOWN', error_detail = ?, lease_until = 0, updated_at = ?
            WHERE id = ?
            """,
            (error, now, job_id),
        )
        return cur.rowcount > 0

    async def get_pending_count(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) as cnt FROM delivery_jobs WHERE status IN ('PENDING', 'CLAIMED', 'RETRY_WAIT')"
        )
        return int(row["cnt"]) if row else 0

    async def get_stats(self) -> dict:
        rows = self._db.query_all("SELECT status, COUNT(*) as cnt FROM delivery_jobs GROUP BY status")
        res = {s.value: 0 for s in DeliveryStatus}
        for r in rows:
            res[r["status"]] = int(r["cnt"])
        return res

    async def recover_expired_leases(self) -> int:
        now = time.time()
        cur = self._db.execute(
            """
            UPDATE delivery_jobs
            SET status = 'RETRY_WAIT', lease_until = 0, updated_at = ?
            WHERE status = 'CLAIMED' AND lease_until < ?
            """,
            (int(now), now),
        )
        return cur.rowcount

    async def get_queue_age_metrics(self) -> dict:
        row = self._db.query_one(
            """
            SELECT MIN(created_at) as oldest_ts, COUNT(*) as cnt
            FROM delivery_jobs
            WHERE status IN ('PENDING', 'CLAIMED', 'RETRY_WAIT')
            """
        )
        if not row or not row["cnt"]:
            return {"pending_count": 0, "oldest_job_age_seconds": 0.0}
        oldest_ts = row["oldest_ts"]
        age = max(0.0, time.time() - float(oldest_ts)) if oldest_ts else 0.0
        return {"pending_count": int(row["cnt"]), "oldest_job_age_seconds": round(age, 2)}

    async def cleanup(self, retention_seconds: int = 60 * 60 * 24 * 7) -> int:
        threshold = int(time.time() - retention_seconds)
        cur = self._db.execute(
            """
            DELETE FROM delivery_jobs
            WHERE status IN ('SENT', 'FAILED') AND updated_at < ?
            """,
            (threshold,),
        )
        return cur.rowcount


class SqlitePromptRepository(IPromptRepository):
    """Production-grade SQLite repository for prompt templates and historic versions."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def add_template(self, template: PromptTemplate) -> PromptTemplate:
        self._db.execute(
            """
            INSERT INTO prompt_templates (id, name, description, system_prompt, user_prompt_template,
                target_language, current_version, is_system, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template.id, template.name, template.description, template.system_prompt,
                template.user_prompt_template, template.target_language, template.current_version,
                int(template.is_system), template.created_at, template.updated_at,
            ),
        )
        return template

    async def update_template(self, template: PromptTemplate) -> PromptTemplate:
        template.updated_at = int(time.time())
        self._db.execute(
            """
            UPDATE prompt_templates
            SET name=?, description=?, system_prompt=?, user_prompt_template=?,
                target_language=?, current_version=?, is_system=?, updated_at=?
            WHERE id=?
            """,
            (
                template.name, template.description, template.system_prompt,
                template.user_prompt_template, template.target_language,
                template.current_version, int(template.is_system),
                template.updated_at, template.id,
            ),
        )
        return template

    async def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        row = self._db.query_one("SELECT * FROM prompt_templates WHERE id=?", (template_id,))
        if not row:
            return None
        return PromptTemplate(
            id=row["id"], name=row["name"], description=row["description"],
            system_prompt=row["system_prompt"], user_prompt_template=row["user_prompt_template"],
            target_language=row["target_language"], current_version=int(row["current_version"]),
            is_system=bool(row["is_system"]), created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def list_templates(self) -> List[PromptTemplate]:
        rows = self._db.query_all("SELECT * FROM prompt_templates ORDER BY created_at ASC")
        return [
            PromptTemplate(
                id=r["id"], name=r["name"], description=r["description"],
                system_prompt=r["system_prompt"], user_prompt_template=r["user_prompt_template"],
                target_language=r["target_language"], current_version=int(r["current_version"]),
                is_system=bool(r["is_system"]), created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def delete_template(self, template_id: str) -> bool:
        cur = self._db.execute("DELETE FROM prompt_templates WHERE id=?", (template_id,))
        self._db.execute("DELETE FROM prompt_versions WHERE prompt_id=?", (template_id,))
        return cur.rowcount > 0

    async def add_version(self, version: PromptVersion) -> PromptVersion:
        self._db.execute(
            """
            INSERT INTO prompt_versions (id, prompt_id, version, system_prompt,
                user_prompt_template, change_summary, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.id, version.prompt_id, version.version, version.system_prompt,
                version.user_prompt_template, version.change_summary,
                int(version.is_active), version.created_at,
            ),
        )
        return version

    async def get_version(self, prompt_id: str, version: int) -> Optional[PromptVersion]:
        row = self._db.query_one(
            "SELECT * FROM prompt_versions WHERE prompt_id=? AND version=?",
            (prompt_id, version),
        )
        if not row:
            return None
        return PromptVersion(
            id=row["id"], prompt_id=row["prompt_id"], version=int(row["version"]),
            system_prompt=row["system_prompt"], user_prompt_template=row["user_prompt_template"],
            change_summary=row["change_summary"], is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )

    async def list_versions(self, prompt_id: str) -> List[PromptVersion]:
        rows = self._db.query_all(
            "SELECT * FROM prompt_versions WHERE prompt_id=? ORDER BY version DESC",
            (prompt_id,),
        )
        return [
            PromptVersion(
                id=r["id"], prompt_id=r["prompt_id"], version=int(r["version"]),
                system_prompt=r["system_prompt"], user_prompt_template=r["user_prompt_template"],
                change_summary=r["change_summary"], is_active=bool(r["is_active"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def activate_version(self, prompt_id: str, version: int) -> bool:
        ver = await self.get_version(prompt_id, version)
        if not ver:
            return False
        self._db.execute(
            """
            UPDATE prompt_templates
            SET system_prompt=?, user_prompt_template=?, current_version=?, updated_at=?
            WHERE id=?
            """,
            (ver.system_prompt, ver.user_prompt_template, version, int(time.time()), prompt_id),
        )
        self._db.execute(
            "UPDATE prompt_versions SET is_active = (version = ?) WHERE prompt_id=?",
            (version, prompt_id),
        )
        return True

    async def get_active_version(self, prompt_id: str) -> Optional[PromptVersion]:
        row = self._db.query_one(
            "SELECT * FROM prompt_versions WHERE prompt_id=? AND is_active=1 LIMIT 1",
            (prompt_id,),
        )
        if not row:
            return None
        return PromptVersion(
            id=row["id"],
            prompt_id=row["prompt_id"],
            version=int(row["version"]),
            system_prompt=row["system_prompt"],
            user_prompt_template=row["user_prompt_template"],
            change_summary=row["change_summary"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )



