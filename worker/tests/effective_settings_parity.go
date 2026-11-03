//go:build stage42_parity
// +build stage42_parity

package main

// effective_settings_parity.go is the Go half of the
// "effective_settings" parity engine: the cases that pin the
// configuration-ownership plane (Stage 5 W2) across the two engines.
//
// Each case is answered by the REAL production code paths:
//
//   - "resolution" cases run settings.Effective over the case's metadata
//     and the bot's defaults — the same resolution the rulestore's
//     materialize performs at load time;
//   - "pinned" cases run settings.PinnedKeys so the two engines agree on
//     which settings are the task's OWN choices (the decision every
//     inheritance question turns on);
//   - "materialize" cases run settings.Materialize — the copy-not-mutate
//     contract a save attempt depends on;
//   - "store" cases seed a real bot_tokens table and run the rulestore's
//     owner-scoped read through a real composite store — the isolation
//     boundary measured against the same SQLite DDL production uses.
//
// The internal packages are not importable from package main, so every
// case runs through the PARITY PROBES the packages export for exactly
// this purpose (ProbeEffective/ProbePinned/ProbeMaterialize and
// rulestore.ProbeEffectiveStore) — the same convention ProbeTransform
// established for the Stage 4.2 harness. A case therefore exercises the
// same code the delivery engine consults, never a reimplementation.
//
// The Python adapters in scripts/test_stage42_parity.py answer the same
// cases through core.domain.bot_defaults / core.domain.rule_settings and
// (for the store cases) the real SqliteBotTokenRepository, so a divergence
// is the two engines disagreeing about the same task.

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"atf/worker/internal/rulestore"
	_ "github.com/mattn/go-sqlite3"
)

// runEffectiveSettingsCase answers one effective_settings case.
//
// The output grammar is deliberately compact and stable: the Python side
// produces the SAME strings from its own adapters, so the comparison is
// byte-for-byte without either side formatting the other's shape.
//
//	"resolved <key>=<json>"          resolution: the effective value
//	"pinned <key>=true|false"        pinned: the task's own choice?
//	"copy=unchanged"                 materialize: nothing was inherited
//	"copy=done mark=<list> values=…" materialize: what resolution filled in
//	"copy=… resolved=… stored=…"     store: the owner-scoped read, plus
//	                                 what the STORED row still holds
func runEffectiveSettingsCase(c testCase) string {
	switch op := c.SettingsOp; op {
	case "", "resolution":
		return effectiveResolution(c)
	case "pinned":
		return effectivePinned(c)
	case "materialize":
		return effectiveMaterialize(c)
	case "store":
		return effectiveStore(c)
	default:
		return "unknown op: " + op
	}
}

// defaultsFor reads the case's bot_defaults blob as the Python adapter
// does: null means "no bot attached" (nil), anything else must be a JSON
// object — the shape defaults_json carries.
func defaultsFor(raw any) map[string]any {
	if dict, ok := raw.(map[string]any); ok {
		return dict
	}
	return nil
}

// effectiveResolution answers "which value does this task run with?" for
// every key the case asks about, through the same settings.Effective call
// the rulestore's materialize performs.
func effectiveResolution(c testCase) string {
	values := rulestore.ProbeSettingsEffective(c.Metadata, defaultsFor(c.BotDefaults))
	return "resolved " + joinKeyValues(values, effectiveCaseKeys(c))
}

// effectivePinned answers "is this key the task's own choice?" through the
// real PinnedKeys — the decision every inheritance question turns on, and
// the one place a Go re-derivation could quietly disagree with Python.
func effectivePinned(c testCase) string {
	pinned := rulestore.ProbeSettingsPinned(c.Metadata)
	out := make([]string, 0, len(effectiveCaseKeys(c)))
	for _, key := range effectiveCaseKeys(c) {
		out = append(out, fmt.Sprintf("%s=%t", key, pinned[key]))
	}
	return "pinned " + strings.Join(out, " ")
}

// effectiveMaterialize answers "what did resolution actually fill in, and
// what mark does it carry?" — the contract a save attempt depends on.
func effectiveMaterialize(c testCase) string {
	values, inherited, copied := rulestore.ProbeSettingsMaterialized(c.Metadata, defaultsFor(c.BotDefaults))
	if !copied {
		return "copy=unchanged"
	}
	names := append([]string(nil), inherited...)
	sort.Strings(names)
	return fmt.Sprintf("copy=done mark=%s values=%s",
		strings.Join(names, ","), joinKeyValues(values, effectiveCaseKeys(c)))
}

// effectiveStore runs the owner-scoped read against a REAL composite store
// over a real bot_tokens table: the case seeds the bots, the probe loads
// the rule through the rulestore's own GetByID, and the result reports
// both what the task resolved to and what the STORED row still holds (the
// copy-not-mutate rule, measured against the database).
func effectiveStore(c testCase) string {
	dir, err := os.MkdirTemp("", "parity-settings")
	if err != nil {
		return "mkdir: " + err.Error()
	}
	defer os.RemoveAll(dir)
	dbPath := dir + "/atf.db"
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		return "open: " + err.Error()
	}
	defer db.Close()
	if _, err := db.Exec(paritySettingsSchema); err != nil {
		return "schema: " + err.Error()
	}

	// Seed every bot row the case declares.
	for _, bot := range c.SettingsBots {
		raw := "{}"
		if bot.Defaults != nil {
			out, err := json.Marshal(bot.Defaults)
			if err != nil {
				return "seed: " + err.Error()
			}
			raw = string(out)
		}
		if _, err := db.Exec(`INSERT OR REPLACE INTO bot_tokens
			(id, label, token_encrypted, is_active, created_at, updated_at, owner_user_id, defaults_json)
			VALUES (?,?,?,?,?,?,?,?)`,
			bot.TokenID, bot.TokenID, "ENCRYPTED", 1, 0, 0, bot.Owner, raw); err != nil {
			return "seed: " + err.Error()
		}
	}

	// The rule row: the case's metadata (which carries bot_token_id and
	// any task-side pins) and the owner the scoped read binds to.
	meta := "{}"
	if c.Metadata != nil {
		raw, err := json.Marshal(c.Metadata)
		if err != nil {
			return "rule: " + err.Error()
		}
		meta = string(raw)
	}
	if _, err := db.Exec(`INSERT OR REPLACE INTO forward_rules
		(id, session_id, is_active, is_paused, paused_until, forward_mode, content_mode,
		 filter_rule_id, metadata, owner_user_id, source_chat_id, target_chat_id,
		 target_chat_ids, fallback_enabled)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		"r-parity", "s1", 1, 0, 0, "COPY_MESSAGE", "AUTO", nil, meta, c.SettingsOwner,
		"-1001", "-1002", "[]", 1); err != nil {
		return "rule: " + err.Error()
	}

	result, err := rulestore.ProbeEffectiveStore(context.Background(), db, "r-parity")
	if err != nil {
		return "get: " + err.Error()
	}
	if result.Rule == nil {
		return "get: rule missing"
	}
	copyState := "unchanged"
	if _, marked := result.Rule.Metadata["_inherited_defaults"]; marked {
		copyState = "done"
	}
	// The resolved values are read THROUGH the registry, the way the
	// Python adapter's getattr(resolved, key) resolves through the entity
	// property — so a key the task never set and no bot supplied answers
	// with the registry default rather than "absent". Both engines must
	// answer the same value for the same key, and "absent" is not a value.
	resolved := rulestore.ProbeRuleSettings(result.Rule)
	return fmt.Sprintf("copy=%s resolved=%s stored=%s", copyState,
		joinKeyValues(resolved, effectiveCaseKeys(c)),
		joinKeyValues(result.StoredMetadata, effectiveCaseKeys(c)))
}

// joinKeyValues renders "k=<json>" pairs for the keys the case asks about,
// so the Python adapter's identical rendering compares byte-for-byte.
func joinKeyValues(values map[string]any, keys []string) string {
	out := make([]string, 0, len(keys))
	for _, key := range keys {
		raw, err := json.Marshal(values[key])
		if err != nil {
			raw = []byte("null")
		}
		out = append(out, fmt.Sprintf("%s=%s", key, string(raw)))
	}
	return strings.Join(out, " ")
}

// effectiveCaseKeys is the key list a case asks about: its settings_keys
// list, its single settings_key, or every registered key in sorted order
// when it names none (the same order Python's schema() emits).
func effectiveCaseKeys(c testCase) []string {
	if len(c.SettingsKeys) > 0 {
		return c.SettingsKeys
	}
	if c.SettingsKey != "" {
		return []string{c.SettingsKey}
	}
	return rulestore.ProbeSortedSettingsKeys()
}

// paritySettingsSchema is the Core's own DDL for the three tables a
// store-backed case needs: forward_rules (the projection the rulestore
// reads), filter_rules (the always-SQLite table the composite store
// requires) and bot_tokens (the owner-scoped defaults read) — all in the
// shape the live SQLite database carries.
const paritySettingsSchema = `
CREATE TABLE IF NOT EXISTS forward_rules (
	id TEXT PRIMARY KEY,
	session_id TEXT NOT NULL DEFAULT '',
	is_active INTEGER NOT NULL DEFAULT 1,
	is_paused INTEGER NOT NULL DEFAULT 0,
	paused_until INTEGER NOT NULL DEFAULT 0,
	forward_mode TEXT NOT NULL DEFAULT 'COPY_MESSAGE',
	content_mode TEXT NOT NULL DEFAULT 'AUTO',
	filter_rule_id TEXT,
	metadata TEXT NOT NULL DEFAULT '{}',
	owner_user_id INTEGER NOT NULL DEFAULT 0,
	fallback_enabled INTEGER NOT NULL DEFAULT 1,
	intermediate_channel_id TEXT NOT NULL DEFAULT '',
	use_intermediate INTEGER NOT NULL DEFAULT 0,
	message_category TEXT NOT NULL DEFAULT 'ALL',
	detection_criteria TEXT NOT NULL DEFAULT '{}',
	split_long_caption INTEGER NOT NULL DEFAULT 1,
	ai_config_id TEXT NOT NULL DEFAULT '',
	ai_secondary_config_id TEXT,
	ai_fallback_policy TEXT NOT NULL DEFAULT 'DROP',
	ai_prompt_version INTEGER NOT NULL DEFAULT 0,
	ai_timeout_seconds REAL NOT NULL DEFAULT 15.0,
	source_chat_id TEXT NOT NULL DEFAULT '',
	source_chat_name TEXT NOT NULL DEFAULT '',
	target_chat_id TEXT NOT NULL DEFAULT '',
	target_chat_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS filter_rules (
	id TEXT PRIMARY KEY,
	name TEXT NOT NULL DEFAULT '',
	whitelist_keywords TEXT NOT NULL DEFAULT '[]',
	blacklist_keywords TEXT NOT NULL DEFAULT '[]',
	regex_patterns TEXT NOT NULL DEFAULT '[]',
	regex_block TEXT,
	regex_require TEXT,
	allowed_media_types TEXT NOT NULL DEFAULT '[]',
	blocked_media_types TEXT NOT NULL DEFAULT '[]',
	drop_service_messages INTEGER NOT NULL DEFAULT 1,
	min_message_length INTEGER NOT NULL DEFAULT 0,
	max_message_length INTEGER NOT NULL DEFAULT 0,
	owner_user_id INTEGER NOT NULL DEFAULT 0,
	is_enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bot_tokens (
	id TEXT PRIMARY KEY,
	label TEXT NOT NULL DEFAULT '',
	token_encrypted TEXT NOT NULL DEFAULT '',
	is_active INTEGER NOT NULL DEFAULT 1,
	created_at INTEGER NOT NULL DEFAULT 0,
	updated_at INTEGER NOT NULL DEFAULT 0,
	owner_user_id INTEGER NOT NULL DEFAULT 0,
	defaults_json TEXT NOT NULL DEFAULT '{}'
);
`
