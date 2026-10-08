"""SQLite connection management + schema migrations (WAL, foreign keys)."""

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

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
