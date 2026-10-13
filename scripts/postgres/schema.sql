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
    CONSTRAINT delivery_jobs_unique UNIQUE (rule_id, source_chat_id, source_message_id, target_chat_id)
);

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

COMMIT;
