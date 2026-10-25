"""SQLite connection management + schema migrations (WAL, foreign keys)."""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# v12 adds owner scoping to api_credentials and prompt_templates. The real work
# is done by _apply_additive_columns (guarded ALTER + index); MIGRATIONS[12]
# only exists so schema_version tracks the upgrade and reconciliation runs.

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

    -- v2.0 app update: Duplicate Filters — content-level dedup store.
    -- One row per (rule, content hash): a re-posted message with a different
    -- message id but byte-identical normalized content lands here and is
    -- skipped by the pipeline. Scoped to the rule so the same text aiming at
    -- a different audience is never collapsed.
    CREATE TABLE IF NOT EXISTS content_dedup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(rule_id, content_hash)
    );
    CREATE INDEX IF NOT EXISTS idx_content_dedup_rule ON content_dedup(rule_id, content_hash);

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
        -- v20: the deliverable kind is part of the dedup key. Without it a
        -- keyword reply to a target the source message was also forwarded to
        -- collided with the forward's row and INSERT OR IGNORE dropped it.
        job_kind TEXT NOT NULL DEFAULT 'forward',
        UNIQUE(rule_id, source_chat_id, source_message_id, target_chat_id, job_kind)
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
        owner_user_id INTEGER NOT NULL DEFAULT 0,
        regex_block TEXT NOT NULL DEFAULT '[]',
        regex_require TEXT NOT NULL DEFAULT '[]'
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
    14: """
    -- v14: Queue scalability.
    -- idx_delivery_jobs_claim is the claim path index. The old
    -- idx_delivery_jobs_poll (status, next_retry_at, lease_until) forced a
    -- TEMP B-TREE sort on every claim_batch call because the query orders by
    -- created_at, which is not a leading column anywhere. At 50k pending rows
    -- each claim paid a full in-memory sort.
    -- The new index leads with exactly the claim predicate and the ORDER BY
    -- key, so the planner walks the index in order and stops at LIMIT with no
    -- sort step.
    CREATE INDEX IF NOT EXISTS idx_delivery_jobs_claim
        ON delivery_jobs(status, next_retry_at, lease_until, created_at);
    -- Retention index: cleanup() deletes by status + updated_at; that was a
    -- full table scan on a growing table.
    CREATE INDEX IF NOT EXISTS idx_delivery_jobs_cleanup
        ON delivery_jobs(status, updated_at);
    """,
    15: """
    -- Channel polling cursors. The ChannelPollingService stores the newest
    -- channel post it has already replayed here so a restart does not rescan
    -- the whole channel history.
    CREATE TABLE IF NOT EXISTS channel_cursors (
        chat_id TEXT PRIMARY KEY,
        last_message_id INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_channel_cursors_updated
        ON channel_cursors(updated_at);
    """,
    16: """
    -- v16: Administrative Control Plane, Multi-Admin Governance & Broadcast Queue.
    CREATE TABLE IF NOT EXISTS administrators (
        user_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL DEFAULT 'admin',  -- 'owner' | 'admin'
        added_by INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_administrators_role ON administrators(role);

    CREATE TABLE IF NOT EXISTS broadcast_queue (
        id TEXT PRIMARY KEY,
        initiator_user_id INTEGER NOT NULL,
        message_text TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT 'all',  -- 'all' | 'free' | 'vip'
        status TEXT NOT NULL DEFAULT 'pending', -- pending | running | completed | failed | cancelled
        total_recipients INTEGER NOT NULL DEFAULT 0,
        sent_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_broadcast_status ON broadcast_queue(status, created_at);
    """,
    17: """
    -- v17: Ordered delivery lanes.
    -- claim_batch orders by (created_at, source_chat_id, source_message_id, id)
    -- and refuses to claim a job while an earlier unsent job exists for the same
    -- target chat, so a burst arrives in the order it was posted. That guard is
    -- a correlated NOT EXISTS on (target_chat_id, status): without an index it
    -- scanned every pending job for every candidate row.
    CREATE INDEX IF NOT EXISTS idx_delivery_jobs_lane
        ON delivery_jobs(target_chat_id, status, created_at, source_message_id);
    """,
    20: """
    -- v20: a bot carries settings for every task that uses it.
    -- The column is added by _apply_additive_columns (guarded ALTER, and an
    -- older database can jump straight to v20); this entry exists so
    -- schema_version records the upgrade.
    SELECT 1;
    """,
    21: """
    -- v21: the two reads a message and a task list hit on every call are
    -- indexed. Measured on 10,000 tenants, `list_active_by_source` was a full
    -- scan of forward_rules — its OR/EXISTS over json_each cannot use a
    -- single-column index — and the owner-scoped page sorted through a temp
    -- B-tree. The column and indexes are installed by _apply_scale_indexes
    -- (idempotent, and an older database can jump straight here); this entry
    -- exists so schema_version records the upgrade.
    SELECT 1;
    """,
    22: """
    -- v22: dashboard links carry a single-use login code, not a session token.
    -- The row is the whole mechanism: `code_hash` is SHA-256 of the code (never
    -- the code itself), `used_at` makes it single-use, `expires_at` bounds it.
    -- The exchange UPDATE matches `used_at IS NULL AND expires_at > now`, so one
    -- link opens exactly one session even under a race.
    CREATE TABLE IF NOT EXISTS web_login_codes (
        code_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used_at INTEGER,
        purpose TEXT NOT NULL DEFAULT 'dashboard'
    );
    CREATE INDEX IF NOT EXISTS idx_web_login_codes_user ON web_login_codes(user_id);
    CREATE INDEX IF NOT EXISTS idx_web_login_codes_created ON web_login_codes(created_at);
    """,
    23: """
    -- v23: the deployment's own secrets, starting with the session signing key.
    -- A signing key derived from something public (a path, a default name) is
    -- not a secret: the old fallback hashed the database path, which is in the
    -- compose file, so anyone could sign a token for any account. The generated
    -- key lives here instead — random, one row, shared by every process that
    -- reads this database, and never part of an image or a config file.
    CREATE TABLE IF NOT EXISTS app_secrets (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    """,
    24: """
    -- v24: Per-user task lifecycle (API-first phase 2). Every long-running
    -- user operation is a durable, tenant-owned row: the authoritative state
    -- machine lives here, not in Redis and not in a process's memory. The
    -- idempotency partial index makes a retried submit return the same task
    -- instead of a duplicate. The claim index serves the Core worker pool:
    -- lease_token/lease_until give a bounded takeover so a crashed worker
    -- cannot hold task state hostage.
    CREATE TABLE IF NOT EXISTS user_tasks (
        id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        idempotency_key TEXT NOT NULL DEFAULT '',
        attempt INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        lease_token TEXT NOT NULL DEFAULT '',
        lease_until INTEGER NOT NULL DEFAULT 0,
        run_at INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        started_at INTEGER NOT NULL DEFAULT 0,
        finished_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_tasks_idem
        ON user_tasks(owner_user_id, kind, idempotency_key) WHERE idempotency_key != '';
    CREATE INDEX IF NOT EXISTS idx_user_tasks_owner ON user_tasks(owner_user_id, created_at DESC);
    -- (status, run_at) alone made the 10k-row claim a temp B-tree sort;
    -- including the ordering columns keeps the claim an index-ordered scan
    -- (measured 820us -> <60us at 10k rows).
    CREATE INDEX IF NOT EXISTS idx_user_tasks_pick ON user_tasks(status, run_at, created_at, id);
    """,
    25: """
    -- v25: Retention sweep for the per-user task ledger. Terminal rows are kept
    -- for a bounded window and then deleted (core/application/user_task_worker
    -- drives it); without an index on (status, finished_at) that sweep is a full
    -- scan of every terminal row ever written, which grows without bound. The
    -- index is additive and reversible: dropping it changes only the cost of the
    -- sweep, never the data. PENDING/RETRY_WAIT/RUNNING rows are live work and
    -- are never eligible, so the sweep's predicate starts with status.
    CREATE INDEX IF NOT EXISTS idx_user_tasks_finished ON user_tasks(status, finished_at);
    """,
    26: """
    -- v26: account-level suspensions are database state, not process memory.
    -- A Telegram flood wait can outlast the process that received it; keeping
    -- it in a dict meant a restart (or a second replica) saw a free account and
    -- sent again, which Telegram punishes by extending the wait. `until` is
    -- Telegram's own deadline, `code` the error id that imposed it, and the
    -- row is deleted when it expires so "resumed" is a fact every process sees.
    CREATE TABLE IF NOT EXISTS account_limits (
        session_id TEXT PRIMARY KEY,
        until REAL NOT NULL,
        code TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_account_limits_until ON account_limits(until);
    """,
    19: """
    -- v19: bot tokens are per-owner. The column + index are added by
    -- _apply_additive_columns (which runs after this script and is idempotent);
    -- the entry exists so schema_version records the upgrade.
    SELECT 1;
    """,
    28: """
    -- v28: AI private-message auto-reply.
    -- Everything below is tenant-owned (owner_user_id is part of every predicate,
    -- including every index: isolation is enforced where the query runs, not in
    -- application code afterwards).

    -- One behaviour switchboard row per account.
    CREATE TABLE IF NOT EXISTS autoreply_settings (
        owner_user_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        ai_config_id TEXT NOT NULL DEFAULT '',
        prompt_id TEXT NOT NULL DEFAULT '',
        target_mode TEXT NOT NULL DEFAULT 'EVERYONE',
        language TEXT NOT NULL DEFAULT '',
        tone TEXT NOT NULL DEFAULT 'friendly',
        response_length TEXT NOT NULL DEFAULT 'short',
        temperature REAL NOT NULL DEFAULT 0.7,
        max_tokens INTEGER NOT NULL DEFAULT 300,
        context_messages INTEGER NOT NULL DEFAULT 6,
        memory_enabled INTEGER NOT NULL DEFAULT 1,
        memory_max_facts INTEGER NOT NULL DEFAULT 12,
        extra_instructions TEXT NOT NULL DEFAULT '',
        max_reply_chars INTEGER NOT NULL DEFAULT 1200,
        typing_simulation INTEGER NOT NULL DEFAULT 1,
        typing_delay_min REAL NOT NULL DEFAULT 1.5,
        typing_delay_max REAL NOT NULL DEFAULT 4.0,
        cooldown_seconds INTEGER NOT NULL DEFAULT 15,
        daily_limit_per_chat INTEGER NOT NULL DEFAULT 0,
        rate_limit_per_minute INTEGER NOT NULL DEFAULT 20,
        max_concurrency INTEGER NOT NULL DEFAULT 3,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        retry_backoff_seconds REAL NOT NULL DEFAULT 20.0,
        quiet_hours_start INTEGER NOT NULL DEFAULT -1,
        quiet_hours_end INTEGER NOT NULL DEFAULT -1,
        timezone_offset_minutes INTEGER NOT NULL DEFAULT 0,
        active_days TEXT NOT NULL DEFAULT '[]',
        fallback_text TEXT NOT NULL DEFAULT '',
        ignore_bots INTEGER NOT NULL DEFAULT 1,
        version INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );

    -- The user's own prompt library: permanently stored, duplicable,
    -- import/export-able, switchable, assignable to contacts and rules.
    -- (owner_user_id, name) is unique: no two of a tenant's prompts share a name.
    CREATE TABLE IF NOT EXISTS autoreply_prompts (
        id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        system_prompt TEXT NOT NULL DEFAULT '',
        user_prompt_template TEXT NOT NULL DEFAULT '{text}',
        tone TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT '',
        response_length TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '[]',
        is_default INTEGER NOT NULL DEFAULT 0,
        is_archived INTEGER NOT NULL DEFAULT 0,
        use_count INTEGER NOT NULL DEFAULT 0,
        last_used_at INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_prompts_name
        ON autoreply_prompts(owner_user_id, name);
    CREATE INDEX IF NOT EXISTS idx_autoreply_prompts_owner
        ON autoreply_prompts(owner_user_id, updated_at DESC);

    -- Targeting list. is_enabled flips each chat on/off individually; the mode
    -- decides whether the list is a whitelist, blacklist or nothing.
    CREATE TABLE IF NOT EXISTS autoreply_targets (
        id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        chat_type TEXT NOT NULL DEFAULT 'private',
        title TEXT NOT NULL DEFAULT '',
        username TEXT NOT NULL DEFAULT '',
        is_enabled INTEGER NOT NULL DEFAULT 1,
        prompt_id TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        priority INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_targets_chat
        ON autoreply_targets(owner_user_id, chat_id);
    CREATE INDEX IF NOT EXISTS idx_autoreply_targets_enabled
        ON autoreply_targets(owner_user_id, is_enabled);

    -- Advanced rules; resolution order is (priority DESC, specificity DESC,
    -- created ASC, id ASC) and it is computed, not stored.
    CREATE TABLE IF NOT EXISTS autoreply_rules (
        id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        is_enabled INTEGER NOT NULL DEFAULT 1,
        priority INTEGER NOT NULL DEFAULT 10,
        action TEXT NOT NULL DEFAULT 'REPLY',
        cooldown_seconds INTEGER NOT NULL DEFAULT -1,
        prompt_id TEXT NOT NULL DEFAULT '',
        ai_config_id TEXT NOT NULL DEFAULT '',
        tone TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT '',
        response_length TEXT NOT NULL DEFAULT '',
        conditions_json TEXT NOT NULL DEFAULT '{}',
        exclusions_json TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_autoreply_rules_owner
        ON autoreply_rules(owner_user_id, priority DESC);

    -- Facts the responder remembers per conversation (bounded, expiring).
    CREATE TABLE IF NOT EXISTS autoreply_memory (
        id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'FACT',
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_autoreply_memory_chat
        ON autoreply_memory(owner_user_id, chat_id, updated_at DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_memory_dedupe
        ON autoreply_memory(owner_user_id, chat_id, content_hash);

    -- Rolling conversation context the prompt is built from (retention-swept).
    CREATE TABLE IF NOT EXISTS autoreply_messages (
        id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        message_id INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_autoreply_messages_chat
        ON autoreply_messages(owner_user_id, chat_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_autoreply_messages_retention
        ON autoreply_messages(created_at) WHERE created_at > 0;

    -- The queue itself: idempotent per (host, chat, message), leased, retried,
    -- dead-lettered within this table so there is no second table to sweep.
    CREATE TABLE IF NOT EXISTS autoreply_jobs (
        id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        chat_id TEXT NOT NULL,
        chat_title TEXT NOT NULL DEFAULT '',
        source_message_id INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'PENDING',
        attempt INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        next_attempt_at INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT NOT NULL DEFAULT '',
        lease_until INTEGER NOT NULL DEFAULT 0,
        prompt_id TEXT NOT NULL DEFAULT '',
        rule_id TEXT NOT NULL DEFAULT '',
        ai_config_id TEXT NOT NULL DEFAULT '',
        inbound_text TEXT NOT NULL DEFAULT '',
        reply_text TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        correlation_id TEXT NOT NULL DEFAULT '',
        latency_ms INTEGER NOT NULL DEFAULT 0,
        sent_message_id INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        sent_at INTEGER NOT NULL DEFAULT 0
    );
    -- Idempotency: one inbound private message creates at most one job, even
    -- when the event fires twice (poller replay, reconnect).
    CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_jobs_dedupe
        ON autoreply_jobs(owner_user_id, session_id, chat_id, source_message_id);
    -- Claim path: same shape the delivery queue uses, so the planner walks it.
    CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_claim
        ON autoreply_jobs(status, next_attempt_at, created_at);
    -- Per-chat ordering: a chat's jobs are sent in the order they arrived.
    CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_lane
        ON autoreply_jobs(owner_user_id, session_id, chat_id, status, source_message_id);
    -- Tenant queue listings are newest-first.
    CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_owner
        ON autoreply_jobs(owner_user_id, created_at DESC);
    -- Dead-letter browser.
    CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_dead
        ON autoreply_jobs(status, updated_at);
    """,
    18: """
    -- v18: the lane's order is the source's own order, not the ingest second.
    -- v17's key started with created_at, which is a one-second timestamp. A
    -- 50-post burst that straddled a second boundary was therefore reordered by
    -- *when the poller read each post* instead of by the post's own order: the
    -- post that happened to arrive first but had a higher message id jumped
    -- ahead of every lower id, which is exactly the reported symptom ("50
    -- messages sent at the same time are not forwarded in the source order").
    -- created_at still breaks ties, but only within one source chat.
    DROP INDEX IF EXISTS idx_delivery_jobs_lane;
    CREATE INDEX IF NOT EXISTS idx_delivery_jobs_lane
        ON delivery_jobs(target_chat_id, status, source_chat_id, source_message_id);
    """,
    27: """
    -- v27: "Forward every Nth message" needs to know where a task is in its
    -- cadence, and that position is state, not a statistic: deriving it from
    -- processed_messages would lose it on the 30-day retention sweep (and a
    -- sweep between two messages would forward two in a row). One row per task,
    -- one UPSERT per accepted message. `every_n` is stored beside the count so a
    -- settings change can be detected inside the same statement and restart the
    -- cycle, without a second read or a lock held across two calls.
    CREATE TABLE IF NOT EXISTS rule_counters (
        rule_id TEXT PRIMARY KEY,
        owner_user_id INTEGER NOT NULL DEFAULT 0,
        every_n INTEGER NOT NULL DEFAULT 1,
        accepted INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_rule_counters_owner ON rule_counters(owner_user_id);
    """,
    29: """
    -- v29: referral attribution for /make_money.
    -- One row per invited account, keyed by the invitee so a second /start with
    -- somebody else's link cannot re-attribute an existing user (INSERT OR
    -- IGNORE). The inviter is a Telegram user id, not a task: no foreign key to
    -- forward_rules, because an invitee never has to own a task to count.
    CREATE TABLE IF NOT EXISTS referrals (
        invitee_user_id INTEGER PRIMARY KEY,
        inviter_user_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_referrals_inviter
        ON referrals(inviter_user_id, created_at DESC);
    """,
    30: """
    -- v30: a filter row can be switched off without losing its lists. The
    -- toggle is a first-class column (not a metadata blob) so the engine, the
    -- API and every client read the same value a write stored, and a legacy
    -- row defaults to enabled: a filter that already exists keeps working.
    --
    -- A database that predates the filter_rules table entirely (an install
    -- that never created a saved filter) reaches this migration with no
    -- table at all, so the table is created with the full current shape
    -- first. The column itself is deliberately NOT in the CREATE: a table
    -- carried over from an older schema already exists, so the guarded ALTER
    -- in _apply_additive_columns adds the column — the same code path that
    -- heals a database restored from any older backup.
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
        owner_user_id INTEGER NOT NULL DEFAULT 0,
        regex_block TEXT NOT NULL DEFAULT '[]',
        regex_require TEXT NOT NULL DEFAULT '[]'
    );
    """,
}


# Derived from the migrations themselves rather than hand-maintained: this
# constant had drifted to 15 while MIGRATIONS ran to 19, so every comparison
# against a live database's version compared against a version no database
# had. A new migration now moves it, by construction.
SCHEMA_VERSION = max(MIGRATIONS)

# Columns added by additive migrations that must also work on a table carried
# over from an older schema (v11 runs the guarded ALTER in _apply_additive_columns).
# (table, column, declaration, index) — an empty index name creates no index,
# for columns that are read with their row and never filtered on.
_ADDITIVE_COLUMNS = (
    ("ai_configs", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_ai_configs_owner"),
    ("filter_rules", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_filter_rules_owner"),
    ("filter_rules", "regex_block", "TEXT NOT NULL DEFAULT '[]'", None),
    ("filter_rules", "regex_require", "TEXT NOT NULL DEFAULT '[]'", None),
    # v30: the enable/disable toggle. A database carried over from an older
    # schema lacks the column; a guarded ALTER adds it as enabled so an existing
    # filter keeps working instead of silently switching off.
    ("filter_rules", "is_enabled", "INTEGER NOT NULL DEFAULT 1", None),
    # v12: credential vault + prompt templates are per-tenant, not global.
    # Without this any authenticated tenant can read another tenant's api_hash
    # through a backup export, and overwrite another tenant's prompt templates.
    ("api_credentials", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_api_credentials_owner"),
    ("prompt_templates", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_prompt_templates_owner"),
    ("users", "status", "TEXT NOT NULL DEFAULT 'active'", "idx_users_status"),
    # v19: a bot token belongs to the tenant that pasted it. Without an owner
    # every owner's route screen listed every other owner's bots, and any
    # tenant could attach — or delete — another tenant's credential.
    ("bot_tokens", "owner_user_id", "INTEGER NOT NULL DEFAULT 0", "idx_bot_tokens_owner"),
    # v20: per-bot defaults ("{}" = none, the behaviour of every bot attached
    # before this existed). Read with the token row, never queried on its own.
    ("bot_tokens", "defaults_json", "TEXT NOT NULL DEFAULT '{}'", ""),
    # v20: the deliverable kind ("forward" | "keyword_reply") is part of a queue
    # row's *identity*. The column is added here; the UNIQUE key that must
    # include it cannot be altered in place, so an existing table is rebuilt by
    # _rebuild_delivery_jobs_deliverable_key (called below, after this loop).
    ("delivery_jobs", "job_kind", "TEXT NOT NULL DEFAULT 'forward'", ""),
)

# Columns the rebuilt delivery_jobs table must have. Each entry is applied as a
# guarded ALTER before the rebuild copies rows, so a table restored from an old
# backup lands on the same schema as a freshly created one.
_DELIVERY_JOB_COLUMNS = (
    ("job_kind", "TEXT NOT NULL DEFAULT 'forward'"),
    ("intermediate_chat_id", "TEXT"),
    ("intermediate_message_id", "INTEGER"),
    ("delivery_stage", "TEXT NOT NULL DEFAULT 'DIRECT'"),
)

# The rebuilt table's UNIQUE key, compared against sqlite_master.sql with all
# whitespace removed (SQLite stores the original text, whose spacing is not
# guaranteed).
_DELIVERY_JOB_UNIQUE_COLS = (
    "rule_id", "source_chat_id", "source_message_id", "target_chat_id", "job_kind",
)

_DELIVERY_JOBS_REBUILD_DDL = """
CREATE TABLE delivery_jobs_migrating (
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
    job_kind TEXT NOT NULL DEFAULT 'forward',
    intermediate_chat_id TEXT,
    intermediate_message_id INTEGER,
    delivery_stage TEXT NOT NULL DEFAULT 'DIRECT',
    UNIQUE(rule_id, source_chat_id, source_message_id, target_chat_id, job_kind)
);
"""


class Row(dict):
    """A result row that is both a mapping and a positional sequence.

    SQLite hands back ``sqlite3.Row``, which supports ``row["col"]`` and
    ``row[0]`` but **not** ``row.get(...)``, ``row.values()`` or ``row in``.
    PostgreSQL hands back a dict. Code written against one engine therefore broke
    on the other, and broke quietly: the reads sat inside ``except Exception:
    pass`` blocks, so a user's stored language preference resolved as English
    instead of raising. Making the two engines agree on the shape is the only fix
    that cannot be defeated by the next row read somebody writes.

    The name lookup keeps ``sqlite3.Row``'s error: a key that is not a column
    raises ``IndexError``, which existing handlers already catch.
    """

    __slots__ = ("_values",)

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        super().__init__(zip(columns, values))
        self._values: Tuple[Any, ...] = tuple(values)

    def __getitem__(self, key):
        # Hot path: this runs once per column per row, and a rules page or an
        # inbound message hydrates thousands of them (measured: 2M lookups for
        # one 1000-rule resolve). `type(key) is int` skips the subclass scan of
        # isinstance, and `dict.__getitem__` skips building a `super` object.
        if type(key) is int:
            return self._values[key]
        try:
            return dict.__getitem__(self, key)
        except KeyError as exc:  # sqlite3.Row semantics
            raise IndexError(key) from exc

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def to_dict(self) -> Dict[str, Any]:
        """A plain ``dict`` copy, for code that wants to mutate it."""
        return dict(self.items())


def _row_factory(cursor: sqlite3.Cursor, values: Sequence[Any]) -> Row:
    return Row([column[0] for column in cursor.description or ()], values)


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
        # Rows are mappings on both engines (see `Row`): PostgreSQL returns a
        # dict, and `sqlite3.Row` does not, which silently broke every read that
        # used `.get(...)` on this side of the boundary.
        self._conn.row_factory = _row_factory
        self._lock = threading.Lock()
        self._apply_pragmas()
        self._migrate()

    @property
    def path(self) -> str:
        """The database this handle points at (``:memory:`` included)."""
        return self._db_path

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
            if not index:
                continue
            try:
                self._conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {index} ON {table}({column})"
                )
            except Exception:
                pass

        self._apply_scale_indexes()
        # v20: widen the queue's dedup key (see the method for why an existing
        # table must be rebuilt rather than ALTERed).
        self._rebuild_delivery_jobs_deliverable_key()

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

    def _rebuild_delivery_jobs_deliverable_key(self) -> None:
        """Widen ``delivery_jobs``' dedup key to include the deliverable kind.

        The queue dedups with ``INSERT OR IGNORE`` on a UNIQUE key that used to
        be (rule_id, source_chat_id, source_message_id, target_chat_id). A
        keyword reply is delivered to the *same* target as the forward of the
        same source message, so its row matched that key, the insert was
        ignored, ``enqueue_delivery`` returned False and the reply never
        shipped — while the pipeline logged that it had.

        SQLite cannot alter a UNIQUE constraint in place, so the table is
        rebuilt once: rows are copied into a new table whose key carries
        ``job_kind``, the row counts are compared, and only then is the old
        table dropped and the new one renamed over it. The swap runs inside a
        SAVEPOINT, so a failure leaves the existing table and every queued job
        exactly as they were.
        """
        try:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='delivery_jobs'"
            ).fetchone()
        except Exception:
            return
        if not row or not row["sql"]:
            return
        flat = "".join(str(row["sql"]).split()).lower()
        wanted = "unique(" + ",".join(_DELIVERY_JOB_UNIQUE_COLS) + ")"
        if wanted in flat:
            return

        # Every column the rebuild copies must exist first. The additive loop
        # above already adds job_kind; the v9 hop columns may still be missing
        # on a table restored from an older backup.
        for column, decl in _DELIVERY_JOB_COLUMNS:
            cols = {
                str(r["name"])
                for r in self._conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()
            }
            if column in cols:
                continue
            try:
                self._conn.execute(f"ALTER TABLE delivery_jobs ADD COLUMN {column} {decl}")
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("[DB:MIGRATION] delivery_jobs rebuild skipped: %s", exc)
                return

        cols = {
            str(r["name"])
            for r in self._conn.execute("PRAGMA table_info(delivery_jobs)").fetchall()
        }
        target_cols = [
            "id", "rule_id", "source_chat_id", "source_message_id", "target_chat_id",
            "payload_json", "status", "attempts", "max_attempts", "next_retry_at",
            "lease_until", "worker_id", "error_detail", "created_at", "updated_at",
            "job_kind", "intermediate_chat_id", "intermediate_message_id", "delivery_stage",
        ]
        # A column absent from the old table is left out of the INSERT so the
        # new table's own default applies; only job_kind is derivable from the
        # row itself (the reply's id carries its kind as a suffix).
        copy_cols = [c for c in target_cols if c in cols]
        exprs = [
            "CASE WHEN id LIKE '%:keyword_reply' THEN 'keyword_reply' ELSE 'forward' END"
            if c == "job_kind" else c
            for c in copy_cols
        ]

        self._conn.execute("SAVEPOINT delivery_jobs_rebuild")
        try:
            before = self._conn.execute(
                "SELECT COUNT(*) AS n FROM delivery_jobs"
            ).fetchone()["n"]
            self._conn.execute(_DELIVERY_JOBS_REBUILD_DDL)
            self._conn.execute(
                f"INSERT INTO delivery_jobs_migrating ({', '.join(copy_cols)}) "
                f"SELECT {', '.join(exprs)} FROM delivery_jobs"
            )
            after = self._conn.execute(
                "SELECT COUNT(*) AS n FROM delivery_jobs_migrating"
            ).fetchone()["n"]
            if int(after) != int(before):
                raise RuntimeError(f"row count changed during rebuild: {before} -> {after}")
            self._conn.execute("DROP TABLE delivery_jobs")
            self._conn.execute("ALTER TABLE delivery_jobs_migrating RENAME TO delivery_jobs")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_jobs_poll "
                "ON delivery_jobs(status, next_retry_at, lease_until)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_jobs_rule ON delivery_jobs(rule_id)"
            )
        except Exception as exc:
            self._conn.execute("ROLLBACK TO delivery_jobs_rebuild")
            self._conn.execute("RELEASE delivery_jobs_rebuild")
            logger.error(
                "[DB:MIGRATION] delivery_jobs deliverable-key rebuild failed, "
                "table left untouched: %s", exc,
            )
            return
        self._conn.execute("RELEASE delivery_jobs_rebuild")
        logger.info(
            "[DB:MIGRATION] delivery_jobs dedup key now includes job_kind (%s row(s) migrated)",
            after,
        )

    def _apply_scale_indexes(self) -> None:
        """Install the indexes that keep two hot reads bounded as tenants grow.

        Both were found by measuring, not by reading the schema:

        * ``list_active_by_source`` — the query the worker runs for every inbound
          message — was a full scan of ``forward_rules``. Its
          ``source_chat_id = ? OR EXISTS(json_each(...))`` cannot be answered by
          a single-column index, so at 10,000 tenants every message read every
          rule. The fix is a derived flag (``has_extra_sources``), computed by
          SQLite from ``detection_criteria`` itself, plus a *partial* index that
          covers only the rows that actually carry extra sources — so the JSON
          branch scans a handful of rows instead of the whole table.

          Derived, not stored: nothing writes it, so it cannot drift from the
          JSON it is derived from, and no backfill is needed for existing rows.

        * the owner-scoped task page sorted through a temp B-tree; a composite
          ``(owner_user_id, created_at)`` index matches the read exactly. The
          single-column index it subsumes is dropped, so the write cost does not
          grow.

        Idempotent, and safe to run against a database that already has either
        index: everything is guarded.
        """
        try:
            cols = {
                str(r[0])
                for r in self._conn.execute("PRAGMA table_info(forward_rules)").fetchall()
            }
        except Exception:
            return
        if not cols:
            # No such table (a database that predates it): nothing to index.
            return
        if "has_extra_sources" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE forward_rules ADD COLUMN has_extra_sources INTEGER"
                    " GENERATED ALWAYS AS (CASE WHEN detection_criteria IS NOT NULL"
                    "   AND json_valid(detection_criteria)"
                    "   AND json_type(detection_criteria, '$.source_chat_ids') = 'array'"
                    "   AND json_array_length(json_extract(detection_criteria,"
                    "        '$.source_chat_ids')) > 0"
                    "  THEN 1 ELSE 0 END) VIRTUAL"
                )
            except Exception:
                # SQLite older than 3.31 (or a build without JSON1) cannot do
                # this. The repository's query still returns the right rows in
                # that case; it just cannot be indexed, so leave it unindexed
                # rather than refuse to start.
                return
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_rules_extra_sources"
            " ON forward_rules(has_extra_sources) WHERE has_extra_sources = 1",
            "CREATE INDEX IF NOT EXISTS idx_rules_owner_created"
            " ON forward_rules(owner_user_id, created_at)",
        ):
            try:
                self._conn.execute(ddl)
            except Exception:
                pass
        # Subsumed by the composite index above: any owner-only lookup can use
        # the leftmost prefix, so keeping it would only cost writes.
        try:
            self._conn.execute("DROP INDEX IF EXISTS idx_rules_owner")
        except Exception:
            pass

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

    # ------------------------------------------------------------------ #
    # Referrals (/make_money)
    # ------------------------------------------------------------------ #
    def record_referral(self, invitee_user_id: int, inviter_user_id: int,
                        now: Optional[int] = None) -> bool:
        """Attribute one invitee to one inviter, first writer wins.

        Returns True only when a row was actually inserted, so a repeated
        ``/start ref_<id>`` (Telegram replays it) does not inflate a count and
        a second link cannot steal an existing invitee.
        """
        invitee = int(invitee_user_id or 0)
        inviter = int(inviter_user_id or 0)
        if invitee <= 0 or inviter <= 0 or invitee == inviter:
            return False
        ts = int(now if now is not None else time.time())
        try:
            cur = self.execute(
                "INSERT OR IGNORE INTO referrals (invitee_user_id, inviter_user_id, created_at)"
                " VALUES (?,?,?)",
                (invitee, inviter, ts),
            )
            return bool(cur.rowcount)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning("referral insert failed: %s", exc)
            return False

    def count_referrals(self, inviter_user_id: int) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM referrals WHERE inviter_user_id=?",
            (int(inviter_user_id or 0),),
        )
        return int(row["n"]) if row else 0

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
