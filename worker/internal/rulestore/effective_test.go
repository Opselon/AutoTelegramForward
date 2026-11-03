package rulestore

import (
	"database/sql"
	"encoding/json"
	"testing"
)

// effective_test.go pins the Stage 5 (W2) contract: the Go Worker owns the
// per-user / per-task / per-source / per-target configuration resolution —
// the inheritance, override and TENANT-ISOLATION layer Python's
// core/application/effective_settings.py implements.
//
// The integration point the tests exercise is getRule: resolution runs
// inside the rule-load path (the counterpart of pipeline.py:787, where
// materialize() runs FIRST, before the paused check and every gate), so
// every consumer of GetByID sees the bot-defaults-merged task.
//
// Two users, two bots, different defaults: each task resolves only its
// owner's bot's defaults, a task whose bot_token_id points at the other
// tenant's bot gets NO defaults (registry defaults), and resolution never
// mutates the stored rule.

// applyBotTokensSchema creates the bot_tokens table in the shape the
// Python Core's own migrations produce on a live database: the columns are
// (id, label, token_encrypted, is_active, created_at, updated_at,
// owner_user_id, defaults_json). defaults_json is the one column the
// Worker reads; owner_user_id is part of every owner-scoped WHERE clause.
func applyBotTokensSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS bot_tokens (
		id TEXT PRIMARY KEY,
		label TEXT NOT NULL DEFAULT '',
		token_encrypted TEXT NOT NULL DEFAULT '',
		is_active INTEGER NOT NULL DEFAULT 1,
		created_at INTEGER NOT NULL DEFAULT 0,
		updated_at INTEGER NOT NULL DEFAULT 0,
		owner_user_id INTEGER NOT NULL DEFAULT 0,
		defaults_json TEXT NOT NULL DEFAULT '{}'
	)`); err != nil {
		t.Fatalf("apply bot_tokens schema: %v", err)
	}
}

// insertBotToken writes one bot_tokens row. The token itself never enters
// the worker (it stays in the Python boundary), so the column carries a
// placeholder; defaults_json is what the resolution reads.
func insertBotToken(t *testing.T, db *sql.DB, id string, owner int64, defaults map[string]any) {
	t.Helper()
	raw := "{}"
	if defaults != nil {
		out, err := json.Marshal(defaults)
		if err != nil {
			t.Fatalf("marshal defaults for %s: %v", id, err)
		}
		raw = string(out)
	}
	if _, err := db.Exec(`INSERT OR REPLACE INTO bot_tokens
		(id, label, token_encrypted, is_active, created_at, updated_at, owner_user_id, defaults_json)
		VALUES (?,?,?,?,?,?,?,?)`,
		id, id, "ENCRYPTED", 1, 0, 0, owner, raw); err != nil {
		t.Fatalf("insert bot token %s: %v", id, err)
	}
}

// insertRuleFull writes a forward_rules row whose metadata blob is the raw
// JSON the test supplies (NOT re-wrapped the way the auto_pause fixture
// helper does), so an effective-settings test can set bot_token_id and any
// task-side setting exactly as the Python repository stores them.
func insertRuleFull(t *testing.T, db *sql.DB, id string, owner int64, metadata map[string]any) {
	t.Helper()
	raw := "{}"
	if metadata != nil {
		out, err := json.Marshal(metadata)
		if err != nil {
			t.Fatalf("marshal metadata for %s: %v", id, err)
		}
		raw = string(out)
	}
	if _, err := db.Exec(`INSERT OR REPLACE INTO forward_rules
		(id, session_id, is_active, is_paused, paused_until, forward_mode, content_mode,
		 filter_rule_id, metadata, owner_user_id, source_chat_id, target_chat_id,
		 target_chat_ids, fallback_enabled)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		id, "s1", 1, 0, 0, "COPY_MESSAGE", "AUTO", nil, raw, owner,
		"-1001", "-1002", "[]", 1); err != nil {
		t.Fatalf("insert rule %s: %v", id, err)
	}
}

// --- the resolution contract ------------------------------------------------ //

func TestTaskInheritsItsOwnersBotsDefaults(t *testing.T) {
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-alice", 5094837833, map[string]any{
		"remove_emojis":       true,
		"auto_delete_seconds": 42,
	})
	// No task-side settings at all: every inheritable default the bot
	// carries must reach the projection.
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id": "tok-alice",
	})

	r, _, err := store.GetByID("r-alice")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	meta := r.Metadata
	if !metaBoolView(meta, "remove_emojis") {
		t.Error("the task did not inherit its owner bot's remove_emojis=true")
	}
	if got := intView(meta, "auto_delete_seconds"); got != 42 {
		t.Errorf("auto_delete_seconds = %d, want the inherited 42", got)
	}
}

func TestTheTasksOwnChoiceWinsOverTheBotsDefault(t *testing.T) {
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-alice", 5094837833, map[string]any{
		"remove_emojis": true,
	})
	// The task turned emoji removal off for ITSELF. The value differs
	// from the registry default, so it is pinned and must win.
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id":     "tok-alice",
		"remove_emojis":    false,
		"_pinned_settings": []any{"remove_emojis"},
	})

	r, _, err := store.GetByID("r-alice")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if metaBoolView(r.Metadata, "remove_emojis") {
		t.Error("the task's pinned override lost to the bot's default")
	}
}

func TestANonInheritableBotDefaultNeverReachesTheTask(t *testing.T) {
	// A bot default may never name a chat, a route or a schedule: a task
	// whose bot carries delivery_transport/source_policies must NOT
	// inherit them, or one task's forwards would point at another
	// task's chats.
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-evil", 5094837833, map[string]any{
		"delivery_transport":  "BOT",
		"bot_token_id":        "tok-somebody-else",
		"source_policies":     map[string]any{"-1001": map[string]any{}},
		"link_buttons":        []any{"x"},
		"auto_post_scheduler": map[string]any{"enabled": true},
	})
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id": "tok-evil",
	})

	r, _, err := store.GetByID("r-alice")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	meta := r.Metadata
	if _, present := meta["delivery_transport"]; present {
		t.Errorf("a non-inheritable delivery_transport leaked into the task: %v", meta["delivery_transport"])
	}
	if _, present := meta["bot_token_id"]; present && meta["bot_token_id"] != "tok-evil" {
		t.Errorf("the bot's bot_token_id overrode the task's own: %v", meta["bot_token_id"])
	}
	if _, present := meta["source_policies"]; present {
		t.Error("a non-inheritable source_policies leaked into the task")
	}
	if _, present := meta["link_buttons"]; present {
		t.Error("a non-inheritable link_buttons leaked into the task")
	}
	if _, present := meta["auto_post_scheduler"]; present {
		t.Error("a non-inheritable auto_post_scheduler leaked into the task")
	}
	// The rule's attached-token field is unchanged: the task's own
	// bot_token_id (the routing identifier) survives resolution.
	if r.BotTokenID != "tok-evil" {
		t.Errorf("BotTokenID = %q, want the task's own tok-evil", r.BotTokenID)
	}
}

func TestABotDefaultThatFailsValidationIsSkippedNotFatal(t *testing.T) {
	// Only possible if storage was hand-edited, and a convenience
	// setting must never stop a task from forwarding: nothing is raised,
	// and no partial set is applied. Python's materialiser is
	// all-or-nothing (apply_settings validates everything before it
	// mutates), so one out-of-range default makes the task run on its
	// own values rather than on a hand-picked subset of the bot's.
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-broken", 5094837833, map[string]any{
		"remove_emojis":       true,
		"auto_delete_seconds": 999999999, // out of the registry's range
	})
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id": "tok-broken",
	})

	r, _, err := store.GetByID("r-alice")
	if err != nil {
		t.Fatalf("a hand-edited bot default produced a store error (%v); the delivery would fail instead of deferring", err)
	}
	if r == nil {
		t.Fatal("a hand-edited bot default lost the rule")
	}
	if metaBoolView(r.Metadata, "remove_emojis") {
		t.Error("a carry set with an invalid default was partially applied; the whole set must be refused")
	}
	if _, marked := r.Metadata["_inherited_defaults"]; marked {
		t.Error("a failed carry set produced the inherited mark")
	}
	// The lone valid default still lands, proving the refusal is the
	// invalid value's fault and not the mechanism's.
	insertBotToken(t, store.rulesDB, "tok-clean", 5094837833, map[string]any{
		"remove_emojis": true,
	})
	insertRuleFull(t, store.rulesDB, "r-clean", 5094837833, map[string]any{
		"bot_token_id": "tok-clean",
	})
	clean, _, err := store.GetByID("r-clean")
	if err != nil || clean == nil {
		t.Fatalf("GetByID(r-clean): rule=%v err=%v", clean, err)
	}
	if !metaBoolView(clean.Metadata, "remove_emojis") {
		t.Error("the lone valid default did not reach the task")
	}
}

func TestATaskWithNoBotAttachedIsUntouched(t *testing.T) {
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-alice", 5094837833, map[string]any{
		"remove_emojis": true,
	})
	insertRuleFull(t, store.rulesDB, "r-plain", 5094837833, map[string]any{})

	r, _, err := store.GetByID("r-plain")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if metaBoolView(r.Metadata, "remove_emojis") {
		t.Error("a task with no attached bot inherited a bot's default")
	}
	if _, present := r.Metadata["_inherited_defaults"]; present {
		t.Error("a task with no attached bot carries the inherited mark")
	}
}

func TestAnEmptyDefaultsJsonMeansNoDefaults(t *testing.T) {
	// The live schema's default is '{}': every bot attached before v20
	// carries nothing, and resolution must treat that as "run on the
	// registry defaults" — never as an error and never as a copy.
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-empty", 5094837833, map[string]any{})
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id": "tok-empty",
	})

	r, _, err := store.GetByID("r-alice")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if metaBoolView(r.Metadata, "remove_emojis") {
		t.Error("an empty defaults_json produced inherited values")
	}
	if _, present := r.Metadata["_inherited_defaults"]; present {
		t.Error("an empty defaults_json produced the inherited mark")
	}
}

func TestAMissingTokenDegradesToNoDefaults(t *testing.T) {
	// The token row may be gone (the owner deleted the bot while the
	// job sat in the queue): that is "no defaults", never an error.
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id": "tok-deleted",
	})

	r, _, err := store.GetByID("r-alice")
	if err != nil {
		t.Fatalf("a missing token row produced an error (%v); the delivery would fail instead of deferring", err)
	}
	if r == nil {
		t.Fatal("a missing token row lost the rule")
	}
	if metaBoolView(r.Metadata, "remove_emojis") {
		t.Error("a deleted token's defaults reached the task")
	}
}

// --- the isolation boundary: two users, two bots --------------------------- //

// TestTwoUsersOnDifferentBotsNeverShareDefaults is the required isolation
// test. Two tenants, two bots with OPPOSITE defaults, one task each, and a
// third task whose bot_token_id points at the other tenant's bot:
//
//   - each task resolves only its owner's bot's defaults;
//   - the transplanted id gets NO defaults (registry defaults), because the
//     owner-scoped read simply does not find the row;
//   - and resolution never mutates the stored rule.
func TestTwoUsersOnDifferentBotsNeverShareDefaults(t *testing.T) {
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	const alice, bob = 5094837833, 6278059256

	insertBotToken(t, store.rulesDB, "tok-alice", alice, map[string]any{
		"remove_emojis": true,
	})
	insertBotToken(t, store.rulesDB, "tok-bob", bob, map[string]any{
		"remove_emojis": false,
	})

	insertRuleFull(t, store.rulesDB, "r-alice", alice, map[string]any{"bot_token_id": "tok-alice"})
	insertRuleFull(t, store.rulesDB, "r-bob", bob, map[string]any{"bot_token_id": "tok-bob"})
	// Alice's task, but the token id names Bob's bot. The id is a
	// client-visible routing identifier (it lives in the rule's
	// metadata), so nothing stops a task from carrying a foreign one —
	// the owner-scoped read is what stops the consequences.
	insertRuleFull(t, store.rulesDB, "r-transplanted", alice, map[string]any{"bot_token_id": "tok-bob"})

	aliceRule, _, err := store.GetByID("r-alice")
	if err != nil || aliceRule == nil {
		t.Fatalf("GetByID(r-alice): rule=%v err=%v", aliceRule, err)
	}
	if !metaBoolView(aliceRule.Metadata, "remove_emojis") {
		t.Error("Alice's task did not inherit Alice's bot's remove_emojis=true")
	}

	bobRule, _, err := store.GetByID("r-bob")
	if err != nil || bobRule == nil {
		t.Fatalf("GetByID(r-bob): rule=%v err=%v", bobRule, err)
	}
	if metaBoolView(bobRule.Metadata, "remove_emojis") {
		t.Error("Bob's task inherited a default his bot does not carry (the registry default is false)")
	}

	transplanted, _, err := store.GetByID("r-transplanted")
	if err != nil || transplanted == nil {
		t.Fatalf("GetByID(r-transplanted): rule=%v err=%v", transplanted, err)
	}
	if metaBoolView(transplanted.Metadata, "remove_emojis") {
		t.Error("a task whose bot_token_id names ANOTHER TENANT'S bot inherited that bot's defaults — the isolation boundary leaked")
	}
	if _, present := transplanted.Metadata["_inherited_defaults"]; present {
		t.Error("a foreign-tenant bot produced the inherited mark")
	}
	// The row exists and is readable — the refusal is about ownership,
	// not about the token having disappeared.
	var exists int64
	if err := store.rulesDB.QueryRow(`SELECT COUNT(*) FROM bot_tokens WHERE id = 'tok-bob'`).Scan(&exists); err != nil || exists != 1 {
		t.Fatalf("tok-bob exists? count=%d err=%v", exists, err)
	}
}

// TestResolutionNeverMutatesTheStoredRule is the copy-not-mutate rule,
// measured against the database itself: a job that inherits and is then
// saved (or re-loaded) must find the task's own row untouched, or the
// inherited values would have become the task's own choices — the exact
// opposite of inheriting them.
func TestResolutionNeverMutatesTheStoredRule(t *testing.T) {
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-alice", 5094837833, map[string]any{
		"remove_emojis": true,
	})
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id": "tok-alice",
	})

	if _, _, err := store.GetByID("r-alice"); err != nil {
		t.Fatalf("GetByID: %v", err)
	}
	// The stored row — after resolution — must carry neither the
	// inherited value nor the inherited mark.
	var stored string
	if err := store.rulesDB.QueryRow(`SELECT metadata FROM forward_rules WHERE id = 'r-alice'`).Scan(&stored); err != nil {
		t.Fatalf("read back the stored rule: %v", err)
	}
	var meta map[string]any
	if err := json.Unmarshal([]byte(stored), &meta); err != nil {
		t.Fatalf("stored metadata is not JSON: %v", err)
	}
	if _, present := meta["remove_emojis"]; present {
		t.Errorf("resolution wrote the inherited value into the STORED rule (%v); a save would pin it", meta["remove_emojis"])
	}
	if _, present := meta["_inherited_defaults"]; present {
		t.Error("the stored rule carries the inherited mark")
	}
	// And the in-memory projection the store keeps is untouched too:
	// a second load answers the same way, so no state survived the
	// first resolution in the store itself.
	again, _, err := store.GetByID("r-alice")
	if err != nil || again == nil {
		t.Fatalf("second GetByID: rule=%v err=%v", again, err)
	}
	if !metaBoolView(again.Metadata, "remove_emojis") {
		t.Error("a second load no longer inherited; resolution is not repeatable/idempotent across loads")
	}
}

// TestResolutionIsDeliveredThroughEveryDerivedField checks the projection's
// derived struct fields, not just the blob: the delivery engine reads
// remove_emojis through the transform projection, block_links through the
// preflight gate and forward_every_n through the cadence gate. A merged
// value that only reached the blob would be invisible to those gates.
func TestResolutionIsDeliveredThroughEveryDerivedField(t *testing.T) {
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-alice", 5094837833, map[string]any{
		"remove_emojis":   true,
		"block_links":     true,
		"forward_every_n": 3,
	})
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id": "tok-alice",
	})

	r, _, err := store.GetByID("r-alice")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	// The transform projection: remove_emojis feeds the legacy cleaner
	// flag path, so an inherited true must reach the cleaner rule.
	if cleaned := r.Transform.Cleaner; cleaned == nil || !cleaned.RemoveEmojis {
		t.Errorf("the inherited remove_emojis did not reach the transform projection: %+v", cleaned)
	}
	// The preflight gate.
	if !r.BlockLinks {
		t.Error("the inherited block_links did not reach the projection's link gate")
	}
	// The cadence gate.
	if r.ForwardEveryN != 3 {
		t.Errorf("ForwardEveryN = %d, want the inherited 3", r.ForwardEveryN)
	}
}

// TestARecordedPinStandsAgainstABotDefault: the pin index is what keeps a
// task's explicit choice standing when the value happens to equal the
// registry default — the "turn the bot's cleaning back off" case.
func TestARecordedPinStandsAgainstABotDefault(t *testing.T) {
	store := newTestStore(t)
	applyBotTokensSchema(t, store.rulesDB)
	insertBotToken(t, store.rulesDB, "tok-alice", 5094837833, map[string]any{
		"remove_emojis": true,
	})
	// remove_emojis=false is exactly the registry default, so the
	// value-difference test alone sees no choice; the recorded pin is
	// the only thing that makes the choice stand.
	insertRuleFull(t, store.rulesDB, "r-alice", 5094837833, map[string]any{
		"bot_token_id":     "tok-alice",
		"remove_emojis":    false,
		"_pinned_settings": []any{"remove_emojis"},
	})

	r, _, err := store.GetByID("r-alice")
	if err != nil || r == nil {
		t.Fatalf("GetByID: rule=%v err=%v", r, err)
	}
	if metaBoolView(r.Metadata, "remove_emojis") {
		t.Error("a task's recorded pin lost to the bot's default")
	}
	if cleaned := r.Transform.Cleaner; cleaned != nil && cleaned.RemoveEmojis {
		t.Error("the pinned override did not reach the transform projection")
	}
}

// --- helpers for reading the merged view ----------------------------------- //

func metaBoolView(meta map[string]any, key string) bool {
	v, ok := meta[key]
	if !ok {
		return false
	}
	switch b := v.(type) {
	case bool:
		return b
	case float64:
		return b != 0
	}
	return false
}

func intView(meta map[string]any, key string) int64 {
	switch n := meta[key].(type) {
	case float64:
		return int64(n)
	case int64:
		return n
	case int:
		return int64(n)
	}
	return 0
}
