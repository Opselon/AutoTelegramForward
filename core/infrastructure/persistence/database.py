"""SQLite connection management + schema migrations (WAL, foreign keys)."""

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 6

MIGRATIONS = {
    1: """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        phone_number TEXT NOT NULL,
        session_string_encrypted TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '',
        first_name TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        is_authorized INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_phone ON sessions(phone_number);

    CREATE TABLE IF NOT EXISTS forward_rules (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        source_chat_id TEXT NOT NULL,
        source_chat_name TEXT NOT NULL DEFAULT '',
        target_chat_id TEXT NOT NULL,
        target_chat_name TEXT NOT NULL DEFAULT '',
        routing_type TEXT NOT NULL,
        forward_mode TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        filter_rule_id TEXT,
        ai_config_id TEXT,
        remove_links INTEGER NOT NULL DEFAULT 0,
        custom_caption_template TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_rules_session ON forward_rules(session_id);
    CREATE INDEX IF NOT EXISTS idx_rules_source ON forward_rules(source_chat_id);

    CREATE TABLE IF NOT EXISTS filter_rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        whitelist_keywords TEXT NOT NULL DEFAULT '[]',
        blacklist_keywords TEXT NOT NULL DEFAULT '[]',
        regex_patterns TEXT NOT NULL DEFAULT '[]',
        allowed_media_types TEXT NOT NULL DEFAULT '[]',
        blocked_media_types TEXT NOT NULL DEFAULT '[]',
        drop_service_messages INTEGER NOT NULL DEFAULT 1,
        min_message_length INTEGER NOT NULL DEFAULT 0,
        max_message_length INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS ai_configs (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        api_key_encrypted TEXT NOT NULL DEFAULT '',
        base_url TEXT NOT NULL DEFAULT '',
        system_prompt TEXT NOT NULL DEFAULT '',
        user_prompt_template TEXT NOT NULL DEFAULT '{text}',
        temperature REAL NOT NULL DEFAULT 0.7,
        is_enabled INTEGER NOT NULL DEFAULT 1,
        target_language TEXT NOT NULL DEFAULT 'en'
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        category TEXT NOT NULL,
        detail TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
    """,
    2: """
    -- v2: multi-target rules, flexible scheduling, credential vault.
    ALTER TABLE forward_rules ADD COLUMN target_chat_ids TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE forward_rules ADD COLUMN delay_seconds REAL NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN skip_history INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE forward_rules ADD COLUMN since_ts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN ignore_edits INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN trigger_events TEXT NOT NULL DEFAULT 'NEW_MESSAGE';
    ALTER TABLE forward_rules ADD COLUMN content_mode TEXT NOT NULL DEFAULT 'AUTO';
    ALTER TABLE forward_rules ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';

    ALTER TABLE sessions ADD COLUMN api_credential_id TEXT NOT NULL DEFAULT 'default';
    ALTER TABLE sessions ADD COLUMN proxy TEXT NOT NULL DEFAULT '';

    -- Encrypted credential vault: api_id/api_hash pairs, bot tokens, proxies.
    CREATE TABLE IF NOT EXISTS api_credentials (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL DEFAULT '',
        api_id INTEGER NOT NULL DEFAULT 0,
        api_hash_encrypted TEXT NOT NULL DEFAULT '',
        proxy TEXT NOT NULL DEFAULT '',
        is_default INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bot_tokens (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL DEFAULT '',
        token_encrypted TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS processed_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        media_group_id TEXT,
        ts INTEGER NOT NULL,
        UNIQUE(rule_id, chat_id, message_id)
    );
    CREATE INDEX IF NOT EXISTS idx_processed_rule ON processed_messages(rule_id, ts);
    """,
    # ------------------------------------------------------------------ #
    # v3: professional state DB — durable UI flows, metrics, error log,
    #     per-user settings, rule chains, text replacements.
    # ------------------------------------------------------------------ #
    3: """
    -- Durable per-user UI state: multi-step flows survive a restart.
    CREATE TABLE IF NOT EXISTS ui_states (
        user_id INTEGER PRIMARY KEY,
        step TEXT NOT NULL DEFAULT '',
        buffer TEXT NOT NULL DEFAULT '{}',
        updated_at INTEGER NOT NULL
    );

    -- Per-user preferences + quota/plan state (millions of rows friendly).
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        language TEXT NOT NULL DEFAULT '',
        is_admin INTEGER NOT NULL DEFAULT 0,
        plan TEXT NOT NULL DEFAULT 'free',
        quota_forwarded INTEGER NOT NULL DEFAULT 0,
        quota_reset_at INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_users_plan ON users(plan);

    -- Structured error log: exact Telegram RPC error surfaced to the user.
    CREATE TABLE IF NOT EXISTS error_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        user_id INTEGER,
        session_id TEXT,
        rule_id TEXT,
        category TEXT NOT NULL,          -- login | forward | ai | auth | bot
        error_name TEXT NOT NULL,        -- exact pyrogram exception class
        severity TEXT NOT NULL,          -- info | warn | error | auth | fatal
        detail TEXT NOT NULL DEFAULT '',
        recoverable INTEGER NOT NULL DEFAULT 0,
        chat_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_error_ts ON error_log(ts);
    CREATE INDEX IF NOT EXISTS idx_error_category ON error_log(category, severity);
    CREATE INDEX IF NOT EXISTS idx_error_session ON error_log(session_id);

    -- Rule chaining: A -> B -> C (rule.target may feed another rule's source).
    ALTER TABLE forward_rules ADD COLUMN chain_of TEXT NOT NULL DEFAULT '';
    -- Text replacement: words/usernames/URLs to swap before sending.
    ALTER TABLE forward_rules ADD COLUMN replacements TEXT NOT NULL DEFAULT '{}';
    -- Header / footer templates.
    ALTER TABLE forward_rules ADD COLUMN header TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN footer TEXT NOT NULL DEFAULT '';
    -- Sync mode: replicate edits AND deletes.
    ALTER TABLE forward_rules ADD COLUMN sync_edits INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE forward_rules ADD COLUMN sync_deletes INTEGER NOT NULL DEFAULT 0;
    -- Whitelist / blacklist of *senders* (user ids).
    ALTER TABLE forward_rules ADD COLUMN allow_senders TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE forward_rules ADD COLUMN block_senders TEXT NOT NULL DEFAULT '[]';
    -- Topic (forum) support: target topic id.
    ALTER TABLE forward_rules ADD COLUMN target_topic_id INTEGER NOT NULL DEFAULT 0;

    -- Per-rule live counters (hot path: bumped without touching the rule row).
    CREATE TABLE IF NOT EXISTS rule_stats (
        rule_id TEXT PRIMARY KEY,
        forwarded INTEGER NOT NULL DEFAULT 0,
        filtered INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        last_forward_ts INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (rule_id) REFERENCES forward_rules(id) ON DELETE CASCADE
    );

    -- Hourly throughput buckets for the dashboard (rolling, TTL-pruned).
    CREATE TABLE IF NOT EXISTS metrics_hourly (
        ts_hour INTEGER NOT NULL,
        forwarded INTEGER NOT NULL DEFAULT 0,
        filtered INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (ts_hour)
    );
    """,
    4: """
    -- TASK 03 / audit D1: persisted authentication state machine.
    -- A login in progress must survive a process restart (or at worst
    -- degrade to a resumable state) instead of vanishing silently.
    CREATE TABLE IF NOT EXISTS auth_flows (
        user_id INTEGER PRIMARY KEY,
        state TEXT NOT NULL,                -- AuthState value
        phone_number TEXT NOT NULL DEFAULT '',
        credential_id TEXT NOT NULL DEFAULT 'default',
        phone_code_hash TEXT NOT NULL DEFAULT '',
        -- retry counters survive a restart so a crash cannot be used to
        -- reset the Telegram-side attempt budget
        code_attempts INTEGER NOT NULL DEFAULT 0,
        password_attempts INTEGER NOT NULL DEFAULT 0,
        -- version for optimistic concurrency: idempotent transitions
        version INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        last_error TEXT NOT NULL DEFAULT '',
        correlation_id TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_auth_flows_state ON auth_flows(state);
    CREATE INDEX IF NOT EXISTS idx_auth_flows_expires ON auth_flows(expires_at);
    """,
    5: """
    -- v5: optimistic concurrency / versioning on forward_rules
    -- protects against lost updates during concurrent edits
    ALTER TABLE forward_rules ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
    """,
    6: """
    -- v6: P0 Data-Plane Reliability
    -- Persistent message mapping for edit/delete synchronization
    CREATE TABLE IF NOT EXISTS message_map (
        id TEXT PRIMARY KEY,
        rule_id TEXT NOT NULL,
        source_chat_id TEXT NOT NULL,
        source_message_id INTEGER NOT NULL,
        target_chat_id TEXT NOT NULL,
        target_message_id INTEGER,
        media_group_id TEXT,
        delivery_status TEXT NOT NULL DEFAULT 'PENDING',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(rule_id, source_chat_id, source_message_id, target_chat_id)
    );
    CREATE INDEX IF NOT EXISTS idx_msgmap_src ON message_map(source_chat_id, source_message_id);
    CREATE INDEX IF NOT EXISTS idx_msgmap_tgt ON message_map(target_chat_id, target_message_id);
    CREATE INDEX IF NOT EXISTS idx_msgmap_rule_src ON message_map(rule_id, source_chat_id, source_message_id);
    CREATE INDEX IF NOT EXISTS idx_msgmap_media ON message_map(media_group_id);

    -- Durable delivery jobs queue for non-blocking asynchronous workers and restart recovery
    CREATE TABLE IF NOT EXISTS delivery_jobs (
        id TEXT PRIMARY KEY,
        rule_id TEXT NOT NULL,
        source_chat_id TEXT NOT NULL,
        source_message_id INTEGER NOT NULL,
        target_chat_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        next_retry_at REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0,
        worker_id TEXT,
        error_detail TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(rule_id, source_chat_id, source_message_id, target_chat_id)
    );
    CREATE INDEX IF NOT EXISTS idx_delivery_jobs_poll ON delivery_jobs(status, next_retry_at, lease_until);
    CREATE INDEX IF NOT EXISTS idx_delivery_jobs_rule ON delivery_jobs(rule_id);
    """,
}


class SqliteDatabase:
    """Thread-safe async-friendly SQLite connection holder.

    SQLite is the default database; a future PostgreSQL/MySQL adapter only
    needs to provide the same repository interfaces.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = sqlite3.connect(
            db_path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._migrate()

    def get_schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            return int(row["version"]) if row else 0

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            current = row["version"] if row else 0
            for version in sorted(MIGRATIONS):
                if version > current:
                    self._conn.executescript(MIGRATIONS[version])
                    current = version
            if row:
                self._conn.execute(
                    "UPDATE schema_version SET version = ?", (current,)
                )
            else:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (current,)
                )
            self._conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_one(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def dumps(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def loads(value: str, default):
        try:
            return json.loads(value) if value else default
        except (json.JSONDecodeError, TypeError):
            return default

    def close(self) -> None:
        with self._lock:
            self._conn.close()
