-- AutoTelegramForward — PostgreSQL schema for the durable delivery queue (v14)
--
-- SQLite stays the default for single-node installs. This file is the
-- equivalent schema for ATF_DATABASE_URL deployments: the same tables and
-- the same claim path, with the index that removes the sort from
-- claim_batch and SKIP LOCKED doing the row-level locking.
--
-- Apply once per database:
--   psql "$ATF_DATABASE_URL" -f scripts/postgres/schema.sql
--
-- The service owns ONLY these tables. Sessions, rules, filters and the rest
-- of the application schema remain in SQLite (multi-DB architecture: each
-- service owns its data) unless you migrate them separately.

BEGIN;

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
    -- The deliverable kind ("forward" | "keyword_reply") is part of the dedup
    -- key: one source message carries a forward *and* a keyword reply to the
    -- same target, and with the kind left out of the key the reply collided
    -- with the forward's row and was dropped by ON CONFLICT DO NOTHING.
    job_kind TEXT NOT NULL DEFAULT 'forward',
    CONSTRAINT delivery_jobs_unique
        UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id, job_kind)
);

-- Existing installs created before the deliverable kind was part of the key:
-- widen the constraint once. Adding the column is additive-safe; the UNIQUE
-- swap is idempotent because the constraint name is checked first.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'delivery_jobs' AND column_name = 'job_kind'
    ) THEN
        ALTER TABLE delivery_jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'forward';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'delivery_jobs_unique'
          AND pg_get_constraintdef(oid) NOT LIKE '%job_kind%'
    ) THEN
        ALTER TABLE delivery_jobs DROP CONSTRAINT delivery_jobs_unique;
        ALTER TABLE delivery_jobs ADD CONSTRAINT delivery_jobs_unique
            UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id, job_kind);
    END IF;
END $$;

-- Claim path: the WHERE clause is (status, next_retry_at, lease_until) and
-- the query ORDERs BY created_at. Leading with all three predicate columns
-- plus created_at lets the planner satisfy the claim with an ordered index
-- walk and stop at LIMIT — no sort. Without created_at the claim did a
-- TEMP B-TREE sort on every poll.
CREATE INDEX IF NOT EXISTS idx_delivery_jobs_claim
    ON delivery_jobs (status, next_retry_at, lease_until, created_at);

CREATE INDEX IF NOT EXISTS idx_delivery_jobs_rule ON delivery_jobs (rule_id);

-- Retention: cleanup() deletes by status + updated_at.
CREATE INDEX IF NOT EXISTS idx_delivery_jobs_cleanup
    ON delivery_jobs (status, updated_at);

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

CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    source_message_id BIGINT NOT NULL,
    target_chat_id TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    error_category TEXT NOT NULL DEFAULT '',
    dead_lettered_at BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dlq_rule ON dead_letter_queue (rule_id, dead_lettered_at);
CREATE INDEX IF NOT EXISTS idx_dlq_ts ON dead_letter_queue (dead_lettered_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dlq_job ON dead_letter_queue (job_id);

-- Content fingerprint deduplication store (v2.0)
CREATE TABLE IF NOT EXISTS content_dedup (
    id BIGSERIAL PRIMARY KEY,
    rule_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    CONSTRAINT content_dedup_unique UNIQUE (rule_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_content_dedup_rule ON content_dedup (rule_id, created_at);

-- Multi-tenant forward rules
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

-- Prompt templates and versions
CREATE TABLE IF NOT EXISTS prompt_templates (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    system_prompt TEXT NOT NULL,
    user_prompt_template TEXT NOT NULL,
    target_language VARCHAR(8) NOT NULL DEFAULT 'en',
    current_version INTEGER NOT NULL DEFAULT 1,
    is_system BOOLEAN NOT NULL DEFAULT FALSE,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    owner_user_id BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pg_prompt_templates_lang ON prompt_templates (target_language);
CREATE INDEX IF NOT EXISTS idx_pg_prompt_templates_owner ON prompt_templates (owner_user_id);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id VARCHAR(64) PRIMARY KEY,
    prompt_id VARCHAR(64) NOT NULL REFERENCES prompt_templates(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    system_prompt TEXT NOT NULL,
    user_prompt_template TEXT NOT NULL,
    change_summary TEXT DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at BIGINT NOT NULL,
    CONSTRAINT prompt_versions_unique UNIQUE (prompt_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pg_prompt_versions_active ON prompt_versions (prompt_id, is_active);

-- Live rule stats, hourly metrics, and error logs
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

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

COMMIT;
