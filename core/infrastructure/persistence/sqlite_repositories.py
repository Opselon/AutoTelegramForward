"""SQLite repository implementations of the application repository ports.

A future PostgreSQL/MySQL backend implements the same interfaces and is
swapped in the DI container — domain and application layers are untouched.
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _dumps(value) -> str:
    """Serialize a job payload the same way the queue does."""
    from .database import SqliteDatabase
    return SqliteDatabase.dumps(value)


def _payload_dict(raw) -> dict:
    """Decode a stored job payload, tolerating either row shape."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


from .coercion import enum_str

logger = logging.getLogger(__name__)


from ...application.repositories import (
    ApiCredential,
    BotToken,
    DeliveryAttemptKey,
    IAIConfigRepository,
    IApiCredentialRepository,
    IAuthFlowRepository,
    IBotTokenRepository,
    IDeliveryAttemptRepository,
    IDeliveryQueueRepository,
    IErrorLogRepository,
    IDiagnosticEventRepository,
    IFilterRuleRepository,
    IForwardRuleRepository,
    IIngestionGapRepository,
    IMessageMapRepository,
    IMetricsRepository,
    ITaskRepository,
    IProcessedMessageRepository,
    IPromptRepository,
    IRuleCounterRepository,
    IRuleStatsRepository,
    ISessionRepository,
    ISettingsRepository,
    IUiStateRepository,
    IUserRepository,
    kind_filter_sql,
    TASK_PURGE_BATCH,
    TASK_RETENTION_SECONDS,
)
from ...domain.entities import (
    AIConfig,
    DeliveryJob,
    UserTask,
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


def _safe_decrypt(crypto, enc: str, *, kind: str, row_id: str) -> str:
    """Decrypt one stored secret, or return "" for a row that cannot be read.

    One undecryptable row (written under a previous ``ATF_MASTER_KEY``, or
    truncated) used to fail the whole *list*: ListBotTokens answered
    "Unexpected [Error]" with a base64 traceback and the dashboard rendered a
    blank page. A per-row failure now degrades to an empty secret with a named
    warning, so the rest of the page still works and the operator knows exactly
    which row to re-enter.
    """
    if not enc:
        return ""
    try:
        return crypto.decrypt(enc)
    except Exception as exc:  # noqa: BLE001 - one bad row must not fail a list
        logger.warning(
            "%s %s cannot be decrypted (%s); returning an empty secret — "
            "re-enter it or restore the master key",
            kind, str(row_id)[:8], exc,
        )
        return ""


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


def _safe_list(raw, default=None):
    # Empty is by far the most common stored value for these JSON columns, and
    # ``_row_to_entity`` parses eight of them per rule: a rules page that
    # hydrates 1000 rows otherwise runs 8000 json.loads calls to discover that
    # eight lists are empty. The literal is checked first; anything else takes
    # the real parser.
    if raw == "[]":
        return []
    try:
        val = json.loads(raw) if raw else default
        return val if isinstance(val, list) else (default or [])
    except (json.JSONDecodeError, TypeError):
        return default or []


def _rule_meta_json(rule) -> str:
    """Serialise rule metadata, mirroring name/description into it.

    forward_rules has no name or description column, so a task renamed through
    the Telegram bot or the REST API was accepted, reported as saved, and then
    silently lost on the next reload. `metadata` is the JSON column every rule
    already carries, so mirroring persists the value with no schema migration;
    `_row_to_entity` reads it back.
    """
    meta = dict(rule.metadata) if isinstance(rule.metadata, dict) else {}
    name = str(getattr(rule, "name", "") or "")
    description = str(getattr(rule, "description", "") or "")
    if name:
        meta["name"] = name
    else:
        meta.pop("name", None)
    if description:
        meta["description"] = description
    else:
        meta.pop("description", None)
    return json.dumps(meta)


def _safe_dict(raw, default=None):
    # See ``_safe_list``: the empty document is the common case and the parser
    # is not free.
    if raw == "{}":
        return {}
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
            session_string_encrypted=_safe_decrypt(self._crypto, enc, kind="session", row_id=row["id"]),
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

    async def list_active(self) -> List[TelegramSession]:
        rows = self._db.query_all("SELECT * FROM sessions WHERE is_active=1 AND is_authorized=1")
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, session_id: str) -> bool:
        cur = self._db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0


def _reject_inherited_write(rule: Any) -> None:
    """Refuse to save a rule carrying inherited bot defaults.

    The forwarding paths hand the pipeline a *copy* of a rule with the attached
    bot's defaults filled in (see core.application.effective_settings). Those
    values are not the task's choices; writing them back would pin them and the
    task would silently stop tracking its bot. A save of such an object is a bug
    in the caller, so it fails loudly here instead of corrupting configuration.
    """
    inherited = getattr(rule, "_inherited_defaults", None)
    if inherited:
        raise ValueError(
            "refusing to persist a rule carrying inherited bot defaults "
            f"({', '.join(inherited)}); resolve them for forwarding without "
            "writing them back"
        )


#: Process-wide invalidation counter for `list_active_by_source`'s cache, keyed
#: by database. Each repository keeps its own dict, but a write through *any*
#: repository on the same database has to invalidate all of them: a caller that
#: loads a rule with a fresh repository, edits it and saves it otherwise leaves
#: the pipeline serving the pre-edit routing table until the TTL expires. That
#: is not hypothetical — it starved a second task of the same tenant in
#: `test_full_stack_cross_feature.py`, because the pipeline's repository never
#: saw the write.
_ACTIVE_GENERATION: Dict[str, int] = {}
_ACTIVE_GENERATION_LOCK = threading.Lock()


def _db_identity(db: Any) -> str:
    """A stable key for one database file, across handles and instances."""
    path = getattr(db, "path", None) or getattr(db, "_db_path", None)
    if path:
        try:
            return os.path.realpath(str(path))
        except OSError:  # pragma: no cover - exotic paths only
            return str(path)
    return f"id:{id(db)}"


def _active_generation(db: Any) -> int:
    with _ACTIVE_GENERATION_LOCK:
        return _ACTIVE_GENERATION.get(_db_identity(db), 0)


def _bump_active_generation(db: Any) -> None:
    key = _db_identity(db)
    with _ACTIVE_GENERATION_LOCK:
        _ACTIVE_GENERATION[key] = _ACTIVE_GENERATION.get(key, 0) + 1


class SqliteForwardRuleRepository(IForwardRuleRepository):
    #: How long a resolved active-rule set may be reused, in seconds.
    #: ``list_active_by_source`` runs once per inbound message. Its result is a
    #: *routing table*, not a live reading: it changes when a human creates,
    #: edits, pauses or deletes a task, which is minutes apart, while a busy
    #: channel delivers many messages per second. Without reuse, every message
    #: in a burst repeats the identical query — measured at 0.08ms on a
    #: distributed store, but 24ms on one where 1000 tasks watch a single
    #: source, which is the shape a large install actually has.
    #:
    #: Correctness is guarded from both sides: every write through this
    #: repository drops the cache immediately (so a pause is visible on the
    #: next message, not on the next expiry), and the TTL bounds staleness for
    #: a writer in *another* process — a second core process, or the worker —
    #: which cannot reach this dict.
    ACTIVE_CACHE_TTL = float(os.environ.get("ATF_RULE_CACHE_TTL", "1.0"))
    #: Bounded so a deployment with thousands of distinct sources cannot grow
    #: the cache without limit; the whole map is dropped when it fills.
    ACTIVE_CACHE_MAX = 1024

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db
        self._active_cache: Dict[str, tuple] = {}
        self._active_lock = threading.Lock()
        self._active_hits = 0
        self._active_misses = 0

    def invalidate_active_cache(self) -> None:
        """Forget every cached active-rule set, for every handle on this database."""
        with self._active_lock:
            self._active_cache.clear()
        # Bumping the shared generation is what makes a write through a *second*
        # repository instance visible here; clearing the local dict alone only
        # covers writers that happen to share this object.
        _bump_active_generation(self._db)

    def active_cache_stats(self) -> Dict[str, int]:
        with self._active_lock:
            return {
                "hits": self._active_hits,
                "misses": self._active_misses,
                "entries": len(self._active_cache),
            }

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
            enum_str(getattr(rule, "fallback_mode", None), "COPY_MESSAGE"),
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
        # A new task can start watching a source that already has a cached
        # routing table; drop every entry before the row exists.
        self.invalidate_active_cache()
        rule_version = int(getattr(rule, "version", 1) or 1)
        ai_fallback = enum_str(getattr(rule, "ai_fallback_policy", None), AIFallbackPolicy.DROP.value)
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
                    enum_str(getattr(rule, "content_mode", None), ContentMode.AUTO.value),
                    _rule_meta_json(rule), rule_version, rule.created_at, rule.updated_at,
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
                        enum_str(getattr(rule, "content_mode", None), ContentMode.AUTO.value),
                        _rule_meta_json(rule), rule.created_at, rule.updated_at,
                    ),
                )
            else:
                raise
        return rule

    async def update(self, rule: ForwardRule, expected_version: Optional[int] = None) -> ForwardRule:
        self.invalidate_active_cache()
        _reject_inherited_write(rule)
        rule_version = int(getattr(rule, "version", 1) or 1)
        next_version = rule_version + 1

        trigger_json = json.dumps(
            getattr(rule, "trigger_events", [TriggerEvent.NEW_MESSAGE.value])
            if isinstance(getattr(rule, "trigger_events", None), list)
            else [getattr(rule, "trigger_events", TriggerEvent.NEW_MESSAGE.value)]
        )
        content_val = enum_str(getattr(rule, "content_mode", None), ContentMode.AUTO.value)
        meta_json = _rule_meta_json(rule)

        ai_fallback = enum_str(getattr(rule, "ai_fallback_policy", None), AIFallbackPolicy.DROP.value)
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
        # ``Row`` is a dict subclass whose ``__getitem__`` is Python-level for
        # string keys (it also supports positional access, see database.py), and
        # this method reads ~60 columns per row. ``dict.get`` is *not*
        # overridden, so it takes the C path: optional columns go through it,
        # and only the handful of required ones use ``[]``, which keeps
        # ``IndexError`` for a column an older schema does not have.
        get = row.get

        # ``.get`` answers None both for a NULL column and for a column that is
        # absent, which is exactly what the ``in cols`` checks this replaced
        # meant.
        def _col(name, default=None):
            value = get(name)
            return default if value is None else value

        trigger_raw = _col("trigger_events", TriggerEvent.NEW_MESSAGE.value)
        triggers = _safe_list(trigger_raw, [TriggerEvent.NEW_MESSAGE.value])
        if isinstance(triggers, str):
            triggers = [triggers]
        try:
            cm = ContentMode(_col("content_mode", ContentMode.AUTO.value))
        except ValueError:
            cm = ContentMode.AUTO
        rule_version = int(_col("version", 1))
        try:
            fb_policy = AIFallbackPolicy(_col("ai_fallback_policy", AIFallbackPolicy.DROP.value))
        except ValueError:
            fb_policy = AIFallbackPolicy.DROP
        ai_ver = int(_col("ai_prompt_version", 0) or 0)
        ai_timeout = float(_col("ai_timeout_seconds", 15.0))
        ai_sec_raw = _col("ai_secondary_config_id")
        ai_sec = None if ai_sec_raw is None else str(ai_sec_raw)

        try:
            fallback_mode = ForwardMode(_col("fallback_mode", ForwardMode.COPY_MESSAGE.value))
        except Exception:
            fallback_mode = ForwardMode.COPY_MESSAGE

        detection_criteria = _safe_dict(_col("detection_criteria", "{}"), {})
        meta = _safe_dict(_col("metadata", "{}"), {})

        return ForwardRule(
            id=row["id"], session_id=row["session_id"],
            source_chat_id=row["source_chat_id"], source_chat_name=row["source_chat_name"],
            target_chat_id=row["target_chat_id"], target_chat_name=row["target_chat_name"],
            target_chat_ids=_safe_list(_col("target_chat_ids", "[]"), []),
            routing_type=RoutingType(row["routing_type"]),
            forward_mode=ForwardMode(row["forward_mode"]),
            is_active=bool(row["is_active"]), filter_rule_id=row["filter_rule_id"],
            ai_config_id=row["ai_config_id"], remove_links=bool(row["remove_links"]),
            custom_caption_template=row["custom_caption_template"],
            delay_seconds=float(_col("delay_seconds", 0.0) or 0.0),
            skip_history=bool(_col("skip_history", True)),
            since_ts=int(_col("since_ts", 0) or 0),
            ignore_edits=bool(_col("ignore_edits", False)),
            trigger_events=triggers,
            content_mode=cm,
            ai_fallback_policy=fb_policy,
            ai_prompt_version=ai_ver,
            ai_timeout_seconds=ai_timeout,
            ai_secondary_config_id=ai_sec,
            metadata=meta,
            # name/description live in metadata (see _rule_meta_json)
            name=str(meta.get("name") or ""),
            description=str(meta.get("description") or ""),
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

    async def get_owned(self, rule_id: str, owner_user_id: int) -> Optional[ForwardRule]:
        """Single owner-scoped fetch. Fail-closed: returns None unless the row
        exists AND belongs to owner_user_id. One indexed row instead of
        list_by_owner's full scan — used by per-task actions at scale."""
        row = self._db.query_one(
            "SELECT * FROM forward_rules WHERE id=? AND owner_user_id=?",
            (rule_id, int(owner_user_id)),
        )
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

    async def list_by_owner(
        self, owner_user_id: int, limit: Optional[int] = None, offset: int = 0
    ) -> List[ForwardRule]:
        """A tenant's own tasks, optionally one page of them.

        Unpaged by default: every existing caller keeps the behaviour it had.
        ``limit`` exists for the paths that put tasks on a screen, where reading
        a tenant's whole configuration to show fifty rows is the difference
        between a bounded read and one that grows with the tenant's history.
        """
        sql = "SELECT * FROM forward_rules WHERE owner_user_id=? ORDER BY created_at"
        params: List[Any] = [int(owner_user_id)]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [max(0, int(limit)), max(0, int(offset))]
        rows = self._db.query_all(sql, tuple(params))
        return [self._row_to_entity(r) for r in rows]

    async def count_by_owner(self, owner_user_id: int) -> int:
        """How many tasks a tenant has — without materialising them.

        A page count is not a reason to read every row: this answers "how many
        pages" with one aggregate.
        """
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM forward_rules WHERE owner_user_id=?",
            (int(owner_user_id),),
        )
        return int(row["n"] if isinstance(row, dict) else row[0])

    async def list_all(self) -> List[ForwardRule]:
        rows = self._db.query_all("SELECT * FROM forward_rules ORDER BY created_at")
        return [self._row_to_entity(r) for r in rows]

    async def list_active_by_source(
        self, source_chat_id: str, fresh: bool = False
    ) -> List[ForwardRule]:
        """Active rules that watch ``source_chat_id``.

        A task may watch several sources: its primary ``source_chat_id`` plus the
        ids in ``detection_criteria.source_chat_ids``. The extra ids are matched
        with SQLite's JSON1 so the primary branch still uses idx_rules_source,
        and ``json_valid`` guards rows whose detection_criteria is not JSON — an
        invalid document would otherwise raise inside json_each and abort the
        whole query, silently stopping forwarding for EVERY rule.

        Written as two branches over ``UNION ALL`` rather than one ``OR``, because
        the OR is what made this a **full scan**: measured at 10,000 tenants, no
        index can answer ``source_chat_id = ? OR EXISTS(json_each(...))``, so
        every inbound message read every rule in the table.

          * the first branch is the common case and is an index seek on
            ``idx_rules_source``;
          * the second is guarded by the derived ``has_extra_sources`` flag,
            which SQLite computes from ``detection_criteria`` itself, and a
            partial index covers only those rows — normally none, so the branch
            costs an index probe rather than a table scan.

        ``UNION ALL`` (not ``UNION``) with the ``source_chat_id <> ?`` guard: a
        rule whose *primary* source is the searched chat is already in the first
        branch, and would otherwise appear twice — once per branch — and forward
        the message twice.

        The result is cached for ``ACTIVE_CACHE_TTL`` seconds and dropped by
        every write in this repository, because this is the query every inbound
        message runs and a burst from one channel would otherwise resolve the
        same routing table once per message. Pass ``fresh=True`` to bypass the
        cache — the dry-run/preview paths that must reflect a change the user
        made a moment ago.

        Callers must treat the returned rules as read-only: on a cache hit they
        are the same objects the previous caller received.
        """
        cid = str(source_chat_id)
        if not fresh:
            cached = self._cache_get(cid)
            if cached is not None:
                return cached
        rows = self._db.query_all(
            "SELECT * FROM forward_rules WHERE is_active=1 AND source_chat_id = ?"
            " UNION ALL"
            " SELECT * FROM forward_rules WHERE is_active=1 AND has_extra_sources = 1"
            "   AND source_chat_id <> ?"
            "   AND json_valid(detection_criteria) AND EXISTS ("
            "        SELECT 1 FROM json_each(detection_criteria, '$.source_chat_ids')"
            "        WHERE value = ?)",
            (cid, cid, cid),
        )
        rules = [self._row_to_entity(r) for r in rows]
        self._cache_put(cid, rules)
        return list(rules)

    def _cache_get(self, cid: str):
        now = time.monotonic()
        generation = _active_generation(self._db)
        with self._active_lock:
            entry = self._active_cache.get(cid)
            if entry is None:
                self._active_misses += 1
                return None
            stored_at, stored_generation, rules = entry
            if (
                now - stored_at > self.ACTIVE_CACHE_TTL
                or stored_generation != generation
            ):
                # Expired, or a writer on another handle touched the table: a
                # routing table from before that write must not be served.
                self._active_cache.pop(cid, None)
                self._active_misses += 1
                return None
            self._active_hits += 1
            return list(rules)

    def _cache_put(self, cid: str, rules: List[ForwardRule]) -> None:
        generation = _active_generation(self._db)
        with self._active_lock:
            if len(self._active_cache) >= self.ACTIVE_CACHE_MAX:
                self._active_cache.clear()
            self._active_cache[cid] = (time.monotonic(), generation, tuple(rules))

    #: A task's own bookkeeping, all keyed by ``rule_id``. Deleting a task has to
    #: take these with it:
    #:
    #: * a leftover ``message_map`` row claims the task already delivered a
    #:   source message to a target, so re-creating the task (or restoring a
    #:   backup that reuses the id) can silently skip that message;
    #: * ``processed_messages`` keeps the same claim for the dedupe stage;
    #: * jobs, dead letters, counters and error rows grow without bound and keep
    #:   a deleted task visible in every operator view that reads them.
    DEPENDENT_TABLES = (
        "message_map",
        "processed_messages",
        "delivery_jobs",
        "dead_letter_queue",
        "rule_stats",
        "error_log",
        "content_dedup",
        "rule_counters",
        # The auto-reply queue: a job whose rule is gone has nothing left to
        # enforce (cooldowns, exclusions, prompt choice), so sending it would
        # bypass the rule the owner just removed. The FK semantics live in
        # autoreply_rules, but the generic table sweep cannot know that.
        "autoreply_jobs",
        # Send-side idempotency fence rows key on rule_id (audit finding #5):
        # a fence for a deleted task has nothing left to protect, and leaving
        # it would keep a permanent SENT record that could suppress a
        # re-created task's delivery of the same message.
        "delivery_attempts",
    )

    async def delete(self, rule_id: str) -> bool:
        """Delete the task and its own bookkeeping as one atomic unit.

        All-or-nothing on purpose: each write goes through ``execute()``, which
        commits per statement, so a crash between "the rule is gone" and "the
        mappings are gone" would leave exactly the inconsistency this exists to
        prevent — and no watchdog reconciles those tables.        A store that predates
        one of them simply has nothing to clean there.
        """
        self.invalidate_active_cache()

        def unit(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("DELETE FROM forward_rules WHERE id=?", (rule_id,))
            if cur.rowcount == 0:
                return False
            for table in self.DEPENDENT_TABLES:
                try:
                    conn.execute(f"DELETE FROM {table} WHERE rule_id=?", (rule_id,))
                except sqlite3.OperationalError:
                    # Unknown table = an older store; there is nothing to clean.
                    continue
            return True

        return bool(self._db.run_in_transaction(unit))

    async def set_task_status(
        self,
        rule_id: str,
        *,
        target_active: bool,
        expected_state: str,
        owner_user_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Atomic on/off transition, conditional on the state the caller saw.

        One conditional UPDATE, so two taps on the same screen cannot both win and
        a screen that is no longer true cannot overwrite a newer change. The
        condition is the exact state the caller was shown (`status_state()`), so
        e.g.        an untimed pause is not confused with a timed one that has already
        elapsed.
        """
        # A pause is exactly the change a stale routing table would hide.
        self.invalidate_active_cache()
        from ...domain.entities import (
            STATUS_DISABLED,
            STATUS_PAUSED,
            STATUS_PAUSED_UNTIL,
            STATUS_RUNNING,
        )

        ts = time.time() if now is None else float(now)
        # `paused_now` is the engine's own "still paused" test.
        paused_now = "(is_paused = 1 AND (paused_until = 0 OR paused_until > ?))"
        if target_active:
            conditions = {
                STATUS_DISABLED: ("is_active = 0", []),
                STATUS_PAUSED: ("is_active = 1 AND is_paused = 1 AND paused_until = 0", []),
                STATUS_PAUSED_UNTIL: ("is_active = 1 AND is_paused = 1 AND paused_until > ?", [ts]),
            }
        else:
            conditions = {STATUS_RUNNING: (f"is_active = 1 AND NOT {paused_now}", [ts])}
        if expected_state not in conditions:
            # A caller that believes a running task is off (or the reverse) is
            # working from a screen that is already wrong: refuse, never flip.
            return False
        condition, cond_params = conditions[expected_state]

        if target_active:
            sql = (
                "UPDATE forward_rules SET is_active=1, is_paused=0, paused_until=0,"
                " version=version+1, updated_at=?"
                f" WHERE id=? AND {condition}"
            )
        else:
            sql = (
                "UPDATE forward_rules SET is_active=0, version=version+1, updated_at=?"
                f" WHERE id=? AND {condition}"
            )
        params: List[Any] = [_now(), rule_id, *cond_params]
        if owner_user_id:
            sql += " AND owner_user_id=?"
            params.append(int(owner_user_id))
        cur = self._db.execute(sql, tuple(params))
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
            json.dumps(getattr(fr, "regex_block", []) or []),
            json.dumps(getattr(fr, "regex_require", []) or []),
            int(getattr(fr, "is_enabled", True)),
        )

    async def add(self, filter_rule: FilterRule) -> FilterRule:
        self._db.execute(
            "INSERT INTO filter_rules (id, name, whitelist_keywords, blacklist_keywords, regex_patterns,"
            " allowed_media_types, blocked_media_types, drop_service_messages, min_message_length,"
            " max_message_length, owner_user_id, regex_block, regex_require, is_enabled)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._cols(filter_rule),
        )
        return filter_rule

    async def update(self, filter_rule: FilterRule) -> FilterRule:
        self._db.execute(
            "UPDATE filter_rules SET name=?, whitelist_keywords=?, blacklist_keywords=?, regex_patterns=?,"
            " allowed_media_types=?, blocked_media_types=?, drop_service_messages=?, min_message_length=?,"
            " max_message_length=?, owner_user_id=?, regex_block=?, regex_require=?, is_enabled=?"
            " WHERE id=?",
            (*self._cols(filter_rule)[1:], filter_rule.id),
        )
        return filter_rule

    @staticmethod
    def _row_to_entity(row) -> FilterRule:
        keys = row.keys() if hasattr(row, "keys") else ()
        rx_blk = json.loads(row["regex_block"]) if "regex_block" in keys and row["regex_block"] else []
        rx_req = json.loads(row["regex_require"]) if "regex_require" in keys and row["regex_require"] else []
        # A row written before v30 has no is_enabled column: the filter was
        # active then, so it stays active. Never default a stored filter to off.
        is_enabled = bool(row["is_enabled"]) if "is_enabled" in keys and row["is_enabled"] is not None else True
        return FilterRule(
            id=row["id"], name=row["name"],
            whitelist_keywords=json.loads(row["whitelist_keywords"]),
            blacklist_keywords=json.loads(row["blacklist_keywords"]),
            regex_patterns=json.loads(row["regex_patterns"]),
            regex_block=rx_blk,
            regex_require=rx_req,
            allowed_media_types=json.loads(row["allowed_media_types"]),
            blocked_media_types=json.loads(row["blocked_media_types"]),
            drop_service_messages=bool(row["drop_service_messages"]),
            min_message_length=row["min_message_length"],
            max_message_length=row["max_message_length"],
            owner_user_id=int(row["owner_user_id"]) if "owner_user_id" in keys and row["owner_user_id"] else 0,
            is_enabled=is_enabled,
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
            api_key=_safe_decrypt(self._crypto, enc, kind="ai config", row_id=row["id"]),
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

    async def get_owned(self, config_id: str, owner_user_id: int) -> Optional[AIConfig]:
        """Owner-scoped read: the caller's own config, or nothing.

        Scoping in SQL (rather than reading then comparing) means a foreign row
        is never even materialised, so no caller can accidentally hold a config
        it does not own.
        """
        row = self._db.query_one(
            "SELECT * FROM ai_configs WHERE id=? AND owner_user_id=?",
            (config_id, int(owner_user_id or 0)),
        )
        return self._row_to_entity(row) if row else None

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
            " is_default, created_at, updated_at, owner_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cred.id, cred.label, cred.api_id,
                self._crypto.encrypt(cred.api_hash) if cred.api_hash else "",
                cred.proxy, int(cred.is_default), cred.created_at, cred.updated_at,
                int(getattr(cred, "owner_user_id", 0) or 0),
            ),
        )
        return cred

    async def update(self, cred: ApiCredential) -> ApiCredential:
        self._db.execute(
            "UPDATE api_credentials SET label=?, api_id=?, api_hash_encrypted=?, proxy=?,"
            " is_default=?, updated_at=?, owner_user_id=? WHERE id=?",
            (
                cred.label, cred.api_id,
                self._crypto.encrypt(cred.api_hash) if cred.api_hash else "",
                cred.proxy, int(cred.is_default), cred.updated_at,
                int(getattr(cred, "owner_user_id", 0) or 0), cred.id,
            ),
        )
        return cred

    def _row_to_entity(self, row) -> ApiCredential:
        enc = row["api_hash_encrypted"]
        return ApiCredential(
            id=row["id"], label=row["label"], api_id=row["api_id"],
            api_hash=_safe_decrypt(self._crypto, enc, kind="api credential", row_id=row["id"]),
            proxy=row["proxy"], is_default=bool(row["is_default"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            owner_user_id=int(row["owner_user_id"]) if "owner_user_id" in row.keys() else 0,
        )

    async def get_by_id(self, cred_id: str) -> Optional[ApiCredential]:
        row = self._db.query_one("SELECT * FROM api_credentials WHERE id=?", (cred_id,))
        return self._row_to_entity(row) if row else None

    async def get_default(self) -> Optional[ApiCredential]:
        row = self._db.query_one("SELECT * FROM api_credentials WHERE is_default=1 LIMIT 1")
        return self._row_to_entity(row) if row else None

    async def get_default_by_owner(self, owner_user_id: int) -> Optional[ApiCredential]:
        # v12: the caller may only receive their own default credential. Falls
        # back to the shared system scope (0) so a legacy single-tenant install
        # keeps working.
        row = self._db.query_one(
            "SELECT * FROM api_credentials WHERE is_default=1"
            " AND (owner_user_id=? OR owner_user_id=0) ORDER BY owner_user_id DESC LIMIT 1",
            (int(owner_user_id),),
        )
        return self._row_to_entity(row) if row else None

    async def list_by_owner(self, owner_user_id: int) -> List[ApiCredential]:
        rows = self._db.query_all(
            "SELECT * FROM api_credentials WHERE owner_user_id=? ORDER BY label",
            (int(owner_user_id),),
        )
        return [self._row_to_entity(r) for r in rows]

    async def list_all(self) -> List[ApiCredential]:
        rows = self._db.query_all("SELECT * FROM api_credentials ORDER BY label")
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, cred_id: str) -> bool:
        cur = self._db.execute("DELETE FROM api_credentials WHERE id=?", (cred_id,))
        return cur.rowcount > 0


def _dump_defaults(defaults: Optional[Dict[str, Any]]) -> str:
    """Serialise a bot's defaults. Values were validated by the registry first;
    this layer only has to be total."""
    try:
        return json.dumps(defaults or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def _parse_defaults(raw: str) -> Dict[str, Any]:
    """Read a stored mapping back. Anything unusable reads as "no defaults"
    rather than failing the whole token — a bot whose defaults cannot be parsed
    still forwards with the registry defaults."""
    try:
        loaded = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


class SqliteBotTokenRepository(IBotTokenRepository):
    def __init__(self, db: SqliteDatabase, crypto: CryptoService) -> None:
        self._db = db
        self._crypto = crypto

    async def add(self, token: BotToken) -> BotToken:
        self._db.execute(
            "INSERT INTO bot_tokens"
            " (id, label, token_encrypted, is_active, created_at, updated_at,"
            "  owner_user_id, defaults_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token.id, token.label,
                self._crypto.encrypt(token.token) if token.token else "",
                int(token.is_active), token.created_at, token.updated_at,
                int(token.owner_user_id or 0),
                _dump_defaults(token.defaults),
            ),
        )
        return token

    def _row_to_entity(self, row) -> BotToken:
        enc = row["token_encrypted"]
        try:
            owner = int(row["owner_user_id"] or 0)
        except (IndexError, KeyError):
            owner = 0  # a row read from a database that predates the column
        try:
            raw_defaults = row["defaults_json"] or "{}"
        except (IndexError, KeyError):
            raw_defaults = "{}"  # a row from a database that predates v20
        return BotToken(
            id=row["id"], label=row["label"],
            token=_safe_decrypt(self._crypto, enc, kind="bot token", row_id=row["id"]),
            is_active=bool(row["is_active"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            owner_user_id=owner,
            defaults=_parse_defaults(raw_defaults),
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

    # -- owner-scoped paths: every human-facing read and write goes here ---- #
    async def list_by_owner(self, owner_user_id: int) -> List[BotToken]:
        rows = self._db.query_all(
            "SELECT * FROM bot_tokens WHERE owner_user_id=? ORDER BY created_at",
            (int(owner_user_id or 0),),
        )
        return [self._row_to_entity(r) for r in rows]

    async def get_owned(self, token_id: str, owner_user_id: int) -> Optional[BotToken]:
        # The owner is part of the WHERE clause, not an if after the read:
        # a transplanted id must not even round-trip the secret.
        row = self._db.query_one(
            "SELECT * FROM bot_tokens WHERE id=? AND owner_user_id=?",
            (token_id, int(owner_user_id or 0)),
        )
        return self._row_to_entity(row) if row else None

    async def delete_owned(self, token_id: str, owner_user_id: int) -> bool:
        cur = self._db.execute(
            "DELETE FROM bot_tokens WHERE id=? AND owner_user_id=?",
            (token_id, int(owner_user_id or 0)),
        )
        return cur.rowcount > 0

    async def set_defaults(
        self, token_id: str, owner_user_id: int, defaults: Dict[str, Any]
    ) -> bool:
        """Replace this owner's bot defaults.

        The owner is in the WHERE clause for the same reason it is on every
        other human path: a transplanted token id must not let one tenant write
        settings onto another tenant's bot. A row this owner does not own
        returns False, which the caller answers as "not found".
        """
        cur = self._db.execute(
            "UPDATE bot_tokens SET defaults_json=?, updated_at=? "
            "WHERE id=? AND owner_user_id=?",
            (
                _dump_defaults(defaults),
                int(time.time()),
                token_id,
                int(owner_user_id or 0),
            ),
        )
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


class SqliteRuleCounterRepository(IRuleCounterRepository):
    """The "forward every Nth message" position, one row per task.

    ``advance`` is a single UPSERT plus a read of the row it just wrote, both
    inside the database lock and one transaction, which is what makes the
    sequence unique under concurrency: two pipeline tasks handling two messages
    of the same rule cannot both see ``accepted = 4`` and both decide they own
    slot 5. Doing the arithmetic in SQL rather than in Python is the same
    point — the increment and the reset-on-interval-change are one expression,
    so they cannot be split by another writer.
    """

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    _ADVANCE = (
        "INSERT INTO rule_counters (rule_id, owner_user_id, every_n, accepted, updated_at)"
        " VALUES (?, ?, ?, 1, ?)"
        " ON CONFLICT(rule_id) DO UPDATE SET"
        "  owner_user_id=excluded.owner_user_id,"
        # The CASE reads the *previous* row (all right-hand sides of an UPSERT
        # see the old values), so a changed interval restarts the count at 1 and
        # an unchanged one just steps forward.
        "  accepted=CASE WHEN rule_counters.every_n=excluded.every_n"
        "                THEN rule_counters.accepted + 1 ELSE 1 END,"
        "  every_n=excluded.every_n,"
        "  updated_at=excluded.updated_at"
    )

    async def advance(self, rule_id: str, every_n: int, owner_user_id: int = 0) -> int:
        if not rule_id:
            raise ValueError("rule_id is required")
        every_n = max(1, int(every_n or 1))

        def unit(conn: sqlite3.Connection) -> int:
            conn.execute(self._ADVANCE, (rule_id, int(owner_user_id or 0), every_n, _now()))
            row = conn.execute(
                "SELECT accepted FROM rule_counters WHERE rule_id=?", (rule_id,)
            ).fetchone()
            return int(row[0]) if row else 0

        return int(self._db.run_in_transaction(unit) or 0)

    async def peek(self, rule_id: str) -> Optional[dict]:
        row = self._db.query_one(
            "SELECT rule_id, owner_user_id, every_n, accepted, updated_at"
            " FROM rule_counters WHERE rule_id=?",
            (rule_id,),
        )
        if row is None:
            return None
        return {
            "rule_id": row[0],
            "owner_user_id": row[1],
            "every_n": row[2],
            "accepted": row[3],
            "updated_at": row[4],
        }

    async def reset(self, rule_id: str) -> bool:
        cur = self._db.execute("DELETE FROM rule_counters WHERE rule_id=?", (rule_id,))
        return cur.rowcount > 0


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

    async def save(self, row: dict) -> bool:
        """Upsert one flow, but only if it beats the stored revision.

        The guard is in the statement because that is the only place it can be
        atomic: `WHERE excluded.version > auth_flows.version` makes a stale
        writer (a late callback for a flow that already moved on) a no-op
        instead of a silent clobber. It also covers the INSERT branch racing an
        existing row — the same version loses there too (1 > 1 is false).
        """
        cols = [c for c in self._COLUMNS if c in row]
        placeholders = ",".join("?" for _ in cols)
        assignments = ",".join(f"{c}=excluded.{c}" for c in cols)
        values = [row[c] for c in cols]
        cur = self._db.execute(
            f"INSERT INTO auth_flows ({','.join(cols)}) VALUES ({placeholders})"
            f" ON CONFLICT(user_id) DO UPDATE SET {assignments}"
            f" WHERE excluded.version > auth_flows.version",
            tuple(values),
        )
        return bool(cur.rowcount)

    async def delete(self, user_id: int, *, version: Optional[int] = None) -> bool:
        """Delete a flow; with ``version``, only that exact revision.

        A conditional delete is what stops `reset()` — which runs off a copy it
        read earlier — from removing a flow the user started in between.
        """
        if version is None:
            cur = self._db.execute(
                "DELETE FROM auth_flows WHERE user_id=?", (int(user_id),)
            )
        else:
            cur = self._db.execute(
                "DELETE FROM auth_flows WHERE user_id=? AND version=?",
                (int(user_id), int(version)),
            )
        return bool(cur.rowcount)

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
    """Per-user language, plan, quota, status."""

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
            " quota_reset_at, created_at, updated_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, language, 0, "free", 0, now, now, now, "active"),
        )
        return {
            "user_id": uid, "language": language, "is_admin": 0, "plan": "free",
            "quota_forwarded": 0, "quota_reset_at": now, "status": "active",
        }

    async def get(self, user_id: int) -> Optional[dict]:
        row = self._db.query_one("SELECT * FROM users WHERE user_id=?", (int(user_id),))
        return dict(row) if row else None

    async def list_all(self, limit: int = 100, offset: int = 0) -> List[dict]:
        rows = self._db.query_all(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (max(1, int(limit)), max(0, int(offset)))
        )
        return [dict(r) for r in (rows or [])]

    async def count_all(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS total FROM users")
        return int(row["total"]) if row else 0

    async def set_status(self, user_id: int, status: str) -> None:
        self._db.execute(
            "UPDATE users SET status=?, updated_at=? WHERE user_id=?",
            (str(status), int(time.time()), int(user_id)),
        )

    async def set_admin(self, user_id: int, is_admin: int) -> None:
        self._db.execute(
            "UPDATE users SET is_admin=?, updated_at=? WHERE user_id=?",
            (int(is_admin), int(time.time()), int(user_id)),
        )

    async def set_plan(self, user_id: int, plan: str) -> None:
        self._db.execute(
            "UPDATE users SET plan=?, updated_at=? WHERE user_id=?",
            (str(plan), int(time.time()), int(user_id)),
        )

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

    async def add_or_update(self, mapping: MessageMapping, conn=None) -> MessageMapping:
        """Upsert a source->target mapping.

        conn: v12.3 (ACID atomicity) — when supplied, runs on that connection
        without committing so the caller can group it with the delivery job
        insert in one atomic unit. Replays are safe: the ON CONFLICT clause
        only promotes (never regresses) the delivery status.
        """
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
        # Audit finding #3: an upsert must never regress a terminal mapping. A
        # replay of an already-processed event rewrites this row; without the
        # guard it flips a SENT mapping back to PENDING while no delivery job
        # exists to drive it — the target has the message but the store says it
        # does not (and edit/delete sync then reads a lie).
        guarded = sql.replace(
            "delivery_status = excluded.delivery_status,",
            "delivery_status = CASE\n"
            "                WHEN message_map.delivery_status IN ('SENT') THEN message_map.delivery_status\n"
            "                ELSE excluded.delivery_status\n"
            "            END,",
        )
        if conn is None:
            self._db.execute(guarded, (
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
        else:
            conn.execute(guarded, (
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
            ))
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

    async def list_by_source_message_id(self, source_message_id: int) -> List[MessageMapping]:
        rows = self._db.query_all(
            "SELECT * FROM message_map WHERE source_message_id=?",
            (int(source_message_id),),
        )
        return [self._row_to_entity(r) for r in rows]

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

    # --- v2.0 app update: Duplicate Filters content-level dedup -------------
    async def was_content_forwarded(self, rule_id: str, content_hash: str) -> bool:
        """True when this rule already forwarded this exact content fingerprint."""
        row = self._db.query_one(
            "SELECT 1 FROM content_dedup WHERE rule_id=? AND content_hash=?",
            (rule_id, content_hash),
        )
        return row is not None

    async def record_content_hash(self, rule_id: str, content_hash: str) -> None:
        """Record a fingerprint. INSERT OR IGNORE keeps replay idempotent."""
        self._db.execute(
            "INSERT OR IGNORE INTO content_dedup (rule_id, content_hash, created_at)"
            " VALUES (?, ?, ?)",
            (rule_id, content_hash, _now()),
        )

    async def purge_content_hashes(self, rule_id: str, before_ts: int) -> int:
        cur = self._db.execute(
            "DELETE FROM content_dedup WHERE rule_id=? AND created_at < ?",
            (rule_id, before_ts),
        )
        return cur.rowcount

    async def claim_content_hash(self, rule_id: str, content_hash: str) -> bool:
        """Atomic claim: True when this caller owns the fingerprint.

        ``INSERT OR IGNORE`` plus ``rowcount`` lets the UNIQUE(rule_id,
        content_hash) index decide the winner instead of a read-then-write
        race, so two workers cannot both forward the same content.
        """
        cur = self._db.execute(
            "INSERT OR IGNORE INTO content_dedup (rule_id, content_hash, created_at)"
            " VALUES (?, ?, ?)",
            (rule_id, content_hash, _now()),
        )
        return int(getattr(cur, "rowcount", 0) or 0) == 1

    async def release_content_hash(self, rule_id: str, content_hash: str) -> None:
        """Give a claim back when the message was never delivered."""
        self._db.execute(
            "DELETE FROM content_dedup WHERE rule_id=? AND content_hash=?",
            (rule_id, content_hash),
        )

    async def purge_content_hashes_all(self, before_ts: int) -> int:
        cur = self._db.execute(
            "DELETE FROM content_dedup WHERE created_at < ?", (int(before_ts),)
        )
        return cur.rowcount


class SqliteDeliveryQueueRepository(IDeliveryQueueRepository):
    """SQLite implementation of the durable delivery jobs queue."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db
        # v14: enqueue() calls get_pending_count() on every message, and that
        # COUNT(*) over three statuses is the single biggest cost on the ingest
        # hot path (it is a scan of the pending set, once per enqueued job).
        # Track the count in memory: it changes only through this repository's
        # own enqueue/claim/mark_* methods, which all run under the DB lock.
        self._pending_estimate = 0
        try:
            row = self._db.query_one(
                "SELECT COUNT(*) as cnt FROM delivery_jobs WHERE status IN ('PENDING','CLAIMED','RETRY_WAIT')"
            )
            self._pending_estimate = int(row["cnt"]) if row else 0
        except Exception:
            self._pending_estimate = 0

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
            job_kind=str(row["job_kind"]) if "job_kind" in keys and row["job_kind"] else "forward",
        )

    async def enqueue(self, job: DeliveryJob, conn=None) -> bool:
        """Enqueue a delivery job.

        conn: v12.3 (ACID atomicity) — when supplied, the INSERT runs on that
        connection without committing, so the caller can group it with other
        writes (the job + its message mapping) in one atomic unit. The job
        insert stays INSERT OR IGNORE, so re-enqueueing after a replay
        remains a no-op instead of a duplicate delivery.
        """
        payload_str = SqliteDatabase.dumps(job.payload_data)
        status_str = getattr(job.status, "value", str(job.status))
        runner = self._db.execute if conn is None else conn.execute
        try:
            cur = runner(
                """
                INSERT OR IGNORE INTO delivery_jobs (
                    id, rule_id, source_chat_id, source_message_id, target_chat_id,
                    payload_json, status, attempts, max_attempts, next_retry_at,
                    lease_until, worker_id, error_detail, created_at, updated_at,
                    intermediate_chat_id, intermediate_message_id, delivery_stage,
                    job_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    getattr(job, "job_kind", "forward") or "forward",
                ),
            )
        except Exception:
            # Fallback for tables without v9 columns
            cur = runner(
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
        inserted = cur.rowcount > 0
        if inserted:
            # A new job enters the pending set. Replays (INSERT OR IGNORE)
            # return rowcount 0 and must not inflate the counter.
            self._pending_estimate += 1
        return inserted

    async def claim_batch(
        self, worker_id: str, batch_size: int = 5, lease_duration: float = 30.0
    ) -> List[DeliveryJob]:
        """Claim the next deliverable jobs, preserving source order per target.

        Two properties make a burst leave in the order it arrived:

        * the sort key is ``(source_chat_id, source_message_id, created_at, id)``.
          The source's own order is the message id, so that leads; ``created_at``
          only breaks ties inside one chat. (Ordering by ``created_at`` first was
          a defect: it is a one-second timestamp, so a burst that straddled a
          second boundary left in *ingest* order — the post the poller happened
          to read first jumped ahead of lower message ids. Sorting on
          ``created_at`` alone was worse still: it left the order of a burst to
          SQLite's row order, and the round-robin across workers then scrambled
          it.)
        * a job is only claimable when no *earlier* unsent job exists for the same
          target chat. That makes a destination a single lane with one in-flight
          job at a time, so two workers can never deliver to the same target
          concurrently — while different targets still proceed in parallel.

        An earlier job that cannot be delivered eventually dead-letters (or its
        lease expires and is reclaimed), which releases the lane; nothing is
        silently skipped and nothing overtakes.
        """
        now = time.time()
        claimed: List[DeliveryJob] = []
        with self._db._lock:
            # v14: the claim index (status, next_retry_at, lease_until, created_at)
            # matches the whole predicate and the ORDER BY, so this is an
            # ordered index walk that stops at LIMIT — no TEMP B-TREE sort
            # (the pre-v14 idx_delivery_jobs_poll sorted every claim).
            rows = self._db._conn.execute(
                """
                SELECT * FROM delivery_jobs AS j
                WHERE (
                    j.status = 'PENDING'
                    OR j.status = 'RETRY_WAIT'
                    OR (j.status = 'CLAIMED' AND j.lease_until < ?)
                )
                AND j.next_retry_at <= ?
                AND NOT EXISTS (
                    SELECT 1 FROM delivery_jobs AS o
                    WHERE o.target_chat_id = j.target_chat_id
                      AND o.id <> j.id
                      AND o.status IN ('PENDING', 'RETRY_WAIT', 'CLAIMED')
                      AND (o.source_chat_id, o.source_message_id, o.created_at, o.id)
                          < (j.source_chat_id, j.source_message_id, j.created_at, j.id)
                )
                ORDER BY j.source_chat_id ASC, j.source_message_id ASC,
                         j.created_at ASC, j.id ASC
                LIMIT ?
                """,
                (now, now, int(batch_size)),
            ).fetchall()

            if not rows:
                return []

            lease_until = now + lease_duration
            now_int = int(now)
            ids = tuple(r["id"] for r in rows)
            placeholders = ",".join("?" * len(ids))
            # v14: one UPDATE for the whole batch instead of one per row.
            # The batch is already serialized by _lock and every row was just
            # fetched from the same connection, so this is the same claim set.
            self._db._conn.execute(
                f"""
                UPDATE delivery_jobs
                SET status = 'CLAIMED', worker_id = ?, lease_until = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (worker_id, lease_until, now_int, *ids),
            )
            self._db._conn.commit()
            # v14: PENDING/RETRY_WAIT -> CLAIMED. Both are inside the pending
            # set, so the count does not change — the claim only marks who
            # owns the row. The counter moves on mark_sent/mark_failed.

            for r in rows:
                job = self._row_to_entity(r)
                job.status = DeliveryStatus.CLAIMED
                job.worker_id = worker_id
                job.lease_until = lease_until
                claimed.append(job)

        return claimed

    async def mark_sent_batch(self, job_ids: List[tuple]) -> int:
        """Mark a batch of jobs SENT in one transaction.

        Each entry of ``job_ids`` is ``(job_id, target_message_id)``. Returns
        the number of rows updated. v14: the worker previously issued one
        UPDATE + one commit per delivered job (5 per claim, and commits are
        what make SQLite slow under load — every commit is an fsync).
        """
        if not job_ids:
            return 0
        now = int(time.time())
        ids = [jid for jid, _ in job_ids]
        placeholders = ",".join("?" * len(ids))
        with self._db._lock:
            cur = self._db._conn.execute(
                f"""
                UPDATE delivery_jobs
                SET status = 'SENT', lease_until = 0, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, *ids),
            )
            self._db._conn.commit()
        updated = cur.rowcount
        self._pending_estimate = max(0, self._pending_estimate - updated)
        return updated

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
        if cur.rowcount > 0:
            self._pending_estimate = max(0, self._pending_estimate - 1)
        return cur.rowcount > 0

    async def mark_retry(self, job_id: str, error: str, backoff_seconds: float) -> bool:
        now = time.time()
        row = self._db.query_one("SELECT attempts, max_attempts FROM delivery_jobs WHERE id = ?", (job_id,))
        if not row:
            return False

        attempts = int(row["attempts"]) + 1
        max_attempts = int(row["max_attempts"])

        if attempts >= max_attempts:
            # v14: route the exhausted job to the dead-letter queue instead of
            # silently leaving it FAILED. The insert keeps the payload so an
            # operator can inspect or replay it.
            self._dead_letter_row(job_id, error, "DELIVERY_EXHAUSTED", now, attempts)
            cur = self._db.execute(
                """
                UPDATE delivery_jobs
                SET status = 'FAILED', attempts = ?, error_detail = ?, lease_until = 0, updated_at = ?
                WHERE id = ?
                """,
                (attempts, error, int(now), job_id),
            )
            if cur.rowcount > 0:
                self._pending_estimate = max(0, self._pending_estimate - 1)
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

    async def defer(self, job_id: str, reason: str, backoff_seconds: float) -> bool:
        """Postpone without consuming an attempt (see the interface docstring).

        ``attempts`` is deliberately untouched: the blockers this serves (paused
        task, rate limit, time limit, no live client, unusable bot token) are
        states of the *environment*, not failures of the job. A SENT or FAILED
        job is never reopened by a late deferral.
        """
        now = time.time()
        next_retry = now + max(0.0, float(backoff_seconds))
        cur = self._db.execute(
            """
            UPDATE delivery_jobs
            SET status = 'RETRY_WAIT', next_retry_at = ?, error_detail = ?,
                lease_until = 0, updated_at = ?
            WHERE id = ? AND status NOT IN ('SENT', 'FAILED')
            """,
            (next_retry, reason, int(now), job_id),
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
        if cur.rowcount > 0:
            self._pending_estimate = max(0, self._pending_estimate - 1)
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

    def _dead_letter_row(self, job_id: str, error: str, category: str, now: float, attempts: int = 0) -> bool:
        """Write the dead-letter entry for a job and report whether it landed.

        The ONLY producer: every path that gives up on a job calls this, so the
        entry cannot be written with one set of column names by one caller and a
        different set by another. (It used to be two copies, and the copy in
        ``mark_dead_letter`` named `error_message` — a column this schema does
        not have — inside a bare ``except: pass``, so that path lost the entry
        and said nothing. Callers that cannot fail silently must not swallow
        this: a lost entry is a job nobody can see or retry.)

        ``attempts`` is the number of attempts the delivery actually made. It is
        passed in rather than copied from the row because the row is updated
        *after* the entry is written, so copying it recorded one attempt fewer
        than the delivery had really used.
        """
        try:
            self._db.execute(
                """
                INSERT OR REPLACE INTO dead_letter_queue (
                    id, job_id, rule_id, source_chat_id, source_message_id,
                    target_chat_id, payload_json, attempts, last_error,
                    error_category, dead_lettered_at
                )
                SELECT ?, id, rule_id, source_chat_id, source_message_id,
                       target_chat_id, payload_json, ?, ?, ?, ?
                FROM delivery_jobs WHERE id = ?
                """,
                (f"dlq_{job_id}", int(attempts), error, category, int(now), job_id),
            )
            row = self._db.query_one("SELECT 1 AS ok FROM dead_letter_queue WHERE id = ?", (f"dlq_{job_id}",))
            if row is None:
                # The job row is gone (cleanup, or a replay that already
                # consumed it): the SELECT inserted nothing.
                logger.warning(
                    "dead-letter entry for job %s was not written: no such delivery_jobs row",
                    job_id,
                )
                return False
            return True
        except Exception as exc:
            logger.warning("dead-letter entry for job %s failed: %s", job_id, exc)
            return False

    async def mark_dead_letter(self, job_id: str, error: str, category: str = "DELIVERY_EXHAUSTED") -> bool:
        now = _now()
        current = self._db.query_one("SELECT attempts FROM delivery_jobs WHERE id = ?", (job_id,))
        attempts = int(current["attempts"]) if current is not None else 0
        recorded = self._dead_letter_row(job_id, error, category, now, attempts)
        cur = self._db.execute(
            """
            UPDATE delivery_jobs
            SET status = 'DEAD_LETTER', error_detail = ?, lease_until = 0, updated_at = ?
            WHERE id = ?
            """,
            (error, now, job_id),
        )
        if cur.rowcount > 0:
            self._pending_estimate = max(0, self._pending_estimate - 1)
        return recorded

    # ------------------------------------------------------------------ #
    # Dead-letter queue (owner-scoped read / replay / purge)
    # ------------------------------------------------------------------ #

    def _dlq_scope_clause(self, rule_ids: Optional[Sequence[str]]) -> tuple:
        """``(sql_fragment, params)`` for a tenant scope.

        ``None`` is unscoped for an internal caller that already resolved its
        authority. An empty scope is NOT "no filter": it matches nothing, so a
        tenant that owns no rules sees no entries.
        """
        if rule_ids is None:
            return "", ()
        ids = [str(r) for r in rule_ids if r]
        if not ids:
            return " AND 1=0", ()
        return f" AND rule_id IN ({','.join('?' for _ in ids)})", tuple(ids)

    async def list_dead_letters(
        self,
        rule_ids: Optional[Sequence[str]] = None,
        limit: int = 20,
        rule_id: str = "",
        since: int = 0,
    ) -> List[Dict[str, Any]]:
        scope_sql, scope_params = self._dlq_scope_clause(rule_ids)
        params: tuple = scope_params
        # Narrow within the scope, in SQL. A LIMIT that truncates the page first
        # would turn "older than this page" into "does not exist".
        if rule_id:
            scope_sql += " AND rule_id=?"
            params = params + (str(rule_id),)
        if since:
            scope_sql += " AND dead_lettered_at>=?"
            params = params + (int(since),)
        try:
            rows = self._db.query_all(
                "SELECT * FROM dead_letter_queue WHERE 1=1" + scope_sql
                + " ORDER BY dead_lettered_at DESC LIMIT ?",
                params + (max(int(limit), 1),),
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("dead-letter list failed: %s", exc)
            return []

    async def count_dead_letters(self, rule_ids: Optional[Sequence[str]] = None) -> int:
        scope_sql, scope_params = self._dlq_scope_clause(rule_ids)
        try:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM dead_letter_queue WHERE 1=1" + scope_sql,
                scope_params,
            )
            return int((dict(row) if row else {}).get("n", 0) or 0)
        except Exception as exc:
            logger.warning("dead-letter count failed: %s", exc)
            return 0

    async def replay_dead_letter(
        self, entry_id: str, rule_ids: Optional[Sequence[str]] = None
    ) -> str:
        scope_sql, scope_params = self._dlq_scope_clause(rule_ids)
        rows = self._db.query_all(
            "SELECT * FROM dead_letter_queue WHERE id=?" + scope_sql,
            (str(entry_id),) + scope_params,
        )
        if not rows:
            # Unknown id and another tenant's id answer identically, on
            # purpose: an id must not be usable to probe for existence.
            return "not_found"
        entry = dict(rows[0])

        rule_id = str(entry.get("rule_id") or "")
        src_chat = str(entry.get("source_chat_id") or "")
        src_msg = int(entry.get("source_message_id") or 0)
        tgt_chat = str(entry.get("target_chat_id") or "")
        job_id = str(entry.get("job_id") or "")
        payload = entry.get("payload_json") or "{}"
        if isinstance(payload, dict):
            payload = SqliteDatabase.dumps(payload)

        # `delivery_jobs` is UNIQUE(rule_id, source_chat_id, source_message_id,
        # target_chat_id): one delivery per source message per target is a
        # deliberate dedup, not an accident. So a replay can never be a second
        # row — it either wakes the existing one up or is refused. That
        # constraint is also why a replay cannot double-post.
        original = None
        if job_id:
            original = self._db.query_one("SELECT * FROM delivery_jobs WHERE id=?", (job_id,))
        if original is None:
            original = self._db.query_one(
                "SELECT * FROM delivery_jobs WHERE rule_id=? AND source_chat_id=?"
                " AND source_message_id=? AND target_chat_id=?",
                (rule_id, src_chat, src_msg, tgt_chat),
            )

        now = _now()
        if original is not None:
            status = str(dict(original).get("status") or "").upper()
            if status in ("PENDING", "CLAIMED", "RETRY_WAIT", "PROCESSING"):
                # Already on its way: replaying would deliver twice.
                return "already_pending"
            if status == "SENT":
                # The store says this message reached the target. Re-queueing it
                # would repost something the channel already has.
                return "already_sent"
            row = dict(original)
            was_attempts = int(row.get("attempts") or 0)
            max_attempts = int(row.get("max_attempts") or 0) or DeliveryJob().max_attempts

            def _unit(conn) -> bool:
                cur = conn.execute(
                    """
                    UPDATE delivery_jobs
                    SET status = 'PENDING', attempts = 0, max_attempts = ?,
                        next_retry_at = 0, lease_until = 0, worker_id = '',
                        error_detail = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (max(1, max_attempts), now, row.get("id")),
                )
                if cur.rowcount <= 0:
                    return False
                conn.execute("DELETE FROM dead_letter_queue WHERE id=?", (str(entry_id),))
                return True

            try:
                reset = self._db.run_in_transaction(_unit)
            except Exception as exc:
                logger.warning("replay of dead letter %s failed: %s", entry_id, exc)
                return "replay_failed"
            if not reset:
                return "replay_failed"
        else:
            # The terminal job row has already been purged (cleanup keeps a
            # window, not forever). Rebuild it from the entry, which carries the
            # ids and the payload, and give it the entry's own attempt count as
            # its ceiling rather than inventing a budget.
            max_attempts = max(1, int(entry.get("attempts") or 0) or DeliveryJob().max_attempts)
            job = DeliveryJob(
                id=job_id or f"replay_{entry_id}",
                rule_id=rule_id,
                source_chat_id=src_chat,
                source_message_id=src_msg,
                target_chat_id=tgt_chat,
                payload_data=SqliteDatabase.loads(payload, {}),
                status=DeliveryStatus.PENDING,
                attempts=0,
                max_attempts=max_attempts,
                next_retry_at=0.0,
                lease_until=0.0,
                created_at=now,
                updated_at=now,
            )
            was_attempts = int(entry.get("attempts") or 0)

            def _unit(conn) -> bool:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO delivery_jobs (
                        id, rule_id, source_chat_id, source_message_id, target_chat_id,
                        payload_json, status, attempts, max_attempts, next_retry_at,
                        lease_until, worker_id, error_detail, created_at, updated_at,
                        intermediate_chat_id, intermediate_message_id, delivery_stage
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, 0, 0, '', '', ?, ?, NULL, NULL, 'DIRECT')
                    """,
                    (
                        job.id, job.rule_id, str(job.source_chat_id), int(job.source_message_id),
                        str(job.target_chat_id), SqliteDatabase.dumps(job.payload_data),
                        int(job.max_attempts), int(job.created_at), int(job.updated_at),
                    ),
                )
                if cur.rowcount <= 0:
                    # The unique key answered after all: a row for this
                    # delivery already exists (someone replayed it first).
                    return False
                conn.execute("DELETE FROM dead_letter_queue WHERE id=?", (str(entry_id),))
                return True

            try:
                inserted = self._db.run_in_transaction(_unit)
            except Exception as exc:
                logger.warning("replay of dead letter %s failed: %s", entry_id, exc)
                return "replay_failed"
            if not inserted:
                return "already_pending"

        # The replayed delivery rejoins the pending set; the counter must follow
        # it, or backpressure reads low until the next restart recounts.
        self._pending_estimate += 1
        # Ids only — never the payload, which is message content.
        logger.info(
            "dead letter %s replayed (rule=%s target=%s attempts_used=%s)",
            entry_id, rule_id, tgt_chat, was_attempts,
        )
        return "requeued"

    async def purge_dead_letters(
        self,
        rule_ids: Optional[Sequence[str]] = None,
        rule_id: str = "",
        older_than: int = 0,
    ) -> int:
        scope_sql, params = self._dlq_scope_clause(rule_ids)
        sql = "DELETE FROM dead_letter_queue WHERE 1=1" + scope_sql
        if rule_id:
            sql += " AND rule_id=?"
            params = params + (str(rule_id),)
        if older_than:
            sql += " AND dead_lettered_at<?"
            params = params + (int(older_than),)
        try:
            cur = self._db.execute(sql, params)
            return int(cur.rowcount or 0)
        except Exception:
            return 0

    async def get_dead_letter_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self.list_dead_letters(None, limit)

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
        if cur.rowcount > 0:
            self._pending_estimate = max(0, self._pending_estimate - 1)
        return cur.rowcount > 0

    async def get_pending_count(self) -> int:
        # v14: serve the in-memory counter. enqueue() calls this on every
        # ingested message; the exact COUNT(*) it replaced scanned the whole
        # pending set per call. The counter changes only under _lock inside
        # this repository's own mutators, so it stays exact for the common
        # single-repository case and is reconciled by _refresh_pending_count()
        # on the periodic retention cleanup.
        return self._pending_estimate

    def _refresh_pending_count(self) -> None:
        try:
            row = self._db.query_one(
                "SELECT COUNT(*) as cnt FROM delivery_jobs WHERE status IN ('PENDING','CLAIMED','RETRY_WAIT')"
            )
            self._pending_estimate = int(row["cnt"]) if row else 0
        except Exception:
            pass

    async def get_stats(self) -> dict:
        rows = self._db.query_all("SELECT status, COUNT(*) as cnt FROM delivery_jobs GROUP BY status")
        res = {s.value: 0 for s in DeliveryStatus}
        for r in rows:
            res[r["status"]] = int(r["cnt"])
        return res

    async def counts_for_rule(self, rule_id: str) -> dict:
        """Per-status delivery counts for one rule (the status screen)."""
        if not rule_id:
            return {}
        rows = self._db.query_all(
            "SELECT status, COUNT(*) as cnt FROM delivery_jobs WHERE rule_id=? GROUP BY status",
            (str(rule_id),),
        )
        return {str(r["status"]): int(r["cnt"]) for r in rows or ()}

    async def record_album_progress(self, job_id: str, sent_map: Dict[str, int]) -> None:
        """Merge a split album's per-item confirmations (audit finding E2E-5).

        Additive only: entries the caller did not confirm are untouched, and a
        terminal (SENT) row is never rewritten — resurrecting a payload on a
        delivered job would be dead state.
        """
        if not sent_map:
            return
        row = self._db.query_one("SELECT payload_json FROM delivery_jobs WHERE id = ?", (str(job_id),))
        if row is None:
            return
        payload = row["payload_json"] if isinstance(row, dict) else row[0]
        data = _payload_dict(payload)
        existing = dict(data.get("album_sent") or {})
        existing.update({str(k): int(v) for k, v in sent_map.items()})
        data["album_sent"] = existing
        self._db.execute(
            "UPDATE delivery_jobs SET payload_json = ? WHERE id = ? AND status NOT IN ('SENT')",
            (_dumps(data), str(job_id)),
        )

    async def fold_edit(
        self,
        rule_id: str,
        source_chat_id: str,
        source_message_id: int,
        target_chat_id: str,
        text: str,
        caption: str,
        is_edit: bool,
        edit_date: int,
    ) -> bool:
        """Splice an edit's text into a delivery job that has not run yet (E2E-3).

        Only the job's own content fields change — ``text``/``caption`` plus a
        marker that the first post is an edit. Identity fields (rule, source,
        target, job_kind) are untouched so the idempotency fence, the mapping
        and any retry still key on the original event. A job no longer waiting
        to run is never touched: SENT is the mapping's business and
        dead-lettered stays dead-lettered.
        """
        rows = self._db.query_all(
            "SELECT id, payload_json FROM delivery_jobs "
            "WHERE rule_id = ? AND source_chat_id = ? AND source_message_id = ? "
            "AND target_chat_id = ? AND status IN ('PENDING', 'RETRY_WAIT', 'CLAIMED')",
            (str(rule_id), str(source_chat_id), int(source_message_id), str(target_chat_id)),
        )
        if not rows:
            return False
        updated = False
        for row in rows:
            data = _payload_dict(row["payload_json"])
            data["text"] = text
            data["caption"] = caption
            data["effective_text"] = text
            data["is_edit_folded"] = True
            data["edit_date"] = int(edit_date or 0)
            self._db.execute(
                "UPDATE delivery_jobs SET payload_json = ?, updated_at = ? WHERE id = ? "
                "AND status IN ('PENDING', 'RETRY_WAIT', 'CLAIMED')",
                (_dumps(data), int(time.time()), row["id"]),
            )
            updated = True
        return updated

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
        # CLAIMED is part of the pending set and RETRY_WAIT is too, so the
        # count is unchanged — but the rows did move, so this is also the
        # natural place to defend against a drifted counter.
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
        # v14: idx_delivery_jobs_cleanup (status, updated_at) makes this an
        # index scan instead of a full table scan on a table that only grows.
        threshold = int(time.time() - retention_seconds)
        cur = self._db.execute(
            """
            DELETE FROM delivery_jobs
            WHERE status IN ('SENT', 'FAILED') AND updated_at < ?
            """,
            (threshold,),
        )
        # Reconcile the in-memory counter: a crash, a concurrent writer or a
        # direct DB edit can all drift it. The pending set is what the
        # backpressure guard reads, so a stale-high value would silently
        # refuse every enqueue after a restart.
        self._refresh_pending_count()
        return cur.rowcount


class SqliteDeliveryAttemptRepository(IDeliveryAttemptRepository):
    """SQLite implementation of the send-side idempotency fence.

    The claim must be atomic under concurrent workers, which SQLite's shared
    connection plus ``_lock`` gives us; the logical atomicity comes from
    ``INSERT ... ON CONFLICT``: a conflicting row is either a live claim by
    somebody else (in flight), a recorded SENT (never re-dispatch), or an
    exhausted/unknown fence. There is no read-then-write window anywhere.
    """

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def claim_or_check(self, key: DeliveryAttemptKey, worker_id: str,
                             lease_until: float) -> Tuple[str, Optional[dict]]:
        """Atomically check-and-claim one logical send.

        Outcomes, and the rule each enforces:

        * ``dispatch`` — the caller owns this send and may dispatch it. Only a
          row that does not exist yet, or one whose last outcome was FAILED
          (a replay), can be claimed. **CLAIMED/SENDING rows are never taken
          from their holder**, live or expired.
        * ``in_flight`` — another worker holds a live claim; must not dispatch.
        * ``already_sent`` — a previous attempt recorded SENT; never dispatch.
        * ``unknown`` / ``stale`` — an earlier send's outcome was never
          recorded (the crash window, or an expired claim nobody resolved).
          The caller must NOT dispatch; it reconciles and then reopens the
          fence through ``resolve_unknown``. This is the difference between
          at-least-once and at-most-once-by-accident.
        """
        now = int(time.time())
        with self._db._lock:
            # The WHERE clause is the whole contract: a CLAIMED/SENDING row is
            # never stolen, and an UNKNOWN row (a send whose outcome was lost)
            # is never silently reopened. Only an absent row or a FAILED one
            # (the delivery is being replayed, e.g. from the dead-letter queue)
            # can be claimed.
            self._db._conn.execute(
                """
                INSERT INTO delivery_attempts (
                    id, rule_id, source_chat_id, source_message_id, target_chat_id,
                    job_kind, status, worker_id, lease_until, attempts,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'CLAIMED', ?, ?, 0, '', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = 'CLAIMED',
                    worker_id = excluded.worker_id,
                    lease_until = excluded.lease_until,
                    attempts = delivery_attempts.attempts + 1,
                    last_error = '',
                    updated_at = excluded.updated_at
                WHERE delivery_attempts.status = 'FAILED'
                """,
                (
                    key.id, key.rule_id, str(key.source_chat_id),
                    int(key.source_message_id), str(key.target_chat_id),
                    str(key.job_kind or "forward"), worker_id, float(lease_until),
                    now, now,
                ),
            )
            self._db._conn.commit()
            row = self._db._conn.execute(
                "SELECT status, worker_id, lease_until, target_message_id, last_error, attempts "
                "FROM delivery_attempts WHERE id = ?",
                (key.id,),
            ).fetchone()
        if row is None:
            return ("dispatch", None)
        status = str(row["status"])
        holder = str(row["worker_id"] or "")
        lease = float(row["lease_until"] or 0)
        # SENT is terminal: a recorded success is never re-dispatched.
        if status == "SENT":
            return ("already_sent", dict(row))
        # UNKNOWN: an earlier send may be sitting in the target right now.
        if status == "UNKNOWN":
            return ("unknown", dict(row))
        # FAILED is retryable: the UPSERT above re-claimed it for this caller.
        if status == "FAILED":
            return ("dispatch", dict(row))
        # CLAIMED/SENDING: only the holder dispatches, and only while its lease
        # is live.
        if holder == worker_id:
            return ("dispatch", dict(row))
        if lease > 0 and lease >= now:
            return ("in_flight", dict(row))
        # An expired claim held by somebody else: the previous send may have
        # been accepted by Telegram. The watchdog will move it to UNKNOWN; the
        # caller must not dispatch it in the meantime.
        return ("stale", dict(row))

    async def record_sent(self, key: DeliveryAttemptKey, worker_id: str,
                          target_message_id: int) -> bool:
        now = int(time.time())
        cur = self._db.execute(
            """
            UPDATE delivery_attempts
            SET status = 'SENT', target_message_id = ?, lease_until = 0,
                last_error = '', updated_at = ?
            WHERE id = ? AND status != 'SENT'
            """,
            (int(target_message_id), now, key.id),
        )
        return cur.rowcount > 0

    async def record_outcome(self, key: DeliveryAttemptKey, worker_id: str,
                             status: str, error: str = "") -> bool:
        if status not in ("FAILED", "UNKNOWN"):
            raise ValueError(f"delivery_attempts: cannot record status {status!r}")
        now = int(time.time())
        cur = self._db.execute(
            """
            UPDATE delivery_attempts
            SET status = ?, last_error = ?, lease_until = 0, updated_at = ?
            WHERE id = ? AND status NOT IN ('SENT')
            """,
            (status, str(error)[:4096], now, key.id),
        )
        return cur.rowcount > 0

    async def expire_stale_claims(self, now: float) -> int:
        """A CLAIMED fence whose lease passed becomes UNKNOWN, never 'free'.

        The previous send may have been accepted by Telegram and simply never
        recorded (the crash window). Marking the row UNKNOWN — instead of
        silently reopening it for dispatch — is what keeps the fence honest:
        an operator sees the ambiguity instead of the target receiving a
        second copy.
        """
        cur = self._db.execute(
            """
            UPDATE delivery_attempts
            SET status = 'UNKNOWN',
                last_error = COALESCE(NULLIF(last_error, ''), 'claim expired before outcome was recorded'),
                lease_until = 0, updated_at = ?
            WHERE status IN ('CLAIMED', 'SENDING') AND lease_until > 0 AND lease_until < ?
            """,
            (int(now), float(now)),
        )
        return cur.rowcount

    async def resolve_unknown(self, key: DeliveryAttemptKey, worker_id: str,
                              resolution_reason: str) -> bool:
        """Reopen an UNKNOWN fence with a recorded reason.

        This is the *only* sanctioned transition out of UNKNOWN. The caller
        must have verified (a Telegram-side read, or a bounded recovery
        decision after the retry policy elapsed) that no earlier copy is in
        the target. The reason is kept so an operator can audit why a
        possibly-already-sent delivery was re-dispatched; a bare reopen would
        make this decision invisible.
        """
        now = int(time.time())
        cur = self._db.execute(
            """
            UPDATE delivery_attempts
            SET status = 'FAILED', lease_until = 0,
                last_error = COALESCE(?, last_error), updated_at = ?
            WHERE id = ? AND status = 'UNKNOWN'
            """,
            (f"REOPENED: {str(resolution_reason)[:380]}"[:4096], now, key.id),
        )
        return cur.rowcount > 0


class SqliteIngestionGapRepository(IIngestionGapRepository):
    """SQLite implementation of the ingestion-gap recovery store.

    Rows are created when the ingest path (poller or catch-up) asked for a
    range of source ids and Telegram did not return part of it, and deleted
    when the pipeline actually *sees* that id — the only proof the message
    exists and was not lost.
    """

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def record_gaps(self, session_id: str, source_chat_id: str,
                          message_ids: Sequence[int]) -> int:
        """Record missing ids as recovery work; return how many were **new**.

        ``INSERT OR IGNORE`` means an id already known (or already terminal)
        is not rewritten — without returning the genuinely-new count the
        poller cannot tell "I just found a gap" from "I keep noticing the
        same one", and logs the same truncation warning every sweep.
        """
        if not message_ids:
            return 0
        now = int(time.time())
        rows = [
            (f"gap:{session_id}:{source_chat_id}:{int(mid)}", str(session_id),
             str(source_chat_id), int(mid), now, now)
            for mid in message_ids
        ]
        with self._db._lock:
            cur = self._db._conn.cursor()
            cur.executemany(
                """
                INSERT OR IGNORE INTO ingestion_gaps (
                    id, session_id, source_chat_id, source_message_id,
                    state, attempts, next_retry_at, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', 0, 0, '', ?, ?)
                """,
                rows,
            )
            inserted = int(cur.rowcount or 0)
            self._db._conn.commit()
        return inserted

    async def resolve_gap(self, session_id: str, source_chat_id: str,
                          message_id: int) -> bool:
        cur = self._db.execute(
            """
            DELETE FROM ingestion_gaps
            WHERE session_id = ? AND source_chat_id = ? AND source_message_id = ?
              AND state IN ('PENDING', 'RETRYING')
            """,
            (str(session_id), str(source_chat_id), int(message_id)),
        )
        return cur.rowcount > 0

    async def resolve_gap_for_source(self, source_chat_id: str, message_id: int) -> bool:
        """Resolve every non-terminal gap row for one source message.

        The pipeline does not know which session carried the post (live updates
        and poller replays share one handler), so the resolution deletes by
        source chat + id; the UNIQUE(session, chat, id) key means one row per
        watcher anyway. FAILED rows (retry budget spent) are deliberately kept:
        resolving them would erase the actionable record of an unrecoverable id.
        """
        cur = self._db.execute(
            """
            DELETE FROM ingestion_gaps
            WHERE source_chat_id = ? AND source_message_id = ?
              AND state IN ('PENDING', 'RETRYING')
            """,
            (str(source_chat_id), int(message_id)),
        )
        return cur.rowcount > 0

    async def pending_gaps(self, session_id: str, source_chat_id: str,
                           limit: int = 100) -> List[dict]:
        rows = self._db.query_all(
            """
            SELECT source_message_id, state, attempts, last_error, created_at
            FROM ingestion_gaps
            WHERE session_id = ? AND source_chat_id = ?
            ORDER BY source_message_id ASC LIMIT ?
            """,
            (str(session_id), str(source_chat_id), max(int(limit), 1)),
        )
        return [dict(r) for r in rows or []]

    async def open_gaps(self, session_id: str, source_chat_id: str,
                        below: int = 0, limit: int = 100) -> List[dict]:
        """Still-open recovery work for one channel.

        ``RESOLVED`` and ``FAILED`` are terminal — the post was delivered, or
        the budget was spent on a post that does not exist — so they no
        longer count as outstanding and must not hold the poller's cursor.
        """
        sql = (
            "SELECT source_message_id, state, attempts, last_error, created_at "
            "FROM ingestion_gaps "
            "WHERE session_id = ? AND source_chat_id = ? AND state IN ('PENDING','RETRYING')"
        )
        params: tuple = (str(session_id), str(source_chat_id))
        if below:
            sql += " AND source_message_id <= ?"
            params = params + (int(below),)
        sql += " ORDER BY source_message_id ASC LIMIT ?"
        params = params + (max(int(limit), 1),)
        rows = self._db.query_all(sql, params)
        return [dict(r) for r in rows or []]

    async def due_gaps(self, now: float, limit: int = 100) -> List[dict]:
        rows = self._db.query_all(
            """
            SELECT id, session_id, source_chat_id, source_message_id,
                   state, attempts, next_retry_at, last_error
            FROM ingestion_gaps
            WHERE state IN ('PENDING', 'RETRYING') AND next_retry_at <= ?
            ORDER BY next_retry_at ASC, source_message_id ASC LIMIT ?
            """,
            (float(now), max(int(limit), 1)),
        )
        return [dict(r) for r in rows or []]

    async def mark_gap_attempt(self, gap_id: str, error: str,
                               next_retry_at: float, exhausted: bool = False) -> bool:
        now = int(time.time())
        state = "FAILED" if exhausted else "RETRYING"
        cur = self._db.execute(
            """
            UPDATE ingestion_gaps
            SET state = ?, attempts = attempts + 1, last_error = ?,
                next_retry_at = ?, updated_at = ?
            WHERE id = ? AND state IN ('PENDING', 'RETRYING')
            """,
            (state, str(error)[:4096], float(next_retry_at), now, str(gap_id)),
        )
        return cur.rowcount > 0


class SqliteDiagnosticEventRepository(IDiagnosticEventRepository):
    """Append-only diagnostic history (schema v32), owner-scoped on read.

    Writes never block the delivery path for long: one bounded
    ``executemany`` and no index rebuild. Reads are the facet-filtered
    monitoring queries and the per-message drill-down.

    Owner scoping is enforced in this layer rather than the caller: every
    read method adds the ``owner_id`` predicate, so a caller that forgets to
    scope still cannot see another tenant's events.
    """

    _COLUMNS = (
        "id", "ts", "event_name", "severity", "trace_id", "correlation_id",
        "task_id", "owner_id", "job_id", "delivery_attempt_id", "worker_id",
        "source_peer_id", "source_message_id", "target_peer_id",
        "destination_message_id", "album_id", "stage", "strategy", "attempt",
        "error_origin", "category", "reason", "retryable", "fallback_eligible",
        "raw_exception_class", "raw_rpc_code", "raw_rpc_message", "raw_rpc_id",
        "raw_library", "raw_method", "raw_retry_after", "parser_name",
        "parser_version", "media_type", "entity_count", "text_length",
        "explanation",
    )

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    # -- write ------------------------------------------------------------ #

    def _row_from_event(self, event: Any) -> tuple:
        to_bool = lambda v: 1 if v else 0
        opt = lambda v: int(v) if v is not None else None
        return (
            str(getattr(event, "event_id", "") or uuid.uuid4().hex),
            float(getattr(event, "timestamp", 0) or time.time()),
            str(getattr(event, "event_name", "") or "")[:120],
            str(getattr(event, "severity", "ERROR") or "ERROR")[:16],
            str(getattr(event, "trace_id", "") or "")[:128],
            str(getattr(event, "correlation_id", "") or "")[:128],
            str(getattr(event, "task_id", "") or "")[:128],
            int(getattr(event, "owner_id", 0) or 0),
            str(getattr(event, "job_id", "") or "")[:128],
            str(getattr(event, "delivery_attempt_id", "") or "")[:128],
            str(getattr(event, "worker_id", "") or "")[:64],
            str(getattr(event, "source_peer_id", "") or "")[:64],
            int(getattr(event, "source_message_id", 0) or 0),
            str(getattr(event, "target_peer_id", "") or "")[:64],
            int(getattr(event, "destination_message_id", 0) or 0),
            str(getattr(event, "album_id", "") or "")[:64],
            str(getattr(event, "stage", "") or "")[:24],
            str(getattr(event, "strategy", "") or "")[:24],
            int(getattr(event, "attempt", 0) or 0),
            str(getattr(event, "error_origin", "UNKNOWN") or "UNKNOWN")[:16],
            str(getattr(event, "category", "UNKNOWN_INTERNAL_ERROR") or "")[:48],
            str(getattr(event, "reason", "") or "")[:96],
            to_bool(getattr(event, "retryable", False)),
            to_bool(getattr(event, "fallback_eligible", False)),
            str(getattr(event, "raw_exception_class", "") or "")[:96],
            opt(getattr(event, "raw_rpc_code", None)),
            str(getattr(event, "raw_rpc_message", "") or "")[:1000],
            str(getattr(event, "raw_rpc_id", "") or "")[:96],
            str(getattr(event, "raw_library", "") or "")[:64],
            str(getattr(event, "raw_method", "") or "")[:96],
            opt(getattr(event, "raw_retry_after", None)),
            str(getattr(event, "parser_name", "") or "")[:64],
            str(getattr(event, "parser_version", "") or "")[:48],
            str(getattr(event, "media_type", "") or "")[:32],
            opt(getattr(event, "entity_count", None)),
            opt(getattr(event, "text_length", None)),
            str(getattr(event, "explanation", "") or "")[:1000],
        )

    async def record_event(self, event: Any) -> bool:
        try:
            row = self._row_from_event(event)
            self._db.execute(
                f"INSERT OR REPLACE INTO diagnostic_events ({','.join(self._COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in self._COLUMNS)})",
                row,
            )
            return True
        except Exception as exc:
            logger.warning("diagnostic_events write failed: %s", exc)
            return False

    async def record_events(self, events) -> int:
        rows = []
        for event in events or ():
            try:
                rows.append(self._row_from_event(event))
            except Exception:
                continue
        if not rows:
            return 0
        try:
            with self._db._lock:
                self._db._conn.executemany(
                    f"INSERT OR REPLACE INTO diagnostic_events ({','.join(self._COLUMNS)}) "
                    f"VALUES ({','.join('?' for _ in self._COLUMNS)})",
                    rows,
                )
                self._db._conn.commit()
            return len(rows)
        except Exception as exc:
            logger.warning("diagnostic_events batch write failed: %s", exc)
            return 0

    # -- read ------------------------------------------------------------- #

    def _row_to_dict(self, row) -> dict:
        if row is None:
            return {}
        return dict(row) if isinstance(row, dict) else dict(zip(self._COLUMNS, row))

    def _scope(self, owner_id: Optional[int]) -> str:
        # Owner scoping is structural here, not the caller's job.
        return " AND owner_id = ?" if owner_id is not None else ""

    async def query_events(self, *, owner_id: Optional[int] = None,
                           since_ts: float = 0, until_ts: float = 0,
                           category: str = "", stage: str = "", severity: str = "",
                           task_id: str = "", source_peer_id: str = "",
                           source_message_id: Optional[int] = None,
                           target_peer_id: str = "", trace_id: str = "",
                           reason: str = "", limit: int = 100,
                           offset: int = 0) -> List[dict]:
        where = ["1=1"]
        params: list = []
        if owner_id is not None:
            where.append("owner_id = ?"); params.append(int(owner_id))
        if since_ts:
            where.append("ts >= ?"); params.append(float(since_ts))
        if until_ts:
            where.append("ts <= ?"); params.append(float(until_ts))
        for col, val in (("category", category), ("stage", stage), ("severity", severity),
                         ("task_id", task_id), ("source_peer_id", source_peer_id),
                         ("target_peer_id", target_peer_id), ("trace_id", trace_id),
                         ("reason", reason)):
            if val:
                where.append(f"{col} = ?"); params.append(str(val))
        if source_message_id is not None:
            where.append("source_message_id = ?"); params.append(int(source_message_id))
        sql = ("SELECT * FROM diagnostic_events WHERE " + " AND ".join(where)
               + " ORDER BY ts DESC LIMIT ? OFFSET ?")
        params += [max(1, min(int(limit), 500)), max(0, int(offset))]
        try:
            return [self._row_to_dict(r) for r in self._db.query_all(sql, tuple(params)) or ()]
        except Exception as exc:
            logger.warning("diagnostic_events query failed: %s", exc)
            return []

    async def message_timeline(self, source_peer_id: str, source_message_id: int,
                               owner_id: Optional[int] = None) -> List[dict]:
        scope = self._scope(owner_id)
        params = [str(source_peer_id), int(source_message_id)]
        sql = ("SELECT * FROM diagnostic_events WHERE source_peer_id = ? "
               "AND source_message_id = ?" + scope + " ORDER BY ts ASC")
        if owner_id is not None:
            params.insert(2, int(owner_id))
        try:
            return [self._row_to_dict(r) for r in self._db.query_all(sql, tuple(params)) or ()]
        except Exception as exc:
            logger.warning("diagnostic_events timeline failed: %s", exc)
            return []

    async def facet_counts(self, *, owner_id: Optional[int] = None,
                           since_ts: float = 0, until_ts: float = 0,
                           facet: str = "category", limit: int = 50) -> List[dict]:
        allowed = {"category", "stage", "severity", "reason", "task_id",
                   "error_origin", "strategy", "raw_rpc_id"}
        if facet not in allowed:
            facet = "category"
        where = ["1=1"]
        params: list = []
        if owner_id is not None:
            where.append("owner_id = ?"); params.append(int(owner_id))
        if since_ts:
            where.append("ts >= ?"); params.append(float(since_ts))
        if until_ts:
            where.append("ts <= ?"); params.append(float(until_ts))
        sql = (f"SELECT {facet} AS facet, COUNT(*) AS n FROM diagnostic_events "
               "WHERE " + " AND ".join(where) + f" GROUP BY {facet} ORDER BY n DESC LIMIT ?")
        params.append(max(1, min(int(limit), 200)))
        try:
            return [dict(r) for r in self._db.query_all(sql, tuple(params)) or ()]
        except Exception as exc:
            logger.warning("diagnostic_events facet query failed: %s", exc)
            return []

    async def retention_delete_before(self, before_ts: float, limit: int = 5000) -> int:
        """Bounded deletion: one sweep removes at most ``limit`` old rows.

        Bounded on purpose — an unbounded DELETE on a large table holds the
        SQLite write lock for a long time, which blocks the delivery path's
        own writes.
        """
        try:
            cur = self._db.execute(
                "DELETE FROM diagnostic_events WHERE ts < ? AND id IN "
                "(SELECT id FROM diagnostic_events WHERE ts < ? LIMIT ?)",
                (float(before_ts), float(before_ts), max(1, int(limit))),
            )
            return int(cur.rowcount or 0)
        except Exception as exc:
            logger.warning("diagnostic_events retention failed: %s", exc)
            return 0


class SqlitePromptRepository(IPromptRepository):
    """Production-grade SQLite repository for prompt templates and historic versions."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def add_template(self, template: PromptTemplate) -> PromptTemplate:
        self._db.execute(
            """
            INSERT INTO prompt_templates (id, name, description, system_prompt, user_prompt_template,
                target_language, current_version, is_system, created_at, updated_at, owner_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template.id, template.name, template.description, template.system_prompt,
                template.user_prompt_template, template.target_language, template.current_version,
                int(template.is_system), template.created_at, template.updated_at,
                int(getattr(template, "owner_user_id", 0) or 0),
            ),
        )
        return template

    async def update_template(self, template: PromptTemplate) -> PromptTemplate:
        template.updated_at = int(time.time())
        self._db.execute(
            """
            UPDATE prompt_templates
            SET name=?, description=?, system_prompt=?, user_prompt_template=?,
                target_language=?, current_version=?, is_system=?, updated_at=?, owner_user_id=?
            WHERE id=?
            """,
            (
                template.name, template.description, template.system_prompt,
                template.user_prompt_template, template.target_language,
                template.current_version, int(template.is_system),
                template.updated_at, int(getattr(template, "owner_user_id", 0) or 0),
                template.id,
            ),
        )
        return template

    async def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        row = self._db.query_one("SELECT * FROM prompt_templates WHERE id=?", (template_id,))
        if not row:
            return None
        return self._row_to_template(row)

    async def list_templates(self) -> List[PromptTemplate]:
        rows = self._db.query_all("SELECT * FROM prompt_templates ORDER BY created_at ASC")
        return [self._row_to_template(r) for r in rows]

    async def list_templates_by_owner(self, owner_user_id: int) -> List[PromptTemplate]:
        # v12: system prompts are shared; user prompts are per-tenant.
        rows = self._db.query_all(
            "SELECT * FROM prompt_templates WHERE owner_user_id=? OR is_system=1"
            " ORDER BY created_at ASC",
            (int(owner_user_id),),
        )
        return [self._row_to_template(r) for r in rows]

    def _row_to_template(self, r) -> PromptTemplate:
        return PromptTemplate(
            id=r["id"], name=r["name"], description=r["description"],
            system_prompt=r["system_prompt"], user_prompt_template=r["user_prompt_template"],
            target_language=r["target_language"], current_version=int(r["current_version"]),
            is_system=bool(r["is_system"]), created_at=r["created_at"], updated_at=r["updated_at"],
            owner_user_id=int(r["owner_user_id"]) if "owner_user_id" in r.keys() else 0,
        )

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





class SqliteSettingsRepository(ISettingsRepository):
    """Key/value store for operator-editable runtime settings (app_settings)."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def get(self, key: str, default: str = "") -> str:
        row = self._db.query_one("SELECT value FROM app_settings WHERE key=?", (key,))
        if not row:
            return default
        return row["value"] or default

    async def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (key, value, int(time.time())),
        )


class SqliteChannelCursorRepository:
    """Per-channel polling cursors for the ChannelPollingService.

    Stores the newest channel post id the poller has already replayed so a
    restart does not rescan the whole channel. The pipeline's dedupe layer is
    the real authority; this only keeps the poller from re-reading the tail
    every sweep.
    """

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    async def load_channel_cursors(self) -> dict:
        rows = self._db.query_all("SELECT chat_id, last_message_id FROM channel_cursors")
        return {str(r["chat_id"]): int(r["last_message_id"]) for r in rows or []}

    async def save_channel_cursors(self, cursors: dict) -> None:
        if not cursors:
            return
        now = int(time.time())

        def _write(conn) -> None:
            for chat_id, last_id in cursors.items():
                conn.execute(
                    "INSERT INTO channel_cursors (chat_id, last_message_id, updated_at)"
                    " VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET"
                    " last_message_id=excluded.last_message_id, updated_at=excluded.updated_at",
                    (str(chat_id), int(last_id), now),
                )

        # One atomic unit through the database lock. This used to issue a bare
        # `BEGIN` / `ROLLBACK` on the shared connection, which raced every other
        # writer: when the connection already had a transaction open, `BEGIN`
        # raised "cannot start a transaction within a transaction", the handler's
        # `ROLLBACK` then raised "cannot rollback - no transaction is active",
        # and that masking error is what reached the log
        # ("poll: cannot persist cursors: ...") while the real cause and the
        # cursor write were both lost. run_in_transaction serialises the write,
        # commits on success and rolls back only when a transaction is open,
        # never replacing the original error.
        self._db.run_in_transaction(_write)


# --------------------------------------------------------------------------- #
# Per-user task lifecycle (API-first phase 2)
# --------------------------------------------------------------------------- #

class SqliteUserTaskRepository(ITaskRepository):
    """User tasks in the same authoritative database as every other tenant
    resource. The worker-claim path is one UPDATE ... RETURNING so two workers
    can never hold the same row."""

    _COLS = (
        "id, owner_user_id, kind, status, idempotency_key, attempt, max_attempts,"
        " lease_token, lease_until, run_at, payload_json, result_json, error,"
        " error_code, created_at, updated_at, started_at, finished_at"
    )

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def _row_to_entity(self, row) -> UserTask:
        return UserTask(
            id=row["id"],
            owner_user_id=int(row["owner_user_id"]),
            kind=row["kind"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            lease_token=row["lease_token"],
            lease_until=int(row["lease_until"]),
            run_at=int(row["run_at"]),
            payload_json=row["payload_json"],
            result_json=row["result_json"],
            error=row["error"],
            error_code=row["error_code"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            started_at=int(row["started_at"]),
            finished_at=int(row["finished_at"]),
        )

    async def add(self, task: UserTask) -> UserTask:
        self._db.execute(
            f"INSERT INTO user_tasks ({self._COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            self._params(task),
        )
        return task

    async def insert_if_absent(self, task: UserTask) -> Optional[UserTask]:
        def _tx(conn):
            try:
                conn.execute(
                    f"INSERT INTO user_tasks ({self._COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    self._params(task),
                )
                return task
            except sqlite3.IntegrityError:
                # The idempotency race lost: return the row that owns the key.
                row = conn.execute(
                    "SELECT * FROM user_tasks WHERE owner_user_id=? AND kind=? AND idempotency_key=?",
                    (int(task.owner_user_id), task.kind, task.idempotency_key),
                ).fetchone()
                return self._row_to_entity(row) if row else task

        out = self._db.run_in_transaction(_tx)
        return out

    @staticmethod
    def _params(task: UserTask) -> tuple:
        return (
            task.id, int(task.owner_user_id), task.kind, task.status,
            task.idempotency_key, int(task.attempt), int(task.max_attempts),
            task.lease_token, int(task.lease_until), int(task.run_at),
            task.payload_json, task.result_json, task.error, task.error_code,
            int(task.created_at), int(task.updated_at),
            int(task.started_at), int(task.finished_at),
        )

    async def get(self, task_id: str) -> Optional[UserTask]:
        row = self._db.query_one("SELECT * FROM user_tasks WHERE id=?", (task_id,))
        return self._row_to_entity(row) if row else None

    async def list_for_owner(self, owner_user_id: int, status: str = "",
                             kind: str = "", limit: int = 50,
                             offset: int = 0) -> List[UserTask]:
        conds, params = ["owner_user_id=?"], [int(owner_user_id)]
        if status:
            conds.append("status=?"); params.append(status)
        if kind:
            conds.append("kind=?"); params.append(kind)
        params += [max(1, min(int(limit), 200)), max(0, int(offset))]
        rows = self._db.query_all(
            "SELECT * FROM user_tasks WHERE " + " AND ".join(conds) +
            " ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [self._row_to_entity(r) for r in rows]

    async def count_for_owner(self, owner_user_id: int, status: str = "",
                              kind: str = "") -> int:
        conds, params = ["owner_user_id=?"], [int(owner_user_id)]
        if status:
            conds.append("status=?"); params.append(status)
        if kind:
            conds.append("kind=?"); params.append(kind)
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM user_tasks WHERE " + " AND ".join(conds),
            tuple(params),
        )
        return int(row["n"]) if row else 0

    async def replace(self, task: UserTask) -> UserTask:
        self._db.execute(
            "UPDATE user_tasks SET owner_user_id=?, kind=?, status=?, idempotency_key=?,"
            " attempt=?, max_attempts=?, lease_token=?, lease_until=?, run_at=?,"
            " payload_json=?, result_json=?, error=?, error_code=?, created_at=?,"
            " updated_at=?, started_at=?, finished_at=? WHERE id=?",
            self._params(task)[1:] + (task.id,),
        )
        return task

    async def acquire_next(self, lease_token: str, lease_until: int,
                           now: int,
                           kinds: Optional[Sequence[str]] = None) -> Optional[UserTask]:
        kind_clause, kind_params = kind_filter_sql(kinds)
        params = (lease_token, int(lease_until), int(now), int(now), int(now)) + kind_params

        def _tx(conn):
            cur = conn.execute(
                "UPDATE user_tasks SET status='RUNNING', attempt=attempt+1,"
                " lease_token=?, lease_until=?, started_at=?, updated_at=?,"
                " result_json='', error='', error_code=''"
                " WHERE id IN (SELECT id FROM user_tasks WHERE status IN ('PENDING','RETRY_WAIT')"
                " AND run_at<=?" + kind_clause + " ORDER BY created_at, id LIMIT 1)"
                " RETURNING *",
                params,
            )
            row = cur.fetchone()
            return self._row_to_entity(row) if row else None

        return self._db.run_in_transaction(_tx)

    async def renew_lease(self, task_id: str, token: str, lease_until: int) -> bool:
        cur = self._db.execute(
            "UPDATE user_tasks SET lease_until=? WHERE id=? AND lease_token=?"
            " AND status='RUNNING'",
            (int(lease_until), task_id, token),
        )
        return cur.rowcount > 0

    async def cleanup(self, retention_seconds: int = TASK_RETENTION_SECONDS) -> int:
        # Terminal-only, batch-bounded (see ITaskRepository.cleanup). Ordered by
        # finished_at so the oldest rows go first and the index on
        # (status, finished_at) serves the subquery in insertion order.
        SUCCEEDED, FAILED, CANCELLED = "SUCCEEDED", "FAILED", "CANCELLED"
        if int(retention_seconds) <= 0:
            return 0
        cutoff = _now() - int(retention_seconds)
        cur = self._db.execute(
            "DELETE FROM user_tasks WHERE id IN ("
            " SELECT id FROM user_tasks WHERE status IN (?, ?, ?)"
            " AND finished_at > 0 AND finished_at < ?"
            " ORDER BY finished_at LIMIT ?)",
            # Literal terminal states, not an import: user_tasks imports this
            # module, so importing its constants here would be a cycle. The
            # values are pinned to core.application.user_tasks.TERMINAL_STATES by
            # test_task_retention.py::test_the_sweep_set_is_exactly_the_terminal_states.
            (SUCCEEDED, FAILED, CANCELLED, int(cutoff), int(TASK_PURGE_BATCH)),
        )
        return int(cur.rowcount or 0) if cur is not None else 0

    async def reclaim_expired(self, now: int) -> int:
        # The attempt budget is respected on takeover too: a task whose
        # attempts are exhausted must NOT cycle forever between a crashed
        # worker and this reclaim; it settles as FAILED instead.
        cur = self._db.execute(
            "UPDATE user_tasks SET status=CASE WHEN attempt>=max_attempts THEN 'FAILED'"
            " ELSE 'RETRY_WAIT' END,"
            " lease_token='', lease_until=0,"
            " run_at=CASE WHEN attempt>=max_attempts THEN run_at ELSE ? END,"
            " finished_at=CASE WHEN attempt>=max_attempts THEN ? ELSE 0 END,"
            " updated_at=?,"
            " error='reclaimed from an expired worker lease', error_code='lease_expired'"
            " WHERE status='RUNNING' AND lease_until>0 AND lease_until<=?",
            (int(now), int(now), int(now), int(now)),
        )
        return cur.rowcount

    async def finish_succeeded(self, task_id: str, token: str, result_json: str,
                               now: int) -> bool:
        cur = self._db.execute(
            "UPDATE user_tasks SET status='SUCCEEDED', result_json=?, finished_at=?,"
            " updated_at=?, lease_token='', lease_until=0"
            " WHERE id=? AND lease_token=? AND status='RUNNING'",
            (result_json, int(now), int(now), task_id, token))
        return cur.rowcount > 0

    async def finish_failed(self, task_id: str, token: str, error: str,
                            error_code: str, now: int) -> bool:
        cur = self._db.execute(
            "UPDATE user_tasks SET status='FAILED', error=?, error_code=?,"
            " finished_at=?, updated_at=?, lease_token='', lease_until=0"
            " WHERE id=? AND lease_token=? AND status='RUNNING'",
            (error, error_code, int(now), int(now), task_id, token))
        return cur.rowcount > 0

    async def pause_for_retry(self, task_id: str, token: str, run_at: int,
                              error: str, error_code: str, now: int) -> bool:
        cur = self._db.execute(
            "UPDATE user_tasks SET status='RETRY_WAIT', error=?, error_code=?,"
            " run_at=?, lease_token='', lease_until=0, updated_at=?"
            " WHERE id=? AND lease_token=? AND status='RUNNING'",
            (error, error_code, int(run_at), int(now), task_id, token))
        return cur.rowcount > 0

    async def cancel_queued(self, task_id: str, owner_user_id: int,
                            now: int) -> bool:
        cur = self._db.execute(
            "UPDATE user_tasks SET status='CANCELLED', finished_at=?, updated_at=?,"
            " error='', error_code='cancelled', lease_token='', lease_until=0"
            " WHERE id=? AND owner_user_id=? AND status IN ('PENDING','RETRY_WAIT')",
            (int(now), int(now), task_id, int(owner_user_id)))
        return cur.rowcount > 0

    async def retry_failed(self, task_id: str, owner_user_id: int,
                           now: int) -> Optional[UserTask]:
        def _tx(conn):
            cur = conn.execute(
                "UPDATE user_tasks SET status='PENDING', attempt=0, error='',"
                " error_code='', lease_token='', lease_until=0, run_at=0,"
                " started_at=0, finished_at=0, result_json='', updated_at=?"
                " WHERE id=? AND owner_user_id=? AND status='FAILED'"
                " RETURNING *",
                (int(now), task_id, int(owner_user_id)),
            )
            row = cur.fetchone()
            return self._row_to_entity(row) if row else None

        return self._db.run_in_transaction(_tx)

    async def requeue_unclaimed(self, task_id: str, token: str, run_at: int,
                                now: int) -> bool:
        cur = self._db.execute(
            "UPDATE user_tasks SET status='RETRY_WAIT', error='', error_code='',"
            " run_at=?, lease_token='', lease_until=0, updated_at=?"
            " WHERE id=? AND lease_token=? AND status='RUNNING'",
            (int(run_at), int(now), task_id, token))
        return cur.rowcount > 0

