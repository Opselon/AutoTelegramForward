"""PostgreSQL repository implementations of the application repository ports.

Provides standard PostgreSQL support with parameterized queries (%s / $n),
JSONB serialization, proper boolean types, transaction integrity, and
optimistic concurrency control (OCC) using the `version` column.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from .coercion import enum_str

logger = logging.getLogger(__name__)

from ...application.repositories import (
    ConcurrencyError,
    ITaskRepository,
    IDeliveryQueueRepository,
    IForwardRuleRepository,
    IMessageMapRepository,
    IPromptRepository,
    kind_filter_sql,
    TASK_PURGE_BATCH,
    TASK_RETENTION_SECONDS,
)
from ...domain.entities import (
    ContentMode,
    UserTask,
    DeliveryJob,
    DeliveryStatus,
    ForwardMode,
    ForwardRule,
    MessageMapping,
    PromptTemplate,
    PromptVersion,
    RoutingType,
    TriggerEvent,
)
from ...domain.value_objects import AIFallbackPolicy


class PostgresConnection(Protocol):
    """Protocol representing a PostgreSQL database connection or cursor."""
    async def execute(self, query: str, params: tuple | list = ()) -> Any: ...
    async def fetchone(self, query: str, params: tuple | list = ()) -> Any: ...
    async def fetchall(self, query: str, params: tuple | list = ()) -> List[Any]: ...
    async def fetchrow(self, query: str, *params: Any) -> Any: ...
    async def fetch(self, query: str, *params: Any) -> List[Any]: ...


def _safe_list(val: Any, default: list) -> list:
    if val is None:
        return default
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else default
        except Exception:
            return default
    return default


def _safe_dict(val: Any, default: dict) -> dict:
    if val is None:
        return default
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else default
        except Exception:
            return default
    return default


def _format_sql(query: str, db: Any) -> str:
    is_asyncpg = "asyncpg" in type(db).__module__ or hasattr(db, "_protocol")
    if is_asyncpg and "%s" in query:
        parts = query.split("%s")
        new_q = []
        for i, part in enumerate(parts[:-1], 1):
            new_q.append(f"{part}${i}")
        new_q.append(parts[-1])
        return "".join(new_q)
    return query


_CONN_LOCKS: dict = {}


def _split_sql_script(script: str) -> List[str]:
    """Split a DDL script into runnable statements, ignoring comments.

    Splitting on ";" alone is wrong: the schema scripts carry `--` commentary,
    and two of those comment lines contain a semicolon ("... the contract is
    `last_error` on both backends." / "... ; used_at makes it single-use.").
    The naive split cut the following CREATE TABLE in half and shipped the
    fragments to the server as statements, so `dead_letter_queue` and
    `web_login_codes` were never created on PostgreSQL deployments while the
    error was swallowed by the caller. Strings are tracked so a semicolon
    inside a quoted default is not treated as a statement boundary either.

    PostgreSQL dollar quotes (``$$ ... $$`` / ``$tag$ ... $tag$``) are tracked
    like strings: a ``DO $$ ... ; ... $$`` block carries semicolons *and*
    single quotes inside its body, and a string-only scanner ends the quote at
    the first ``'`` it meets there, then splits the procedural body into
    fragments that fail (or worse, half-apply) — the same silent-failure
    class as the comment defect. The opening tag is captured and matched
    exactly, so ``$a$ ... $a$`` cannot be ended by a stray ``$$``.
    """
    statements: List[str] = []
    buf: List[str] = []
    i, n = 0, len(script)
    in_string = False
    dollar_tag: Optional[str] = None
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if dollar_tag is not None:
            # Inside a dollar-quoted body: every byte is literal text.
            # PostgreSQL's lexer treats the whole body as an uninterpreted
            # token, so a single quote here is NOT a string boundary and a
            # semicolon here is NOT a statement boundary. The body ends only
            # at the exact closing tag.
            if script.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            tag = _dollar_quote_tag(script, i)
            if tag is not None:
                dollar_tag = tag
                buf.append(tag)
                i += len(tag)
                continue
        if in_string:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":  # escaped quote
                    buf.append(nxt)
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":  # line comment
            while i < n and script[i] != "\n":
                i += 1
            buf.append("\n")
            continue
        if ch == "/" and nxt == "*":  # block comment
            i += 2
            while i + 1 < n and not (script[i] == "*" and script[i + 1] == "/"):
                i += 1
            i += 2
            buf.append("\n")
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _dollar_quote_tag(script: str, pos: int) -> Optional[str]:
    """The dollar tag opening at ``pos`` (``$$`` or ``$tag$``), else None.

    A tag is ``$``, an optional identifier of letters/digits/underscores, and
    a closing ``$``. ``$$`` itself is the empty tag. A lone ``$`` in an
    operator context (e.g. ``a $ b``) or a numeric literal has no second
    ``$`` before the next boundary and is therefore not a quote.
    """
    if script[pos] != "$":
        return None
    i = pos + 1
    while i < len(script) and (script[i].isalnum() or script[i] == "_"):
        i += 1
    if i < len(script) and script[i] == "$":
        return script[pos:i + 1]
    return None


def _affected(result: Any) -> int:
    """Rows affected by a write, across the driver shapes used in this module.

    asyncpg returns a status string ("UPDATE 1", "INSERT 0 1"), psycopg-style
    wrappers return an int, and some wrappers return None or a bool. Reading
    this wrong is not cosmetic: a False "0 rows" turns a lease-loss into
    "lease still held", so parse defensively and treat anything unparseable as
    0 rather than as success.
    """
    if result is None:
        return 0
    if isinstance(result, bool):
        return int(result)
    if isinstance(result, int):
        return result
    numbers = re.findall(r"\d+", str(result))
    return int(numbers[-1]) if numbers else 0


_UNIQUE_VIOLATION_SQLSTATE = "23505"


def _is_unique_violation(exc: BaseException) -> bool:
    """True only for a unique-constraint violation (SQLSTATE 23505).

    The previous check was a substring match on the message ("duplicate key" /
    "already exists"). Any other error whose text happens to contain those
    words — a different constraint, a message change between driver versions,
    a localized message — was then routed into the "return the pre-existing
    row" path, handing the caller a row that has nothing to do with its
    submit. Prefer the structured signal, then the exception type, and only
    then a precise phrase.
    """
    if getattr(exc, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE:
        return True
    if type(exc).__name__ in ("UniqueViolationError", "IntegrityError"):
        return True
    text = str(exc).lower()
    return ("duplicate key value violates unique constraint" in text
            or "unique constraint failed" in text)


def _get_db_lock(db: Any) -> asyncio.Lock:
    db_id = id(db)
    if db_id not in _CONN_LOCKS:
        _CONN_LOCKS[db_id] = asyncio.Lock()
    return _CONN_LOCKS[db_id]


async def _execute_db(db: Any, query: str, params: tuple | list = ()) -> Any:
    formatted = _format_sql(query, db)
    is_asyncpg = "asyncpg" in type(db).__module__ or hasattr(db, "_protocol")
    lock = _get_db_lock(db)
    async with lock:
        if is_asyncpg:
            return await db.execute(formatted, *params)
        return await db.execute(formatted, params)


async def _fetch_one_db(db: Any, query: str, params: tuple | list = ()) -> Any:
    formatted = _format_sql(query, db)
    is_asyncpg = "asyncpg" in type(db).__module__ or hasattr(db, "_protocol")
    lock = _get_db_lock(db)
    async with lock:
        if is_asyncpg:
            if hasattr(db, "fetchrow"):
                return await db.fetchrow(formatted, *params)
            if hasattr(db, "fetchone"):
                return await db.fetchone(formatted, *params)
        if hasattr(db, "fetchone"):
            return await db.fetchone(formatted, params)
        if hasattr(db, "fetchrow"):
            return await db.fetchrow(formatted, *params)
        return None


async def _fetch_all_db(db: Any, query: str, params: tuple | list = ()) -> List[Any]:
    formatted = _format_sql(query, db)
    is_asyncpg = "asyncpg" in type(db).__module__ or hasattr(db, "_protocol")
    lock = _get_db_lock(db)
    async with lock:
        if is_asyncpg:
            if hasattr(db, "fetch"):
                return await db.fetch(formatted, *params)
            if hasattr(db, "fetchall"):
                return await db.fetchall(formatted, *params)
        if hasattr(db, "fetchall"):
            return await db.fetchall(formatted, params)
        if hasattr(db, "fetch"):
            return await db.fetch(formatted, *params)
        # Fail loudly instead of returning [] — a silent empty result here
        # would masquerade as "no rows found" and hide a broken connection
        # (e.g. an un-awaited pool coroutine bound by mistake).
        raise RuntimeError(
            f"_fetch_all_db: db object of type {type(db).__name__} exposes no "
            f"fetch/fetchall method — refusing to return a silent empty result"
        )


def _rule_meta_json(rule) -> str:
    """Serialise rule metadata, mirroring name/description into it.

    forward_rules has no name or description column, so a task renamed through
    the Telegram bot or the REST API was accepted, reported as saved, and then
    silently lost on the next reload. `metadata` is the JSON column every rule
    already carries, so mirroring persists the value with no schema migration.
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


class PostgresForwardRuleRepository(IForwardRuleRepository):
    """Production-grade PostgreSQL repository for ForwardRule entity.

    Adheres strictly to `IForwardRuleRepository` contract, handles
    atomic transactions and optimistic concurrency control (OCC).
    """

    DDL_SCHEMA = """
    CREATE TABLE IF NOT EXISTS forward_rules (
        id VARCHAR(64) PRIMARY KEY,
        session_id VARCHAR(64) NOT NULL,
        source_chat_id VARCHAR(128) NOT NULL,
        source_chat_name TEXT DEFAULT '',
        target_chat_id VARCHAR(128) NOT NULL,
        target_chat_name TEXT DEFAULT '',
        target_chat_ids JSONB DEFAULT '[]'::jsonb,
        routing_type VARCHAR(32) NOT NULL,
        forward_mode VARCHAR(32) NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        filter_rule_id VARCHAR(64),
        ai_config_id VARCHAR(64),
        remove_links BOOLEAN NOT NULL DEFAULT FALSE,
        custom_caption_template TEXT,
        delay_seconds REAL DEFAULT 0.0,
        skip_history BOOLEAN NOT NULL DEFAULT TRUE,
        since_ts BIGINT DEFAULT 0,
        ignore_edits BOOLEAN NOT NULL DEFAULT FALSE,
        trigger_events JSONB DEFAULT '["NEW_MESSAGE"]'::jsonb,
        content_mode VARCHAR(32) DEFAULT 'AUTO',
        metadata JSONB DEFAULT '{}'::jsonb,
        version INTEGER NOT NULL DEFAULT 1,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        message_category VARCHAR(32) DEFAULT 'ALL',
        intermediate_channel_id VARCHAR(128) DEFAULT '',
        intermediate_channel_name TEXT DEFAULT '',
        intermediate_target_chat_id VARCHAR(128) DEFAULT '',
        use_intermediate BOOLEAN DEFAULT FALSE,
        fallback_mode VARCHAR(32) DEFAULT 'COPY_MESSAGE',
        fallback_enabled BOOLEAN DEFAULT TRUE,
        detection_criteria JSONB DEFAULT '{}'::jsonb,
        priority INTEGER DEFAULT 10,
        execution_order INTEGER DEFAULT 0,
        multi_route BOOLEAN DEFAULT FALSE,
        media_handling VARCHAR(32) DEFAULT 'AUTO',
        dedupe_policy VARCHAR(32) DEFAULT 'STRICT',
        max_retries INTEGER DEFAULT 3,
        retry_backoff_base REAL DEFAULT 2.0,
        rate_limit_per_minute INTEGER DEFAULT 0,
        rate_limit_burst INTEGER DEFAULT 0,
        custom_header TEXT DEFAULT '',
        custom_footer TEXT DEFAULT '',
        header_enabled BOOLEAN DEFAULT FALSE,
        template_text TEXT DEFAULT '',
        template_media TEXT DEFAULT '',
        template_album TEXT DEFAULT '',
        caption_max_length INTEGER DEFAULT 0,
        preserve_signature BOOLEAN DEFAULT FALSE,
        is_paused BOOLEAN DEFAULT FALSE,
        paused_until BIGINT DEFAULT 0,
        link_policy VARCHAR(64) DEFAULT 'PRESERVE_ALL',
        domain_allowlist JSONB DEFAULT '[]'::jsonb,
        domain_blocklist JSONB DEFAULT '[]'::jsonb,
        link_rewrite_map JSONB DEFAULT '{}'::jsonb,
        allowed_media_types JSONB DEFAULT '[]'::jsonb,
        split_long_caption BOOLEAN DEFAULT TRUE,
        owner_user_id BIGINT NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pg_fwd_source ON forward_rules (source_chat_id, is_active);
    CREATE INDEX IF NOT EXISTS idx_pg_fwd_session ON forward_rules (session_id);
    CREATE INDEX IF NOT EXISTS idx_pg_fwd_owner ON forward_rules (owner_user_id);
    CREATE INDEX IF NOT EXISTS idx_pg_fwd_session_owner ON forward_rules (session_id, owner_user_id);
    """

    def __init__(self, db_client: Any) -> None:
        self._db = db_client

    async def add(self, rule: ForwardRule) -> ForwardRule:
        rule_version = int(getattr(rule, "version", 1) or 1)
        trigger_json = json.dumps(
            getattr(rule, "trigger_events", [TriggerEvent.NEW_MESSAGE.value])
            if isinstance(getattr(rule, "trigger_events", None), list)
            else [getattr(rule, "trigger_events", TriggerEvent.NEW_MESSAGE.value)]
        )
        content_val = enum_str(getattr(rule, "content_mode", None), ContentMode.AUTO.value)
        meta_json = _rule_meta_json(rule)
        targets_json = json.dumps(rule.target_chat_ids or [])

        query = """
        INSERT INTO forward_rules (
            id, session_id, source_chat_id, source_chat_name, target_chat_id,
            target_chat_name, target_chat_ids, routing_type, forward_mode, is_active,
            filter_rule_id, ai_config_id, remove_links, custom_caption_template,
            delay_seconds, skip_history, since_ts, ignore_edits, trigger_events,
            content_mode, metadata, version, created_at, updated_at, owner_user_id
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
        """
        params = (
            rule.id, rule.session_id, rule.source_chat_id, rule.source_chat_name,
            rule.target_chat_id, rule.target_chat_name, targets_json,
            rule.routing_type.value, rule.forward_mode.value, bool(rule.is_active),
            rule.filter_rule_id, rule.ai_config_id, bool(rule.remove_links),
            rule.custom_caption_template, float(rule.delay_seconds), bool(rule.skip_history),
            int(rule.since_ts or 0), bool(rule.ignore_edits), trigger_json,
            content_val, meta_json, rule_version, rule.created_at, rule.updated_at,
            int(getattr(rule, "owner_user_id", 0) or 0),
        )
        await _execute_db(self._db, query, params)
        return rule

    async def update(self, rule: ForwardRule, expected_version: Optional[int] = None) -> ForwardRule:
        rule_version = int(getattr(rule, "version", 1) or 1)
        next_version = rule_version + 1

        trigger_json = json.dumps(
            getattr(rule, "trigger_events", [TriggerEvent.NEW_MESSAGE.value])
            if isinstance(getattr(rule, "trigger_events", None), list)
            else [getattr(rule, "trigger_events", TriggerEvent.NEW_MESSAGE.value)]
        )
        content_val = enum_str(getattr(rule, "content_mode", None), ContentMode.AUTO.value)
        meta_json = _rule_meta_json(rule)
        targets_json = json.dumps(rule.target_chat_ids or [])

        common_params = (
            rule.session_id, rule.source_chat_id, rule.source_chat_name,
            rule.target_chat_id, rule.target_chat_name, targets_json,
            rule.routing_type.value, rule.forward_mode.value, bool(rule.is_active),
            rule.filter_rule_id, rule.ai_config_id, bool(rule.remove_links),
            rule.custom_caption_template, float(rule.delay_seconds), bool(rule.skip_history),
            int(rule.since_ts or 0), bool(rule.ignore_edits),
            trigger_json, content_val, meta_json, next_version, rule.updated_at,
            int(getattr(rule, "owner_user_id", 0) or 0),
        )

        if expected_version is not None:
            query = """
            UPDATE forward_rules SET
                session_id = %s, source_chat_id = %s, source_chat_name = %s, target_chat_id = %s,
                target_chat_name = %s, target_chat_ids = %s, routing_type = %s, forward_mode = %s,
                is_active = %s, filter_rule_id = %s, ai_config_id = %s, remove_links = %s,
                custom_caption_template = %s, delay_seconds = %s, skip_history = %s, since_ts = %s,
                ignore_edits = %s, trigger_events = %s, content_mode = %s, metadata = %s,
                version = %s, updated_at = %s, owner_user_id = %s
            WHERE id = %s AND version = %s
            """
            result = await _execute_db(self._db, query, (*common_params, rule.id, int(expected_version)))
            rowcount = getattr(result, "rowcount", None)
            if rowcount == 0 or result == 0 or result == "UPDATE 0":
                existing = await self.get_by_id(rule.id)
                if not existing:
                    raise ValueError(f"Rule {rule.id} does not exist")
                raise ConcurrencyError(
                    f"PostgreSQL concurrency conflict on rule {rule.id}: expected version {expected_version}, "
                    f"current version is {existing.version}"
                )
        else:
            query = """
            UPDATE forward_rules SET
                session_id = %s, source_chat_id = %s, source_chat_name = %s, target_chat_id = %s,
                target_chat_name = %s, target_chat_ids = %s, routing_type = %s, forward_mode = %s,
                is_active = %s, filter_rule_id = %s, ai_config_id = %s, remove_links = %s,
                custom_caption_template = %s, delay_seconds = %s, skip_history = %s, since_ts = %s,
                ignore_edits = %s, trigger_events = %s, content_mode = %s, metadata = %s,
                version = %s, updated_at = %s, owner_user_id = %s
            WHERE id = %s
            """
            await _execute_db(self._db, query, (*common_params, rule.id))

        rule.version = next_version
        return rule

    async def get_by_id(self, rule_id: str) -> Optional[ForwardRule]:
        query = "SELECT * FROM forward_rules WHERE id = %s"
        row = await _fetch_one_db(self._db, query, (rule_id,))
        return self._row_to_entity(row) if row else None

    async def get_owned(self, rule_id: str, owner_user_id: int) -> Optional[ForwardRule]:
        """Single owner-scoped fetch (fail-closed). Mirror of the SQLite repo's
        get_owned — one indexed row for per-task actions at scale."""
        query = "SELECT * FROM forward_rules WHERE id = %s AND owner_user_id = %s"
        row = await _fetch_one_db(self._db, query, (rule_id, int(owner_user_id)))
        return self._row_to_entity(row) if row else None

    async def list_by_session(self, session_id: str, owner_user_id: int = 0) -> List[ForwardRule]:
        if owner_user_id:
            query = ("SELECT * FROM forward_rules WHERE session_id = %s AND owner_user_id = %s"
                     " ORDER BY created_at ASC")
            rows = await _fetch_all_db(self._db, query, (session_id, int(owner_user_id)))
        else:
            query = "SELECT * FROM forward_rules WHERE session_id = %s ORDER BY created_at ASC"
            rows = await _fetch_all_db(self._db, query, (session_id,))
        return [self._row_to_entity(r) for r in rows]

    async def list_by_owner(self, owner_user_id: int) -> List[ForwardRule]:
        query = "SELECT * FROM forward_rules WHERE owner_user_id = %s ORDER BY created_at ASC"
        rows = await _fetch_all_db(self._db, query, (int(owner_user_id),))
        return [self._row_to_entity(r) for r in rows]

    async def list_all(self) -> List[ForwardRule]:
        query = "SELECT * FROM forward_rules ORDER BY created_at ASC"
        rows = await _fetch_all_db(self._db, query, ())
        return [self._row_to_entity(r) for r in rows]

    async def list_active_by_source(self, source_chat_id: str) -> List[ForwardRule]:
        """Active rules that watch ``source_chat_id`` (primary or multi-source).

        The extra sources of a task live in detection_criteria.source_chat_ids;
        `jsonb_typeof` keeps a row whose detection_criteria is not a JSON object
        from raising and taking the whole query down with it.
        """
        query = (
            "SELECT * FROM forward_rules WHERE is_active = TRUE AND ("
            "  source_chat_id = %s"
            "  OR (jsonb_typeof(detection_criteria) = 'object' AND EXISTS ("
            "        SELECT 1 FROM jsonb_array_elements_text("
            "          COALESCE(detection_criteria->'source_chat_ids', '[]'::jsonb)) AS s"
            "        WHERE s = %s))"
            ")"
        )
        cid = str(source_chat_id)
        rows = await _fetch_all_db(self._db, query, (cid, cid))
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, rule_id: str) -> bool:
        """Delete the task and its own bookkeeping in one statement.

        Data-modifying CTEs make it atomic — there is no window in which the
        rule is gone but its mappings are not. A leftover ``message_map`` row
        claims the task already delivered a source message, so a re-created task
        could silently skip it; jobs, dead letters, counters and error rows would
        keep a deleted task visible in every operator view that reads them.
        """
        query = """
            WITH deleted AS (
                DELETE FROM forward_rules WHERE id = %s RETURNING id
            ), mappings AS (
                DELETE FROM message_map WHERE rule_id IN (SELECT id FROM deleted)
            ), jobs AS (
                DELETE FROM delivery_jobs WHERE rule_id IN (SELECT id FROM deleted)
            ), letters AS (
                DELETE FROM dead_letter_queue WHERE rule_id IN (SELECT id FROM deleted)
            ), counters AS (
                DELETE FROM rule_stats WHERE rule_id IN (SELECT id FROM deleted)
            ), errors AS (
                DELETE FROM error_log WHERE rule_id IN (SELECT id FROM deleted)
            )
            SELECT COUNT(*) AS deleted_count FROM deleted
        """
        row = await _fetch_one_db(self._db, query, (rule_id,))
        if row is None:
            return False
        try:
            count = int(row["deleted_count"])
        except (TypeError, KeyError, IndexError):
            count = int(row[0])
        return count > 0

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

        Mirrors the SQLite implementation: a single conditional UPDATE whose
        condition is the exact state the caller was shown, so a stale screen or a
        concurrent change cannot be overwritten (it returns False instead).
        """
        from ...domain.entities import (
            STATUS_DISABLED,
            STATUS_PAUSED,
            STATUS_PAUSED_UNTIL,
            STATUS_RUNNING,
        )

        ts = time.time() if now is None else float(now)
        paused_now = "(is_paused = TRUE AND (paused_until = 0 OR paused_until > %s))"
        if target_active:
            conditions = {
                STATUS_DISABLED: ("is_active = FALSE", []),
                STATUS_PAUSED: ("is_active = TRUE AND is_paused = TRUE AND paused_until = 0", []),
                STATUS_PAUSED_UNTIL: ("is_active = TRUE AND is_paused = TRUE AND paused_until > %s", [ts]),
            }
        else:
            conditions = {STATUS_RUNNING: (f"is_active = TRUE AND NOT {paused_now}", [ts])}
        if expected_state not in conditions:
            return False
        condition, cond_params = conditions[expected_state]

        if target_active:
            set_clause = "is_active = TRUE, is_paused = FALSE, paused_until = 0, version = version + 1, updated_at = %s"
        else:
            set_clause = "is_active = FALSE, version = version + 1, updated_at = %s"
        params: List[Any] = [int(time.time()), *cond_params, rule_id]
        where_owner = ""
        if owner_user_id:
            where_owner = " AND owner_user_id = %s"
        query = f"UPDATE forward_rules SET {set_clause} WHERE {condition} AND id = %s{where_owner}"
        if owner_user_id:
            params.append(int(owner_user_id))
        res = await _execute_db(self._db, query, tuple(params))
        count = getattr(res, "rowcount", None)
        if count is not None:
            return count > 0
        return True

    @staticmethod
    def _row_to_entity(row: Any) -> ForwardRule:
        d = dict(row) if hasattr(row, "keys") else row
        cols = set(d.keys())

        trigger_raw = d.get("trigger_events", "NEW_MESSAGE")
        triggers = _safe_list(trigger_raw, [TriggerEvent.NEW_MESSAGE.value])
        if isinstance(triggers, str):
            triggers = [triggers]

        content_raw = d.get("content_mode", ContentMode.AUTO.value)
        try:
            cm = ContentMode(content_raw)
        except ValueError:
            cm = ContentMode.AUTO

        fb_raw = d.get("ai_fallback_policy", AIFallbackPolicy.DROP.value)
        try:
            fb_policy = AIFallbackPolicy(fb_raw)
        except ValueError:
            fb_policy = AIFallbackPolicy.DROP
        ai_ver = int(d.get("ai_prompt_version", 0) or 0)
        ai_timeout = float(d.get("ai_timeout_seconds", 15.0) or 15.0)
        ai_sec = str(d.get("ai_secondary_config_id")) if d.get("ai_secondary_config_id") else None

        return ForwardRule(
            id=d["id"],
            session_id=d["session_id"],
            source_chat_id=str(d["source_chat_id"]),
            source_chat_name=d.get("source_chat_name", ""),
            target_chat_id=str(d["target_chat_id"]),
            target_chat_name=d.get("target_chat_name", ""),
            target_chat_ids=_safe_list(d.get("target_chat_ids"), []),
            routing_type=RoutingType(d["routing_type"]),
            forward_mode=ForwardMode(d["forward_mode"]),
            is_active=bool(d["is_active"]),
            filter_rule_id=d.get("filter_rule_id"),
            ai_config_id=d.get("ai_config_id"),
            remove_links=bool(d.get("remove_links", False)),
            custom_caption_template=str(d.get("custom_caption_template") or ""),
            delay_seconds=float(d.get("delay_seconds", 0.0) or 0.0),
            skip_history=bool(d.get("skip_history", True)),
            since_ts=int(d.get("since_ts", 0) or 0),
            ignore_edits=bool(d.get("ignore_edits", False)),
            trigger_events=[str(t) for t in triggers],
            content_mode=cm,
            ai_fallback_policy=fb_policy,
            ai_prompt_version=ai_ver,
            ai_timeout_seconds=ai_timeout,
            ai_secondary_config_id=ai_sec,
            metadata=_safe_dict(d.get("metadata"), {}),
            name=str(_safe_dict(d.get("metadata"), {}).get("name") or ""),
            description=str(_safe_dict(d.get("metadata"), {}).get("description") or ""),
            version=int(d.get("version", 1) or 1),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            message_category=d.get("message_category", "ALL") or "ALL",
            intermediate_channel_id=str(d.get("intermediate_channel_id") or ""),
            intermediate_channel_name=str(d.get("intermediate_channel_name") or ""),
            intermediate_target_chat_id=str(d.get("intermediate_target_chat_id") or ""),
            use_intermediate=bool(d.get("use_intermediate", False)),
            fallback_mode=ForwardMode(d["fallback_mode"]) if d.get("fallback_mode") in ForwardMode._value2member_map_ else ForwardMode.COPY_MESSAGE,
            fallback_enabled=bool(d.get("fallback_enabled", True)),
            detection_criteria=_safe_dict(d.get("detection_criteria"), {}),
            priority=int(d.get("priority", 10) or 10),
            execution_order=int(d.get("execution_order", 0) or 0),
            multi_route=bool(d.get("multi_route", False)),
            media_handling=d.get("media_handling", "AUTO") or "AUTO",
            dedupe_policy=d.get("dedupe_policy", "STRICT") or "STRICT",
            max_retries=int(d.get("max_retries", 3) or 3),
            retry_backoff_base=float(d.get("retry_backoff_base", 2.0) or 2.0),
            rate_limit_per_minute=int(d.get("rate_limit_per_minute", 0) or 0),
            rate_limit_burst=int(d.get("rate_limit_burst", 0) or 0),
            custom_header=str(d.get("custom_header") or ""),
            custom_footer=str(d.get("custom_footer") or ""),
            header_enabled=bool(d.get("header_enabled", False)),
            template_text=str(d.get("template_text") or ""),
            template_media=str(d.get("template_media") or ""),
            template_album=str(d.get("template_album") or ""),
            caption_max_length=int(d.get("caption_max_length", 0) or 0),
            preserve_signature=bool(d.get("preserve_signature", False)),
            is_paused=bool(d.get("is_paused", False)),
            paused_until=int(d.get("paused_until", 0) or 0),
            link_policy=d.get("link_policy", "PRESERVE_ALL") or "PRESERVE_ALL",
            domain_allowlist=_safe_list(d.get("domain_allowlist"), []),
            domain_blocklist=_safe_list(d.get("domain_blocklist"), []),
            link_rewrite_map=_safe_dict(d.get("link_rewrite_map"), {}),
            allowed_media_types=_safe_list(d.get("allowed_media_types"), []),
            split_long_caption=bool(d.get("split_long_caption", True)),
            owner_user_id=int(d.get("owner_user_id") or 0),
        )


class PostgresMessageMapRepository(IMessageMapRepository):
    """PostgreSQL implementation of IMessageMapRepository."""

    def __init__(self, db: PostgresConnection) -> None:
        self._db = db

    def _row_to_entity(self, row: Any) -> MessageMapping:
        d = dict(row) if hasattr(row, "keys") else row
        status_val = d.get("delivery_status", "PENDING")
        try:
            status = DeliveryStatus(status_val)
        except (ValueError, TypeError):
            status = DeliveryStatus.PENDING

        return MessageMapping(
            id=d["id"],
            rule_id=d["rule_id"],
            source_chat_id=str(d["source_chat_id"]),
            source_message_id=int(d["source_message_id"]),
            target_chat_id=str(d["target_chat_id"]),
            target_message_id=int(d["target_message_id"]) if d.get("target_message_id") is not None else None,
            media_group_id=d.get("media_group_id") if d.get("media_group_id") else None,
            delivery_status=status,
            created_at=int(d["created_at"]),
            updated_at=int(d["updated_at"]),
        )

    async def _fetch_all(self, query: str, params: tuple = ()) -> list:
        return await _fetch_all_db(self._db, query, params)

    async def _fetch_one(self, query: str, params: tuple = ()) -> Optional[dict]:
        return await _fetch_one_db(self._db, query, params)

    async def add_or_update(self, mapping: MessageMapping) -> MessageMapping:
        now = int(time.time())
        mapping.updated_at = now
        query = """
        INSERT INTO message_map (
            id, rule_id, source_chat_id, source_message_id, target_chat_id,
            target_message_id, media_group_id, delivery_status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(rule_id, source_chat_id, source_message_id, target_chat_id) DO UPDATE SET
            target_message_id = COALESCE(EXCLUDED.target_message_id, message_map.target_message_id),
            media_group_id = COALESCE(EXCLUDED.media_group_id, message_map.media_group_id),
            delivery_status = EXCLUDED.delivery_status,
            updated_at = EXCLUDED.updated_at
        """
        status_str = getattr(mapping.delivery_status, "value", str(mapping.delivery_status))
        await _execute_db(
            self._db,
            query,
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
        query = "SELECT * FROM message_map WHERE source_chat_id = %s AND source_message_id = %s"
        rows = await self._fetch_all(query, (str(source_chat_id), int(source_message_id)))
        return [self._row_to_entity(r) for r in rows]

    async def get_by_target(self, target_chat_id: str, target_message_id: int) -> Optional[MessageMapping]:
        query = "SELECT * FROM message_map WHERE target_chat_id = %s AND target_message_id = %s"
        row = await self._fetch_one(query, (str(target_chat_id), int(target_message_id)))
        return self._row_to_entity(row) if row else None

    async def list_by_source_message_id(self, source_message_id: int) -> List[MessageMapping]:
        query = "SELECT * FROM message_map WHERE source_message_id = %s"
        rows = await self._fetch_all(query, (int(source_message_id),))
        return [self._row_to_entity(r) for r in rows]

    async def get_by_rule_and_source(
        self, rule_id: str, source_chat_id: str, source_message_id: int
    ) -> List[MessageMapping]:
        query = "SELECT * FROM message_map WHERE rule_id = %s AND source_chat_id = %s AND source_message_id = %s"
        rows = await self._fetch_all(query, (rule_id, str(source_chat_id), int(source_message_id)))
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
        now = int(time.time())
        status_str = getattr(status, "value", str(status))
        query = """
        UPDATE message_map SET
            target_message_id = COALESCE(%s, target_message_id),
            delivery_status = %s,
            updated_at = %s
        WHERE rule_id = %s AND source_chat_id = %s AND source_message_id = %s AND target_chat_id = %s
        """
        await _execute_db(
            self._db,
            query,
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
        return True

    async def cleanup(self, retention_seconds: int = 60 * 60 * 24 * 30) -> int:
        cutoff = int(time.time()) - retention_seconds
        query = "DELETE FROM message_map WHERE updated_at < %s"
        res = await _execute_db(self._db, query, (cutoff,))
        if isinstance(res, str) and res.startswith("DELETE "):
            return int(res.split()[-1])
        count = getattr(res, "rowcount", None)
        return count if count is not None else 0

    # --- v2.0 app update: Duplicate Filters content-level dedup -------------
    async def was_content_forwarded(self, rule_id: str, content_hash: str) -> bool:
        """True when this rule already forwarded this exact content fingerprint."""
        query = "SELECT 1 FROM content_dedup WHERE rule_id = %s AND content_hash = %s"
        row = await self._fetch_one(query, (rule_id, content_hash))
        return row is not None

    async def record_content_hash(self, rule_id: str, content_hash: str) -> None:
        """Record a fingerprint. ON CONFLICT DO NOTHING keeps replay idempotent."""
        now = int(time.time())
        query = """
        INSERT INTO content_dedup (rule_id, content_hash, created_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (rule_id, content_hash) DO NOTHING
        """
        await _execute_db(self._db, query, (rule_id, content_hash, now))

    async def purge_content_hashes(self, rule_id: str, before_ts: int) -> int:
        query = "DELETE FROM content_dedup WHERE rule_id = %s AND created_at < %s"
        res = await _execute_db(self._db, query, (rule_id, int(before_ts)))
        if isinstance(res, str) and res.startswith("DELETE "):
            return int(res.split()[-1])
        count = getattr(res, "rowcount", None)
        return count if count is not None else 0

    async def claim_content_hash(self, rule_id: str, content_hash: str) -> bool:
        """Atomic claim decided by the UNIQUE(rule_id, content_hash) index."""
        now = int(time.time())
        query = """
        INSERT INTO content_dedup (rule_id, content_hash, created_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (rule_id, content_hash) DO NOTHING
        RETURNING 1
        """
        row = await self._fetch_one(query, (rule_id, content_hash, now))
        return row is not None

    async def release_content_hash(self, rule_id: str, content_hash: str) -> None:
        """Give a claim back when the message was never delivered."""
        query = "DELETE FROM content_dedup WHERE rule_id = %s AND content_hash = %s"
        await _execute_db(self._db, query, (rule_id, content_hash))

    async def purge_content_hashes_all(self, before_ts: int) -> int:
        query = "DELETE FROM content_dedup WHERE created_at < %s"
        res = await _execute_db(self._db, query, (int(before_ts),))
        if isinstance(res, str) and res.startswith("DELETE "):
            return int(res.split()[-1])
        count = getattr(res, "rowcount", None)
        return count if count is not None else 0


class PostgresDeliveryQueueRepository(IDeliveryQueueRepository):
    """PostgreSQL implementation of IDeliveryQueueRepository."""

    def __init__(self, db: PostgresConnection) -> None:
        self._db = db

    def _row_to_entity(self, row: Any) -> DeliveryJob:
        d = dict(row) if hasattr(row, "keys") else row
        status_val = d.get("status", "PENDING")
        try:
            status = DeliveryStatus(status_val)
        except (ValueError, TypeError):
            status = DeliveryStatus.PENDING

        payload_val = d.get("payload_json", {})
        if isinstance(payload_val, str):
            try:
                payload = json.loads(payload_val)
            except Exception:
                payload = {}
        elif isinstance(payload_val, dict):
            payload = payload_val
        else:
            payload = {}

        return DeliveryJob(
            id=d["id"],
            rule_id=d["rule_id"],
            source_chat_id=str(d["source_chat_id"]),
            source_message_id=int(d["source_message_id"]),
            target_chat_id=str(d["target_chat_id"]),
            payload_data=payload,
            status=status,
            attempts=int(d.get("attempts", 0) or 0),
            max_attempts=int(d.get("max_attempts", 5) or 5),
            next_retry_at=float(d.get("next_retry_at", 0.0) or 0.0),
            lease_until=float(d.get("lease_until", 0.0) or 0.0),
            worker_id=d.get("worker_id"),
            error_detail=d.get("error_detail"),
            created_at=int(d.get("created_at", 0) or 0),
            updated_at=int(d.get("updated_at", 0) or 0),
            intermediate_chat_id=str(d.get("intermediate_chat_id")) if d.get("intermediate_chat_id") else None,
            intermediate_message_id=int(d["intermediate_message_id"]) if d.get("intermediate_message_id") is not None else None,
            delivery_stage=str(d.get("delivery_stage") or "DIRECT"),
            job_kind=str(d.get("job_kind") or "forward"),
        )

    async def _fetch_all(self, query: str, params: tuple = ()) -> list:
        return await _fetch_all_db(self._db, query, params)

    async def _fetch_one(self, query: str, params: tuple = ()) -> Optional[dict]:
        return await _fetch_one_db(self._db, query, params)

    async def enqueue(self, job: DeliveryJob) -> bool:
        payload_str = json.dumps(job.payload_data)
        status_str = getattr(job.status, "value", str(job.status))
        query = """
        INSERT INTO delivery_jobs (
            id, rule_id, source_chat_id, source_message_id, target_chat_id,
            payload_json, status, attempts, max_attempts, next_retry_at,
            lease_until, worker_id, error_detail, created_at, updated_at, job_kind
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(rule_id, source_chat_id, source_message_id, target_chat_id, job_kind) DO NOTHING
        """
        res = await _execute_db(
            self._db,
            query,
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
                str(getattr(job, "job_kind", "forward") or "forward"),
            ),
        )
        if isinstance(res, str) and res.startswith("INSERT "):
            return int(res.split()[-1]) > 0
        count = getattr(res, "rowcount", None)
        return count > 0 if count is not None else True

    async def claim_batch(
        self, worker_id: str, batch_size: int = 5, lease_duration: float = 30.0
    ) -> List[DeliveryJob]:
        now = time.time()
        query = """
        UPDATE delivery_jobs
        SET status = 'CLAIMED', worker_id = %s, lease_until = %s, updated_at = %s
        WHERE id IN (
            SELECT id FROM delivery_jobs AS j
            WHERE (
                j.status = 'PENDING'
                OR j.status = 'RETRY_WAIT'
                OR (j.status = 'CLAIMED' AND j.lease_until < %s)
            )
            AND j.next_retry_at <= %s
            -- One in-flight job per destination, and the destination's own
            -- source order: a burst must arrive in the order it was posted.
            -- Different targets still claim in parallel.
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
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """
        lease_until = now + lease_duration
        now_int = int(now)
        try:
            rows = await self._fetch_all(query, (worker_id, lease_until, now_int, now, now, int(batch_size)))
            return [self._row_to_entity(r) for r in rows]
        except Exception:
            # Fallback if FOR UPDATE SKIP LOCKED is not supported in mock
            sel_query = """
            SELECT * FROM delivery_jobs
            WHERE (
                status = 'PENDING'
                OR status = 'RETRY_WAIT'
                OR (status = 'CLAIMED' AND lease_until < %s)
            )
            AND next_retry_at <= %s
            ORDER BY created_at ASC
            LIMIT %s
            """
            rows = await self._fetch_all(sel_query, (now, now, int(batch_size)))
            claimed = []
            for r in rows:
                d = dict(r) if hasattr(r, "keys") else r
                upd_query = """
                UPDATE delivery_jobs
                SET status = 'CLAIMED', worker_id = %s, lease_until = %s, updated_at = %s
                WHERE id = %s
                """
                await _execute_db(self._db, upd_query, (worker_id, lease_until, now_int, d["id"]))
                entity = self._row_to_entity(d)
                entity.status = DeliveryStatus.CLAIMED
                entity.worker_id = worker_id
                entity.lease_until = lease_until
                claimed.append(entity)
            return claimed

    async def mark_sent(self, job_id: str, target_message_id: Optional[int] = None) -> bool:
        now = int(time.time())
        query = "UPDATE delivery_jobs SET status = 'SENT', lease_until = 0, updated_at = %s WHERE id = %s"
        res = await _execute_db(self._db, query, (now, job_id))
        if isinstance(res, str) and res.startswith("UPDATE "):
            return int(res.split()[-1]) > 0
        count = getattr(res, "rowcount", None)
        return count > 0 if count is not None else True

    async def mark_sent_batch(self, job_ids: List[tuple]) -> int:
        """Mark a batch SENT in one statement (v14).

        Each entry is ``(job_id, target_message_id)``. SKIP LOCKED is already
        used at claim time so two workers never hold the same job; here the
        batch belongs to one worker.
        """
        if not job_ids:
            return 0
        now = int(time.time())
        ids = [jid for jid, _ in job_ids]
        placeholders = ",".join("%s" for _ in ids)
        query = f"""
        UPDATE delivery_jobs
        SET status = 'SENT', lease_until = 0, updated_at = %s
        WHERE id IN ({placeholders})
        """
        try:
            res = await _execute_db(self._db, query, (now, *ids))
        except Exception:
            return 0
        if isinstance(res, str) and res.startswith("UPDATE "):
            return int(res.split()[-1])
        return getattr(res, "rowcount", 0) or 0

    async def mark_retry(self, job_id: str, error: str, backoff_seconds: float) -> bool:
        now = time.time()
        row = await self._fetch_one("SELECT attempts, max_attempts FROM delivery_jobs WHERE id = %s", (job_id,))
        if not row:
            return False
        d = dict(row) if hasattr(row, "keys") else row
        attempts = int(d.get("attempts", 0)) + 1
        max_attempts = int(d.get("max_attempts", 5))

        if attempts >= max_attempts:
            # v14: route the exhausted job to the dead-letter queue.
            await self._dead_letter_row(job_id, error, "DELIVERY_EXHAUSTED", now, attempts)
            query = """
            UPDATE delivery_jobs
            SET status = 'FAILED', attempts = %s, error_detail = %s, lease_until = 0, updated_at = %s
            WHERE id = %s
            """
            await _execute_db(self._db, query, (attempts, error, int(now), job_id))
        else:
            next_retry = now + backoff_seconds
            query = """
            UPDATE delivery_jobs
            SET status = 'RETRY_WAIT', attempts = %s, next_retry_at = %s,
                error_detail = %s, lease_until = 0, updated_at = %s
            WHERE id = %s
            """
            await _execute_db(self._db, query, (attempts, next_retry, error, int(now), job_id))
        return True

    async def defer(self, job_id: str, reason: str, backoff_seconds: float) -> bool:
        """Postpone without consuming an attempt (see the interface docstring)."""
        now = time.time()
        next_retry = now + max(0.0, float(backoff_seconds))
        query = """
        UPDATE delivery_jobs
        SET status = 'RETRY_WAIT', next_retry_at = %s, error_detail = %s,
            lease_until = 0, updated_at = %s
        WHERE id = %s AND status NOT IN ('SENT', 'FAILED')
        """
        res = await _execute_db(self._db, query, (next_retry, reason, int(now), job_id))
        return bool(getattr(res, "rowcount", 1))

    async def mark_failed(self, job_id: str, error: str) -> bool:
        now = int(time.time())
        query = "UPDATE delivery_jobs SET status = 'FAILED', error_detail = %s, lease_until = 0, updated_at = %s WHERE id = %s"
        await _execute_db(self._db, query, (error, now, job_id))
        return True

    async def mark_intermediate_copied(self, job_id: str, inter_chat: str, inter_msg_id: int) -> bool:
        now = int(time.time())
        query = """
        UPDATE delivery_jobs
        SET status = 'COPIED_TO_C', delivery_stage = 'COPIED_TO_C',
            intermediate_chat_id = %s, intermediate_message_id = %s, updated_at = %s
        WHERE id = %s
        """
        res = await _execute_db(self._db, query, (str(inter_chat), int(inter_msg_id), now, job_id))
        return True

    async def _dead_letter_row(
        self, job_id: str, error: str, category: str, now: float, attempts: int = 0
    ) -> bool:
        """Write the dead-letter entry for a job; the ONLY producer.

        Mirrors `SqliteDeliveryQueueRepository._dead_letter_row` column for
        column. `attempts` is passed in rather than copied from the job row,
        which is updated after this runs — copying it recorded one attempt fewer
        than the delivery had used.
        """
        try:
            ins_query = """
            INSERT INTO dead_letter_queue (
                id, job_id, rule_id, source_chat_id, source_message_id,
                target_chat_id, payload_json, attempts, last_error,
                error_category, dead_lettered_at
            )
            SELECT %s, id, rule_id, source_chat_id, source_message_id,
                   target_chat_id, payload_json, %s, %s, %s, %s
            FROM delivery_jobs WHERE id = %s
            ON CONFLICT (id) DO UPDATE SET
                last_error = EXCLUDED.last_error,
                attempts = EXCLUDED.attempts,
                error_category = EXCLUDED.error_category,
                dead_lettered_at = EXCLUDED.dead_lettered_at
            """
            await _execute_db(
                self._db,
                ins_query,
                (f"dlq_{job_id}", int(attempts), error, category, int(now), job_id),
            )
            row = await self._fetch_one(
                "SELECT 1 AS ok FROM dead_letter_queue WHERE id = %s", (f"dlq_{job_id}",)
            )
            if row is None:
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
        now = int(time.time())
        current = await self._fetch_one("SELECT attempts FROM delivery_jobs WHERE id = %s", (job_id,))
        attempts = int(dict(current).get("attempts", 0)) if current is not None else 0
        recorded = await self._dead_letter_row(job_id, error, category, now, attempts)
        query = "UPDATE delivery_jobs SET status = 'DEAD_LETTER', error_detail = %s, lease_until = 0, updated_at = %s WHERE id = %s"
        await _execute_db(self._db, query, (error, now, job_id))
        return recorded

    # ------------------------------------------------------------------ #
    # Dead-letter queue (owner-scoped read / replay / purge)
    #
    # Same contract and same result codes as the SQLite implementation; the
    # callers (bot, servicer) do not know which backend they are on.
    # ------------------------------------------------------------------ #

    def _dlq_scope_clause(self, rule_ids: Optional[Sequence[str]]) -> tuple:
        """``(sql_fragment, params)`` for a tenant scope.

        ``None`` is unscoped for an internal caller; an empty scope matches
        nothing rather than degrading into "no filter".
        """
        if rule_ids is None:
            return "", ()
        ids = [str(r) for r in rule_ids if r]
        if not ids:
            return " AND 1=0", ()
        return f" AND rule_id IN ({','.join(['%s'] * len(ids))})", tuple(ids)

    async def list_dead_letters(
        self,
        rule_ids: Optional[Sequence[str]] = None,
        limit: int = 20,
        rule_id: str = "",
        since: int = 0,
    ) -> List[Dict[str, Any]]:
        scope_sql, scope_params = self._dlq_scope_clause(rule_ids)
        params: tuple = scope_params
        if rule_id:
            scope_sql += " AND rule_id=%s"
            params = params + (str(rule_id),)
        if since:
            scope_sql += " AND dead_lettered_at>=%s"
            params = params + (int(since),)
        try:
            rows = await self._fetch_all(
                "SELECT * FROM dead_letter_queue WHERE 1=1" + scope_sql
                + " ORDER BY dead_lettered_at DESC LIMIT %s",
                params + (max(int(limit), 1),),
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("dead-letter list failed: %s", exc)
            return []

    async def count_dead_letters(self, rule_ids: Optional[Sequence[str]] = None) -> int:
        scope_sql, scope_params = self._dlq_scope_clause(rule_ids)
        try:
            row = await self._fetch_one(
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
        try:
            rows = await self._fetch_all(
                "SELECT * FROM dead_letter_queue WHERE id=%s" + scope_sql,
                (str(entry_id),) + scope_params,
            )
        except Exception as exc:
            logger.warning("dead letter %s could not be read: %s", entry_id, exc)
            return "replay_failed"
        if not rows:
            return "not_found"
        entry = dict(rows[0])

        rule_id = str(entry.get("rule_id") or "")
        src_chat = str(entry.get("source_chat_id") or "")
        src_msg = int(entry.get("source_message_id") or 0)
        tgt_chat = str(entry.get("target_chat_id") or "")
        job_id = str(entry.get("job_id") or "")

        # delivery_jobs is UNIQUE(rule_id, source_chat_id, source_message_id,
        # target_chat_id), so a replay wakes the one delivery up instead of
        # adding a second: that is what makes it unable to double-post.
        original = None
        if job_id:
            original = await self._fetch_one("SELECT * FROM delivery_jobs WHERE id=%s", (job_id,))
        if original is None:
            original = await self._fetch_one(
                "SELECT * FROM delivery_jobs WHERE rule_id=%s AND source_chat_id=%s"
                " AND source_message_id=%s AND target_chat_id=%s",
                (rule_id, src_chat, src_msg, tgt_chat),
            )

        now = int(time.time())
        try:
            if original is not None:
                row = dict(original)
                status = str(row.get("status") or "").upper()
                if status in ("PENDING", "CLAIMED", "RETRY_WAIT", "PROCESSING"):
                    return "already_pending"
                if status == "SENT":
                    return "already_sent"
                max_attempts = int(row.get("max_attempts") or 0) or DeliveryJob().max_attempts
                await _execute_db(
                    self._db,
                    "UPDATE delivery_jobs SET status = 'PENDING', attempts = 0, max_attempts = %s,"
                    " next_retry_at = 0, lease_until = 0, worker_id = '', error_detail = '',"
                    " updated_at = %s WHERE id = %s",
                    (max(1, max_attempts), now, row.get("id")),
                )
            else:
                max_attempts = max(1, int(entry.get("attempts") or 0) or DeliveryJob().max_attempts)
                payload = entry.get("payload_json") or "{}"
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload)
                await _execute_db(
                    self._db,
                    "INSERT INTO delivery_jobs ("
                    " id, rule_id, source_chat_id, source_message_id, target_chat_id,"
                    " payload_json, status, attempts, max_attempts, next_retry_at,"
                    " lease_until, worker_id, error_detail, created_at, updated_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, 'PENDING', 0, %s, 0, 0, '', '', %s, %s)"
                    " ON CONFLICT (rule_id, source_chat_id, source_message_id, target_chat_id) DO NOTHING",
                    (
                        job_id or f"replay_{entry_id}", rule_id, src_chat, src_msg, tgt_chat,
                        payload, max(1, max_attempts), now, now,
                    ),
                )
            await _execute_db(self._db, "DELETE FROM dead_letter_queue WHERE id=%s", (str(entry_id),))
        except Exception as exc:
            logger.warning("replay of dead letter %s failed: %s", entry_id, exc)
            return "replay_failed"
        logger.info(
            "dead letter %s replayed (rule=%s target=%s)", entry_id, rule_id, tgt_chat
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
            sql += " AND rule_id=%s"
            params = params + (str(rule_id),)
        if older_than:
            sql += " AND dead_lettered_at<%s"
            params = params + (int(older_than),)
        try:
            res = await _execute_db(self._db, sql, params)
            if isinstance(res, str) and res.upper().startswith("DELETE"):
                return int(res.split()[-1])
            count = getattr(res, "rowcount", None)
            return int(count) if count is not None else 0
        except Exception:
            return 0

    async def get_dead_letter_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self.list_dead_letters(None, limit)

    async def mark_unknown(self, job_id: str, error: str) -> bool:
        now = int(time.time())
        query = "UPDATE delivery_jobs SET status = 'UNKNOWN', error_detail = %s, lease_until = 0, updated_at = %s WHERE id = %s"
        await _execute_db(self._db, query, (error, now, job_id))
        return True

    async def get_pending_count(self) -> int:
        query = "SELECT COUNT(*) as cnt FROM delivery_jobs WHERE status IN ('PENDING', 'CLAIMED', 'RETRY_WAIT')"
        row = await self._fetch_one(query, ())
        if not row:
            return 0
        d = dict(row) if hasattr(row, "keys") else row
        return int(d.get("cnt", 0))

    async def get_stats(self) -> dict:
        query = "SELECT status, COUNT(*) as cnt FROM delivery_jobs GROUP BY status"
        rows = await self._fetch_all(query, ())
        res = {s.value: 0 for s in DeliveryStatus}
        for r in rows:
            d = dict(r) if hasattr(r, "keys") else r
            res[d["status"]] = int(d["cnt"])
        return res

    async def counts_for_rule(self, rule_id: str) -> dict:
        """Per-status delivery counts for one rule (the status screen)."""
        if not rule_id:
            return {}
        rows = await self._fetch_all(
            "SELECT status, COUNT(*) as cnt FROM delivery_jobs WHERE rule_id = %s GROUP BY status",
            (str(rule_id),),
        )
        res: dict = {}
        for r in rows or ():
            d = dict(r) if hasattr(r, "keys") else r
            res[str(d["status"])] = int(d["cnt"])
        return res

    async def recover_expired_leases(self) -> int:
        now = time.time()
        query = """
        UPDATE delivery_jobs
        SET status = 'RETRY_WAIT', lease_until = 0, updated_at = %s
        WHERE status = 'CLAIMED' AND lease_until < %s
        """
        res = await _execute_db(self._db, query, (int(now), now))
        if isinstance(res, str) and res.startswith("UPDATE "):
            return int(res.split()[-1])
        count = getattr(res, "rowcount", None)
        return count if count is not None else 0

    async def get_queue_age_metrics(self) -> dict:
        query = """
        SELECT MIN(created_at) as oldest_ts, COUNT(*) as cnt
        FROM delivery_jobs
        WHERE status IN ('PENDING', 'CLAIMED', 'RETRY_WAIT')
        """
        row = await self._fetch_one(query, ())
        if not row:
            return {"pending_count": 0, "oldest_job_age_seconds": 0.0}
        d = dict(row) if hasattr(row, "keys") else row
        cnt = int(d.get("cnt", 0) or 0)
        oldest_ts = d.get("oldest_ts")
        age = max(0.0, time.time() - float(oldest_ts)) if oldest_ts else 0.0
        return {"pending_count": cnt, "oldest_job_age_seconds": round(age, 2)}

    async def cleanup(self, retention_seconds: int = 60 * 60 * 24 * 7) -> int:
        threshold = int(time.time() - retention_seconds)
        query = "DELETE FROM delivery_jobs WHERE status IN ('SENT', 'FAILED') AND updated_at < %s"
        res = await _execute_db(self._db, query, (threshold,))
        if isinstance(res, str) and res.startswith("DELETE "):
            return int(res.split()[-1])
        count = getattr(res, "rowcount", None)
        return count if count is not None else 0


class PostgresPromptRepository(IPromptRepository):
    """Production-grade PostgreSQL repository for prompt templates and historic versions."""

    DDL_SCHEMA = """
    CREATE TABLE IF NOT EXISTS prompt_templates (
        id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        description TEXT DEFAULT '',
        system_prompt TEXT NOT NULL,
        user_prompt_template TEXT NOT NULL DEFAULT '{text}',
        target_language VARCHAR(32) DEFAULT 'en',
        current_version INTEGER NOT NULL DEFAULT 1,
        is_system BOOLEAN NOT NULL DEFAULT FALSE,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        owner_user_id BIGINT NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pg_prompt_name ON prompt_templates (name);

    CREATE TABLE IF NOT EXISTS prompt_versions (
        id VARCHAR(64) PRIMARY KEY,
        prompt_id VARCHAR(64) NOT NULL REFERENCES prompt_templates(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        system_prompt TEXT NOT NULL,
        user_prompt_template TEXT NOT NULL,
        change_summary TEXT DEFAULT '',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at BIGINT NOT NULL,
        UNIQUE(prompt_id, version)
    );
    CREATE INDEX IF NOT EXISTS idx_pg_prompt_ver ON prompt_versions (prompt_id, version);
    CREATE INDEX IF NOT EXISTS idx_pg_prompt_active ON prompt_versions (prompt_id, is_active);
    """

    def __init__(self, db: PostgresConnection) -> None:
        self._db = db

    async def init_schema(self) -> None:
        if hasattr(self._db, "execute"):
            for stmt in self.DDL_SCHEMA.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await _execute_db(self._db, stmt)
            # v12: owner scoping is additive. CREATE TABLE IF NOT EXISTS does
            # not upgrade a table created before v12, so add the column
            # idempotently here (PostgreSQL supports ADD COLUMN IF NOT EXISTS;
            # SQLite does not, so the sqlite adapter has its own path).
            for stmt in self.ADDITIVE_COLUMNS.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await _execute_db(self._db, stmt)

    # v12: guarded ALTERs so an existing pre-v12 schema is upgraded in place.
    ADDITIVE_COLUMNS = """
    ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS owner_user_id BIGINT NOT NULL DEFAULT 0;
    CREATE INDEX IF NOT EXISTS idx_pg_prompt_templates_owner ON prompt_templates (owner_user_id);
    """

    async def add_template(self, template: PromptTemplate) -> PromptTemplate:
        query = """
        INSERT INTO prompt_templates (
            id, name, description, system_prompt, user_prompt_template,
            target_language, current_version, is_system, created_at, updated_at, owner_user_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        params = (
            template.id, template.name, template.description, template.system_prompt,
            template.user_prompt_template, template.target_language, template.current_version,
            bool(template.is_system), int(template.created_at), int(template.updated_at),
            int(getattr(template, "owner_user_id", 0) or 0),
        )
        await _execute_db(self._db, query, params)
        return template

    async def update_template(self, template: PromptTemplate) -> PromptTemplate:
        template.updated_at = int(time.time())
        query = """
        UPDATE prompt_templates
        SET name = %s, description = %s, system_prompt = %s, user_prompt_template = %s,
            target_language = %s, current_version = %s, is_system = %s, updated_at = %s,
            owner_user_id = %s
        WHERE id = %s
        """
        params = (
            template.name, template.description, template.system_prompt,
            template.user_prompt_template, template.target_language,
            template.current_version, bool(template.is_system),
            template.updated_at, int(getattr(template, "owner_user_id", 0) or 0),
            template.id,
        )
        await _execute_db(self._db, query, params)
        return template

    async def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        query = "SELECT * FROM prompt_templates WHERE id = %s"
        row = await _fetch_one_db(self._db, query, (template_id,))
        if not row:
            return None
        return self._row_to_template(row)

    async def list_templates(self) -> List[PromptTemplate]:
        query = "SELECT * FROM prompt_templates ORDER BY created_at ASC"
        rows = await _fetch_all_db(self._db, query, ())
        return [self._row_to_template(r) for r in rows]

    async def list_templates_by_owner(self, owner_user_id: int) -> List[PromptTemplate]:
        # v12: system prompts stay shared; user prompts are per-tenant.
        query = (
            "SELECT * FROM prompt_templates WHERE owner_user_id = %s OR is_system = TRUE"
            " ORDER BY created_at ASC"
        )
        rows = await _fetch_all_db(self._db, query, (int(owner_user_id),))
        return [self._row_to_template(r) for r in rows]

    def _row_to_template(self, r) -> PromptTemplate:
        d = dict(r) if hasattr(r, "keys") else r
        return PromptTemplate(
            id=d["id"], name=d["name"], description=d.get("description", ""),
            system_prompt=d["system_prompt"], user_prompt_template=d.get("user_prompt_template", "{text}"),
            target_language=d.get("target_language", "en"), current_version=int(d["current_version"]),
            is_system=bool(d["is_system"]), created_at=int(d["created_at"]), updated_at=int(d["updated_at"]),
            owner_user_id=int(d.get("owner_user_id") or 0),
        )

    async def delete_template(self, template_id: str) -> bool:
        await _execute_db(self._db, "DELETE FROM prompt_versions WHERE prompt_id = %s", (template_id,))
        res = await _execute_db(self._db, "DELETE FROM prompt_templates WHERE id = %s", (template_id,))
        if isinstance(res, str) and res.startswith("DELETE "):
            return int(res.split()[-1]) > 0
        count = getattr(res, "rowcount", None)
        return count > 0 if count is not None else True

    async def add_version(self, version: PromptVersion) -> PromptVersion:
        query = """
        INSERT INTO prompt_versions (
            id, prompt_id, version, system_prompt, user_prompt_template,
            change_summary, is_active, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        params = (
            version.id, version.prompt_id, version.version, version.system_prompt,
            version.user_prompt_template, version.change_summary,
            bool(version.is_active), int(version.created_at),
        )
        await _execute_db(self._db, query, params)
        return version

    async def get_version(self, prompt_id: str, version: int) -> Optional[PromptVersion]:
        query = "SELECT * FROM prompt_versions WHERE prompt_id = %s AND version = %s"
        row = await _fetch_one_db(self._db, query, (prompt_id, version))
        if not row:
            return None
        d = dict(row) if hasattr(row, "keys") else row
        return PromptVersion(
            id=d["id"], prompt_id=d["prompt_id"], version=int(d["version"]),
            system_prompt=d["system_prompt"], user_prompt_template=d["user_prompt_template"],
            change_summary=d.get("change_summary", ""), is_active=bool(d["is_active"]),
            created_at=int(d["created_at"]),
        )

    async def list_versions(self, prompt_id: str) -> List[PromptVersion]:
        query = "SELECT * FROM prompt_versions WHERE prompt_id = %s ORDER BY version DESC"
        rows = await _fetch_all_db(self._db, query, (prompt_id,))
        res = []
        for r in rows:
            d = dict(r) if hasattr(r, "keys") else r
            res.append(PromptVersion(
                id=d["id"], prompt_id=d["prompt_id"], version=int(d["version"]),
                system_prompt=d["system_prompt"], user_prompt_template=d["user_prompt_template"],
                change_summary=d.get("change_summary", ""), is_active=bool(d["is_active"]),
                created_at=int(d["created_at"]),
            ))
        return res

    async def activate_version(self, prompt_id: str, version: int) -> bool:
        ver = await self.get_version(prompt_id, version)
        if not ver:
            return False
        now = int(time.time())
        await _execute_db(
            self._db,
            """
            UPDATE prompt_templates
            SET system_prompt = %s, user_prompt_template = %s, current_version = %s, updated_at = %s
            WHERE id = %s
            """,
            (ver.system_prompt, ver.user_prompt_template, version, now, prompt_id),
        )
        await _execute_db(
            self._db,
            "UPDATE prompt_versions SET is_active = (version = %s) WHERE prompt_id = %s",
            (version, prompt_id),
        )
        return True

    async def get_active_version(self, prompt_id: str) -> Optional[PromptVersion]:
        query = "SELECT * FROM prompt_versions WHERE prompt_id = %s AND is_active = TRUE LIMIT 1"
        row = await _fetch_one_db(self._db, query, (prompt_id,))
        if not row:
            return None
        d = dict(row)
        return PromptVersion(
            id=d["id"],
            prompt_id=d["prompt_id"],
            version=int(d["version"]),
            system_prompt=d["system_prompt"],
            user_prompt_template=d["user_prompt_template"],
            change_summary=d.get("change_summary", ""),
            is_active=bool(d["is_active"]),
            created_at=int(d["created_at"]),
        )


async def initialize_postgres_schema(db: Any) -> None:
    """Ensure all required PostgreSQL tables, indexes, and additive columns exist.

    Idempotent: safe to run on every startup against existing or clean databases.
    """
    ddl_statements = [
        # 1. Delivery jobs queue
        """
        CREATE TABLE IF NOT EXISTS delivery_jobs (
            id TEXT PRIMARY KEY,
            rule_id TEXT NOT NULL,
            source_chat_id TEXT NOT NULL,
            source_message_id BIGINT NOT NULL,
            target_chat_id TEXT NOT NULL,
            payload_json JSONB NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            next_retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
            worker_id TEXT NOT NULL DEFAULT '',
            error_detail TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            intermediate_chat_id TEXT NOT NULL DEFAULT '',
            intermediate_message_id BIGINT,
            delivery_stage TEXT NOT NULL DEFAULT 'DIRECT',
            -- The deliverable kind ("forward" | "keyword_reply") is part of the
            -- dedup key: one source message carries a forward *and* a keyword
            -- reply to the same target, and with the kind left out of the key
            -- the reply collided with the forward's row and was dropped by
            -- ON CONFLICT DO NOTHING. Fresh databases get the column and the
            -- widened key here; an existing table is healed by the guarded
            -- additive ALTER below, which also swaps the constraint once.
            job_kind TEXT NOT NULL DEFAULT 'forward',
            CONSTRAINT delivery_jobs_unique
                UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id, job_kind)
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_jobs_claim
            ON delivery_jobs (status, next_retry_at, lease_until, created_at);
        CREATE INDEX IF NOT EXISTS idx_delivery_jobs_rule ON delivery_jobs (rule_id);
        CREATE INDEX IF NOT EXISTS idx_delivery_jobs_cleanup
            ON delivery_jobs (status, updated_at);
        """,
        # 2. Message map for reply/edit/delete sync
        """
        CREATE TABLE IF NOT EXISTS message_map (
            id TEXT PRIMARY KEY,
            rule_id TEXT NOT NULL,
            source_chat_id TEXT NOT NULL,
            source_message_id BIGINT NOT NULL,
            target_chat_id TEXT NOT NULL,
            target_message_id BIGINT,
            media_group_id TEXT NOT NULL DEFAULT '',
            delivery_status TEXT NOT NULL DEFAULT 'PENDING',
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            CONSTRAINT message_map_unique UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id)
        );
        CREATE INDEX IF NOT EXISTS idx_msgmap_src ON message_map (source_chat_id, source_message_id);
        CREATE INDEX IF NOT EXISTS idx_msgmap_tgt ON message_map (target_chat_id, target_message_id);
        CREATE INDEX IF NOT EXISTS idx_msgmap_media ON message_map (media_group_id);
        """,
        # 3. Dead letter queue
        """
        CREATE TABLE IF NOT EXISTS dead_letter_queue (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            source_chat_id TEXT NOT NULL,
            source_message_id BIGINT NOT NULL,
            target_chat_id TEXT NOT NULL,
            payload_json JSONB NOT NULL DEFAULT '{}',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            error_category TEXT NOT NULL DEFAULT '',
            dead_lettered_at BIGINT NOT NULL,
            -- Legacy: this backend's older name for `last_error`, kept so no
            -- deployed row loses its error text. Nothing reads or writes it any
            -- more; the contract is `last_error` on both backends.
            error_message TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_dlq_rule ON dead_letter_queue (rule_id, dead_lettered_at);
        CREATE INDEX IF NOT EXISTS idx_dlq_ts ON dead_letter_queue (dead_lettered_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dlq_job ON dead_letter_queue (job_id);
        """,
        # 4. Content fingerprint deduplication (v2.0)
        """
        CREATE TABLE IF NOT EXISTS content_dedup (
            id BIGSERIAL PRIMARY KEY,
            rule_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            CONSTRAINT content_dedup_unique UNIQUE (rule_id, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_content_dedup_rule ON content_dedup (rule_id, created_at);
        """,
        # 5. Forward rules
        PostgresForwardRuleRepository.DDL_SCHEMA,
        # 6. Prompt templates & versions
        PostgresPromptRepository.DDL_SCHEMA,
        # 7. Rule stats, error log, and hourly metrics
        """
        CREATE TABLE IF NOT EXISTS rule_stats (
            rule_id TEXT PRIMARY KEY,
            forwarded BIGINT NOT NULL DEFAULT 0,
            filtered BIGINT NOT NULL DEFAULT 0,
            errors BIGINT NOT NULL DEFAULT 0,
            last_forward_ts BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS metrics_hourly (
            ts_hour BIGINT PRIMARY KEY,
            forwarded BIGINT NOT NULL DEFAULT 0,
            filtered BIGINT NOT NULL DEFAULT 0,
            errors BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS error_log (
            id BIGSERIAL PRIMARY KEY,
            ts BIGINT NOT NULL,
            user_id BIGINT,
            session_id TEXT,
            rule_id TEXT,
            category TEXT NOT NULL,
            error_name TEXT NOT NULL,
            severity TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            recoverable INTEGER NOT NULL DEFAULT 0,
            chat_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pg_error_ts ON error_log(ts);
        CREATE INDEX IF NOT EXISTS idx_pg_error_category ON error_log(category, severity);
        CREATE INDEX IF NOT EXISTS idx_pg_error_rule ON error_log(rule_id);
        """,
        # 8. Additive columns for safe upgrades
        """
        ALTER TABLE forward_rules ADD COLUMN IF NOT EXISTS owner_user_id BIGINT NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_pg_fwd_owner ON forward_rules (owner_user_id);
        CREATE INDEX IF NOT EXISTS idx_pg_fwd_session_owner ON forward_rules (session_id, owner_user_id);
        ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS owner_user_id BIGINT NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_pg_prompt_templates_owner ON prompt_templates (owner_user_id);
        -- Dead-letter contract (must match the SQLite schema exactly: the same
        -- repository code writes both, so a column named differently on one
        -- backend is a write that silently does nothing on that backend).
        ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT '';
        ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
        UPDATE dead_letter_queue SET last_error = error_message WHERE last_error = '' AND error_message <> '';
        -- v20 mirror: widen the queue's dedup key to include the deliverable
        -- kind. On SQLite this needs a table rebuild; on PostgreSQL the column
        -- is additive and the UNIQUE constraint can be swapped in place. A
        -- database created before v20 otherwise enqueues a keyword_reply
        -- against a key that does not contain it, so the reply collides with
        -- the forward's row and ON CONFLICT DO NOTHING drops it — the exact
        -- defect the SQLite rebuild fixed, left un-mirrored here.
        ALTER TABLE delivery_jobs ADD COLUMN IF NOT EXISTS job_kind TEXT NOT NULL DEFAULT 'forward';
        -- The constraint swap is guarded by name *and* definition, so an
        -- already-widened database runs it as a no-op instead of dropping and
        -- recreating the index on every boot (an exclusive constraint rebuild
        -- on a live queue).
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'delivery_jobs_unique'
                  AND pg_get_constraintdef(oid)
                      LIKE '%target_chat_id, job_kind%'
            ) THEN
                ALTER TABLE delivery_jobs DROP CONSTRAINT IF EXISTS delivery_jobs_unique;
                ALTER TABLE delivery_jobs ADD CONSTRAINT delivery_jobs_unique
                    UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id, job_kind);
            END IF;
        END $$;
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
        -- v22: single-use dashboard login codes (mirror of the SQLite table).
        -- code_hash is SHA-256 of the code; used_at makes it single-use.
        CREATE TABLE IF NOT EXISTS web_login_codes (
            code_hash TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            created_at BIGINT NOT NULL,
            expires_at BIGINT NOT NULL,
            used_at BIGINT,
            purpose TEXT NOT NULL DEFAULT 'dashboard'
        );
        CREATE INDEX IF NOT EXISTS idx_pg_web_login_codes_user ON web_login_codes (user_id);
        CREATE INDEX IF NOT EXISTS idx_pg_web_login_codes_created ON web_login_codes (created_at);
        -- v23: the deployment's own secrets (mirror of the SQLite table). The
        -- session signing key lives here when no ATF_JWT_SECRET is configured:
        -- random, once, and never derived from anything public.
        CREATE TABLE IF NOT EXISTS app_secrets (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at BIGINT NOT NULL
        );
        -- v24 (mirror of the SQLite migration): the per-user task lifecycle.
        -- main.py binds PostgresUserTaskRepository whenever ATF_DATABASE_URL is
        -- set, so without this DDL every task operation on a PostgreSQL
        -- deployment fails with UndefinedTableError on first use. Types mirror
        -- the SQLite columns (BIGINT for the epoch timestamps and the owner id,
        -- TEXT with NOT NULL defaults elsewhere) so the repository SQL runs
        -- unchanged against either engine.
        CREATE TABLE IF NOT EXISTS user_tasks (
            id TEXT PRIMARY KEY,
            owner_user_id BIGINT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            idempotency_key TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            lease_token TEXT NOT NULL DEFAULT '',
            lease_until BIGINT NOT NULL DEFAULT 0,
            run_at BIGINT NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            started_at BIGINT NOT NULL DEFAULT 0,
            finished_at BIGINT NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_tasks_idem
            ON user_tasks (owner_user_id, kind, idempotency_key)
            WHERE idempotency_key != '';
        CREATE INDEX IF NOT EXISTS idx_user_tasks_owner
            ON user_tasks (owner_user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_tasks_pick
            ON user_tasks (status, run_at, created_at, id);
            -- v25 parity: the retention sweep (status, finished_at). Additive and
            -- reversible; only the cost of the sweep changes, never the data.
            CREATE INDEX IF NOT EXISTS idx_user_tasks_finished
                ON user_tasks(status, finished_at);
        """,
    ]

    for stmt in ddl_statements:
        for query in _split_sql_script(stmt):
            cleaned = query.strip()
            if cleaned:
                try:
                    await _execute_db(db, cleaned)
                except Exception as exc:
                    # Not fatal: "already exists" style races between replicas
                    # are expected. Anything else must still be visible — a
                    # swallowed CREATE TABLE is how the user_tasks table went
                    # missing on PostgreSQL deployments without a single
                    # warning. The statement text (never the data) is logged.
                    logger.warning(
                        "PostgreSQL schema statement failed (%s): %s",
                        type(exc).__name__, cleaned.split("\n", 1)[0][:160])






class PostgresUserTaskRepository(ITaskRepository):
    """UserTask repository for the PostgreSQL engine (same contract as the
    SQLite one: atomic claim via UPDATE ... RETURNING, partial idempotency
    index)."""

    def __init__(self, db: PostgresConnection) -> None:
        self._db = db

    @staticmethod
    def _row_to_entity(row: Any) -> UserTask:
        d = dict(row) if hasattr(row, "keys") else dict(zip(
            ["id", "owner_user_id", "kind", "status", "idempotency_key", "attempt",
             "max_attempts", "lease_token", "lease_until", "run_at", "payload_json",
             "result_json", "error", "error_code", "created_at", "updated_at",
             "started_at", "finished_at"], row)) if not hasattr(row, "keys") else dict(row)
        return UserTask(
            id=d["id"], owner_user_id=int(d["owner_user_id"]), kind=d["kind"],
            status=d["status"], idempotency_key=d["idempotency_key"],
            attempt=int(d["attempt"]), max_attempts=int(d["max_attempts"]),
            lease_token=d.get("lease_token", ""), lease_until=int(d.get("lease_until") or 0),
            run_at=int(d.get("run_at") or 0), payload_json=d.get("payload_json") or "{}",
            result_json=d.get("result_json") or "", error=d.get("error") or "",
            error_code=d.get("error_code") or "",
            created_at=int(d["created_at"]), updated_at=int(d["updated_at"]),
            started_at=int(d.get("started_at") or 0), finished_at=int(d.get("finished_at") or 0),
        )

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

    async def add(self, task: UserTask) -> UserTask:
        await _execute_db(
            self._db,
            "INSERT INTO user_tasks (id, owner_user_id, kind, status, idempotency_key,"
            " attempt, max_attempts, lease_token, lease_until, run_at, payload_json,"
            " result_json, error, error_code, created_at, updated_at, started_at, finished_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            self._params(task),
        )
        return task

    async def insert_if_absent(self, task: UserTask) -> Optional[UserTask]:
        try:
            return await self.add(task)
        except Exception as exc:  # unique violation on the partial idempotency index
            if _is_unique_violation(exc):
                row = await _fetch_one_db(
                    self._db,
                    "SELECT * FROM user_tasks WHERE owner_user_id=%s AND kind=%s"
                    " AND idempotency_key=%s",
                    (int(task.owner_user_id), task.kind, task.idempotency_key),
                )
                return self._row_to_entity(row) if row else task
            raise

    async def get(self, task_id: str) -> Optional[UserTask]:
        row = await _fetch_one_db(self._db, "SELECT * FROM user_tasks WHERE id=%s", (task_id,))
        return self._row_to_entity(row) if row else None

    async def list_for_owner(self, owner_user_id: int, status: str = "",
                             kind: str = "", limit: int = 50,
                             offset: int = 0) -> List[UserTask]:
        conds, params = ["owner_user_id=%s"], [int(owner_user_id)]
        if status:
            conds.append("status=%s"); params.append(status)
        if kind:
            conds.append("kind=%s"); params.append(kind)
        params += [max(1, min(int(limit), 200)), max(0, int(offset))]
        rows = await _fetch_all_db(
            self._db,
            "SELECT * FROM user_tasks WHERE " + " AND ".join(conds) +
            " ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
            tuple(params),
        )
        return [self._row_to_entity(r) for r in rows] if rows else []

    async def count_for_owner(self, owner_user_id: int, status: str = "",
                              kind: str = "") -> int:
        conds, params = ["owner_user_id=%s"], [int(owner_user_id)]
        if status:
            conds.append("status=%s"); params.append(status)
        if kind:
            conds.append("kind=%s"); params.append(kind)
        row = await _fetch_one_db(
            self._db, "SELECT COUNT(*) AS n FROM user_tasks WHERE " + " AND ".join(conds),
            tuple(params),
        )
        if row is None:
            return 0
        d = dict(row) if hasattr(row, "keys") else row
        return int(d["n"] if hasattr(d, "__getitem__") else 0)

    async def replace(self, task: UserTask) -> UserTask:
        await _execute_db(
            self._db,
            "UPDATE user_tasks SET owner_user_id=%s, kind=%s, status=%s, idempotency_key=%s,"
            " attempt=%s, max_attempts=%s, lease_token=%s, lease_until=%s, run_at=%s,"
            " payload_json=%s, result_json=%s, error=%s, error_code=%s, created_at=%s,"
            " updated_at=%s, started_at=%s, finished_at=%s WHERE id=%s",
            self._params(task)[1:] + (task.id,),
        )
        return task

    async def acquire_next(self, lease_token: str, lease_until: int,
                           now: int,
                           kinds: Optional[Sequence[str]] = None) -> Optional[UserTask]:
        """Claim one runnable task, locking the candidate before the UPDATE.

        The previous form selected the candidate in an InitPlan subquery and
        relied on the status predicate living inside it. Under READ COMMITTED a
        worker that waits on a row another worker just claimed re-evaluates
        ONLY the outer quals (id equality) — the status predicate inside the
        subquery is never rechecked, so the loser overwrote the winner's
        lease_token and both executors ran the same task (attempt inflated).

        `FOR UPDATE SKIP LOCKED` puts the lock in the same statement as the
        candidate selection: a concurrent worker skips the locked row and takes
        the next candidate instead of waiting for it, and the outer status/run_at
        quals are rechecked after the lock is acquired.
        """
        kind_clause, kind_params = kind_filter_sql(kinds, placeholder="%s")
        row = await _fetch_one_db(
            self._db,
            "UPDATE user_tasks SET status='RUNNING', attempt=attempt+1, lease_token=%s,"
            " lease_until=%s, started_at=%s, updated_at=%s, result_json='',"
            " error='', error_code=''"
            " WHERE id IN ("
            " SELECT id FROM ("
            " SELECT id FROM user_tasks WHERE status IN ('PENDING','RETRY_WAIT')"
            " AND run_at<=%s" + kind_clause + " ORDER BY created_at, id LIMIT 1"
            " FOR UPDATE SKIP LOCKED"
            " ) AS candidate)"
            " AND status IN ('PENDING','RETRY_WAIT') AND run_at<=%s"
            " RETURNING *",
            (lease_token, int(lease_until), int(now), int(now), int(now))
            + kind_params + (int(now),),
        )
        return self._row_to_entity(row) if row else None

    async def renew_lease(self, task_id: str, token: str, lease_until: int) -> bool:
        result = await _execute_db(
            self._db,
            "UPDATE user_tasks SET lease_until=%s WHERE id=%s AND lease_token=%s"
            " AND status='RUNNING'",
            (int(lease_until), task_id, token),
        )
        return _affected(result) > 0

    async def cleanup(self, retention_seconds: int = TASK_RETENTION_SECONDS) -> int:
        # Terminal-only, batch-bounded (see ITaskRepository.cleanup). SKIP LOCKED
        # keeps a purge running on one replica from blocking a concurrent claim
        # on the rows it is not deleting; the status prefix means live work is
        # never in the candidate set at all. Literal terminal states, not an
        # import from user_tasks (which imports this module) — pinned to
        # user_tasks.TERMINAL_STATES by test_task_retention.py.
        SUCCEEDED, FAILED, CANCELLED = "SUCCEEDED", "FAILED", "CANCELLED"
        if int(retention_seconds) <= 0:
            return 0
        cutoff = int(time.time()) - int(retention_seconds)
        result = await _execute_db(
            self._db,
            "DELETE FROM user_tasks WHERE id IN ("
            " SELECT id FROM user_tasks WHERE status IN (%s, %s, %s)"
            " AND finished_at > 0 AND finished_at < %s"
            " ORDER BY finished_at LIMIT %s FOR UPDATE SKIP LOCKED)",
            (SUCCEEDED, FAILED, CANCELLED, int(cutoff), int(TASK_PURGE_BATCH)),
        )
        return _affected(result)

    async def reclaim_expired(self, now: int) -> int:
        result = await _execute_db(
            self._db,
            "UPDATE user_tasks SET status=CASE WHEN attempt>=max_attempts THEN 'FAILED'"
            " ELSE 'RETRY_WAIT' END,"
            " lease_token='', lease_until=0,"
            " run_at=CASE WHEN attempt>=max_attempts THEN run_at ELSE %s END,"
            " finished_at=CASE WHEN attempt>=max_attempts THEN %s ELSE 0 END,"
            " updated_at=%s,"
            " error='reclaimed from an expired worker lease', error_code='lease_expired'"
            " WHERE status='RUNNING' AND lease_until>0 AND lease_until<=%s",
            (int(now), int(now), int(now), int(now)),
        )
        return _affected(result)

    async def finish_succeeded(self, task_id: str, token: str, result_json: str,
                               now: int) -> bool:
        result = await _execute_db(
            self._db,
            "UPDATE user_tasks SET status='SUCCEEDED', result_json=%s, finished_at=%s,"
            " updated_at=%s, lease_token='', lease_until=0"
            " WHERE id=%s AND lease_token=%s AND status='RUNNING'",
            (result_json, int(now), int(now), task_id, token))
        return _affected(result) > 0

    async def finish_failed(self, task_id: str, token: str, error: str,
                            error_code: str, now: int) -> bool:
        result = await _execute_db(
            self._db,
            "UPDATE user_tasks SET status='FAILED', error=%s, error_code=%s,"
            " finished_at=%s, updated_at=%s, lease_token='', lease_until=0"
            " WHERE id=%s AND lease_token=%s AND status='RUNNING'",
            (error, error_code, int(now), int(now), task_id, token))
        return _affected(result) > 0

    async def pause_for_retry(self, task_id: str, token: str, run_at: int,
                              error: str, error_code: str, now: int) -> bool:
        result = await _execute_db(
            self._db,
            "UPDATE user_tasks SET status='RETRY_WAIT', error=%s, error_code=%s,"
            " run_at=%s, lease_token='', lease_until=0, updated_at=%s"
            " WHERE id=%s AND lease_token=%s AND status='RUNNING'",
            (error, error_code, int(run_at), int(now), task_id, token))
        return _affected(result) > 0

    async def cancel_queued(self, task_id: str, owner_user_id: int,
                            now: int) -> bool:
        result = await _execute_db(
            self._db,
            "UPDATE user_tasks SET status='CANCELLED', finished_at=%s, updated_at=%s,"
            " error='', error_code='cancelled', lease_token='', lease_until=0"
            " WHERE id=%s AND owner_user_id=%s AND status IN ('PENDING','RETRY_WAIT')",
            (int(now), int(now), task_id, int(owner_user_id)))
        return _affected(result) > 0

    async def retry_failed(self, task_id: str, owner_user_id: int,
                           now: int) -> Optional[UserTask]:
        row = await _fetch_one_db(
            self._db,
            "UPDATE user_tasks SET status='PENDING', attempt=0, error='',"
            " error_code='', lease_token='', lease_until=0, run_at=0,"
            " started_at=0, finished_at=0, result_json='', updated_at=%s"
            " WHERE id=%s AND owner_user_id=%s AND status='FAILED'"
            " RETURNING *",
            (int(now), task_id, int(owner_user_id)),
        )
        return self._row_to_entity(row) if row else None

    async def requeue_unclaimed(self, task_id: str, token: str, run_at: int,
                                now: int) -> bool:
        result = await _execute_db(
            self._db,
            "UPDATE user_tasks SET status='RETRY_WAIT', error='', error_code='',"
            " run_at=%s, lease_token='', lease_until=0, updated_at=%s"
            " WHERE id=%s AND lease_token=%s AND status='RUNNING'",
            (int(run_at), int(now), task_id, token))
        return _affected(result) > 0

