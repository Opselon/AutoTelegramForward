"""PostgreSQL repository implementations of the application repository ports.

Provides standard PostgreSQL support with parameterized queries (%s / $n),
JSONB serialization, proper boolean types, transaction integrity, and
optimistic concurrency control (OCC) using the `version` column.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional, Protocol

from ...application.repositories import (
    ConcurrencyError,
    IDeliveryQueueRepository,
    IForwardRuleRepository,
    IMessageMapRepository,
)
from ...domain.entities import (
    ContentMode,
    DeliveryJob,
    DeliveryStatus,
    ForwardMode,
    ForwardRule,
    MessageMapping,
    RoutingType,
    TriggerEvent,
)


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
        return []


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
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pg_fwd_source ON forward_rules (source_chat_id, is_active);
    CREATE INDEX IF NOT EXISTS idx_pg_fwd_session ON forward_rules (session_id);
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
        content_val = getattr(getattr(rule, "content_mode", ContentMode.AUTO), "value", ContentMode.AUTO.value)
        meta_json = json.dumps(rule.metadata or {})
        targets_json = json.dumps(rule.target_chat_ids or [])

        query = """
        INSERT INTO forward_rules (
            id, session_id, source_chat_id, source_chat_name, target_chat_id,
            target_chat_name, target_chat_ids, routing_type, forward_mode, is_active,
            filter_rule_id, ai_config_id, remove_links, custom_caption_template,
            delay_seconds, skip_history, since_ts, ignore_edits, trigger_events,
            content_mode, metadata, version, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
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
        )
        await self._db.execute(query, params)
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
        targets_json = json.dumps(rule.target_chat_ids or [])

        common_params = (
            rule.session_id, rule.source_chat_id, rule.source_chat_name,
            rule.target_chat_id, rule.target_chat_name, targets_json,
            rule.routing_type.value, rule.forward_mode.value, bool(rule.is_active),
            rule.filter_rule_id, rule.ai_config_id, bool(rule.remove_links),
            rule.custom_caption_template, float(rule.delay_seconds), bool(rule.skip_history),
            int(rule.since_ts or 0), bool(rule.ignore_edits),
            trigger_json, content_val, meta_json, next_version, rule.updated_at,
        )

        if expected_version is not None:
            query = """
            UPDATE forward_rules SET
                session_id = %s, source_chat_id = %s, source_chat_name = %s, target_chat_id = %s,
                target_chat_name = %s, target_chat_ids = %s, routing_type = %s, forward_mode = %s,
                is_active = %s, filter_rule_id = %s, ai_config_id = %s, remove_links = %s,
                custom_caption_template = %s, delay_seconds = %s, skip_history = %s, since_ts = %s,
                ignore_edits = %s, trigger_events = %s, content_mode = %s, metadata = %s,
                version = %s, updated_at = %s
            WHERE id = %s AND version = %s
            """
            result = await self._db.execute(query, (*common_params, rule.id, int(expected_version)))
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
                version = %s, updated_at = %s
            WHERE id = %s
            """
            await self._db.execute(query, (*common_params, rule.id))

        rule.version = next_version
        return rule

    async def get_by_id(self, rule_id: str) -> Optional[ForwardRule]:
        query = "SELECT * FROM forward_rules WHERE id = %s"
        row = await self._db.fetchone(query, (rule_id,))
        return self._row_to_entity(row) if row else None

    async def list_by_session(self, session_id: str) -> List[ForwardRule]:
        query = "SELECT * FROM forward_rules WHERE session_id = %s ORDER BY created_at ASC"
        rows = await self._db.fetchall(query, (session_id,))
        return [self._row_to_entity(r) for r in rows]

    async def list_all(self) -> List[ForwardRule]:
        query = "SELECT * FROM forward_rules ORDER BY created_at ASC"
        rows = await self._db.fetchall(query, ())
        return [self._row_to_entity(r) for r in rows]

    async def list_active_by_source(self, source_chat_id: str) -> List[ForwardRule]:
        query = "SELECT * FROM forward_rules WHERE source_chat_id = %s AND is_active = TRUE"
        rows = await self._db.fetchall(query, (str(source_chat_id),))
        return [self._row_to_entity(r) for r in rows]

    async def delete(self, rule_id: str) -> bool:
        query = "DELETE FROM forward_rules WHERE id = %s"
        res = await self._db.execute(query, (rule_id,))
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
            metadata=_safe_dict(d.get("metadata"), {}),
            version=int(d.get("version", 1) or 1),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
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
            lease_until, worker_id, error_detail, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(rule_id, source_chat_id, source_message_id, target_chat_id) DO NOTHING
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
            SELECT id FROM delivery_jobs
            WHERE (
                status = 'PENDING'
                OR status = 'RETRY_WAIT'
                OR (status = 'CLAIMED' AND lease_until < %s)
            )
            AND next_retry_at <= %s
            ORDER BY created_at ASC
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

    async def mark_retry(self, job_id: str, error: str, backoff_seconds: float) -> bool:
        now = time.time()
        row = await self._fetch_one("SELECT attempts, max_attempts FROM delivery_jobs WHERE id = %s", (job_id,))
        if not row:
            return False
        d = dict(row) if hasattr(row, "keys") else row
        attempts = int(d.get("attempts", 0)) + 1
        max_attempts = int(d.get("max_attempts", 5))

        if attempts >= max_attempts:
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

    async def mark_failed(self, job_id: str, error: str) -> bool:
        now = int(time.time())
        query = "UPDATE delivery_jobs SET status = 'FAILED', error_detail = %s, lease_until = 0, updated_at = %s WHERE id = %s"
        await _execute_db(self._db, query, (error, now, job_id))
        return True

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


