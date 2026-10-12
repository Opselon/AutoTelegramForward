"""SQLite connection management + schema migrations (WAL, foreign keys)."""

import json
import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# v12 adds owner scoping to api_credentials and prompt_templates. The real work
# is done by _apply_additive_columns (guarded ALTER + index); MIGRATIONS[12]
# only exists so schema_version tracks the upgrade and reconciliation runs.
SCHEMA_VERSION = 12

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
    7: """
    -- v7: Production-Grade AI Message Transformation Engine (P2)
    CREATE TABLE IF NOT EXISTS prompt_templates (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        system_prompt TEXT NOT NULL,
        user_prompt_template TEXT NOT NULL DEFAULT '{text}',
        target_language TEXT NOT NULL DEFAULT 'en',
        current_version INTEGER NOT NULL DEFAULT 1,
        is_system INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_prompt_name ON prompt_templates(name);

    CREATE TABLE IF NOT EXISTS prompt_versions (
        id TEXT PRIMARY KEY,
        prompt_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        system_prompt TEXT NOT NULL,
        user_prompt_template TEXT NOT NULL,
        change_summary TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        UNIQUE(prompt_id, version)
    );
    CREATE INDEX IF NOT EXISTS idx_prompt_ver ON prompt_versions(prompt_id, version);
    CREATE INDEX IF NOT EXISTS idx_prompt_active ON prompt_versions(prompt_id, is_active);

    ALTER TABLE forward_rules ADD COLUMN ai_fallback_policy TEXT NOT NULL DEFAULT 'DROP';
    ALTER TABLE forward_rules ADD COLUMN ai_prompt_version INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN ai_timeout_seconds REAL NOT NULL DEFAULT 15.0;
    ALTER TABLE forward_rules ADD COLUMN ai_secondary_config_id TEXT;
    """,
    8: """
    -- v8: Smart Forwarding Rules — routing policy, detection, fallback, queue policy
    -- Smart routing fields on forward_rules (kept optional so existing rules stay valid)
    ALTER TABLE forward_rules ADD COLUMN message_category TEXT NOT NULL DEFAULT 'ALL';
    ALTER TABLE forward_rules ADD COLUMN intermediate_channel_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN intermediate_channel_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN intermediate_target_chat_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN use_intermediate INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN fallback_mode TEXT NOT NULL DEFAULT 'COPY_MESSAGE';
    ALTER TABLE forward_rules ADD COLUMN fallback_enabled INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE forward_rules ADD COLUMN detection_criteria TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE forward_rules ADD COLUMN priority INTEGER NOT NULL DEFAULT 10;
    ALTER TABLE forward_rules ADD COLUMN execution_order INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN multi_route INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN media_handling TEXT NOT NULL DEFAULT 'AUTO';
    ALTER TABLE forward_rules ADD COLUMN dedupe_policy TEXT NOT NULL DEFAULT 'STRICT';
    ALTER TABLE forward_rules ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 3;
    ALTER TABLE forward_rules ADD COLUMN retry_backoff_base REAL NOT NULL DEFAULT 2.0;
    ALTER TABLE forward_rules ADD COLUMN rate_limit_per_minute INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN rate_limit_burst INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN custom_header TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN custom_footer TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN header_enabled INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN template_text TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN template_media TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN template_album TEXT NOT NULL DEFAULT '';
    ALTER TABLE forward_rules ADD COLUMN caption_max_length INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN preserve_signature INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN is_paused INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN paused_until INTEGER NOT NULL DEFAULT 0;

    -- Dead-letter queue: messages that exhausted the retry policy
    CREATE TABLE IF NOT EXISTS dead_letter_queue (
        id TEXT PRIMARY KEY,
        job_id TEXT,
        rule_id TEXT NOT NULL,
        source_chat_id TEXT NOT NULL,
        source_message_id INTEGER NOT NULL,
        target_chat_id TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        error_category TEXT NOT NULL DEFAULT '',
        dead_lettered_at INTEGER NOT NULL,
        UNIQUE(rule_id, source_chat_id, source_message_id, target_chat_id)
    );
    CREATE INDEX IF NOT EXISTS idx_dlq_rule ON dead_letter_queue(rule_id, dead_lettered_at);
    CREATE INDEX IF NOT EXISTS idx_dlq_ts ON dead_letter_queue(dead_lettered_at);
    """,
    9: """
    -- v9: Link Management, Media Routing & Crash-Resilient Delivery State Machine
    ALTER TABLE forward_rules ADD COLUMN link_policy TEXT NOT NULL DEFAULT 'PRESERVE_ALL';
    ALTER TABLE forward_rules ADD COLUMN domain_allowlist TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE forward_rules ADD COLUMN domain_blocklist TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE forward_rules ADD COLUMN link_rewrite_map TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE forward_rules ADD COLUMN allowed_media_types TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE forward_rules ADD COLUMN split_long_caption INTEGER NOT NULL DEFAULT 1;

    ALTER TABLE delivery_jobs ADD COLUMN intermediate_chat_id TEXT;
    ALTER TABLE delivery_jobs ADD COLUMN intermediate_message_id INTEGER;
    ALTER TABLE delivery_jobs ADD COLUMN delivery_stage TEXT NOT NULL DEFAULT 'DIRECT';
    """,
    10: """
    -- v10: Unified per-user accounts (Telegram bot + Web dashboard share identity).
    -- Passwords stored as PBKDF2-SHA256 (iterations stored per-row for upgrades).
    CREATE TABLE IF NOT EXISTS web_accounts (
        user_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        password_algo TEXT NOT NULL DEFAULT 'pbkdf2_sha256',
        password_iter INTEGER NOT NULL DEFAULT 240000,
        salt TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        is_admin INTEGER NOT NULL DEFAULT 0,
        plan TEXT NOT NULL DEFAULT 'free',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_web_accounts_username ON web_accounts(username);

    -- Ownership scoping: every session & rule belongs to one account.
    ALTER TABLE sessions ADD COLUMN owner_user_id INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE forward_rules ADD COLUMN owner_user_id INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_user_id);
    CREATE INDEX IF NOT EXISTS idx_rules_owner ON forward_rules(owner_user_id);

    -- One-time dashboard login tokens minted by the Telegram bot (short TTL).
    CREATE TABLE IF NOT EXISTS web_tokens (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_web_tokens_expiry ON web_tokens(expires_at);
    """,
    11: """
    -- v11: Ownership scoping for AI configs and filter rules.
    -- Without this, any authenticated tenant could read another tenant's
    -- LLM provider API key via GET /api/ai-configs.
    -- The tables are (re)declared IF NOT EXISTS because a database restored
    -- from a pre-v6 backup may jump straight into this migration; the owner
    -- column is part of the declaration so a fresh table needs no ALTER.
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
        target_language TEXT NOT NULL DEFAULT 'en',
        owner_user_id INTEGER NOT NULL DEFAULT 0
    );
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
        max_message_length INTEGER NOT NULL DEFAULT 0,
        owner_user_id INTEGER NOT NULL DEFAULT 0
    );
    """,
    12: """
    -- v12: owner scoping for the credential vault and prompt templates.
    -- Previously both were global: GET /api/prompts and a backup export
    -- returned every tenant's rows, so tenant A could read tenant B's
    -- api_hash / system prompts, and replay/purge tenant B's dead-letter jobs.
    -- The columns themselves are added by _apply_additive_columns, which runs
    -- AFTER this script and also on a database carried over from an older
    -- schema; creating the indexes here would fail on a fresh DB because the
    -- column does not exist yet. The additive path creates the indexes once
    -- the ALTER has landed.
    """,
    13: """
    -- v13: operator-editable runtime settings (key/value store).
    -- Holds the public web dashboard URL the bot shows in its dashboard
    -- panel, so an operator can point the one-tap login link at their own
    -- domain without editing config files or restarting the service.
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL
    );
    """,
}

# Columns added by additive migrations that must also work on a table carried
# over from an older schema (v11 runs the guarded ALTER in _apply_additive_columns).
_ADDITIVE_COLUMNS = (
    ("ai_configs", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_ai_configs_owner"),
    ("filter_rules", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_filter_rules_owner"),
    # v12: credential vault + prompt templates are per-tenant, not global.
    # Without this any authenticated tenant can read another tenant's api_hash
    # through a backup export, and overwrite another tenant's prompt templates.
    ("api_credentials", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_api_credentials_owner"),
    ("prompt_templates", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_prompt_templates_owner"),
)


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
        self._apply_pragmas()
        self._migrate()

    def _apply_pragmas(self) -> None:
        """Apply high-performance, low-resource pragmas optimal for low-spec servers."""
        with self._lock:
            # WAL mode allows non-blocking concurrent reads during writes
            self._conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL sync reduces fsyncs by ~95% in WAL mode; safe against crash
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Busy timeout prevents locks under concurrency without high CPU usage
            self._conn.execute("PRAGMA busy_timeout=10000")
            # Temp store in memory avoids disk I/O on slow VPS storage
            self._conn.execute("PRAGMA temp_store=MEMORY")
            # Cache size of -16000 (~16MB) is lightweight but achieves high hit-rates
            self._conn.execute("PRAGMA cache_size=-16000")
            # Memory-mapped I/O for fast zero-copy reads
            try:
                self._conn.execute("PRAGMA mmap_size=67108864")
            except Exception:
                pass
            self._conn.execute("PRAGMA foreign_keys=ON")

    def get_schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            return int(row["version"]) if row else 0

    def _migrate(self) -> None:
        with self._lock:
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
                    # v12.3 (ACID durability): executescript() issues an implicit
                    # COMMIT before it runs, so each migration is already durable
                    # the moment it returns. The recorded version must follow it
                    # immediately — the old code only bumped it once, after the
                    # whole loop. A crash mid-loop left the schema half-upgraded
                    # while schema_version still read the old value, and the next
                    # boot re-ran every script from there, re-applying
                    # non-idempotent migrations against the new schema.
                    if row:
                        self._conn.execute(
                            "UPDATE schema_version SET version = ?", (current,)
                        )
                    else:
                        self._conn.execute(
                            "INSERT INTO schema_version (version) VALUES (?)", (current,)
                        )
                        row = {"version": current}
            self._apply_additive_columns()
            self._conn.commit()

    def _apply_additive_columns(self) -> None:
        """Idempotently add columns introduced by additive migrations.

        A table carried over from an older schema version (for example a
        database restored from a pre-v6 backup) can jump several migrations at
        once; the IF NOT EXISTS table declarations in v11 only help when the
        table is absent, so additive columns still need a guarded ALTER here.
        Indexes are created after the column so the script never runs before
        the column exists.
        """
        for table, column, decl, index in _ADDITIVE_COLUMNS:
            try:
                cols = {
                    str(r["name"])
                    for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except Exception:
                continue
            if not cols:
                continue
            if column not in cols:
                try:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                    )
                except Exception:
                    continue
            try:
                self._conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {index} ON {table}({column})"
                )
            except Exception:
                pass

        # v12: single-tenant upgrade reconciliation.
        # Legacy rows in api_credentials / prompt_templates were created before
        # owner_user_id existed, so they defaulted to 0 (the shared system
        # scope). On a real single-tenant install that silently hides the
        # owner's own credentials and personal prompts from them, because the
        # new owner-scoped queries only look at their own id. If exactly one
        # genuine human account exists, the legacy rows belong to them; if
        # there are zero or several, leave them at 0 (admin/system scope) and
        # let the operator claim them explicitly.
        self._reconcile_legacy_ownership()

    def _reconcile_legacy_ownership(self) -> None:
        try:
            rows = self._conn.execute(
                "SELECT user_id FROM web_accounts WHERE user_id > 0"
            ).fetchall()
        except Exception:
            return
        if len(rows) != 1:
            return
        owner = int(rows[0]["user_id"]) if isinstance(rows[0], dict) else int(rows[0][0])
        if not owner:
            return
        for table in ("api_credentials", "prompt_templates"):
            try:
                # Only rebind rows that are still in the legacy system scope.
                # Anything already claimed by a real owner is untouched.
                cur = self._conn.execute(
                    f"UPDATE {table} SET owner_user_id = ? WHERE owner_user_id = 0",
                    (owner,),
                )
                if cur.rowcount:
                    logger.info(
                        "v12 ownership reconciliation: rebound %d legacy row(s) in"
                        " %s to owner %d",
                        cur.rowcount,
                        table,
                        owner,
                    )
            except Exception as exc:
                logger.warning("v12 ownership reconciliation failed for %s: %s", table, exc)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def run_in_transaction(self, fn):
        """Run fn(conn) as one atomic unit; commit on success, roll back on error.

        v12.3 (ACID atomicity): every repository write goes through execute(),
        which auto-commits each statement. Multi-step invariants (delivery job
        + message mapping, session activate + client start) need a single
        atomic boundary — otherwise a crash between statements leaves a
        half-written state that no watchdog can fully reconcile. fn receives
        the raw sqlite3 connection **under the database lock** and must use it
        directly (no per-statement commit); failures roll the whole unit back.
        """
        with self._lock:
            try:
                result = fn(self._conn)
                self._conn.commit()
                return result
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

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
