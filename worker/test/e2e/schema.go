package e2e

import (
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// bootstrapSchema installs every table the production worker reads that the
// queue test helper does not create. Shapes mirror the Python Core's
// migrations (core/infrastructure/persistence/database.py) and the Go-side
// schemas that already exist in rulestore/counters tests.
func bootstrapSchema(t *testing.T, dbPath string) {
	t.Helper()
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatalf("bootstrap: open: %v", err)
	}
	defer db.Close()
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS forward_rules (
			id TEXT PRIMARY KEY,
			session_id TEXT NOT NULL DEFAULT '',
			source_chat_id TEXT NOT NULL DEFAULT '',
			source_chat_name TEXT NOT NULL DEFAULT '',
			target_chat_id TEXT NOT NULL DEFAULT '',
			target_chat_name TEXT NOT NULL DEFAULT '',
			routing_type TEXT NOT NULL DEFAULT 'CHANNEL_TO_CHANNEL',
			forward_mode TEXT NOT NULL DEFAULT 'COPY_MESSAGE',
			is_active INTEGER NOT NULL DEFAULT 1,
			filter_rule_id TEXT,
			ai_config_id TEXT NOT NULL DEFAULT '',
			remove_links INTEGER NOT NULL DEFAULT 0,
			custom_caption_template TEXT NOT NULL DEFAULT '',
			created_at INTEGER NOT NULL DEFAULT 0,
			updated_at INTEGER NOT NULL DEFAULT 0,
			target_chat_ids TEXT NOT NULL DEFAULT '[]',
			delay_seconds REAL NOT NULL DEFAULT 0,
			skip_history INTEGER NOT NULL DEFAULT 1,
			since_ts INTEGER NOT NULL DEFAULT 0,
			ignore_edits INTEGER NOT NULL DEFAULT 0,
			trigger_events TEXT NOT NULL DEFAULT '["NEW_MESSAGE"]',
			content_mode TEXT NOT NULL DEFAULT 'AUTO',
			metadata TEXT NOT NULL DEFAULT '{}',
			chain_of TEXT NOT NULL DEFAULT '',
			replacements TEXT NOT NULL DEFAULT '{}',
			header TEXT NOT NULL DEFAULT '',
			footer TEXT NOT NULL DEFAULT '',
			sync_edits INTEGER NOT NULL DEFAULT 0,
			sync_deletes INTEGER NOT NULL DEFAULT 0,
			allow_senders TEXT NOT NULL DEFAULT '[]',
			block_senders TEXT NOT NULL DEFAULT '[]',
			target_topic_id INTEGER NOT NULL DEFAULT 0,
			version INTEGER NOT NULL DEFAULT 1,
			ai_fallback_policy TEXT NOT NULL DEFAULT 'DROP',
			ai_prompt_version INTEGER NOT NULL DEFAULT 0,
			ai_timeout_seconds REAL NOT NULL DEFAULT 15.0,
			ai_secondary_config_id TEXT,
			message_category TEXT NOT NULL DEFAULT 'ALL',
			intermediate_channel_id TEXT NOT NULL DEFAULT '',
			intermediate_channel_name TEXT NOT NULL DEFAULT '',
			intermediate_target_chat_id TEXT NOT NULL DEFAULT '',
			use_intermediate INTEGER NOT NULL DEFAULT 0,
			fallback_mode TEXT NOT NULL DEFAULT 'COPY_MESSAGE',
			fallback_enabled INTEGER NOT NULL DEFAULT 1,
			detection_criteria TEXT NOT NULL DEFAULT '{}',
			priority INTEGER NOT NULL DEFAULT 10,
			execution_order INTEGER NOT NULL DEFAULT 0,
			multi_route INTEGER NOT NULL DEFAULT 0,
			media_handling TEXT NOT NULL DEFAULT 'AUTO',
			dedupe_policy TEXT NOT NULL DEFAULT 'STRICT',
			max_retries INTEGER NOT NULL DEFAULT 3,
			retry_backoff_base REAL NOT NULL DEFAULT 2.0,
			rate_limit_per_minute INTEGER NOT NULL DEFAULT 25,
			rate_limit_burst INTEGER NOT NULL DEFAULT 0,
			custom_header TEXT NOT NULL DEFAULT '',
			custom_footer TEXT NOT NULL DEFAULT '',
			header_enabled INTEGER NOT NULL DEFAULT 0,
			template_text TEXT NOT NULL DEFAULT '',
			template_media TEXT NOT NULL DEFAULT '',
			template_album TEXT NOT NULL DEFAULT '',
			caption_max_length INTEGER NOT NULL DEFAULT 0,
			preserve_signature INTEGER NOT NULL DEFAULT 0,
			is_paused INTEGER NOT NULL DEFAULT 0,
			paused_until INTEGER NOT NULL DEFAULT 0,
			link_policy TEXT NOT NULL DEFAULT 'PRESERVE_ALL',
			domain_allowlist TEXT NOT NULL DEFAULT '[]',
			domain_blocklist TEXT NOT NULL DEFAULT '[]',
			link_rewrite_map TEXT NOT NULL DEFAULT '{}',
			allowed_media_types TEXT NOT NULL DEFAULT '[]',
			split_long_caption INTEGER NOT NULL DEFAULT 1,
			owner_user_id INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS filter_rules (
			id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
			is_enabled INTEGER NOT NULL DEFAULT 1,
			whitelist_keywords TEXT NOT NULL DEFAULT '[]',
			blacklist_keywords TEXT NOT NULL DEFAULT '[]',
			regex_patterns TEXT NOT NULL DEFAULT '[]',
			allowed_media_types TEXT NOT NULL DEFAULT '[]',
			blocked_media_types TEXT NOT NULL DEFAULT '[]',
			min_message_length INTEGER NOT NULL DEFAULT 0,
			max_message_length INTEGER NOT NULL DEFAULT 0,
			owner_user_id INTEGER NOT NULL DEFAULT 0,
			regex_block TEXT NOT NULL DEFAULT '[]',
			regex_require TEXT NOT NULL DEFAULT '[]',
			drop_service_messages INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS rule_counters (
			rule_id TEXT PRIMARY KEY,
			accepted INTEGER NOT NULL DEFAULT 0,
			owner_user_id INTEGER NOT NULL DEFAULT 0,
			updated_at INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS bot_tokens (
			id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',
			token_encrypted TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1,
			created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
			owner_user_id INTEGER NOT NULL DEFAULT 0, defaults_json TEXT NOT NULL DEFAULT '{}'
		)`,
		`CREATE TABLE IF NOT EXISTS ai_configs (
			id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
			provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
			api_key_encrypted TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
			system_prompt TEXT NOT NULL DEFAULT '',
			user_prompt_template TEXT NOT NULL DEFAULT '{text}',
			temperature REAL NOT NULL DEFAULT 0.7,
			is_enabled INTEGER NOT NULL DEFAULT 1,
			target_language TEXT NOT NULL DEFAULT 'en',
			owner_user_id INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS prompt_templates (
			id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
			description TEXT NOT NULL DEFAULT '',
			system_prompt TEXT NOT NULL DEFAULT '',
			user_prompt_template TEXT NOT NULL DEFAULT '{text}',
			target_language TEXT NOT NULL DEFAULT 'en',
			current_version INTEGER NOT NULL DEFAULT 1,
			is_system INTEGER NOT NULL DEFAULT 0,
			created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
			owner_user_id INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS prompt_versions (
			id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL,
			version INTEGER NOT NULL, system_prompt TEXT NOT NULL DEFAULT '',
			user_prompt_template TEXT NOT NULL DEFAULT '{text}',
			change_summary TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1,
			created_at INTEGER NOT NULL DEFAULT 0
		)`,
		`CREATE TABLE IF NOT EXISTS processed_messages (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			rule_id TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
			media_group_id TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL DEFAULT 0,
			UNIQUE(rule_id, chat_id, message_id)
		)`,
		`CREATE TABLE IF NOT EXISTS content_dedup (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			rule_id TEXT NOT NULL, content_hash TEXT NOT NULL,
			status TEXT NOT NULL DEFAULT 'claimed',
			created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
			UNIQUE(rule_id, content_hash)
		)`,
	}
	for _, st := range stmts {
		if _, err := db.Exec(st); err != nil {
			t.Fatalf("bootstrap: %v", err)
		}
	}
}
