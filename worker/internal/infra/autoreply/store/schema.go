package store

const SQLiteSchema = `
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
    ON autoreply_messages(created_at);

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_jobs_dedupe
    ON autoreply_jobs(owner_user_id, session_id, chat_id, source_message_id);
CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_claim
    ON autoreply_jobs(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_lane
    ON autoreply_jobs(owner_user_id, session_id, chat_id, status, source_message_id);
CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_owner
    ON autoreply_jobs(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_dead
    ON autoreply_jobs(status, updated_at);
`

const PostgresSchema = `
CREATE TABLE IF NOT EXISTS autoreply_settings (
    owner_user_id BIGINT PRIMARY KEY,
    enabled INT NOT NULL DEFAULT 0,
    ai_config_id TEXT NOT NULL DEFAULT '',
    prompt_id TEXT NOT NULL DEFAULT '',
    target_mode TEXT NOT NULL DEFAULT 'EVERYONE',
    language TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT 'friendly',
    response_length TEXT NOT NULL DEFAULT 'short',
    temperature DOUBLE PRECISION NOT NULL DEFAULT 0.7,
    max_tokens INT NOT NULL DEFAULT 300,
    context_messages INT NOT NULL DEFAULT 6,
    memory_enabled INT NOT NULL DEFAULT 1,
    memory_max_facts INT NOT NULL DEFAULT 12,
    extra_instructions TEXT NOT NULL DEFAULT '',
    max_reply_chars INT NOT NULL DEFAULT 1200,
    typing_simulation INT NOT NULL DEFAULT 1,
    typing_delay_min DOUBLE PRECISION NOT NULL DEFAULT 1.5,
    typing_delay_max DOUBLE PRECISION NOT NULL DEFAULT 4.0,
    cooldown_seconds INT NOT NULL DEFAULT 15,
    daily_limit_per_chat INT NOT NULL DEFAULT 0,
    rate_limit_per_minute INT NOT NULL DEFAULT 20,
    max_concurrency INT NOT NULL DEFAULT 3,
    max_attempts INT NOT NULL DEFAULT 3,
    retry_backoff_seconds DOUBLE PRECISION NOT NULL DEFAULT 20.0,
    quiet_hours_start INT NOT NULL DEFAULT -1,
    quiet_hours_end INT NOT NULL DEFAULT -1,
    timezone_offset_minutes INT NOT NULL DEFAULT 0,
    active_days TEXT NOT NULL DEFAULT '[]',
    fallback_text TEXT NOT NULL DEFAULT '',
    ignore_bots INT NOT NULL DEFAULT 1,
    version INT NOT NULL DEFAULT 1,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS autoreply_prompts (
    id TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    user_prompt_template TEXT NOT NULL DEFAULT '{text}',
    tone TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    response_length TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    is_default INT NOT NULL DEFAULT 0,
    is_archived INT NOT NULL DEFAULT 0,
    use_count INT NOT NULL DEFAULT 0,
    last_used_at BIGINT NOT NULL DEFAULT 0,
    version INT NOT NULL DEFAULT 1,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_prompts_name
    ON autoreply_prompts(owner_user_id, name);
CREATE INDEX IF NOT EXISTS idx_autoreply_prompts_owner
    ON autoreply_prompts(owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS autoreply_targets (
    id TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL DEFAULT 'private',
    title TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    is_enabled INT NOT NULL DEFAULT 1,
    prompt_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    priority INT NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_targets_chat
    ON autoreply_targets(owner_user_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_autoreply_targets_enabled
    ON autoreply_targets(owner_user_id, is_enabled);

CREATE TABLE IF NOT EXISTS autoreply_rules (
    id TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    is_enabled INT NOT NULL DEFAULT 1,
    priority INT NOT NULL DEFAULT 10,
    action TEXT NOT NULL DEFAULT 'REPLY',
    cooldown_seconds INT NOT NULL DEFAULT -1,
    prompt_id TEXT NOT NULL DEFAULT '',
    ai_config_id TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    response_length TEXT NOT NULL DEFAULT '',
    conditions_json TEXT NOT NULL DEFAULT '{}',
    exclusions_json TEXT NOT NULL DEFAULT '{}',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autoreply_rules_owner
    ON autoreply_rules(owner_user_id, priority DESC);

CREATE TABLE IF NOT EXISTS autoreply_memory (
    id TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    chat_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'FACT',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_autoreply_memory_chat
    ON autoreply_memory(owner_user_id, chat_id, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_memory_dedupe
    ON autoreply_memory(owner_user_id, chat_id, content_hash);

CREATE TABLE IF NOT EXISTS autoreply_messages (
    id TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_id BIGINT NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autoreply_messages_chat
    ON autoreply_messages(owner_user_id, chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_autoreply_messages_retention
    ON autoreply_messages(created_at);

CREATE TABLE IF NOT EXISTS autoreply_jobs (
    id TEXT PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL,
    chat_title TEXT NOT NULL DEFAULT '',
    source_message_id BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempt INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    next_attempt_at BIGINT NOT NULL DEFAULT 0,
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until BIGINT NOT NULL DEFAULT 0,
    prompt_id TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL DEFAULT '',
    ai_config_id TEXT NOT NULL DEFAULT '',
    inbound_text TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    latency_ms INT NOT NULL DEFAULT 0,
    sent_message_id BIGINT NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    sent_at BIGINT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_jobs_dedupe
    ON autoreply_jobs(owner_user_id, session_id, chat_id, source_message_id);
CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_claim
    ON autoreply_jobs(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_lane
    ON autoreply_jobs(owner_user_id, session_id, chat_id, status, source_message_id);
CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_owner
    ON autoreply_jobs(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_autoreply_jobs_dead
    ON autoreply_jobs(status, updated_at);
`
